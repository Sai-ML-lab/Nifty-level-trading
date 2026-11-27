from __future__ import annotations

from datetime import datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo
import json
from dataclasses import is_dataclass, replace
from types import SimpleNamespace

import pandas as pd

from .config import Settings, settings
from .db import Store
from .execution import stage_paper_trade, stage_live_order, stage_live_exit, close_paper_trade
from .levels import build_levels
from .option_chain import (normalize_option_chain, attach_snapshot_change, chain_summary, choose_expiry,
                           is_within_otm_band, otm_distance_points)
from .risk import pretrade_gate
from .strategy import evaluate_signals
from .upstox import UpstoxClient

IST = ZoneInfo("Asia/Kolkata")


class TradingEngine:
    def __init__(self, cfg: Settings = settings, store: Store | None = None,
                 client: UpstoxClient | None = None):
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.client = client or UpstoxClient(
            cfg.market_data_token, cfg.trading_token, cfg.base_url, cfg.order_base_url)
        self._contracts_cache: list[dict] | None = None
        self._contracts_cache_at: pd.Timestamp | None = None
        self._daily_cache: pd.DataFrame | None = None
        self._daily_cache_date = None
        self._option_errors: list[dict] = []
        Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)

    def strategy_settings(self):
        """Apply the dashboard-persisted RSI gate to both daemon and UI evaluation."""
        default = bool(getattr(self.cfg, "rsi_divergence_required", False))
        stored = self.store.get_state("rsi_divergence_required", "1" if default else "0")
        required = str(stored).strip().lower() in {"1", "true", "yes", "on"}
        if is_dataclass(self.cfg):
            return replace(self.cfg, rsi_divergence_required=required)
        clone = SimpleNamespace(**vars(self.cfg))
        clone.rsi_divergence_required = required
        return clone

    def _reconcile_live_orders(self) -> list[dict]:
        """Refresh broker status for submitted live orders; never resubmit an order."""
        if not self.cfg.live_is_armed:
            return []
        with self.store.connect() as con:
            rows = con.execute(
                "SELECT order_ref,status,broker_order_id FROM orders "
                "WHERE mode='LIVE' AND status IN ('SUBMITTED','PARTIALLY_FILLED','OPEN','CANCEL_REQUESTED') "
                "AND broker_order_id IS NOT NULL ORDER BY rowid DESC LIMIT 50"
            ).fetchall()
        events = []
        from .execution import sync_live_order
        for row in rows:
            try:
                result = sync_live_order(row["order_ref"], store=self.store, client=self.client, cfg=self.cfg)
                events.append({"type": "LIVE_ORDER_RECONCILED", "order_ref": row["order_ref"], **result})
            except Exception as exc:
                events.append({"type": "LIVE_ORDER_RECONCILIATION_ERROR", "order_ref": row["order_ref"],
                               "error": f"{type(exc).__name__}: {exc}"})
        return events

    def _save_market_files(self, candles: pd.DataFrame, daily: pd.DataFrame) -> None:
        root = Path(self.cfg.data_dir)
        root.mkdir(parents=True, exist_ok=True)
        # Atomic replacement prevents the read-only dashboard from seeing a half-written file.
        for frame, filename in ((candles, "nifty_5m_latest.csv"), (daily, "nifty_daily_latest.csv")):
            destination = root / filename
            temporary = root / (filename + ".tmp")
            frame.to_csv(temporary, index=False)
            temporary.replace(destination)
        try:
            for frame, filename in ((candles, "nifty_5m_latest.parquet"), (daily, "nifty_daily_latest.parquet")):
                destination = root / filename
                temporary = root / (filename + ".tmp.parquet")
                frame.to_parquet(temporary, index=False)
                temporary.replace(destination)
        except (ImportError, ModuleNotFoundError, ValueError):
            pass

    def _collect_option_chains(self, spot: float, now: pd.Timestamp) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
        self._option_errors = []
        if (self._contracts_cache is None or self._contracts_cache_at is None
                or (now - self._contracts_cache_at).total_seconds() > 6 * 60 * 60):
            try:
                self._contracts_cache = self.client.option_contracts(self.cfg.instrument_key)
                self._contracts_cache_at = now
            except Exception as exc:
                self._option_errors.append({"expiry": None, "error": f"{type(exc).__name__}: {exc}"})
                return {}, pd.DataFrame()
        contracts = self._contracts_cache or []
        if not contracts:
            self._option_errors.append({"expiry": None, "error": "Upstox returned an empty option contract master"})
            return {}, pd.DataFrame()
        contract_meta = {x.get("instrument_key"): x for x in contracts if x.get("instrument_key")}
        expiries = sorted({str(x.get("expiry")) for x in contracts if x.get("expiry")})
        today = now.date()
        expiries = [e for e in expiries if pd.Timestamp(e).date() >= today][:3]
        frames: dict[str, pd.DataFrame] = {}
        combined = []
        for expiry in expiries:
            try:
                chain = self.client.option_chain(self.cfg.instrument_key, expiry)
                frame = normalize_option_chain(chain, snapshot_ts=now)
            except Exception as exc:
                self._option_errors.append({"expiry": expiry, "error": f"{type(exc).__name__}: {exc}"})
                continue
            if frame.empty:
                self._option_errors.append({"expiry": expiry, "error": "Upstox returned an empty option chain"})
                continue
            frame["lot_size"] = frame.instrument_key.map(lambda k: int(contract_meta.get(k, {}).get("lot_size") or 0))
            keys = frame.instrument_key.tolist()
            session_date = now.date().isoformat()
            previous = self.store.previous_option_snapshot(keys, session_date=session_date)
            baseline = self.store.first_option_snapshot(keys, session_date=session_date)
            frame = attach_snapshot_change(frame, previous, baseline)
            self.store.save_option_snapshot(frame)
            frames[expiry] = frame
            combined.append(frame)
        return frames, pd.concat(combined, ignore_index=True) if combined else pd.DataFrame()

    @staticmethod
    def _first_spot_exit_trigger(trade: dict, candles: pd.DataFrame) -> tuple[str | None, str | None]:
        """Find the first fully post-entry 5m bar that hit a spot stop/target.

        A candle that was already in progress when the option position opened is
        deliberately excluded because its earlier high/low may predate the fill.
        If stop and target both trade in one candle, stop wins conservatively.
        """
        if candles.empty:
            return None, None
        opened_at = pd.Timestamp(trade["opened_at"])
        opened_at = opened_at.tz_localize(IST) if opened_at.tzinfo is None else opened_at.tz_convert(IST)
        first_full_bar = opened_at.ceil("5min")
        direction = str(trade["direction"]).upper()
        stop, target = float(trade["spot_stop"]), float(trade["spot_target"])
        ordered = candles.sort_values("timestamp")
        for bar in ordered.itertuples(index=False):
            bar_ts = pd.Timestamp(bar.timestamp)
            bar_ts = bar_ts.tz_localize(IST) if bar_ts.tzinfo is None else bar_ts.tz_convert(IST)
            if bar_ts < first_full_bar:
                continue
            if bar_ts + pd.Timedelta(minutes=5) <= opened_at:
                continue
            if direction == "LONG":
                stop_hit = float(bar.low) <= stop
                target_hit = float(bar.high) >= target
            else:
                stop_hit = float(bar.high) >= stop
                target_hit = float(bar.low) <= target
            if stop_hit:
                return "spot_stop", bar_ts.isoformat()
            if target_hit:
                return "spot_target", bar_ts.isoformat()
        return None, None

    def _manage_paper_positions(self, candles: pd.DataFrame, all_options: pd.DataFrame, now: pd.Timestamp) -> list[dict]:
        events = []
        open_trades = self.store.open_trades("PAPER")
        if open_trades.empty:
            return events
        for trade in open_trades.to_dict("records"):
            opened_at = pd.Timestamp(trade["opened_at"])
            opened_at = opened_at.tz_localize(IST) if opened_at.tzinfo is None else opened_at.tz_convert(IST)
            if opened_at.date() < now.date():
                reason, trigger_ts = "overnight_safety_exit", None
            else:
                reason, trigger_ts = self._first_spot_exit_trigger(trade, candles)
            quote_rows = (all_options[all_options.instrument_key == trade["instrument_key"]]
                          if not all_options.empty else pd.DataFrame())
            q = quote_rows.iloc[-1] if not quote_rows.empty else None
            bid = float(q.bid) if q is not None and pd.notna(q.bid) else 0.0
            if reason is None and bid > 0 and bid <= float(trade["premium_stop"]):
                reason = "premium_stop"
            if reason is None and now.time() >= time(15, 20):
                reason = "intraday_time_exit"
            if reason:
                if bid <= 0:
                    events.append({"type": "PAPER_EXIT_BLOCKED_NO_BID", "trade_id": trade["trade_id"],
                                   "trigger": reason, "trigger_bar": trigger_ts,
                                   "reason": "No positive executable bid; position remains open and requires attention"})
                    continue
                result = close_paper_trade(trade, bid, reason, self.cfg, self.store)
                events.append({"type": "PAPER_EXIT", "trade_id": trade["trade_id"], "reason": reason,
                               "trigger_bar": trigger_ts, **result})
        return events

    def _manage_live_positions(self, candles: pd.DataFrame, all_options: pd.DataFrame, now: pd.Timestamp) -> list[dict]:
        events = []
        if not self.cfg.live_is_armed:
            return events
        open_trades = self.store.open_trades("LIVE")
        if open_trades.empty:
            return events
        for trade in open_trades.to_dict("records"):
            opened_at = pd.Timestamp(trade["opened_at"])
            opened_at = opened_at.tz_localize(IST) if opened_at.tzinfo is None else opened_at.tz_convert(IST)
            if opened_at.date() < now.date():
                reason, trigger_ts = "overnight_safety_exit", None
            else:
                reason, trigger_ts = self._first_spot_exit_trigger(trade, candles)
            quotes = (all_options[all_options.instrument_key == trade["instrument_key"]]
                      if not all_options.empty else pd.DataFrame())
            q = quotes.iloc[-1] if not quotes.empty else None
            bid = float(q.bid) if q is not None and pd.notna(q.bid) else 0.0
            if reason is None and bid > 0 and bid <= float(trade["premium_stop"]):
                reason = "premium_stop"
            if reason is None and now.time() >= time(15, 20):
                reason = "intraday_time_exit"
            if reason:
                if bid <= 0:
                    events.append({"type": "LIVE_EXIT_BLOCKED_NO_BID", "trade_id": trade["trade_id"],
                                   "trigger": reason, "trigger_bar": trigger_ts,
                                   "reason": "No positive executable bid; no sell order staged"})
                    continue
                staged = stage_live_exit(trade, bid, cfg=self.cfg, store=self.store)
                if staged.get("status") == "PENDING_APPROVAL":
                    events.append({"type": "LIVE_EXIT_APPROVAL_NEEDED", "trade_id": trade["trade_id"],
                                   "trigger": reason, "trigger_bar": trigger_ts,
                                   "order_ref": staged["order"]["order_ref"]})
                elif staged.get("status") == "ALREADY_STAGED":
                    events.append({"type": "LIVE_EXIT_ALREADY_STAGED", "trade_id": trade["trade_id"],
                                   "trigger": reason, "order_ref": staged.get("order_ref")})
        return events

    def run_once(self, *, now: datetime | None = None, execute_paper: bool = True) -> dict:
        supplied_now = now is not None
        ts = pd.Timestamp(now or datetime.now(IST))
        ts = ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)
        reconciliation_events = self._reconcile_live_orders()
        candles = self.client.intraday_candles(self.cfg.instrument_key, 5)
        if candles.empty:
            raise RuntimeError("Upstox returned no completed NIFTY 5-minute candles")
        candles = candles[candles.timestamp.dt.date == ts.date()].copy().reset_index(drop=True)
        if candles.empty:
            raise RuntimeError("No current-session completed NIFTY candles returned")
        daily_start = (ts - pd.Timedelta(days=90)).date()
        if self._daily_cache is None or self._daily_cache_date != ts.date():
            self._daily_cache = self.client.historical_candles(self.cfg.instrument_key, daily_start, ts.date(), "days", 1)
            self._daily_cache_date = ts.date()
        daily = self._daily_cache
        session_date = ts.date().isoformat()
        manual = self.store.manual_levels(session_date)
        levels = build_levels(candles, daily, manual, self.cfg.pivot_left, self.cfg.pivot_right)
        self._save_market_files(candles, daily)

        age = max(0.0, (ts - pd.Timestamp(candles.iloc[-1].timestamp) - pd.Timedelta(minutes=5)).total_seconds())
        strategy_cfg = self.strategy_settings()
        signals, indicated, context = evaluate_signals(candles, levels, strategy_cfg)
        latest_candle_ts = pd.Timestamp(candles.iloc[-1].timestamp).isoformat()
        self.store.save_diagnostics(context.get("diagnostics", []), latest_candle_ts, ts.isoformat())
        spot = float(candles.iloc[-1].close)
        expiry_chains, option_frame = self._collect_option_chains(spot, ts)
        front_expiry = sorted(expiry_chains)[0] if expiry_chains else None
        summary = chain_summary(expiry_chains[front_expiry]) if front_expiry else chain_summary(option_frame)
        latest_bar = candles.iloc[-1]
        events = reconciliation_events + [
            {"type": "OPTION_CHAIN_FETCH_ERROR", **item} for item in self._option_errors
        ]
        events.extend(self._manage_paper_positions(candles, option_frame, ts))
        events.extend(self._manage_live_positions(candles, option_frame, ts))
        processed = []
        for sig in signals:
            signal_age = max(0.0, age)
            if signal_age > float(getattr(self.cfg, "max_signal_age_seconds", 180)):
                sig.update({"status": "STALE_SIGNAL", "reason": sig["reason"] +
                            f"; signal is {signal_age:.0f}s old, exceeding MAX_SIGNAL_AGE_SECONDS"})
                self.store.save_signal(sig)
                processed.append(sig)
                continue
            option = choose_expiry(expiry_chains, spot, sig["direction"], self.cfg, as_of=ts)
            if option is None:
                sig.update({"status": "NO_ELIGIBLE_OPTION", "reason": sig["reason"] + "; no contract passed liquidity/spread/premium filters"})
                self.store.save_signal(sig)
                processed.append(sig)
                continue
            lot_size = int(option.get("lot_size") or 0)
            sig.update({
                "instrument_key": option["instrument_key"], "expiry": option["expiry"],
                "strike": float(option["strike"]), "option_type": option["option_type"],
                "option_ltp": float(option["ltp"]), "lot_size": lot_size,
                "option_bid": float(option.get("bid") or 0), "option_ask": float(option.get("ask") or 0),
                "option_spread": float(option.get("spread") or 0),
                "option_spread_pct": float(option.get("spread_pct") or 0),
                "option_volume": int(option.get("volume") or 0),
                "option_oi": float(option.get("oi") or 0),
                "option_bid_qty": int(option.get("bid_qty") or 0),
                "option_ask_qty": int(option.get("ask_qty") or 0),
                "otm_distance_points": float(option.get("otm_distance_points", otm_distance_points(
                    str(option["option_type"]), float(option["strike"]), spot))),
                "option_selection_mode": str(getattr(self.cfg, "option_selection_mode", "NEAR_ATM")),
                "option_otm_min_points": float(getattr(self.cfg, "option_otm_min_points", 150.0)),
                "option_otm_max_points": float(getattr(self.cfg, "option_otm_max_points", 200.0)),
                "option_delta": option.get("delta"), "option_iv": option.get("iv"),
                "option_theta": option.get("theta"), "option_gamma": option.get("gamma"),
                "option_vega": option.get("vega"),
                "liquidity_score": float(option.get("score", option.get("liquidity_score", 0)) or 0),
                "liquidity_status": str(option.get("liquidity_status", "UNKNOWN")),
            })
            gate, gate_reason = pretrade_gate(cfg=self.cfg, store=self.store, session_date=session_date,
                                               mode="PAPER", now=ts.to_pydatetime(), data_age_seconds=age)
            sig["reason"] += "; " + gate_reason
            if not gate:
                sig["status"] = "BLOCKED_BY_RISK"
                self.store.save_signal(sig)
                processed.append(sig)
                continue
            # Risk sizing is done on the executable ask, not LTP/mid.
            from .risk import size_long_option
            sized = size_long_option(premium=float(option["ask"]) + self.cfg.slippage_points,
                                     lot_size=lot_size, cfg=self.cfg) if lot_size else None
            if sized is None or not sized.allowed:
                sig["status"] = "BLOCKED_BY_SIZING"
                sig["reason"] += "; " + (sized.reason if sized else "Lot size missing from contract master")
                self.store.save_signal(sig); processed.append(sig); continue
            sig.update({"lots": sized.lots, "quantity": sized.quantity,
                        "estimated_risk": sized.estimated_risk,
                        "maximum_premium_loss": sized.maximum_premium_loss})
            sig["status"] = "READY"
            self.store.save_signal(sig)
            if execute_paper:
                # Re-attempt a READY signal after a process crash between signal
                # persistence and trade insertion. The database's unique
                # (signal_id, mode) index makes this replay idempotent.
                result = stage_paper_trade(sig, option, lot_size, self.cfg, self.store)
                if result.get("status") == "OPEN":
                    events.append({"type": "PAPER_ENTRY", "signal_id": sig["signal_id"], **result})
            processed.append(sig)

        option_age = None
        if not option_frame.empty and "timestamp" in option_frame:
            option_ts = pd.Timestamp(pd.to_datetime(option_frame.timestamp, errors="coerce").max())
            if pd.notna(option_ts):
                option_ts = option_ts.tz_localize(IST) if option_ts.tzinfo is None else option_ts.tz_convert(IST)
                option_age = max(0.0, (ts - option_ts).total_seconds())
        refreshed_at = ts if supplied_now else pd.Timestamp.now(tz=IST)
        result = {
            "timestamp": refreshed_at.isoformat(), "market_data_requested_at": ts.isoformat(),
            "spot": spot, "latest_candle": candles.iloc[-1].to_dict(),
            "candles": candles, "daily": daily, "levels": levels,
            "signals": processed, "indicator_context": context,
            "options": option_frame, "option_summary": summary, "front_expiry": front_expiry,
            "option_summary_by_expiry": {e: chain_summary(f) for e, f in expiry_chains.items()},
            "option_errors": list(self._option_errors),
            "events": events, "data_age_seconds": age, "options_age_seconds": option_age,
        }
        for event in events:
            self.store.save_event(event, event_timestamp=ts.isoformat())
        self.store.set_state("last_refresh", refreshed_at.isoformat())
        return result

    def load_last_snapshot(self, *, now: datetime | None = None) -> dict | None:
        """Rebuild the dashboard view from files/SQLite written by the polling engine.

        This is read-only: it does not call Upstox or evaluate/submit new trades. It
        lets a separate Streamlit process observe a continuously running paper daemon.
        """
        ts = pd.Timestamp(now or datetime.now(IST))
        ts = ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)
        root = Path(self.cfg.data_dir)
        candle_path = root / "nifty_5m_latest.csv"
        daily_path = root / "nifty_daily_latest.csv"
        if not candle_path.exists() or not daily_path.exists():
            return None
        try:
            candles = pd.read_csv(candle_path)
            daily = pd.read_csv(daily_path)
            if candles.empty:
                return None
            candles["timestamp"] = pd.to_datetime(candles["timestamp"], errors="coerce")
            daily["timestamp"] = pd.to_datetime(daily["timestamp"], errors="coerce")
            candles = candles.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
            daily = daily.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
            candles = candles[candles.timestamp.map(lambda x: pd.Timestamp(x).date() == ts.date())].copy()
            if candles.empty:
                return None
            latest_ts = pd.Timestamp(candles.iloc[-1].timestamp)
            if latest_ts.tzinfo is None:
                latest_ts = latest_ts.tz_localize(IST)
            else:
                latest_ts = latest_ts.tz_convert(IST)
            if latest_ts + pd.Timedelta(minutes=5) > ts:
                candles = candles.iloc[:-1].copy()
            if candles.empty:
                return None
            latest_ts = pd.Timestamp(candles.iloc[-1].timestamp)
            if latest_ts.tzinfo is None:
                latest_ts = latest_ts.tz_localize(IST)
            else:
                latest_ts = latest_ts.tz_convert(IST)
            session_date = ts.date().isoformat()
            levels = build_levels(candles, daily, self.store.manual_levels(session_date),
                                  self.cfg.pivot_left, self.cfg.pivot_right)
            _, _, context = evaluate_signals(candles, levels, self.strategy_settings())
            option_frame = self.store.latest_option_snapshots(session_date)
            options_age = None
            if not option_frame.empty and "timestamp" in option_frame:
                option_frame["timestamp"] = pd.to_datetime(option_frame["timestamp"], errors="coerce")
                valid_option_times = option_frame["timestamp"].dropna()
                if not valid_option_times.empty:
                    option_ts = pd.Timestamp(valid_option_times.max())
                    option_ts = option_ts.tz_localize(IST) if option_ts.tzinfo is None else option_ts.tz_convert(IST)
                    options_age = max(0.0, (ts - option_ts).total_seconds())
            expiry_chains = ({str(expiry): group.copy() for expiry, group in option_frame.groupby("expiry")}
                             if not option_frame.empty and "expiry" in option_frame else {})
            front_expiry = sorted(expiry_chains)[0] if expiry_chains else None
            summary = chain_summary(expiry_chains[front_expiry]) if front_expiry else chain_summary(option_frame)
            signals_df = self.store.recent_signals(200)
            if not signals_df.empty:
                signals_df = signals_df[signals_df.created_at.astype(str).eq(latest_ts.isoformat())]
            age = max(0.0, (ts - latest_ts - pd.Timedelta(minutes=5)).total_seconds())
            last_refresh = self.store.get_state("last_refresh", "")
            return {
                "timestamp": last_refresh or ts.isoformat(), "spot": float(candles.iloc[-1].close),
                "latest_candle": candles.iloc[-1].to_dict(), "candles": candles, "daily": daily,
                "levels": levels, "signals": signals_df.to_dict("records") if not signals_df.empty else [],
                "indicator_context": context, "options": option_frame, "option_summary": summary,
                "front_expiry": front_expiry,
                "option_summary_by_expiry": {e: chain_summary(f) for e, f in expiry_chains.items()},
                "option_errors": [], "events": [], "data_age_seconds": age,
                "options_age_seconds": options_age,
                "snapshot_source": "persisted",
            }
        except (OSError, ValueError, KeyError, pd.errors.ParserError):
            return None

    def prepare_live_order(self, signal_id: str) -> dict:
        with self.store.connect() as con:
            row = con.execute("SELECT * FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
        if not row:
            raise KeyError("Signal not found")
        sig = dict(row)
        if sig["status"] != "READY":
            raise ValueError("Only READY signals can be staged for live approval")
        now = pd.Timestamp.now(tz=IST)
        signal_ts = pd.Timestamp(sig["created_at"])
        if signal_ts.tzinfo is None:
            signal_ts = signal_ts.tz_localize(IST)
        age_signal = (now - (signal_ts + pd.Timedelta(minutes=5))).total_seconds()
        if age_signal > 600 or signal_ts.date() != now.date():
            raise TimeoutError("Signal is older than 10 minutes or from another session; refresh and wait for a new setup")
        live_candles = self.client.intraday_candles(self.cfg.instrument_key, 5)
        if live_candles.empty:
            raise RuntimeError("No current completed NIFTY candle available to revalidate this signal")
        latest = live_candles.iloc[-1]
        data_age = max(0.0, (now - pd.Timestamp(latest.timestamp) - pd.Timedelta(minutes=5)).total_seconds())
        if data_age > self.cfg.stale_data_seconds:
            raise TimeoutError(f"NIFTY market data is stale ({data_age:.0f}s)")
        spot_now = float(latest.close)
        level_price = float(sig["level_price"])
        buffer = float(self.cfg.breakout_buffer_points)
        if sig["direction"] == "LONG" and (spot_now <= level_price + buffer or spot_now <= float(sig["spot_stop"]) or spot_now >= float(sig["spot_target"])):
            raise RuntimeError("Underlying has fallen back through the breakout level, invalidated the stop, or reached the target")
        if sig["direction"] == "SHORT" and (spot_now >= level_price - buffer or spot_now >= float(sig["spot_stop"]) or spot_now <= float(sig["spot_target"])):
            raise RuntimeError("Underlying has reclaimed the breakdown level, invalidated the stop, or reached the target")
        # Quote the exact contract immediately before staging; do not use stale signal LTP.
        contracts = self.client.option_chain(self.cfg.instrument_key, sig["expiry"])
        frame = normalize_option_chain(contracts)
        selected = frame[frame.instrument_key == sig["instrument_key"]]
        if selected.empty:
            raise RuntimeError("Selected option contract is no longer present in current chain")
        option = selected.iloc[-1].to_dict()
        option["lot_size"] = int(sig["lot_size"])
        if not is_within_otm_band(str(option["option_type"]), float(option["strike"]), spot_now, self.cfg):
            distance = otm_distance_points(str(option["option_type"]), float(option["strike"]), spot_now)
            raise RuntimeError(
                f"Selected contract is now {distance:.1f} points OTM and outside the configured "
                f"{self.cfg.option_otm_min_points:.0f}-{self.cfg.option_otm_max_points:.0f} point band"
            )
        spread = (float(option["ask"]) - float(option["bid"])) / float(option["ask"]) if float(option["ask"]) > 0 else 1.0
        if float(option["bid"]) <= 0 or float(option["ask"]) <= 0 or spread > self.cfg.max_spread_pct:
            raise RuntimeError("Live contract quote is unavailable or spread exceeds the configured limit")
        if not self.cfg.min_premium_inr <= float(option["ask"]) <= self.cfg.max_premium_inr:
            raise RuntimeError("Live option premium is outside the configured range")
        from .risk import pretrade_gate
        ok, why = pretrade_gate(cfg=self.cfg, store=self.store, session_date=now.date().isoformat(),
                                mode="LIVE", now=now.to_pydatetime(), data_age_seconds=data_age)
        if not ok:
            return {"status": "REJECTED", "reason": why}
        return stage_live_order(sig, option, int(sig["lot_size"]), self.cfg, self.store)

    def prepare_live_exit(self, trade_id: str) -> dict:
        with self.store.connect() as con:
            row = con.execute("SELECT * FROM trades WHERE trade_id=? AND mode='LIVE' AND status='OPEN'", (trade_id,)).fetchone()
        if not row:
            raise KeyError("Open live trade not found")
        trade = dict(row)
        contracts = self.client.option_chain(self.cfg.instrument_key, trade["expiry"])
        frame = normalize_option_chain(contracts)
        quote_rows = frame[frame.instrument_key == trade["instrument_key"]]
        if quote_rows.empty:
            raise RuntimeError("No fresh quote for the open live contract")
        return stage_live_exit(trade, float(quote_rows.iloc[-1].bid), cfg=self.cfg, store=self.store)
