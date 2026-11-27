from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    instrument_key: str = os.getenv("NIFTY_INSTRUMENT_KEY", "NSE_INDEX|Nifty 50")
    market_data_token: str = os.getenv("UPSTOX_ANALYTICS_TOKEN", "") or os.getenv("UPSTOX_ACCESS_TOKEN", "")
    trading_token: str = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    base_url: str = os.getenv("UPSTOX_BASE_URL", "https://api.upstox.com")
    order_base_url: str = os.getenv("UPSTOX_ORDER_BASE_URL", "https://api-hft.upstox.com")
    db_path: str = os.getenv("TRADER_DB_PATH", str(ROOT / "data" / "trader.sqlite3"))
    data_dir: str = os.getenv("TRADER_DATA_DIR", str(ROOT / "data"))
    poll_seconds: int = _int("POLL_SECONDS", 60)
    stale_data_seconds: int = _int("STALE_DATA_SECONDS", 180)
    max_signal_age_seconds: int = _int("MAX_SIGNAL_AGE_SECONDS", 180)
    last_entry_time: str = os.getenv("LAST_ENTRY_TIME", "15:00")

    sma_filter_enabled: bool = _bool("SMA_FILTER_ENABLED", True)
    fvg_filter_enabled: bool = _bool("FVG_FILTER_ENABLED", True)
    rsi_divergence_required: bool = _bool("RSI_DIVERGENCE_REQUIRED", False)
    immediate_entry_enabled: bool = _bool("IMMEDIATE_ENTRY_ENABLED", True)
    retest_entry_enabled: bool = _bool("RETEST_ENTRY_ENABLED", True)
    retest_bars: int = _int("RETEST_BARS", 3)
    pivot_left: int = _int("PIVOT_LEFT", 3)
    pivot_right: int = _int("PIVOT_RIGHT", 3)
    rsi_period: int = _int("RSI_PERIOD", 14)
    breakout_buffer_points: float = _float("BREAKOUT_BUFFER_POINTS", 2.0)
    retest_tolerance_points: float = _float("RETEST_TOLERANCE_POINTS", 8.0)
    atr_stop_multiplier: float = _float("ATR_STOP_MULTIPLIER", 1.0)
    min_reward_risk: float = _float("MIN_REWARD_RISK", 1.2)

    max_trades_per_day: int = _int("MAX_TRADES_PER_DAY", 2)
    max_daily_loss_inr: float = _float("MAX_DAILY_LOSS_INR", 2000.0)
    risk_per_trade_inr: float = _float("RISK_PER_TRADE_INR", 2000.0)
    max_lots: int = _int("MAX_LOTS", 1)
    max_premium_inr: float = _float("MAX_PREMIUM_INR", 500.0)
    min_premium_inr: float = _float("MIN_PREMIUM_INR", 30.0)
    max_spread_pct: float = _float("MAX_SPREAD_PCT", 0.08)
    min_option_volume: int = _int("MIN_OPTION_VOLUME", 1000)
    min_option_oi: int = _int("MIN_OPTION_OI", 10000)
    option_moneyness_pct: float = _float("OPTION_MONEYNESS_PCT", 0.01)
    slippage_points: float = _float("SLIPPAGE_POINTS", 0.5)
    max_approval_price_drift_pct: float = _float("MAX_APPROVAL_PRICE_DRIFT_PCT", 0.02)
    premium_stop_loss_pct: float = _float("PREMIUM_STOP_LOSS_PCT", 0.35)
    estimated_round_trip_cost_per_lot: float = _float("ESTIMATED_ROUND_TRIP_COST_PER_LOT", 100.0)
    max_position_value_inr: float = _float("MAX_POSITION_VALUE_INR", 15000.0)

    live_trading_enabled: bool = _bool("LIVE_TRADING_ENABLED", False)
    allow_live_orders: bool = _bool("ALLOW_LIVE_ORDERS", False)
    live_product: str = os.getenv("LIVE_PRODUCT", "I")

    @property
    def live_is_armed(self) -> bool:
        return bool(self.live_trading_enabled and self.allow_live_orders and self.trading_token)


settings = Settings()
