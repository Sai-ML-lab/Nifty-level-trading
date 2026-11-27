# NIFTY Level Trader

A standalone, Upstox-connected NIFTY intraday trading application built around a **5-minute spot breakout / breakdown strategy**. It includes market data ingestion, technical signals, option-chain analysis, option contract selection, paper execution, risk controls, a Streamlit dashboard, backtesting on spot candles, persistent audit logs, and a separately armed live-order workflow.

> **Safety defaults:** paper trading is the default. Live trading is disabled unless all live flags and a current Upstox trading token are configured. Every live entry and every live exit requires a fresh, exact, per-order approval phrase. This is execution software, not a profit guarantee or investment recommendation. Do not enable live orders until the paper workflow has been validated during live market sessions.

## Strategy implemented

- Underlying: NIFTY 50 spot (`NSE_INDEX|Nifty 50`); signal timeframe: completed 5-minute candles.
- Levels: previous trading session high/low/close, first-15-minute opening range, confirmed swing highs/lows, and manually entered support/resistance/reference levels.
- Breakout / breakdown: a completed 5-minute candle must close beyond a level by the configured buffer.
- Entry styles: **IMMEDIATE** and **RETEST** are independently configurable. A retest requires a prior breakout, no invalidating close in the interim, a touch near the broken level, and a close back on the breakout side.
- Confluence: SMA(5)/SMA(10), RSI(14) divergence, and a fresh, unmitigated 5-minute FVG. SMA and FVG are configurable hard gates; the provided defaults enable both as confluence gates. RSI divergence is displayed and can optionally be made a hard gate.
- Target: next available level in the direction of the trade. Stop: structure/ATR-based spot stop, plus a configurable long-option premium stop.
- Option context: option-chain OI, exchange day-change in OI, change since the previous locally stored snapshot, session change since the first app capture, per-expiry PCR, and largest OI strikes. Timestamped snapshots are persisted in SQLite with an upsert key to prevent duplicate polls. The first local delta is N/A, never a fabricated zero.
- Option quote quality: bid, ask, absolute and percentage spread, bid/ask quantities, volume, OI, configured liquidity filters and a non-predictive liquidity ranking score. The dashboard provides a CE/PE ATM ±10 listed-strike ladder.
- Explainability: a condition-by-condition diagnostic for each level/direction, including breakout/retest, SMA, FVG, optional RSI divergence, next-level target and reward/risk decisions. The RSI divergence hard gate can be changed from the dashboard and is persisted for the background engine.
- Operations audit: option-chain errors, engine errors, reconciliation events and blocked exits are persisted in SQLite and included in the paper-review export.
- Contract selection: CE for bullish spot signals and PE for bearish spot signals. Default OPTION_SELECTION_MODE=OTM_BAND applies a hard directional band: CE strikes must be 150-200 NIFTY points above the signal spot; PE strikes must be 150-200 points below. There is no silent ATM fallback. Candidates must also pass premium, bid/ask, spread, volume and OI filters, then are ranked by liquidity and spread quality across eligible expiries. The previous near-ATM approach is available only when OPTION_SELECTION_MODE=NEAR_ATM is explicitly configured.
- Execution: long options only (buy CE / buy PE). The application does not write options or open short-option positions.

The defaults are intentionally conservative and configurable. They are not tuned or claimed to be profitable. Check the current NIFTY lot size returned by the Upstox contract master; it is not hardcoded.

## Project layout

```text
nifty-level-trader/
├── nifty_trader/
│   ├── config.py          # environment-backed configuration
│   ├── upstox.py          # Upstox REST market-data and order client
│   ├── levels.py          # PDH/PDL/PDC, ORH/ORL, confirmed pivots
│   ├── indicators.py      # SMA, RSI, ATR, divergence, 5m FVG
│   ├── option_chain.py    # OI, ΔOI, PCR and directional OTM contract selection
│   ├── market_clock.py    # IST candle windows and next-close timing
│   ├── strategy.py        # breakout/breakdown + immediate/retest signals
│   ├── risk.py            # NSE-hours, daily loss, sizing and exposure gates
│   ├── execution.py       # paper fills and approval-gated live orders
│   ├── db.py              # SQLite audit trail and persistent state
│   ├── engine.py          # orchestration and position monitoring
│   └── backtest.py        # causal spot-level backtest
├── streamlit_app.py       # dashboard and order approval UI
├── run_engine.py          # market polling / paper daemon
├── tests/test_core.py
├── docs/STRATEGY_AND_OPERATIONS.md
├── .env.example
└── scripts/publish_private_github.sh
```

## Quick start (macOS)

```bash
cd nifty-level-trader
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and add your **current Upstox access/analytics token locally**. Never paste credentials into GitHub, commit them, or share them in chat. `UPSTOX_ANALYTICS_TOKEN` is used for read-only market data. A current `UPSTOX_ACCESS_TOKEN` is required for live order placement and also works for market-data calls.

### Start the dashboard

```bash
streamlit run streamlit_app.py
```

Click **Run one market cycle** during the NSE session. This fetches completed 5-minute NIFTY candles, option contracts and chains; computes levels and signals; stores option snapshots; and evaluates paper entries/exits. The dashboard separately displays the completed candle's start/end, successful refresh timestamp and next expected 5-minute candle close. The countdown is calculated at page render time.

### Run the continuous engine instead

```bash
python run_engine.py --poll-seconds 60
```

Run either the dashboard refresh or the background engine as the primary polling process, not both continuously at once. They share the same SQLite database and Upstox rate limits. For a one-shot data/strategy check:

```bash
python run_engine.py --once --no-paper
```

`--no-paper` evaluates and logs signals without opening paper positions. The default daemon runs paper execution automatically.

## Configuration

All settings are in `.env.example`, including independent immediate/retest switches, SMA/FVG/RSI requirements, breakout/retest buffers, OPTION_SELECTION_MODE=OTM_BAND, the 150-200 point CE/PE OTM band, expiry and liquidity thresholds, lot/risk limits, slippage, data staleness, daily limits, and live arming flags. To intentionally use the near-ATM approach instead, select OPTION_SELECTION_MODE=NEAR_ATM and configure OPTION_MONEYNESS_PCT.

The default live flags are both off:

```dotenv
LIVE_TRADING_ENABLED=0
ALLOW_LIVE_ORDERS=0
```

The app will refuse live staging/submission unless both are explicitly enabled and `UPSTOX_ACCESS_TOKEN` is present. This is deliberately a second switch beyond having a trading token in the environment.

### Live order lifecycle

1. A fresh signal is computed from a completed spot candle.
2. The app rechecks signal age, the latest spot close, the exact option contract, quote spread, premium, and exposure limits.
3. It stages a **LIMIT** order and shows an order-specific approval phrase that expires after 90 seconds.
4. You must type the exact phrase and click approval for that single order.
5. The app submits once, parses the Upstox V3 `data.order_ids` response (also tolerating legacy singular aliases), stores the broker order ID, and offers order-history reconciliation.
6. A live exit is staged for a fresh sell quote and also requires a separate approval phrase. Broker cancellation uses the Upstox V3 cancel endpoint.

Do not retry an order when the network result is ambiguous. Check the Upstox order book and reconcile first. If V3 returns more than one child order ID for one approval, the record is deliberately locked as `SUBMISSION_UNKNOWN`; inspect every child ID and reconcile aggregate fills manually. Multi-child aggregation is not automated. An unresolved submission deliberately blocks additional entries. The dashboard kill switch blocks new entries; it does **not** cancel broker orders or automatically liquidate open positions. You must monitor and manage open positions, especially in live mode. Approval-gated exits require a person to be available to approve them.

## Tests

```bash
PYTHONPATH=. pytest -q
python -m compileall -q nifty_trader streamlit_app.py run_engine.py
```

The automated tests use synthetic local fixtures and fake broker responses. They do not authenticate to Upstox, test live exchange fills, or verify your broker permissions. The Streamlit UI requires the dependencies in `requirements.txt`; run it in the same virtual environment used for the engine.

## Backtesting caveat

The included backtester is a causal **NIFTY spot-level** backtest: signals use only bars available at that point, entry is modeled at the following candle open, and a same-candle stop/target ambiguity is resolved conservatively in favor of the stop. Results are in NIFTY index points. It is **not** an options-premium P&L backtest. Realistic options backtesting needs historical option-chain/contract candles and must model bid/ask, slippage, brokerage, taxes, IV and theta. Do not extrapolate spot points into rupee P&L.

## Create the private GitHub repository

The project includes a helper to publish this folder as a new **private** repository named `Sai-ML-lab/nifty-level-trader`. Install and authenticate GitHub CLI first (`gh auth login`), then run:

```bash
bash scripts/publish_private_github.sh
```

If this directory has not yet been initialized as a Git repository, the script initializes it, uses your authenticated GitHub identity for the initial commit, creates the private GitHub repository, adds `origin`, and pushes `main`. It does not access or modify `nifty_strangle_research`. Override `GITHUB_OWNER` or `GITHUB_REPO_NAME` when running the script to use a different owner/name; change `--private` in the script if you explicitly want a public repository.

## Important limitations before live use

- OAuth token refresh is not automated; supply a currently valid access token.
- Live approvals are rejected if the fresh spot setup is invalid or the selected option quote has moved beyond the configured `MAX_APPROVAL_PRICE_DRIFT_PCT` (default 2%) since staging.
- Corporate-action, instrument-master or broker API changes may require adapter updates.
- Exchange order acceptance, partial fills, rejections and broker connectivity must be verified with paper/sandbox or a controlled, broker-approved process before live use.
- Paper fills use a simplified ask/bid plus configurable slippage model; charges are estimates, not contract-note values.
- The live path uses limit orders. An unfilled order may remain pending at the broker and needs manual monitoring/cancellation.
- No strategy can guarantee returns. Backtest/paper results do not ensure future results.

## One-week paper-trading protocol (recommended before any live use)

### Exact default decision rules

The current defaults are deliberately explicit so the one-week sample can be interpreted:

1. **Signal source:** completed, contiguous 5-minute NIFTY spot candles only. Gapped/invalid candles do not create signals.
2. **Level trigger:** a completed candle closes beyond a resistance/reference level for LONG, or below a support/reference level for SHORT, by `BREAKOUT_BUFFER_POINTS` (default 2 points). A retest entry requires a prior confirmed break, no invalidating interim close, a touch within `RETEST_TOLERANCE_POINTS`, and a close back on the correct side.
3. **SMA gate:** enabled by default. A LONG close must be above both SMA(5) and SMA(10); a SHORT close must be below both. No extra SMA5/SMA10 stacking rule is imposed.
4. **FVG gate:** enabled by default. The engine requires a fresh, contiguous, unmitigated, directionally aligned 5-minute three-candle FVG. A later candle trading into the gap invalidates its freshness.
5. **RSI divergence:** RSI(14) divergence is calculated and logged, but is not a hard gate by default (`RSI_DIVERGENCE_REQUIRED=0`). It must not be assumed to confirm every trade.
6. **Option-chain context:** OI, exchange day-change in OI, PCR, max-OI strikes and change since the previous local poll are recorded/displayed. **They do not currently block or approve a signal**, because no numeric threshold or unambiguous directional interpretation was specified. ΔOI vs previous poll is a one-poll difference, not a cumulative from-open figure.
7. **Contract selection:** long CE for LONG spot signals and long PE for SHORT spot signals. The selector filters by current contract-master lot size, premium, bid/ask spread, volume, OI and ATM distance, then scores eligible expiries/contracts for liquidity and spread. It is a deterministic selection heuristic, not a forecast of option returns.
8. **Risk / execution:** paper entry at ask plus slippage; exit at bid minus slippage. Defaults cap at one open position, two entries per day, ₹2,000 configured stop-risk per trade, ₹2,000 daily realized-loss limit and one lot maximum. Full premium paid remains the maximum loss if a stop cannot execute; the 35% premium stop is only a simulated trigger, not a guarantee.
9. **Exit rules:** spot structural stop/target, premium stop, 15:20 IST time exit and a next-session safety exit. If there is no valid positive bid, the engine does not fabricate a fill; it leaves the position open and logs an exit-blocked event.

Do not change thresholds during the one-week sample. A threshold change starts a new experiment and should be recorded separately.

### Deployment for one week

1. Keep these flags set in your local `.env`:

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

2. Start the paper engine in Terminal 1 and leave it running during market hours:

   ```bash
   source .venv/bin/activate
   python run_engine.py --poll-seconds 60
   ```

3. Start the dashboard in Terminal 2:

   ```bash
   source .venv/bin/activate
   streamlit run streamlit_app.py
   ```

4. The dashboard loads the latest persisted snapshot on startup. While the background engine is running, use **Load saved snapshot** to refresh the display. Do **not** click **Run one market cycle** at the same time as the background engine; that would create a second poller and duplicate API load.

5. Keep the Mac awake and connected to the internet. Check the engine terminal regularly. Investigate any `ENGINE_ERROR`, `PAPER_EXIT_BLOCKED_NO_BID`, stale-data warning, or missing option-chain snapshot. The application cannot manage an open paper position while the machine/process is stopped.

6. At the end of the week, export the evidence:

   ```bash
   python scripts/export_paper_review.py --output results/paper_week1
   ```

   The export includes signals, per-level rule diagnostics, paper trades, orders and option-chain snapshots, plus summary metrics. Share the generated CSVs/JSON (never `.env` or credentials) for a detailed review.

### What to judge after one week

Evaluate operational quality separately from trading performance:

- **Operational:** poller uptime, data freshness, missing 5-minute bars, option quote availability, exit-blocked events, duplicate signals/trades, and state persistence.
- **Rule fidelity:** manually compare every `READY` or `FILTERED` diagnostic to the chart and the level your brother would have used. Record any mismatch rather than changing the rule mid-week.
- **Execution realism:** compare simulated entry ask/exit bid, spread, configured slippage and estimated charges against what was actually visible in the Upstox chain at the time.
- **Trading sample:** number of eligible signals and closed trades, wins/losses, net paper P&L, profit factor, average win/loss, exit reasons and drawdown. A week with only a few trades is too small to establish a reliable win rate.

The app intentionally does not claim a historical options-premium edge. Its backtest is a causal spot-level diagnostic only. Treat this first week as a systems/rule-fidelity trial, not evidence that live capital should be deployed.
