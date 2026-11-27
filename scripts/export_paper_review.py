#!/usr/bin/env python3
"""Export the one-week paper-trading audit dataset from the local SQLite ledger."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from nifty_trader.config import settings

IST = ZoneInfo("Asia/Kolkata")


def read_table(connection: sqlite3.Connection, table: str) -> pd.DataFrame:
    # Allow exports from a database created by an earlier build before a newly
    # introduced audit table existed. Opening the updated app runs the schema migration.
    try:
        return pd.read_sql_query(f"SELECT * FROM {table}", connection)
    except pd.errors.DatabaseError as exc:
        if "no such table" in str(exc).lower():
            return pd.DataFrame()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Export NIFTY paper-trading records for review")
    parser.add_argument("--db", default=settings.db_path, help="SQLite database path")
    parser.add_argument("--output", default=None, help="Output directory (default: results/paper_review_<date>)")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")
    output = Path(args.output) if args.output else Path("results") / f"paper_review_{datetime.now(IST):%Y%m%d_%H%M%S}"
    output.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as con:
        signals = read_table(con, "signals")
        trades = read_table(con, "trades")
        orders = read_table(con, "orders")
        diagnostics = read_table(con, "signal_diagnostics")
        option_rows = read_table(con, "option_snapshots")
        engine_events = read_table(con, "engine_events")

    paper_trades = trades[trades.mode == "PAPER"].copy() if not trades.empty else trades.copy()
    closed = paper_trades[paper_trades.status == "CLOSED"].copy() if not paper_trades.empty else paper_trades.copy()
    open_trades = paper_trades[paper_trades.status == "OPEN"].copy() if not paper_trades.empty else paper_trades.copy()
    if not paper_trades.empty and "opened_at" in paper_trades:
        paper_trades = paper_trades.sort_values("opened_at")
    if not closed.empty:
        closed = closed.sort_values("exited_at")
        pnl = pd.to_numeric(closed.net_pnl, errors="coerce").fillna(0.0)
        wins, losses = pnl[pnl > 0], pnl[pnl < 0]
        gross_profit, gross_loss = float(wins.sum()), float(abs(losses.sum()))
        equity = pnl.cumsum()
        drawdown = equity - equity.cummax().clip(lower=0)
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (None if gross_profit == 0 else "infinite")
        stats = {
            "closed_paper_trades": int(len(closed)),
            "open_paper_trades_at_export": int(len(open_trades)),
            "win_rate_pct": round(float((pnl > 0).mean() * 100), 2),
            "net_pnl_inr": round(float(pnl.sum()), 2),
            "gross_profit_inr": round(gross_profit, 2),
            "gross_loss_inr": round(gross_loss, 2),
            "profit_factor": profit_factor,
            "average_win_inr": round(float(wins.mean()), 2) if not wins.empty else None,
            "average_loss_inr": round(float(losses.mean()), 2) if not losses.empty else None,
            "max_drawdown_inr_from_closed_trade_sequence": round(abs(float(drawdown.min())), 2),
            "exit_reason_counts": closed.exit_reason.value_counts(dropna=False).to_dict(),
        }
    else:
        stats = {
            "closed_paper_trades": 0,
            "open_paper_trades_at_export": int(len(open_trades)),
            "win_rate_pct": None,
            "net_pnl_inr": 0.0,
            "gross_profit_inr": 0.0,
            "gross_loss_inr": 0.0,
            "profit_factor": None,
            "note": "No closed paper trades; do not infer strategy performance from signals alone.",
        }

    stats["signal_status_counts"] = signals.status.value_counts(dropna=False).to_dict() if not signals.empty else {}
    stats["diagnostic_status_counts"] = diagnostics.status.value_counts(dropna=False).to_dict() if not diagnostics.empty else {}
    stats["option_chain_snapshots"] = int(option_rows.timestamp.nunique()) if not option_rows.empty else 0
    stats["orders_recorded"] = int(len(orders))
    stats["engine_event_counts"] = engine_events.event_type.value_counts(dropna=False).to_dict() if not engine_events.empty else {}
    stats["exported_at_ist"] = datetime.now(IST).isoformat()
    stats["database"] = str(db_path.resolve())
    stats["limitations"] = [
        "Paper fills use the configured ask/bid plus slippage approximation; they are not exchange fills.",
        "Charges are configured estimates, not actual contract-note charges.",
        "The spot backtest is not a historical options-premium backtest.",
        "One week of paper results is a process/implementation sample, not statistical proof of an edge.",
    ]

    for name, frame in (("signals", signals), ("paper_trades", paper_trades),
                        ("orders", orders), ("signal_diagnostics", diagnostics),
                        ("option_chain_snapshots", option_rows), ("engine_events", engine_events)):
        frame.to_csv(output / f"{name}.csv", index=False)
    (output / "summary.json").write_text(json.dumps(stats, indent=2, default=str), encoding="utf-8")
    print(f"Paper-trading review exported to: {output.resolve()}")
    print(json.dumps(stats, indent=2, default=str))


if __name__ == "__main__":
    main()
