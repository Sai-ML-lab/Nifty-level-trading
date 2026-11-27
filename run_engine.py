from __future__ import annotations

import argparse
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from nifty_trader.config import settings
from nifty_trader.engine import TradingEngine
from nifty_trader.risk import market_is_open

IST = ZoneInfo("Asia/Kolkata")


def main():
    parser = argparse.ArgumentParser(description="NIFTY level-trading market data and paper engine")
    parser.add_argument("--once", action="store_true", help="Run one collection/decision cycle")
    parser.add_argument("--poll-seconds", type=int, default=settings.poll_seconds)
    parser.add_argument("--no-paper", action="store_true", help="Evaluate and log signals without paper entries")
    args = parser.parse_args()
    if args.poll_seconds < 15 and not args.once:
        parser.error("--poll-seconds must be >= 15 to avoid aggressive API polling")
    engine = TradingEngine(settings)
    print(f"NIFTY_LEVEL_TRADER_STARTED poll_seconds={args.poll_seconds} paper={not args.no_paper}", flush=True)
    while True:
        now = datetime.now(IST)
        if not market_is_open(now):
            try:
                events = engine._reconcile_live_orders()
                for event in events:
                    print(event, flush=True)
            except Exception as exc:
                engine.store.save_event({"type": "BROKER_RECONCILIATION_ERROR",
                                         "error": f"{type(exc).__name__}: {exc}"},
                                        event_timestamp=now.isoformat())
                print(f"{now.isoformat()} BROKER_RECONCILIATION_ERROR {type(exc).__name__}: {exc}", flush=True)
            if args.once:
                print(f"{now.isoformat()} NSE market is outside the configured trading window; no market-data cycle run", flush=True)
                break
            time.sleep(max(15, args.poll_seconds))
            continue
        try:
            result = engine.run_once(now=now, execute_paper=not args.no_paper)
            print(f"{result['timestamp']} spot={result['spot']:.2f} candles={len(result['candles'])} "
                  f"signals={len(result['signals'])} events={len(result['events'])} PCR={result['option_summary'].get('pcr')}", flush=True)
            for event in result["events"]:
                print(event, flush=True)
        except Exception as exc:
            engine.store.save_event({"type": "ENGINE_ERROR",
                                     "error": f"{type(exc).__name__}: {exc}"},
                                    event_timestamp=now.isoformat())
            print(f"{now.isoformat()} ENGINE_ERROR {type(exc).__name__}: {exc}", flush=True)
        if args.once:
            break
        time.sleep(max(15, args.poll_seconds))


if __name__ == "__main__":
    main()
