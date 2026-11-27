from __future__ import annotations

import hashlib
import pandas as pd

from .indicators import add_indicators, latest_fvg, rsi_divergence
from .levels import next_target


def _make_signal(direction: str, mode: str, level: pd.Series, bar: pd.Series,
                 atr: float, levels: pd.DataFrame, cfg, reason_bits: list[str]) -> dict | None:
    entry = float(bar.close)
    buffer = max(float(cfg.breakout_buffer_points), float(cfg.retest_tolerance_points) * 0.25)
    stop_distance = max(float(atr), 1.0) * float(cfg.atr_stop_multiplier)
    if direction == "LONG":
        stop = min(float(level.price) - buffer, entry - stop_distance)
    else:
        stop = max(float(level.price) + buffer, entry + stop_distance)
    target = next_target(levels, entry, direction)
    if target is None:
        return None
    risk = abs(entry - stop)
    reward = abs(target - entry)
    rr = reward / risk if risk > 0 else 0.0
    if not pd.notna(rr) or rr < float(cfg.min_reward_risk):
        return None
    stamp = pd.Timestamp(bar.timestamp)
    session_date = stamp.date().isoformat()
    raw_id = f"{stamp.isoformat()}|{direction}|{mode}|{level['name']}|{float(level.price):.2f}"
    signal_id = hashlib.sha256(raw_id.encode()).hexdigest()[:20]
    return {
        "signal_id": signal_id, "created_at": stamp.isoformat(), "session_date": session_date,
        "direction": direction, "entry_mode": mode, "level_name": str(level["name"]),
        "level_price": float(level.price), "spot_entry": entry, "spot_stop": stop,
        "spot_target": target, "reward_risk": rr, "status": "READY",
        "reason": "; ".join(reason_bits),
    }


def _retest_found(df: pd.DataFrame, level_price: float, direction: str, cfg) -> bool:
    """Find a live retest on the newest candle after a valid recent breakout."""
    if not cfg.retest_entry_enabled or int(cfg.retest_bars) < 1 or len(df) < 3:
        return False
    buffer = float(cfg.breakout_buffer_points)
    tolerance = float(cfg.retest_tolerance_points)
    start = max(1, len(df) - int(cfg.retest_bars) - 1)
    current = df.iloc[-1]
    for j in range(start, len(df) - 1):
        before, breakout = df.iloc[j - 1], df.iloc[j]
        interim = df.iloc[j + 1:-1]
        if direction == "LONG":
            crossed = float(before.close) <= level_price + buffer and float(breakout.close) > level_price + buffer
            held = interim.empty or bool((interim.close > level_price).all())
            touched = (level_price - tolerance <= float(current.low) <= level_price + tolerance)
            if crossed and held and touched and float(current.close) > level_price:
                return True
        else:
            crossed = float(before.close) >= level_price - buffer and float(breakout.close) < level_price - buffer
            held = interim.empty or bool((interim.close < level_price).all())
            touched = (level_price - tolerance <= float(current.high) <= level_price + tolerance)
            if crossed and held and touched and float(current.close) < level_price:
                return True
    return False


def evaluate_signals(candles: pd.DataFrame, levels: pd.DataFrame, cfg) -> tuple[list[dict], pd.DataFrame, dict]:
    """Evaluate the newest completed 5-minute candle with complete gate diagnostics.

    A LONG breakout is evaluated only at RESISTANCE/REFERENCE levels; a SHORT
    breakout only at SUPPORT/REFERENCE levels. A retest requires a prior
    completed breakout, no invalidating interim close, a wick into the tolerance
    band, and a close back on the breakout side.
    """
    empty_context = {
        "bullish_divergence": False, "bearish_divergence": False,
        "long_fvg": None, "short_fvg": None, "diagnostics": [],
        "data_quality": "INSUFFICIENT_BARS",
        "rsi_divergence_required": bool(getattr(cfg, "rsi_divergence_required", False)),
        "sma_filter_enabled": bool(getattr(cfg, "sma_filter_enabled", False)),
        "fvg_filter_enabled": bool(getattr(cfg, "fvg_filter_enabled", False)),
    }
    if candles.empty or len(candles) < 20 or levels.empty:
        return [], candles.copy(), empty_context

    ordered = candles.sort_values("timestamp").reset_index(drop=True).copy()
    try:
        tail_ts = pd.to_datetime(ordered.timestamp.tail(20))
        if len(tail_ts) < 20 or not (tail_ts.diff().dropna() == pd.Timedelta(minutes=5)).all():
            empty_context["data_quality"] = "GAPPED_5M_DATA"
            return [], ordered, empty_context
    except (TypeError, ValueError):
        empty_context["data_quality"] = "INVALID_TIMESTAMPS"
        return [], ordered, empty_context

    df = add_indicators(ordered, cfg.rsi_period, cfg.pivot_left, cfg.pivot_right)
    bar = df.iloc[-1]
    if pd.isna(bar.sma_10) or pd.isna(bar.atr_14):
        empty_context["data_quality"] = "INDICATORS_NOT_WARMED_UP"
        return [], df, empty_context

    div = rsi_divergence(df)
    long_fvg, short_fvg = latest_fvg(df, "LONG"), latest_fvg(df, "SHORT")
    sma_enabled = bool(getattr(cfg, "sma_filter_enabled", True))
    fvg_enabled = bool(getattr(cfg, "fvg_filter_enabled", True))
    rsi_required = bool(getattr(cfg, "rsi_divergence_required", False))
    immediate_enabled = bool(getattr(cfg, "immediate_entry_enabled", True))
    retest_enabled = bool(getattr(cfg, "retest_entry_enabled", True))
    sma_bull = bool(bar.close > bar.sma_5 and bar.close > bar.sma_10)
    sma_bear = bool(bar.close < bar.sma_5 and bar.close < bar.sma_10)
    context = {
        "bullish_divergence": div["bullish"], "bearish_divergence": div["bearish"],
        "long_fvg": long_fvg, "short_fvg": short_fvg,
        "rsi": float(bar.rsi) if pd.notna(bar.rsi) else None,
        "sma_5": float(bar.sma_5), "sma_10": float(bar.sma_10),
        "atr_14": float(bar.atr_14), "data_quality": "OK",
        "sma_bullish": sma_bull, "sma_bearish": sma_bear,
        "rsi_divergence_required": rsi_required,
        "sma_filter_enabled": sma_enabled, "fvg_filter_enabled": fvg_enabled,
        "diagnostics": [],
    }

    prev = df.iloc[-2]
    signals: list[dict] = []
    diagnostics: list[dict] = []
    buffer = float(cfg.breakout_buffer_points)
    for _, level in levels.iterrows():
        level_price = float(level.price)
        level_name = str(level["name"])
        level_side = str(level.get("side", "REFERENCE")).upper()
        can_break_long = level_side in {"RESISTANCE", "REFERENCE"}
        can_break_short = level_side in {"SUPPORT", "REFERENCE"}
        long_break = bool(can_break_long and float(prev.close) <= level_price + buffer
                          and float(bar.close) > level_price + buffer)
        short_break = bool(can_break_short and float(prev.close) >= level_price - buffer
                           and float(bar.close) < level_price - buffer)

        for direction, immediate_trigger, allowed_side in (
            ("LONG", long_break, can_break_long), ("SHORT", short_break, can_break_short)
        ):
            if not allowed_side:
                continue
            retest_trigger = bool(_retest_found(df, level_price, direction, cfg))
            mode = ("IMMEDIATE" if immediate_trigger else "RETEST" if retest_trigger else None)
            mode_enabled = (immediate_enabled if mode == "IMMEDIATE" else
                            retest_enabled if mode == "RETEST" else None)
            sma_pass = sma_bull if direction == "LONG" else sma_bear
            fvg = long_fvg if direction == "LONG" else short_fvg
            fvg_pass = fvg is not None
            rsi_pass = div["bullish"] if direction == "LONG" else div["bearish"]

            sma_status = "DISABLED" if not sma_enabled else "PASS" if sma_pass else "FAIL"
            fvg_status = "DISABLED" if not fvg_enabled else "PASS" if fvg_pass else "FAIL"
            rsi_status = "PASS" if rsi_pass else ("FAIL" if rsi_required else "NOT_REQUIRED")

            target = next_target(levels, float(bar.close), direction)
            plan = None
            if target is not None:
                entry = float(bar.close)
                risk_buffer = max(float(cfg.breakout_buffer_points), float(cfg.retest_tolerance_points) * 0.25)
                stop_distance = max(float(bar.atr_14), 1.0) * float(cfg.atr_stop_multiplier)
                stop = (min(level_price - risk_buffer, entry - stop_distance) if direction == "LONG"
                        else max(level_price + risk_buffer, entry + stop_distance))
                risk = abs(entry - stop)
                reward = abs(float(target) - entry)
                rr = reward / risk if risk > 0 else 0.0
                plan = {"stop": stop, "target": float(target), "risk": risk,
                        "reward": reward, "reward_risk": rr}
            target_pass = target is not None
            rr_pass = (bool(plan and pd.notna(plan["reward_risk"])
                            and plan["reward_risk"] >= float(cfg.min_reward_risk))) if target_pass else False

            failed: list[str] = []
            if mode is None:
                failed.append("No completed-candle breakout or valid retest")
            elif not mode_enabled:
                failed.append(f"{mode} entries are disabled")
            if sma_enabled and not sma_pass:
                failed.append("SMA confirmation failed: close is not above/below both SMA(5) and SMA(10)")
            if fvg_enabled and not fvg_pass:
                failed.append("FVG confirmation failed: no fresh, unmitigated directional 5m gap")
            if rsi_required and not rsi_pass:
                failed.append("RSI divergence gate failed: required regular divergence is absent")
            if mode is not None and not target_pass:
                failed.append("No eligible next support/resistance/reference target")
            elif mode is not None and target_pass and not rr_pass:
                failed.append(f"Reward/risk {plan['reward_risk']:.2f} is below minimum {cfg.min_reward_risk:.2f}")

            ready = mode is not None and not failed
            if ready:
                reason_bits = ["completed 5m level breakout" if mode == "IMMEDIATE" else "completed 5m retest held level"]
                if sma_enabled:
                    reason_bits.append("SMA(5/10) confirmation passed")
                if fvg_enabled:
                    reason_bits.append("fresh unmitigated directional FVG passed")
                if rsi_required:
                    reason_bits.append("required RSI divergence passed")
                elif not rsi_pass:
                    reason_bits.append("RSI divergence not required; none observed")
                else:
                    reason_bits.append("RSI divergence observed (not a required gate)")
                reason_bits.append(f"next level target {target:.2f}; planned R:R {plan['reward_risk']:.2f}")
                raw_id = f"{pd.Timestamp(bar.timestamp).isoformat()}|{direction}|{mode}|{level_name}|{level_price:.2f}"
                signal_id = hashlib.sha256(raw_id.encode()).hexdigest()[:20]
                stamp = pd.Timestamp(bar.timestamp)
                signal = {
                    "signal_id": signal_id, "created_at": stamp.isoformat(),
                    "session_date": stamp.date().isoformat(), "direction": direction,
                    "entry_mode": mode, "level_name": level_name, "level_price": level_price,
                    "spot_entry": float(bar.close), "spot_stop": plan["stop"],
                    "spot_target": plan["target"], "reward_risk": plan["reward_risk"],
                    "status": "READY", "reason": "; ".join(reason_bits),
                    "rule_snapshot": {
                        "breakout_pass": immediate_trigger, "retest_pass": retest_trigger,
                        "entry_mode_enabled": mode_enabled, "sma_status": sma_status,
                        "sma_5": float(bar.sma_5), "sma_10": float(bar.sma_10),
                        "fvg_status": fvg_status, "fvg": fvg,
                        "rsi_status": rsi_status, "rsi": context["rsi"],
                        "target_pass": target_pass, "reward_risk_pass": rr_pass,
                        "minimum_reward_risk": float(cfg.min_reward_risk),
                    },
                }
                signals.append(signal)
            else:
                signal = None

            if mode is None:
                status = "NO_TRIGGER"
            elif ready:
                status = "READY"
            else:
                status = "FILTERED"
            reason = " | ".join(failed) if failed else "All enabled entry gates passed"
            diagnostics.append({
                "level": level_name, "level_price": level_price, "level_side": level_side,
                "direction": direction, "mode": mode or "-", "status": status,
                "previous_close": float(prev.close), "current_close": float(bar.close),
                "breakout_pass": immediate_trigger, "retest_pass": retest_trigger,
                "entry_mode_enabled": mode_enabled,
                "sma_filter_enabled": sma_enabled, "sma_pass": sma_pass if sma_enabled else None,
                "sma_status": sma_status, "sma_5": float(bar.sma_5), "sma_10": float(bar.sma_10),
                "fvg_filter_enabled": fvg_enabled, "fvg_pass": fvg_pass if fvg_enabled else None,
                "fvg_status": fvg_status,
                "fvg_lower": fvg.get("lower") if fvg else None,
                "fvg_upper": fvg.get("upper") if fvg else None,
                "rsi_divergence_observed": rsi_pass, "rsi_required": rsi_required,
                "rsi_pass": rsi_pass if rsi_required else None, "rsi_status": rsi_status,
                "rsi_value": context["rsi"], "target_pass": target_pass,
                "target": float(target) if target is not None else None,
                "reward_risk": float(plan["reward_risk"]) if plan else None,
                "minimum_reward_risk": float(cfg.min_reward_risk), "reward_risk_pass": rr_pass,
                "reason": reason,
            })

    # At most one signal per direction/mode/candle: prefer the highest R:R level.
    signals.sort(key=lambda x: (x["entry_mode"] != "IMMEDIATE", -x["reward_risk"]))
    seen: set[tuple[str, str]] = set()
    deduped = []
    for signal in signals:
        key = (signal["direction"], signal["entry_mode"])
        if key not in seen:
            seen.add(key)
            deduped.append(signal)
    context["diagnostics"] = diagnostics
    return deduped, df, context
