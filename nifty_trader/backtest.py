from __future__ import annotations

from datetime import time

import pandas as pd

from .levels import build_levels
from .strategy import evaluate_signals


def _ist_timestamp(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("Asia/Kolkata") if ts.tzinfo is None else ts.tz_convert("Asia/Kolkata")


def backtest_spot(candles: pd.DataFrame, daily: pd.DataFrame, cfg,
                  manual_levels: list[dict] | None = None,
                  max_trades_per_day: int | None = None) -> tuple[pd.DataFrame, dict]:
    """Causal spot backtest; conservative intrabar and end-of-day handling.

    Signals are decided at a completed 5-minute candle close and enter no earlier
    than the next 5-minute candle open. A signal is skipped if the next open has
    already invalidated the level, stop, or target. A same-bar stop/target tie
    resolves to the stop. Positions are flattened by the 15:20 IST time exit.
    Results are index points, not options premium or rupee P&L.
    """
    required = {"timestamp", "open", "high", "low", "close"}
    if candles.empty or not required.issubset(candles.columns) or len(candles) < 25:
        raise ValueError("At least 25 completed 5-minute spot candles with OHLC timestamps are required")

    data = candles.copy()
    data["timestamp"] = data.timestamp.map(_ist_timestamp)
    for col in ("open", "high", "low", "close"):
        data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.dropna(subset=["timestamp", "open", "high", "low", "close"])
    data = data.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)
    if len(data) < 25:
        raise ValueError("Fewer than 25 valid completed 5-minute candles remain after cleaning")
    invalid = ((data.high < data[["open", "close", "low"]].max(axis=1))
               | (data.low > data[["open", "close", "high"]].min(axis=1)))
    if invalid.any():
        raise ValueError(f"Input contains {int(invalid.sum())} malformed OHLC candles")

    daily_data = daily.copy()
    if not daily_data.empty:
        daily_data["timestamp"] = daily_data.timestamp.map(_ist_timestamp)

    trade_limit = int(max_trades_per_day if max_trades_per_day is not None
                      else getattr(cfg, "max_trades_per_day", 2))
    try:
        last_entry_cutoff = time.fromisoformat(getattr(cfg, "last_entry_time", "15:00"))
    except ValueError:
        last_entry_cutoff = time(15, 0)

    results: list[dict] = []
    traded_signal_ids: set[str] = set()
    trades_by_day: dict[str, int] = {}
    i = 20
    while i < len(data) - 1:
        hist = data.iloc[:i + 1].copy()
        signal_bar = hist.iloc[-1]
        session_day = signal_bar.timestamp.date().isoformat()
        day_hist = hist[hist.timestamp.dt.date.astype(str) == session_day].copy().reset_index(drop=True)
        if len(day_hist) < 20:
            i += 1
            continue

        # The signal bar must close by the configured last-entry cutoff.
        signal_bar_end = signal_bar.timestamp + pd.Timedelta(minutes=5)
        if signal_bar_end.time() > last_entry_cutoff:
            i += 1
            continue

        levels = build_levels(day_hist, daily_data, manual_levels,
                              cfg.pivot_left, cfg.pivot_right)
        signals, _, _ = evaluate_signals(day_hist, levels, cfg)
        candidate = next((s for s in signals
                          if s["signal_id"] not in traded_signal_ids), None)
        if candidate is None or trades_by_day.get(session_day, 0) >= trade_limit:
            i += 1
            continue

        entry_i = i + 1
        entry_bar = data.iloc[entry_i]
        if entry_bar.timestamp.date() != signal_bar.timestamp.date():
            i += 1
            continue
        if entry_bar.timestamp.time() > last_entry_cutoff:
            i += 1
            continue

        direction = candidate["direction"]
        entry = float(entry_bar.open)
        level = float(candidate["level_price"])
        stop = float(candidate["spot_stop"])
        target = float(candidate["spot_target"])
        buffer = float(getattr(cfg, "breakout_buffer_points", 0.0))

        # Reject a stale next-bar entry after price has fallen back through the
        # broken level, already crossed the target, or invalidated the stop.
        if direction == "LONG" and not (entry > level + buffer and stop < entry < target):
            traded_signal_ids.add(candidate["signal_id"])
            i += 1
            continue
        if direction == "SHORT" and not (entry < level - buffer and target < entry < stop):
            traded_signal_ids.add(candidate["signal_id"])
            i += 1
            continue
        traded_signal_ids.add(candidate["signal_id"])

        exit_i, exit_price, reason = None, None, None
        for j in range(entry_i, len(data)):
            bar = data.iloc[j]
            bar_day = bar.timestamp.date()
            if bar_day != entry_bar.timestamp.date():
                exit_i, exit_price, reason = j - 1, float(data.iloc[j - 1].close), "SESSION_END"
                break

            # Stop always wins if both are touched in a single 5m candle.
            if direction == "LONG":
                stop_hit = float(bar.low) <= stop
                target_hit = float(bar.high) >= target
                if stop_hit:
                    exit_i = j
                    exit_price = min(float(bar.open), stop) if float(bar.open) < stop else stop
                    reason = "STOP"
                    break
                if target_hit:
                    exit_i, exit_price, reason = j, max(float(bar.open), target), "TARGET"
                    break
            else:
                stop_hit = float(bar.high) >= stop
                target_hit = float(bar.low) <= target
                if stop_hit:
                    exit_i = j
                    exit_price = max(float(bar.open), stop) if float(bar.open) > stop else stop
                    reason = "STOP"
                    break
                if target_hit:
                    exit_i, exit_price, reason = j, min(float(bar.open), target), "TARGET"
                    break

            bar_end = bar.timestamp + pd.Timedelta(minutes=5)
            if bar_end.time() >= time(15, 20):
                exit_i, exit_price, reason = j, float(bar.close), "TIME_EXIT"
                break

        if exit_i is None:
            exit_i, exit_price, reason = len(data) - 1, float(data.iloc[-1].close), "DATA_END"

        points = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
        results.append({
            "signal_id": candidate["signal_id"], "timestamp": entry_bar.timestamp,
            "direction": direction, "entry_mode": candidate["entry_mode"],
            "level": candidate["level_name"], "entry_spot": entry,
            "stop_spot": stop, "target_spot": target,
            "exit_timestamp": data.iloc[exit_i].timestamp, "exit_spot": exit_price,
            "exit_reason": reason, "pnl_points": points,
            "reward_risk_planned": candidate["reward_risk"],
        })
        trades_by_day[session_day] = trades_by_day.get(session_day, 0) + 1
        i = max(i + 1, exit_i + 1)

    trades = pd.DataFrame(results)
    if trades.empty:
        metrics = {"trades": 0, "win_rate_pct": 0.0, "net_points": 0.0,
                   "profit_factor": None, "max_drawdown_points": 0.0,
                   "note": "No signals passed the configured filters. Spot points only; not options P&L."}
        return trades, metrics

    wins = trades[trades.pnl_points > 0]
    losses = trades[trades.pnl_points < 0]
    gross_profit = float(wins.pnl_points.sum())
    gross_loss = abs(float(losses.pnl_points.sum()))
    equity = trades.pnl_points.cumsum()
    running_peak = equity.cummax().clip(lower=0.0)
    drawdown = equity - running_peak
    metrics = {
        "trades": int(len(trades)),
        "win_rate_pct": float((trades.pnl_points > 0).mean() * 100),
        "net_points": float(trades.pnl_points.sum()),
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "max_drawdown_points": abs(float(drawdown.min())) if not drawdown.empty else 0.0,
        "note": "Spot-index points only. No options premium, IV, theta, bid/ask, brokerage, taxes or fills are simulated.",
    }
    return trades, metrics
