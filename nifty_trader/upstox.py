from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests

IST = "Asia/Kolkata"


class UpstoxAPIError(RuntimeError):
    pass


class UpstoxClient:
    """Thin, explicit Upstox REST client. No hidden retries on order requests."""

    def __init__(self, market_token: str, trading_token: str = "", base_url: str = "https://api.upstox.com",
                 order_base_url: str = "https://api-hft.upstox.com", timeout: float = 12.0,
                 session: requests.Session | None = None):
        self.market_token = market_token.strip()
        self.trading_token = trading_token.strip()
        self.base_url = base_url.rstrip("/")
        self.order_base_url = order_base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def _request(self, method: str, path: str, *, trading: bool = False,
                 params: dict | None = None, json: dict | None = None,
                 order_host: bool = False) -> dict[str, Any]:
        token = self.trading_token if trading else self.market_token
        if not token:
            required = "UPSTOX_ACCESS_TOKEN" if trading else "UPSTOX_ANALYTICS_TOKEN (or UPSTOX_ACCESS_TOKEN)"
            raise ValueError(f"{required} is required for this Upstox API operation")
        base = self.order_base_url if order_host else self.base_url
        response = self.session.request(
            method, f"{base}{path}", params=params, json=json,
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"}, timeout=self.timeout)
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": response.text[:1000]}
        if not response.ok:
            raise UpstoxAPIError(f"Upstox HTTP {response.status_code}: {payload}")
        if isinstance(payload, dict) and payload.get("status") not in (None, "success"):
            raise UpstoxAPIError(f"Upstox API error: {payload}")
        return payload

    def intraday_candles(self, instrument_key: str, interval_minutes: int = 5) -> pd.DataFrame:
        if not 1 <= interval_minutes <= 300:
            raise ValueError("interval_minutes must be 1..300")
        key = quote(instrument_key, safe="")
        payload = self._request("GET", f"/v3/historical-candle/intraday/{key}/minutes/{interval_minutes}")
        return normalize_candles(payload.get("data", {}).get("candles") or [], interval_minutes=interval_minutes)

    def historical_candles(self, instrument_key: str, start: date, end: date,
                           unit: str = "days", interval: int = 1) -> pd.DataFrame:
        key = quote(instrument_key, safe="")
        payload = self._request("GET", f"/v3/historical-candle/{key}/{unit}/{interval}/{end:%Y-%m-%d}/{start:%Y-%m-%d}")
        return normalize_candles(payload.get("data", {}).get("candles") or [], interval_minutes=interval if unit == "minutes" else None)

    def option_contracts(self, underlying_key: str) -> list[dict]:
        payload = self._request("GET", "/v2/option/contract", params={"instrument_key": underlying_key})
        return payload.get("data") or []

    def option_chain(self, underlying_key: str, expiry: str) -> list[dict]:
        payload = self._request("GET", "/v2/option/chain", params={"instrument_key": underlying_key, "expiry_date": expiry})
        return payload.get("data") or []

    def place_limit_order(self, *, instrument_key: str, quantity: int, price: float,
                          side: str, tag: str, product: str = "I") -> dict:
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if quantity <= 0 or price <= 0:
            raise ValueError("quantity and price must be positive")
        body = {
            "quantity": int(quantity), "product": product, "validity": "DAY",
            "price": float(price), "tag": tag[:20], "instrument_token": instrument_key,
            "order_type": "LIMIT", "transaction_type": side,
            "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False, "slice": False,
        }
        return self._request("POST", "/v3/order/place", trading=True, json=body, order_host=True)

    def order_history(self, order_id: str) -> dict:
        return self._request("GET", "/v2/order/history", trading=True,
                             params={"order_id": order_id})

    def order_details(self, order_id: str) -> dict:
        return self._request("GET", "/v2/order/details", trading=True,
                             params={"order_id": order_id})

    def cancel_order(self, order_id: str) -> dict:
        if not str(order_id).strip():
            raise ValueError("order_id is required")
        return self._request("DELETE", "/v3/order/cancel", trading=True,
                             params={"order_id": str(order_id).strip()}, order_host=True)


def normalize_candles(rows: list[list[Any]], interval_minutes: int | None = None) -> pd.DataFrame:
    records = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            ts = pd.Timestamp(row[0])
            if pd.isna(ts):
                continue
            ts = ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)
            o, h, lo, c = (float(row[i]) for i in (1, 2, 3, 4))
            volume = float(row[5] or 0)
            oi = float(row[6]) if len(row) > 6 and row[6] is not None else None
            values = [o, h, lo, c, volume]
            if not all(pd.notna(v) and abs(v) != float("inf") for v in values):
                continue
            if min(o, h, lo, c) < 0 or volume < 0:
                continue
            if h < max(o, c, lo) or lo > min(o, c, h):
                continue
            if oi is not None and (pd.isna(oi) or oi < 0):
                oi = None
            records.append({"timestamp": ts, "open": o, "high": h, "low": lo,
                            "close": c, "volume": volume, "oi": oi})
        except (TypeError, ValueError, OverflowError):
            continue
    df = pd.DataFrame(records, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    if df.empty:
        return df
    df = df.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)
    if interval_minutes:
        as_of = pd.Timestamp.now(tz=IST)
        df = df[df.timestamp + pd.Timedelta(minutes=interval_minutes) <= as_of].reset_index(drop=True)
    return df
