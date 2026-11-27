# Strategy and Operations Specification

## 1. Data source and time handling

The app uses Upstox REST APIs. NIFTY spot candles are requested from the V3 intraday historical-candle endpoint at 5-minute granularity; daily candles use V3 historical candles. Upstox option contracts supply expiry, instrument keys and lot sizes; the option-chain endpoint supplies call/put market data, including LTP, bid/ask, volume, OI, previous OI and Greeks. All timestamps are interpreted in `Asia/Kolkata`.

Only fully completed spot candles enter the strategy. Candle timestamps are treated as candle start times and a bar is complete only after its five-minute interval ends. Duplicate timestamps are removed and candles are sorted ascending.

## 2. Levels

- PDH, PDL and PDC come from the latest daily candle strictly before the active session date.
- ORH and ORL are exposed only when all three 09:15, 09:20 and 09:25 five-minute candles are complete.
- Swing pivots are confirmed only after the configured number of right-side candles have closed; no unconfirmed right-edge pivot is used.
- Manually entered levels are saved to SQLite for the session date, with an active/inactive flag. They are never silently carried into another session.
- The target is the nearest known level strictly beyond the signal close in the trade direction. If no target exists or the reward/risk is below the configured minimum, no signal is emitted.

## 3. Signal state

A bullish signal requires a completed candle to cross from at/below a resistance level to above it by the configured buffer. A bearish signal is the mirror image at support. Immediate and retest signals have separate switches. A retest must follow a qualifying break within the configured lookback, avoid an intervening invalidating close, trade back to the level tolerance band, and close back on the break side.

SMA(5)/SMA(10) confirmation means the LONG candle close must be above both averages, or the SHORT candle close below both; no additional SMA5/SMA10 stacking requirement is imposed. This gate and the fresh directional FVG gate are enabled by default. The FVG must form across three contiguous same-session 5-minute bars and is no longer fresh after a later candle trades into its zone. RSI(14) divergence uses confirmed pivots and is computed/displayed; it can be configured as a hard gate but is not mandatory by default. These switches should not be changed mid-sample.

## 4. Option-chain statistics and contract choice

For each expiry queried:

- `day_change_oi = current OI - exchange previous OI`, when previous OI is provided.
- `intraday_change_oi = current OI - most recent previously stored OI` for the same option instrument in the same IST session. The first snapshot is unavailable (N/A), not zero. Later values are changes since the previous poll, not cumulative since market open.
- `session_change_oi = current OI - first locally captured OI` for the same option instrument. This is explicitly measured from the application's first capture, not the exchange open if the process starts later.
- All snapshots are persisted by timestamp and instrument key; duplicate polls at the same timestamp upsert instead of creating ambiguous prior values.
- `PCR = aggregate PE OI / aggregate CE OI` within the expiry snapshot.
- The dashboard displays a CE/PE ATM ±10 listed-strike ladder with LTP, bid, ask, spread percentage, volume, OI, recent poll ΔOI and liquidity eligibility. Detailed bid/ask quantities, exchange day ΔOI and session ΔOI are available in the full-chain view.
- OI, ΔOI and PCR are context/display data only in this version; they do not gate entries because no numerical thresholds or directional interpretation were frozen.
- Max call/put OI strikes are reported by strike aggregation.

Bullish signals select calls; bearish signals select puts. Candidates are filtered by premium range, positive bid, ask >= bid, maximum bid/ask spread, minimum volume/OI and near-ATM distance. Eligible contracts across the nearest available expiries are scored by liquidity, spread, ATM distance and days to expiry. This score is a ranking heuristic, not a predictive model.

## 5. Risk and execution

- Only long options are traded; option quantity is derived from the latest Upstox contract lot size.
- Sizing is bounded by maximum lots, maximum position value and configured premium-stop risk budget. A trade is rejected if one lot breaches the limits.
- The engine allows one open position per mode and limits the number of trades and realized daily loss. New entries are blocked after the configured last-entry time (default 15:00 IST); the intraday exit window begins at 15:20 IST.
- NSE weekday/session hours and market-data staleness are checked before entries. A persistent kill switch blocks new entries.
- Paper entry uses ask + slippage; paper exit uses bid - slippage. Estimated charges are configuration values only.
- Live mode needs two independent enable flags plus a current access token. Each LIMIT entry/exit order has a short-lived exact approval phrase. Immediately before sending, the app re-fetches spot and option-chain data, validates the breakout/breakdown, spread and price drift, and rejects stale approvals. Submission is guarded against duplicate clicks and an uncertain broker response blocks further entry until reconciliation. Broker order history is reconciled on later engine polls.
- A partially filled entry is not considered a managed position while the unfilled remainder remains working. Request cancellation and reconcile the terminal broker state; any cumulative filled quantity is then recorded as the live position. Partially filled exits update remaining quantity and cumulative realized P&L only after the order reaches a terminal state.
- Engine errors, option-chain errors, broker reconciliation failures, exit-blocked events and execution events are persisted to the `engine_events` table and included in the one-week review export.
- The kill switch is an entry gate only, not a broker cancel/flatten command.

## 6. Backtest methodology and limits

The spot backtester recomputes levels/signals from the historical prefix available at each candle. Signals enter on the next candle open. Stop takes precedence when both stop and target trade within a bar. The output is index points. It does not synthesize options returns, options IV, theta, fills, taxes or brokerage.

## 7. Operational checklist

Before each session:

1. Confirm the Upstox token is valid and account/market permissions are correct.
2. Confirm the current NIFTY instrument, options lot size and contract master are being returned.
3. Check system clock/timezone, database path, disk space and dashboard/engine logs.
4. Verify manual levels and the day's opening range only after the opening bars complete.
5. Keep live flags disabled during paper observation; review quotes, fills, P&L and audit events.
6. Before live use, understand broker-side order behavior, partial fills, cancellation, rejection, order status reconciliation and live exit approval requirements.

This specification is an engineering description, not an assurance of strategy quality or suitability.

## 8. Upstox developer references

- [V3 intraday candle data](https://upstox.com/developer/api-documentation/v3/get-intra-day-candle-data/)
- [V3 historical candle data](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/)
- [Option contracts](https://upstox.com/developer/api-documentation/get-option-contracts/)
- [Put/call option chain](https://upstox.com/developer/api-documentation/get-pc-option-chain/)
- [V3 order placement](https://upstox.com/developer/api-documentation/v3/place-order/) — response `data.order_ids` is an array.
- [V3 order cancellation](https://upstox.com/developer/api-documentation/v3/cancel-order/)
- [Order history](https://upstox.com/developer/api-documentation/get-order-history/)

## 9. One-week paper-trading run

Run the engine continuously at a 60-second poll interval during market hours and run Streamlit separately as a read-only viewer. While the daemon is active, use **Load saved snapshot** rather than **Run one market cycle** in the dashboard. Market files are atomically replaced; option-chain snapshots, signals, trades and per-level diagnostics are retained in SQLite. Keep the Mac awake and verify that the process continues running.

Use `python scripts/export_paper_review.py --output results/paper_week1` after the sample. Review operational uptime, stale/missing data, rejected/filtered signal reasons, option quote quality, exit-blocked events and duplicate prevention separately from net paper P&L. Paper results use a simplified bid/ask plus slippage model and estimated charges. A small one-week trade sample cannot establish an enduring statistical edge.
