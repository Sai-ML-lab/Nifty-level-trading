from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def pd_is_finite(value) -> bool:
    try:
        return float(value) == float(value) and abs(float(value)) != float("inf")
    except (TypeError, ValueError):
        return False


@dataclass
class SizeDecision:
    allowed: bool
    lots: int = 0
    quantity: int = 0
    estimated_risk: float = 0.0
    position_value: float = 0.0
    maximum_premium_loss: float = 0.0
    reason: str = ""


def market_is_open(now: datetime | None = None) -> bool:
    ts = now.astimezone(IST) if now and now.tzinfo else (now.replace(tzinfo=IST) if now else datetime.now(IST))
    return ts.weekday() < 5 and time(9, 15) <= ts.time() <= time(15, 25)


def size_long_option(*, premium: float, lot_size: int, cfg) -> SizeDecision:
    if not (premium > 0 and premium < float("inf")) or lot_size <= 0:
        return SizeDecision(False, reason="Invalid premium or lot size")
    per_lot_value = premium * lot_size
    # Long-option premium risk is conservatively budgeted at a configurable 35% adverse move.
    stop_pct = min(0.95, max(0.05, float(getattr(cfg, "premium_stop_loss_pct", 0.35))))
    per_lot_risk = per_lot_value * stop_pct
    risk_lots = int(cfg.risk_per_trade_inr // per_lot_risk) if per_lot_risk > 0 else 0
    value_lots = int(cfg.max_position_value_inr // per_lot_value) if per_lot_value > 0 else 0
    lots = min(int(cfg.max_lots), risk_lots, value_lots)
    if lots < 1:
        return SizeDecision(False, reason=(f"Cannot size one lot within risk/value caps: "
                                            f"one-lot value ₹{per_lot_value:,.0f}, "
                                            f"estimated risk ₹{per_lot_risk:,.0f}"))
    return SizeDecision(True, lots=lots, quantity=lots * lot_size,
                        estimated_risk=lots * per_lot_risk, position_value=lots * per_lot_value,
                        maximum_premium_loss=lots * per_lot_value,
                        reason=("Within configured lot, stop-risk and premium-value limits; "
                                "maximum loss without an executable stop is the full premium paid"))


def pretrade_gate(*, cfg, store, session_date: str, mode: str = "PAPER",
                  now: datetime | None = None, data_age_seconds: float = 0.0,
                  exclude_order_ref: str | None = None) -> tuple[bool, str]:
    ts = now or datetime.now(IST)
    if not market_is_open(ts):
        return False, "Outside the configured NSE intraday trading window"
    try:
        last_entry = time.fromisoformat(getattr(cfg, "last_entry_time", "15:00"))
    except ValueError:
        last_entry = time(15, 0)
    if ts.astimezone(IST).time() > last_entry:
        return False, f"Past the configured last-entry time ({last_entry.strftime('%H:%M')} IST)"
    if data_age_seconds < 0 or not pd_is_finite(data_age_seconds):
        return False, "Invalid market-data age"
    if data_age_seconds > cfg.stale_data_seconds:
        return False, f"Market data stale ({data_age_seconds:.0f}s old)"
    if store.get_state("kill_switch", "OFF") == "ON":
        return False, "Kill switch is ON"
    if store.count_trades(session_date, mode) >= cfg.max_trades_per_day:
        return False, "Maximum trades per day reached"
    with store.connect() as con:
        if exclude_order_ref:
            pending = con.execute("SELECT COUNT(*) n FROM orders WHERE mode=? AND side='BUY' AND order_ref<>? AND status IN ('PENDING_APPROVAL','SUBMITTING','SUBMITTED','PARTIALLY_FILLED','SUBMIT_ERROR','SUBMISSION_UNKNOWN','CANCEL_REQUESTED','OPEN')", (mode, exclude_order_ref)).fetchone()["n"]
        else:
            pending = con.execute("SELECT COUNT(*) n FROM orders WHERE mode=? AND side='BUY' AND status IN ('PENDING_APPROVAL','SUBMITTING','SUBMITTED','PARTIALLY_FILLED','SUBMIT_ERROR','SUBMISSION_UNKNOWN','CANCEL_REQUESTED','OPEN')", (mode,)).fetchone()["n"]
    if int(pending or 0) > 0:
        return False, "An entry order is awaiting approval, fill or reconciliation"
    if store.day_pnl(session_date, mode) <= -abs(cfg.max_daily_loss_inr):
        return False, "Daily loss limit reached"
    if not store.open_trades(mode).empty:
        return False, "An open position already exists for this execution mode"
    return True, "Pre-trade risk checks passed"
