from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")


def _ist_timestamp(value=None) -> pd.Timestamp:
    ts = pd.Timestamp(value if value is not None else datetime.now(IST))
    return ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)


def candle_window(timestamp, interval_minutes: int = 5) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return the displayed start/end of an Upstox candle timestamped by start time."""
    if int(interval_minutes) < 1:
        raise ValueError("interval_minutes must be positive")
    start = _ist_timestamp(timestamp)
    return start, start + pd.Timedelta(minutes=int(interval_minutes))


def next_candle_close(now=None, interval_minutes: int = 5) -> pd.Timestamp:
    """Return the next interval boundary strictly after now, in Asia/Kolkata."""
    interval = int(interval_minutes)
    if interval < 1:
        raise ValueError("interval_minutes must be positive")
    ts = _ist_timestamp(now)
    step_ns = interval * 60 * 1_000_000_000
    next_ns = (ts.value // step_ns + 1) * step_ns
    return pd.Timestamp(next_ns, tz=ts.tz)


def candle_countdown(now=None, interval_minutes: int = 5) -> str:
    """Human-readable countdown to the next expected candle close."""
    ts = _ist_timestamp(now)
    close = next_candle_close(ts, interval_minutes)
    seconds = max(0, int((close - ts).total_seconds()))
    minutes, remaining = divmod(seconds, 60)
    return f"{close:%H:%M} IST ({minutes}m {remaining:02d}s)"
