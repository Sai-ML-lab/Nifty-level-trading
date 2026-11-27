from __future__ import annotations

from datetime import date
import math
import pandas as pd


def normalize_option_chain(chain: list[dict], snapshot_ts=None) -> pd.DataFrame:
    records = []
    stamp = pd.Timestamp(snapshot_ts or pd.Timestamp.now(tz="Asia/Kolkata"))
    for row in chain:
        expiry = row.get("expiry")
        strike = row.get("strike_price")
        spot = row.get("underlying_spot_price")
        for kind, key in (("CE", "call_options"), ("PE", "put_options")):
            opt = row.get(key) or {}
            market = opt.get("market_data") or {}
            greeks = opt.get("option_greeks") or {}
            if not opt.get("instrument_key"):
                continue
            oi = float(market.get("oi") or 0)
            prev_oi_raw = market.get("prev_oi")
            prev_oi = float(prev_oi_raw) if prev_oi_raw is not None else None
            records.append({
                "timestamp": stamp, "expiry": expiry, "strike": float(strike),
                "option_type": kind, "instrument_key": opt.get("instrument_key"),
                "ltp": float(market.get("ltp") or 0), "volume": int(market.get("volume") or 0),
                "oi": oi, "prev_oi": prev_oi,
                "day_change_oi": oi - prev_oi if prev_oi is not None else float("nan"),
                "bid": float(market.get("bid_price") or 0), "bid_qty": int(market.get("bid_qty") or 0),
                "ask": float(market.get("ask_price") or 0), "ask_qty": int(market.get("ask_qty") or 0),
                "delta": float(greeks.get("delta") or 0), "iv": float(greeks.get("iv") or 0),
                "spot": float(spot or 0),
            })
    return pd.DataFrame(records)


def attach_snapshot_change(
    current: pd.DataFrame,
    previous: pd.DataFrame | None,
    baseline: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Attach ΔOI since the previous poll and since the first local session snapshot.

    The first local snapshot intentionally has NaN for both values. Exchange
    ``prev_oi`` remains the source for the full-session/day change metric.
    """
    df = current.copy()
    df["intraday_change_oi"] = float("nan")
    df["session_change_oi"] = float("nan")
    df["previous_snapshot_timestamp"] = None
    df["session_baseline_timestamp"] = None

    def _mapping(frame: pd.DataFrame | None):
        if frame is None or frame.empty or not {"instrument_key", "oi"}.issubset(frame.columns):
            return None
        cols = ["instrument_key", "oi"]
        if "timestamp" in frame.columns:
            cols.append("timestamp")
        # Deterministic in the unlikely event an old database contains duplicates.
        tmp = frame[cols].drop_duplicates("instrument_key", keep="last").set_index("instrument_key")
        return tmp

    prev_map = _mapping(previous)
    if prev_map is not None:
        matched = df.instrument_key.map(prev_map.oi)
        valid = matched.notna()
        df.loc[valid, "intraday_change_oi"] = df.loc[valid, "oi"] - matched.loc[valid]
        if "timestamp" in prev_map.columns:
            df["previous_snapshot_timestamp"] = df.instrument_key.map(prev_map.timestamp)

    baseline_map = _mapping(baseline)
    if baseline_map is not None:
        matched = df.instrument_key.map(baseline_map.oi)
        valid = matched.notna()
        df.loc[valid, "session_change_oi"] = df.loc[valid, "oi"] - matched.loc[valid]
        if "timestamp" in baseline_map.columns:
            df["session_baseline_timestamp"] = df.instrument_key.map(baseline_map.timestamp)
    return df


def chain_summary(df: pd.DataFrame) -> dict:
    empty = {
        "pcr": None, "call_oi": 0.0, "put_oi": 0.0,
        "call_change_oi": None, "put_change_oi": None,
        "call_day_change_oi": None, "put_day_change_oi": None,
        "call_intraday_change_oi": None, "put_intraday_change_oi": None,
        "call_session_change_oi": None, "put_session_change_oi": None,
        "call_intraday_coverage": 0.0, "put_intraday_coverage": 0.0,
        "call_session_coverage": 0.0, "put_session_coverage": 0.0,
        "max_call_oi_strike": None, "max_put_oi_strike": None,
    }
    if df.empty:
        return empty
    calls = df[df.option_type == "CE"]
    puts = df[df.option_type == "PE"]
    call_oi, put_oi = float(calls.oi.sum()), float(puts.oi.sum())
    pcr = put_oi / call_oi if call_oi > 0 else None

    def available_sum(frame: pd.DataFrame, column: str):
        if column not in frame:
            return None
        value = frame[column].sum(min_count=1)
        return float(value) if pd.notna(value) else None

    call_day, put_day = available_sum(calls, "day_change_oi"), available_sum(puts, "day_change_oi")
    call_poll, put_poll = available_sum(calls, "intraday_change_oi"), available_sum(puts, "intraday_change_oi")
    call_session = available_sum(calls, "session_change_oi")
    put_session = available_sum(puts, "session_change_oi")
    def coverage(frame: pd.DataFrame, column: str) -> float:
        if frame.empty or column not in frame:
            return 0.0
        return float(frame[column].notna().mean() * 100.0)
    call_poll_coverage = coverage(calls, "intraday_change_oi")
    put_poll_coverage = coverage(puts, "intraday_change_oi")
    call_session_coverage = coverage(calls, "session_change_oi")
    put_session_coverage = coverage(puts, "session_change_oi")
    max_call = float(calls.groupby("strike").oi.sum().idxmax()) if not calls.empty else None
    max_put = float(puts.groupby("strike").oi.sum().idxmax()) if not puts.empty else None
    return {
        "pcr": pcr, "call_oi": call_oi, "put_oi": put_oi,
        "call_change_oi": call_day, "put_change_oi": put_day,
        "call_day_change_oi": call_day, "put_day_change_oi": put_day,
        "call_intraday_change_oi": call_poll, "put_intraday_change_oi": put_poll,
        "call_session_change_oi": call_session, "put_session_change_oi": put_session,
        "call_intraday_coverage": call_poll_coverage, "put_intraday_coverage": put_poll_coverage,
        "call_session_coverage": call_session_coverage, "put_session_coverage": put_session_coverage,
        "max_call_oi_strike": max_call, "max_put_oi_strike": max_put,
    }


def add_option_quality_metrics(df: pd.DataFrame, cfg) -> pd.DataFrame:
    """Add transparent quote/spread/volume/OI/moneyness eligibility diagnostics."""
    if df.empty:
        return df.copy()
    out = df.copy()
    numeric = ("ltp", "bid", "ask", "volume", "oi", "strike", "spot", "bid_qty", "ask_qty")
    for col in numeric:
        if col in out:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out["spread"] = out["ask"] - out["bid"]
    out["spread_pct"] = out["spread"] / out["ask"].where(out["ask"] > 0)
    out["moneyness_pct"] = (out["strike"] - out["spot"]).abs() / out["spot"].where(out["spot"] > 0)
    out["quote_valid"] = (out["bid"].gt(0) & out["ask"].gt(0) & out["ask"].ge(out["bid"]))
    out["premium_ok"] = (out["ltp"].between(cfg.min_premium_inr, cfg.max_premium_inr)
                         & out["ask"].between(cfg.min_premium_inr, cfg.max_premium_inr))
    out["spread_ok"] = out["spread_pct"].le(cfg.max_spread_pct)
    out["volume_ok"] = out["volume"].ge(cfg.min_option_volume)
    out["oi_ok"] = out["oi"].ge(cfg.min_option_oi)
    out["moneyness_ok"] = out["moneyness_pct"].le(cfg.option_moneyness_pct)
    out["liquidity_eligible"] = (out["quote_valid"] & out["premium_ok"] & out["spread_ok"]
                                  & out["volume_ok"] & out["oi_ok"] & out["moneyness_ok"])
    out["liquidity_status"] = out.apply(
        lambda r: "PASS" if bool(r["liquidity_eligible"]) else "; ".join(
            label for label, ok in (
                ("invalid quote", r["quote_valid"]), ("premium filter", r["premium_ok"]),
                ("spread filter", r["spread_ok"]), ("volume filter", r["volume_ok"]),
                ("OI filter", r["oi_ok"]), ("ATM-distance filter", r["moneyness_ok"]),
            ) if not bool(ok)
        ), axis=1
    )
    # The score is solely a contract-ranking heuristic, not a directional signal.
    vol_score = out["volume"].clip(lower=0).fillna(0).map(lambda x: math.log1p(float(x)))
    oi_score = out["oi"].clip(lower=0).fillna(0).map(lambda x: math.log1p(float(x)))
    spread_penalty = out["spread_pct"].fillna(1.0).clip(lower=0, upper=1) * 100
    distance_penalty = out["moneyness_pct"].fillna(1.0).clip(lower=0, upper=1) * 100
    out["liquidity_score"] = (vol_score * 0.35 + oi_score * 0.30
                              - spread_penalty * 0.25 - distance_penalty * 0.08)
    return out


def atm_option_chain_window(df: pd.DataFrame, spot: float, strikes_each_side: int = 10, cfg=None) -> pd.DataFrame:
    """Return a compact CE/PE side-by-side view for ATM ± N listed strikes."""
    if df.empty or not pd.notna(spot) or float(spot) <= 0:
        return pd.DataFrame()
    data = df.copy()
    data["strike"] = pd.to_numeric(data["strike"], errors="coerce")
    strikes = sorted(float(x) for x in data["strike"].dropna().unique())
    if not strikes:
        return pd.DataFrame()
    atm_idx = min(range(len(strikes)), key=lambda i: (abs(strikes[i] - float(spot)), strikes[i]))
    lo, hi = max(0, atm_idx - int(strikes_each_side)), min(len(strikes), atm_idx + int(strikes_each_side) + 1)
    selected_strikes = strikes[lo:hi]
    data = add_option_quality_metrics(data[data.strike.isin(selected_strikes)], cfg or _display_cfg())
    value_columns = [c for c in (
        "ltp", "bid", "ask", "spread", "spread_pct", "volume", "oi",
        "day_change_oi", "intraday_change_oi", "session_change_oi", "bid_qty", "ask_qty",
        "liquidity_score", "liquidity_eligible", "liquidity_status", "iv", "delta",
    ) if c in data.columns]
    if data.empty:
        return pd.DataFrame()
    pivot = data.pivot_table(index="strike", columns="option_type", values=value_columns,
                             aggfunc="last", dropna=False)
    pivot.columns = [f"{kind}_{metric}" for metric, kind in pivot.columns]
    pivot = pivot.reset_index()
    pivot.insert(1, "ATM", pivot.strike.eq(strikes[atm_idx]))
    return pivot.sort_values("strike").reset_index(drop=True)


class _DisplayConfig:
    min_premium_inr = 0.0
    max_premium_inr = float("inf")
    max_spread_pct = float("inf")
    min_option_volume = 0
    min_option_oi = 0
    option_moneyness_pct = float("inf")


def _display_cfg():
    return _DisplayConfig()


def choose_expiry(expiry_chains: dict[str, pd.DataFrame], spot: float,
                  direction: str, cfg, as_of=None) -> dict | None:
    """Choose liquid, tight-spread, affordable near-ATM contracts across expiries."""
    option_type = "CE" if direction == "LONG" else "PE"
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    if spot <= 0 or not pd.notna(spot):
        return None
    as_of_ts = pd.Timestamp(as_of or pd.Timestamp.now(tz="Asia/Kolkata"))
    as_of_ts = as_of_ts.tz_localize("Asia/Kolkata") if as_of_ts.tzinfo is None else as_of_ts.tz_convert("Asia/Kolkata")
    today = as_of_ts.date()
    best = None
    for expiry, frame in expiry_chains.items():
        if frame is None or frame.empty:
            continue
        exp_date = pd.Timestamp(expiry).date()
        dte = (exp_date - today).days
        if dte < 0:
            continue
        candidates = add_option_quality_metrics(frame[frame.option_type == option_type], cfg)
        candidates = candidates[candidates.liquidity_eligible].copy()
        if candidates.empty:
            continue
        candidates["score"] = candidates["liquidity_score"] - dte * 0.02
        row = candidates.sort_values(["score", "spread_pct", "moneyness_pct"], ascending=[False, True, True]).iloc[0].to_dict()
        row["expiry"] = str(expiry)
        row["dte"] = dte
        if best is None or row["score"] > best["score"]:
            best = row
    return best
