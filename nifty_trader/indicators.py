from __future__ import annotations

import numpy as np
import pandas as pd


def add_indicators(candles: pd.DataFrame, rsi_period: int = 14,
                   pivot_left: int = 3, pivot_right: int = 3) -> pd.DataFrame:
    if candles.empty:
        return candles.copy()
    df = candles.copy().sort_values("timestamp").reset_index(drop=True)
    close = df["close"].astype(float)
    df["sma_5"] = close.rolling(5, min_periods=5).mean()
    df["sma_10"] = close.rolling(10, min_periods=10).mean()
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder RSI. min_periods avoids manufacturing early values.
    avg_gain = gain.ewm(alpha=1 / rsi_period, adjust=False, min_periods=rsi_period).mean()
    avg_loss = loss.ewm(alpha=1 / rsi_period, adjust=False, min_periods=rsi_period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    df.loc[(avg_loss == 0) & (avg_gain > 0), "rsi"] = 100
    df.loc[(avg_loss == 0) & (avg_gain == 0), "rsi"] = 50

    prev_close = close.shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]).abs(),
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(14, min_periods=14).mean()

    df["pivot_low"] = False
    df["pivot_high"] = False
    # Confirm a pivot only once right-side bars have closed.
    for i in range(pivot_left, len(df) - pivot_right):
        lo = df.loc[i - pivot_left:i + pivot_right, "low"].astype(float)
        hi = df.loc[i - pivot_left:i + pivot_right, "high"].astype(float)
        if df.loc[i, "low"] == lo.min() and (lo == df.loc[i, "low"]).sum() == 1:
            df.loc[i, "pivot_low"] = True
        if df.loc[i, "high"] == hi.max() and (hi == df.loc[i, "high"]).sum() == 1:
            df.loc[i, "pivot_high"] = True
    return df


def rsi_divergence(df: pd.DataFrame) -> dict[str, bool]:
    """Regular divergence from the last two confirmed price pivots."""
    if df.empty or "rsi" not in df:
        return {"bullish": False, "bearish": False}
    lows = df.index[df.get("pivot_low", False) & df["rsi"].notna()].tolist()
    highs = df.index[df.get("pivot_high", False) & df["rsi"].notna()].tolist()
    bull = bear = False
    if len(lows) >= 2:
        a, b = lows[-2:]
        bull = bool(df.loc[b, "low"] < df.loc[a, "low"] and df.loc[b, "rsi"] > df.loc[a, "rsi"])
    if len(highs) >= 2:
        a, b = highs[-2:]
        bear = bool(df.loc[b, "high"] > df.loc[a, "high"] and df.loc[b, "rsi"] < df.loc[a, "rsi"])
    return {"bullish": bull, "bearish": bear}


def latest_fvg(df: pd.DataFrame, direction: str, max_age_bars: int = 3) -> dict | None:
    """Return the newest unmitigated, contiguous three-candle FVG.

    A bullish gap is considered mitigated as soon as a later candle trades into
    its zone (low <= upper edge). A bearish gap is mitigated when a later candle
    trades into its zone (high >= lower edge). Only actual adjacent 5-minute
    bars are used when timestamps are datetime-like.
    """
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    if max_age_bars < 1 or len(df) < 3:
        return None

    data = df.sort_values("timestamp").reset_index(drop=True)
    start = max(2, len(data) - int(max_age_bars))
    for i in range(len(data) - 1, start - 1, -1):
        first, middle, third = data.iloc[i - 2], data.iloc[i - 1], data.iloc[i]
        # Don't form a gap across missing bars or a session boundary.
        try:
            t0, t1, t2 = (pd.Timestamp(first.timestamp), pd.Timestamp(middle.timestamp),
                          pd.Timestamp(third.timestamp))
            datetime_like = not (isinstance(first.timestamp, (int, float))
                                 or isinstance(middle.timestamp, (int, float))
                                 or isinstance(third.timestamp, (int, float)))
            if datetime_like and not (t1 - t0 == pd.Timedelta(minutes=5)
                                      and t2 - t1 == pd.Timedelta(minutes=5)
                                      and t0.date() == t1.date() == t2.date()):
                continue
        except (TypeError, ValueError):
            continue

        if direction == "LONG" and float(third.low) > float(first.high):
            lower, upper = float(first.high), float(third.low)
            later = data.iloc[i + 1:]
            if later.empty or float(later.low.min()) > upper:
                return {"direction": direction, "lower": lower, "upper": upper,
                        "formed_at": third.timestamp, "age_bars": len(data) - 1 - i}
        elif direction == "SHORT" and float(third.high) < float(first.low):
            lower, upper = float(third.high), float(first.low)
            later = data.iloc[i + 1:]
            if later.empty or float(later.high.max()) < lower:
                return {"direction": direction, "lower": lower, "upper": upper,
                        "formed_at": third.timestamp, "age_bars": len(data) - 1 - i}
    return None
