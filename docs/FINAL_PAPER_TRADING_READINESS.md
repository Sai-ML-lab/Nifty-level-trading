# Final Strategy Audit & One-Week Paper-Trading Readiness

## Status

- Automated regression suite: **59 tests passing** in the build environment.
- Python compilation: passed for the application, engine and export script.
- End-to-end synthetic engine scenarios: exercised with mocked Upstox market data and fills.
- Streamlit layout/feature contract: checked in tests. A real Streamlit browser smoke test was not performed in the build environment because the Streamlit package was not installed there; the user's earlier screenshot demonstrates that the earlier dashboard build launched successfully.
- Real Upstox authentication, exchange order acceptance and real fills: **not tested**. Live mode must remain disabled.

## Frozen trading logic

### Market data and levels

1. Underlying: NIFTY 50 spot, Upstox instrument key `NSE_INDEX|Nifty 50`.
2. Signal timeframe: completed 5-minute candles only. The app treats timestamps as candle-start timestamps and drops incomplete bars.
3. Session inputs: previous-session high/low/close (PDH/PDL/PDC), first-15-minute opening range (ORH/ORL), confirmed swing highs/lows and session-scoped manual levels.
4. ORH/ORL is not created until all three exact opening bars (09:15, 09:20 and 09:25 IST) exist and are complete.
5. Swing levels require right-side confirmation bars; pivot windows crossing missing 5-minute bars are rejected.
6. Manual levels are persisted by session date and must be re-entered or reactivated for another session.

### Signal and entry rules

1. A LONG breakout requires the prior completed close to be at/below a resistance/reference level plus the configured buffer, and the latest completed close to cross above that threshold. SHORT is the mirror image at support/reference levels.
2. IMMEDIATE and RETEST entries are independently enabled. A retest must follow a prior break, occur within the configured bar lookback, avoid an intervening invalidating close, return to the tolerance band and close back on the correct side.
3. SMA confirmation (enabled by default): LONG close above both SMA(5) and SMA(10); SHORT close below both.
4. FVG confirmation (enabled by default): a directionally aligned 3-candle 5-minute gap, formed from contiguous same-session bars and not subsequently mitigated.
5. RSI(14) regular divergence is calculated and displayed. It is **not a hard entry gate by default**. The Streamlit toggle persists the setting in SQLite so both the dashboard and separate daemon use the same value. Keep it unchanged for the sample week.
6. The diagnostics panel records each valid level/direction and distinguishes trigger, entry mode, SMA, FVG, RSI, target and reward/risk PASS/FAIL/DISABLED/NOT_REQUIRED states.
7. The next eligible level in the trade direction is the target. A structural/ATR-based spot stop is used; no trade is emitted without a valid next level and minimum configured reward/risk.

### Options and OI context

1. Bullish spot signals select long CE; bearish signals select long PE.
2. Contracts are selected across the nearest available expiries based on current premium, valid positive bid/ask, bid/ask spread, volume, OI, ATM distance and a liquidity-ranking heuristic.
3. The dashboard displays PCR and aggregate CE/PE OI, exchange day ΔOI, change vs the previous locally persisted snapshot, session change vs the first locally captured snapshot, coverage, and an ATM ±10 listed-strike CE/PE ladder.
4. The first local ΔOI is N/A, not zero. `vs prior poll` is a one-poll delta; `since first app snapshot` is measured from the first snapshot the process captured that session, which may be later than the exchange open.
5. OI, ΔOI and PCR are context-only; they do not gate a trade because no numeric thresholds or unambiguous directional rules were supplied and frozen. Do not read high call OI / put OI in isolation as guaranteed resistance/support.

### Risk and paper execution

1. Position size is bounded by lot size, maximum lots, maximum position value and configured risk budget using the configured premium-stop percentage as a sizing estimate.
2. The configured 35% option-premium stop is a simulated trigger, **not a guaranteed maximum loss**. A long-option buyer can lose the full premium paid if the stop cannot be executed or the quote disappears.
3. Default risk controls include a one-lot maximum, two trades per day, ₹2,000 configured risk per trade, ₹2,000 daily realized-loss threshold, stale-data limits and one open position per mode. Confirm the values in the local `.env` before starting; local overrides take precedence over defaults.
4. Paper entry uses ask + configured slippage. Paper exit uses bid − configured slippage. Charges are configurable estimates, not exact broker contract-note charges.
5. The position manager scans all post-entry completed candles since the last poll, not only the latest candle. If stop and target are both touched in the same candle, the stop wins conservatively.
6. Premium stop, 15:20 IST time exit and next-session safety exit are applied. If no positive executable bid is available, the app does not fabricate a fill: it records an exit-blocked event and leaves the position open for attention.
7. Signal-to-trade insertion is idempotent. Repeated polls or a restart cannot silently reopen/reset an already-recorded trade for the same signal and mode.

## Edge cases covered by automated tests

- Incomplete, duplicated, malformed, out-of-order and timezone-aware candles.
- Missing first-15-minute opening-range bars and missing 5-minute bars inside swing-pivot windows.
- Incomplete indicator warm-up, no breakout, wrong-side level, missing target and inadequate reward/risk.
- Immediate/retest modes and configurable RSI divergence gate.
- Fresh FVG becoming mitigated; gaps across sessions or missing bars.
- First option-chain snapshot with no prior ΔOI, missing OI and JSON NaN normalization.
- Repeated snapshot timestamp/instrument upsert and same-session delta calculation.
- Missing or invalid bid/ask, excessive spread, low volume/OI, bad moneyness and multiple expiries with one expiry failing.
- Duplicate signal polls, duplicate trade insertion, restart/recovery and daily risk limits.
- Stop and target within the same candle (stop wins), a stop touched on an earlier missed poll, 15:20 exit, next-session safety exit and missing bid.
- Live mode disabled by default, stale approval, exact approval phrase, duplicate-click protection, unknown network submission and partial-fill reconciliation logic.
- Current Upstox V3 `data.order_ids` response shape; a single ID is stored for reconciliation. Multiple returned child IDs are retained and locked as `SUBMISSION_UNKNOWN` because this application does not automatically aggregate multi-child fills. V3 cancel endpoint use is unit-tested.

## Known limits — do not over-interpret the sample

- The backtester is a causal NIFTY spot-level test. It is **not** a historical option-premium P&L backtest and does not model historical option bid/ask, IV, theta, contract-level gaps, slippage, taxes and exact brokerage.
- Paper order fills use a simplified quote-and-slippage model; the model does not guarantee that a real order would have filled at that price or quantity.
- One week is mainly a test of data reliability, rule fidelity, signal quality and operational stability. A handful of trades cannot establish a statistical edge or justify live capital.
- RSI divergence, FVG, OI and PCR are technical/context features, not standalone predictive guarantees.
- When the machine or engine is stopped, the app cannot monitor or exit an open paper position. The Mac must remain awake and connected during the sample.
- Live API execution remains unverified against the user's account. Do not arm live orders during this paper trial. A manual per-order approval is not a substitute for broker/exchange validation.

## One-week deployment protocol

1. Preserve your existing local `.env`; do not replace it with `.env.example`. Verify that `UPSTOX_ANALYTICS_TOKEN` is present. The daily `UPSTOX_ACCESS_TOKEN` is only needed for live API order operations; paper mode uses the market-data credential.
2. Ensure these safety settings are present:

   ```dotenv
   LIVE_TRADING_ENABLED=0
   ALLOW_LIVE_ORDERS=0
   MAX_LOTS=1
   MAX_TRADES_PER_DAY=2
   MAX_DAILY_LOSS_INR=2000
   RISK_PER_TRADE_INR=2000
   STALE_DATA_SECONDS=180
   MAX_SIGNAL_AGE_SECONDS=180
   ```

3. In the project virtual environment, install dependencies and run checks:

   ```bash
   python -m pip install -r requirements.txt
   python -m pytest -q
   python -m compileall -q nifty_trader streamlit_app.py run_engine.py scripts/export_paper_review.py
   ```

4. Start exactly one continuous polling process in Terminal 1:

   ```bash
   python run_engine.py --poll-seconds 60
   ```

5. Start the dashboard in Terminal 2:

   ```bash
   streamlit run streamlit_app.py
   ```

6. While the daemon is running, use **Load saved snapshot** in the UI. Do not use **Run one market cycle** at the same time; that creates a second poller and unnecessary API load.
7. Do not change thresholds or the RSI divergence toggle mid-week. If a setting changes, record the timestamp and treat subsequent results as a separate experiment.
8. Review `ENGINE_ERROR`, `OPTION_CHAIN_FETCH_ERROR`, stale-data warnings, `PAPER_EXIT_BLOCKED_NO_BID`, missing snapshots and duplicate-prevention events each session.
9. At the end of the week, export:

   ```bash
   python scripts/export_paper_review.py --output results/paper_week1
   ```

10. Assess operational uptime, missing candles, data-age distribution, option quote quality, every READY/FILTERED rule diagnostic, fills vs visible bid/ask, exit reasons, realized P&L, drawdown and number of trades. Do not optimize thresholds on this same one-week sample and then report its results as out-of-sample performance.

## Release validation record

Validation performed in the build environment: **59 automated tests passed**; `compileall` passed. The tests use synthetic local data and fake broker responses. There was no real Upstox API login/order request, no market-hours broker fill test and no live Streamlit server/browser run during this final build. Run the local checks above before beginning the paper sample.
