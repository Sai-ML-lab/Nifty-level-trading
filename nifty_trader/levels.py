from __future__ import annotations

from datetime import time
import pandas as pd


def _row(name: str, price: float, side: str, source: str, timestamp=None) -> dict:
    return {"name": name, "price": float(price), "side": side,
            "source": source, "timestamp": timestamp}


def build_levels(intraday: pd.DataFrame, daily: pd.DataFrame,
                 manual: list[dict] | None = None,
                 pivot_left: int = 3, pivot_right: int = 3) -> pd.DataFrame:
    levels: list[dict] = []
    if not intraday.empty:
        session_date = pd.Timestamp(intraday.iloc[-1].timestamp).date()
    else:
        session_date = None
    if not daily.empty and session_date:
        d = daily.copy()
        d["session_date"] = pd.to_datetime(d["timestamp"]).dt.date
        prior = d[d.session_date < session_date]
        if not prior.empty:
            p = prior.iloc[-1]
            levels += [
                _row("PDH", p.high, "RESISTANCE", "PREVIOUS_DAY", p.timestamp),
                _row("PDL", p.low, "SUPPORT", "PREVIOUS_DAY", p.timestamp),
                _row("PDC", p.close, "REFERENCE", "PREVIOUS_DAY", p.timestamp),
            ]
    if not intraday.empty:
        df = intraday.copy().sort_values("timestamp").reset_index(drop=True)
        ts = pd.to_datetime(df.timestamp)
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize("Asia/Kolkata")
        else:
            ts = ts.dt.tz_convert("Asia/Kolkata")
        df["_ts"] = ts
        day = df[df._ts.dt.date == session_date]
        day = day.sort_values("_ts").reset_index(drop=True)
        opening = day[(day._ts.dt.time >= time(9, 15)) & (day._ts.dt.time < time(9, 30))]
        # Never infer an opening range from three arbitrary surviving bars.
        # Require the exact 09:15, 09:20 and 09:25 candles to be present.
        expected_opening = [
            pd.Timestamp.combine(session_date, time(9, 15)).tz_localize("Asia/Kolkata"),
            pd.Timestamp.combine(session_date, time(9, 20)).tz_localize("Asia/Kolkata"),
            pd.Timestamp.combine(session_date, time(9, 25)).tz_localize("Asia/Kolkata"),
        ]
        opening_stamps = set(opening._ts.tolist())
        if all(stamp in opening_stamps for stamp in expected_opening):
            opening = opening[opening._ts.isin(expected_opening)]
            levels += [
                _row("ORH", opening.high.max(), "RESISTANCE", "OPENING_RANGE"),
                _row("ORL", opening.low.min(), "SUPPORT", "OPENING_RANGE"),
            ]
        # Only completed swing pivots; reject any pivot window crossing a
        # missing 5-minute candle or an overnight/session boundary.
        for i in range(pivot_left, len(day) - pivot_right):
            window = day.iloc[i-pivot_left:i+pivot_right+1]
            window_ts = window._ts.reset_index(drop=True)
            if not (window_ts.diff().dropna() == pd.Timedelta(minutes=5)).all():
                continue
            lo = window.low.astype(float)
            hi = window.high.astype(float)
            if day.iloc[i].low == lo.min() and (lo == day.iloc[i].low).sum() == 1:
                levels.append(_row(f"SWING_L_{i}", day.iloc[i].low, "SUPPORT", "SWING", day.iloc[i].timestamp))
            if day.iloc[i].high == hi.max() and (hi == day.iloc[i].high).sum() == 1:
                levels.append(_row(f"SWING_H_{i}", day.iloc[i].high, "RESISTANCE", "SWING", day.iloc[i].timestamp))
    for item in manual or []:
        if item.get("active", True):
            levels.append(_row(item.get("name", "MANUAL"), item["price"],
                               item.get("side", "REFERENCE"), "MANUAL"))
    if not levels:
        return pd.DataFrame(columns=["name", "price", "side", "source", "timestamp"])
    return (pd.DataFrame(levels).drop_duplicates(subset=["name", "price", "source"])
            .sort_values("price").reset_index(drop=True))


def next_target(levels: pd.DataFrame, entry: float, direction: str) -> float | None:
    """Nearest directional target; a support label is not a long target, etc."""
    if levels.empty:
        return None
    eligible_sides = {"RESISTANCE", "REFERENCE"} if direction == "LONG" else {"SUPPORT", "REFERENCE"}
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    eligible = levels[levels.get("side", pd.Series("REFERENCE", index=levels.index)).astype(str).str.upper().isin(eligible_sides)]
    prices = pd.to_numeric(eligible.price, errors="coerce").dropna()
    if direction == "LONG":
        candidates = sorted(set(float(x) for x in prices if x > entry))
    else:
        candidates = sorted(set(float(x) for x in prices if x < entry), reverse=True)
    return candidates[0] if candidates else None
