from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")


def _json_safe(value):
    """Convert pandas/numpy values and non-finite numbers into strict JSON values."""
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except (ValueError, TypeError):
            pass
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (ValueError, TypeError):
            pass
    try:
        missing = pd.isna(value)
        if isinstance(missing, bool) and missing:
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (str, bool)):
        return value
    return str(value)


def _json_dumps(value) -> str:
    return json.dumps(_json_safe(value), allow_nan=False, separators=(",", ":"))


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS manual_levels (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_date TEXT NOT NULL,
 name TEXT NOT NULL, price REAL NOT NULL, side TEXT NOT NULL,
 notes TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS option_snapshots (
 id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, expiry TEXT NOT NULL,
 instrument_key TEXT NOT NULL, option_type TEXT NOT NULL, strike REAL NOT NULL,
 ltp REAL, volume INTEGER, oi REAL, bid REAL, ask REAL, payload TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_option_snapshots_key_ts ON option_snapshots(instrument_key, timestamp);
CREATE TABLE IF NOT EXISTS signals (
 signal_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, session_date TEXT NOT NULL,
 direction TEXT NOT NULL, entry_mode TEXT NOT NULL, level_name TEXT NOT NULL,
 level_price REAL NOT NULL, spot_entry REAL NOT NULL, spot_stop REAL NOT NULL,
 spot_target REAL NOT NULL, reward_risk REAL NOT NULL, status TEXT NOT NULL,
 reason TEXT NOT NULL, instrument_key TEXT, expiry TEXT, strike REAL,
 option_type TEXT, option_ltp REAL, lot_size INTEGER, lots INTEGER, quantity INTEGER,
 estimated_risk REAL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
 order_ref TEXT PRIMARY KEY, signal_id TEXT NOT NULL, mode TEXT NOT NULL,
 side TEXT NOT NULL, instrument_key TEXT NOT NULL, quantity INTEGER NOT NULL,
 limit_price REAL NOT NULL, status TEXT NOT NULL, broker_order_id TEXT,
 approval_text TEXT, approved_at TEXT, submitted_at TEXT, payload TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS trades (
 trade_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL, mode TEXT NOT NULL,
 direction TEXT NOT NULL, instrument_key TEXT NOT NULL, expiry TEXT,
 entry_price REAL NOT NULL, quantity INTEGER NOT NULL, lot_size INTEGER NOT NULL,
 spot_entry REAL NOT NULL, spot_stop REAL NOT NULL, spot_target REAL NOT NULL,
 premium_stop REAL NOT NULL, status TEXT NOT NULL, opened_at TEXT NOT NULL,
 exit_price REAL, exited_at TEXT, exit_reason TEXT, gross_pnl REAL DEFAULT 0,
 charges REAL DEFAULT 0, net_pnl REAL DEFAULT 0, broker_order_id TEXT,
 payload TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS signal_diagnostics (
 candle_timestamp TEXT NOT NULL, level_name TEXT NOT NULL, direction TEXT NOT NULL,
 mode TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, seen_count INTEGER NOT NULL DEFAULT 1,
 payload TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(candle_timestamp,level_name,direction,mode)
);
CREATE TABLE IF NOT EXISTS engine_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, event_timestamp TEXT NOT NULL,
 event_type TEXT NOT NULL, trade_id TEXT, signal_id TEXT, payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_engine_events_ts ON engine_events(event_timestamp);
CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)
            # Older development databases may contain duplicate polls from a
            # double-click/restart. Keep the last row per quote timestamp/key.
            con.execute("""DELETE FROM option_snapshots
                         WHERE id NOT IN (SELECT MAX(id) FROM option_snapshots
                                          GROUP BY timestamp,instrument_key)""")
            con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_option_snapshot_ts_key
                         ON option_snapshots(timestamp,instrument_key)""")
            # A single signal may create at most one position per execution mode.
            # Preserve the most recently written historical row before enforcing it.
            con.execute("""DELETE FROM trades WHERE rowid NOT IN (
                         SELECT MAX(rowid) FROM trades GROUP BY signal_id,mode)""")
            con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_trade_signal_mode
                         ON trades(signal_id,mode)""")

    def connect(self):
        con = sqlite3.connect(self.path, timeout=20)
        con.row_factory = sqlite3.Row
        return con

    def add_manual_level(self, session_date: str, name: str, price: float,
                         side: str, notes: str = "") -> int:
        from datetime import datetime
        if float(price) <= 0 or side not in {"SUPPORT", "RESISTANCE", "REFERENCE"} or not str(name).strip():
            raise ValueError("Manual level requires a name, positive price and valid side")
        with self.connect() as con:
            cur = con.execute("INSERT INTO manual_levels(session_date,name,price,side,notes,active,created_at) VALUES(?,?,?,?,?,1,?)",
                              (session_date, str(name).strip(), float(price), side, notes, datetime.now(IST).isoformat()))
            return int(cur.lastrowid)

    def manual_levels(self, session_date: str) -> list[dict]:
        with self.connect() as con:
            return [dict(x) for x in con.execute(
                "SELECT * FROM manual_levels WHERE session_date=? AND active=1 ORDER BY price", (session_date,)
            ).fetchall()]

    def deactivate_level(self, level_id: int) -> None:
        with self.connect() as con:
            con.execute("UPDATE manual_levels SET active=0 WHERE id=?", (int(level_id),))

    def save_option_snapshot(self, frame: pd.DataFrame) -> None:
        if frame.empty:
            return
        with self.connect() as con:
            for row in frame.to_dict("records"):
                stamp = str(row.get("timestamp"))
                key = str(row.get("instrument_key") or "")
                if not key or not stamp:
                    continue
                payload = _json_dumps(row)
                con.execute("""
                    INSERT INTO option_snapshots(timestamp,expiry,instrument_key,option_type,strike,
                                                 ltp,volume,oi,bid,ask,payload)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(timestamp,instrument_key) DO UPDATE SET
                      expiry=excluded.expiry, option_type=excluded.option_type,
                      strike=excluded.strike, ltp=excluded.ltp, volume=excluded.volume,
                      oi=excluded.oi, bid=excluded.bid, ask=excluded.ask, payload=excluded.payload
                """, (stamp, str(row.get("expiry")), key, row.get("option_type"),
                      float(row.get("strike") or 0), row.get("ltp"), int(row.get("volume") or 0),
                      float(row.get("oi") or 0), row.get("bid"), row.get("ask"), payload))

    def latest_option_snapshots(self, session_date: str) -> pd.DataFrame:
        """Load the most recent complete option-chain snapshot for an IST session."""
        with self.connect() as con:
            row = con.execute(
                "SELECT MAX(timestamp) ts FROM option_snapshots WHERE substr(timestamp,1,10)=?",
                (str(session_date),),
            ).fetchone()
            latest_ts = row["ts"] if row else None
            if not latest_ts:
                return pd.DataFrame()
            rows = con.execute(
                "SELECT payload FROM option_snapshots WHERE timestamp=? ORDER BY expiry,strike,option_type",
                (latest_ts,),
            ).fetchall()
        records = []
        for item in rows:
            try:
                records.append(json.loads(item["payload"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                continue
        return pd.DataFrame(records)

    def previous_option_snapshot(self, instrument_keys: list[str], session_date: str | None = None) -> pd.DataFrame:
        """Return the latest earlier same-session quote for each option instrument."""
        return self._option_snapshot_by_time(instrument_keys, session_date, first=False)

    def first_option_snapshot(self, instrument_keys: list[str], session_date: str | None = None) -> pd.DataFrame:
        """Return the first locally captured same-session quote for each instrument."""
        return self._option_snapshot_by_time(instrument_keys, session_date, first=True)

    def _option_snapshot_by_time(self, instrument_keys: list[str], session_date: str | None,
                                 *, first: bool) -> pd.DataFrame:
        if not instrument_keys:
            return pd.DataFrame()
        placeholders = ",".join("?" for _ in instrument_keys)
        date_filter = "AND substr(timestamp,1,10)=?" if session_date else ""
        params = list(instrument_keys) + ([str(session_date)] if session_date else [])
        selector = "MIN" if first else "MAX"
        with self.connect() as con:
            rows = con.execute(f"""
                SELECT s.instrument_key,s.oi,s.timestamp FROM option_snapshots s
                JOIN (SELECT instrument_key,{selector}(timestamp) ts FROM option_snapshots
                      WHERE instrument_key IN ({placeholders}) {date_filter} GROUP BY instrument_key) x
                ON s.instrument_key=x.instrument_key AND s.timestamp=x.ts
                ORDER BY s.instrument_key
            """, params).fetchall()
        return pd.DataFrame([dict(r) for r in rows])

    def save_event(self, event: dict, event_timestamp: str | None = None) -> None:
        """Persist an operational/strategy event for post-session audit."""
        from datetime import datetime
        payload = dict(event or {})
        event_type = str(payload.pop("type", "UNCLASSIFIED_EVENT"))
        stamp = str(event_timestamp or payload.pop("timestamp", None) or datetime.now(IST).isoformat())
        trade_id = payload.get("trade_id")
        signal_id = payload.get("signal_id")
        with self.connect() as con:
            con.execute(
                "INSERT INTO engine_events(event_timestamp,event_type,trade_id,signal_id,payload) VALUES(?,?,?,?,?)",
                (stamp, event_type, str(trade_id) if trade_id is not None else None,
                 str(signal_id) if signal_id is not None else None, _json_dumps(payload)),
            )

    def recent_events(self, limit: int = 500) -> pd.DataFrame:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM engine_events ORDER BY event_timestamp DESC,id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return pd.DataFrame([dict(row) for row in rows])

    def save_diagnostics(self, diagnostics: list[dict], candle_timestamp: str, seen_at: str) -> None:
        if not diagnostics:
            return
        with self.connect() as con:
            for item in diagnostics:
                con.execute("""
                    INSERT INTO signal_diagnostics(candle_timestamp,level_name,direction,mode,status,reason,
                                                   first_seen,last_seen,seen_count,payload)
                    VALUES(?,?,?,?,?,?,?,?,1,?)
                    ON CONFLICT(candle_timestamp,level_name,direction,mode) DO UPDATE SET
                      status=excluded.status, reason=excluded.reason, last_seen=excluded.last_seen,
                      seen_count=signal_diagnostics.seen_count+1, payload=excluded.payload
                """, (str(candle_timestamp), str(item.get("level", "")),
                      str(item.get("direction", "")), str(item.get("mode", "-")),
                      str(item.get("status", "")), str(item.get("reason", "")),
                      str(seen_at), str(seen_at), _json_dumps(item)))

    def recent_diagnostics(self, limit: int = 500) -> pd.DataFrame:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM signal_diagnostics ORDER BY candle_timestamp DESC, level_name LIMIT ?",
                (int(limit),)
            ).fetchall()
        return pd.DataFrame([dict(r) for r in rows])

    def save_signal(self, signal: dict) -> bool:
        cols = ["signal_id","created_at","session_date","direction","entry_mode","level_name","level_price",
                "spot_entry","spot_stop","spot_target","reward_risk","status","reason","instrument_key",
                "expiry","strike","option_type","option_ltp","lot_size","lots","quantity","estimated_risk","payload"]
        values = [signal.get(c) for c in cols]
        values[-1] = _json_dumps(signal)
        with self.connect() as con:
            cur = con.execute(f"INSERT OR IGNORE INTO signals({','.join(cols)}) VALUES({','.join('?' for _ in cols)})", values)
            return cur.rowcount == 1

    def recent_signals(self, limit: int = 100) -> pd.DataFrame:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return pd.DataFrame([dict(r) for r in rows])

    def save_order(self, order: dict) -> None:
        cols = ["order_ref","signal_id","mode","side","instrument_key","quantity","limit_price","status",
                "broker_order_id","approval_text","approved_at","submitted_at","payload"]
        values = [order.get(c) for c in cols]
        if values[-1] is None:
            values[-1] = "{}"
        with self.connect() as con:
            con.execute(f"INSERT OR REPLACE INTO orders({','.join(cols)}) VALUES({','.join('?' for _ in cols)})", values)

    def pending_orders(self) -> pd.DataFrame:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM orders WHERE status='PENDING_APPROVAL' ORDER BY rowid DESC").fetchall()
        return pd.DataFrame([dict(r) for r in rows])

    def claim_order_for_submission(self, order_ref: str, approved_at: str) -> bool:
        """Atomically claim a pending approval so double-clicks cannot submit twice."""
        with self.connect() as con:
            cur = con.execute(
                "UPDATE orders SET status='SUBMITTING', approved_at=? "
                "WHERE order_ref=? AND status='PENDING_APPROVAL'",
                (approved_at, order_ref),
            )
            return cur.rowcount == 1

    def update_order(self, order_ref: str, **fields) -> None:
        if not fields:
            return
        allowed = {"status", "broker_order_id", "approval_text", "approved_at", "submitted_at", "payload"}
        if not set(fields).issubset(allowed):
            raise ValueError("Unsupported order field update")
        clause = ",".join(f"{k}=?" for k in fields)
        with self.connect() as con:
            con.execute(f"UPDATE orders SET {clause} WHERE order_ref=?", (*fields.values(), order_ref))

    def save_trade(self, trade: dict) -> bool:
        """Insert a trade once; never replace/reset an existing position on replay."""
        cols = ["trade_id","signal_id","mode","direction","instrument_key","expiry","entry_price","quantity","lot_size",
                "spot_entry","spot_stop","spot_target","premium_stop","status","opened_at","exit_price","exited_at",
                "exit_reason","gross_pnl","charges","net_pnl","broker_order_id","payload"]
        vals = [trade.get(c) for c in cols]
        if vals[-1] is None:
            vals[-1] = "{}"
        with self.connect() as con:
            cur = con.execute(f"INSERT OR IGNORE INTO trades({','.join(cols)}) VALUES({','.join('?' for _ in cols)})", vals)
            return cur.rowcount == 1

    def update_trade(self, trade_id: str, **fields) -> None:
        allowed = {"status", "quantity", "entry_price", "premium_stop", "exit_price", "exited_at",
                   "exit_reason", "gross_pnl", "charges", "net_pnl", "payload"}
        if not set(fields).issubset(allowed):
            raise ValueError("Unsupported trade field update")
        clause = ",".join(f"{k}=?" for k in fields)
        with self.connect() as con:
            con.execute(f"UPDATE trades SET {clause} WHERE trade_id=?", (*fields.values(), trade_id))

    def trades(self, status: str | None = None, limit: int = 200) -> pd.DataFrame:
        with self.connect() as con:
            if status:
                rows = con.execute("SELECT * FROM trades WHERE status=? ORDER BY opened_at DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = con.execute("SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (limit,)).fetchall()
        return pd.DataFrame([dict(r) for r in rows])

    def open_trades(self, mode: str | None = None) -> pd.DataFrame:
        with self.connect() as con:
            if mode:
                rows = con.execute("SELECT * FROM trades WHERE status='OPEN' AND mode=?", (mode,)).fetchall()
            else:
                rows = con.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()
        return pd.DataFrame([dict(r) for r in rows])

    def day_pnl(self, session_date: str, mode: str = "PAPER") -> float:
        with self.connect() as con:
            row = con.execute("SELECT COALESCE(SUM(net_pnl),0) pnl FROM trades WHERE mode=? AND substr(opened_at,1,10)=? AND status='CLOSED'",
                              (mode, session_date)).fetchone()
        return float(row["pnl"] or 0)

    def count_trades(self, session_date: str, mode: str = "PAPER") -> int:
        with self.connect() as con:
            row = con.execute("SELECT COUNT(*) n FROM trades WHERE mode=? AND substr(opened_at,1,10)=?", (mode, session_date)).fetchone()
        return int(row["n"])

    def get_state(self, key: str, default: str = "") -> str:
        with self.connect() as con:
            row = con.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_state(self, key: str, value: str) -> None:
        from datetime import datetime
        with self.connect() as con:
            con.execute("INSERT OR REPLACE INTO app_state(key,value,updated_at) VALUES(?,?,?)",
                        (key, str(value), datetime.now(IST).isoformat()))
