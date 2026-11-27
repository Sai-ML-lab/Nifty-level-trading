from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from nifty_trader.market_clock import candle_countdown, candle_window

from nifty_trader.backtest import backtest_spot
from nifty_trader.option_chain import add_option_quality_metrics, atm_option_chain_window
from nifty_trader.config import settings
from nifty_trader.engine import TradingEngine
from nifty_trader.execution import approve_and_submit, sync_live_order
from nifty_trader.db import Store

IST = ZoneInfo("Asia/Kolkata")
st.set_page_config(page_title="NIFTY Level Trader", page_icon="📈", layout="wide")


@st.cache_resource
def get_engine():
    return TradingEngine(settings)


def _expand_contract_details(frame: pd.DataFrame, *, source: str) -> pd.DataFrame:
    """Expose option contract details stored in signal/trade audit payloads."""
    if frame.empty:
        return frame.copy()
    rows = []
    for record in frame.to_dict("records"):
        payload = record.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                payload = {}
        elif not isinstance(payload, dict):
            payload = {}
        quote = payload.get("entry_quote", {}) if source == "trade" else payload
        quote = quote if isinstance(quote, dict) else {}
        merged = dict(record)
        for key in ("expiry", "strike", "option_type", "otm_distance_points", "delta", "iv",
                    "theta", "gamma", "vega", "ltp", "bid", "ask", "spread_pct",
                    "volume", "oi", "liquidity_status", "selection_mode"):
            value = quote.get(key)
            if value is None:
                value = payload.get(key)
            if value is None:
                aliases = {"delta": "option_delta", "iv": "option_iv", "theta": "option_theta",
                           "gamma": "option_gamma", "vega": "option_vega", "ltp": "option_ltp",
                           "bid": "option_bid", "ask": "option_ask", "spread_pct": "option_spread_pct",
                           "volume": "option_volume", "oi": "option_oi"}
                value = payload.get(aliases.get(key, key))
            if value is None:
                value = record.get(key, record.get(aliases.get(key, key) if source == "signal" else key))
            merged[f"contract_{key}"] = value
        rows.append(merged)
    return pd.DataFrame(rows)


# Persist this rule switch in SQLite so it is honoured by both the Streamlit
# viewer and a separately running background paper engine.
settings_store = Store(settings.db_path)
default_rsi_required = bool(settings.rsi_divergence_required)
stored_rsi = settings_store.get_state("rsi_divergence_required", "1" if default_rsi_required else "0")
initial_rsi_required = str(stored_rsi).strip().lower() in {"1", "true", "yes", "on"}
with st.sidebar:
    st.header("Strategy controls")
    rsi_required = st.toggle(
        "Require RSI divergence for entries", value=initial_rsi_required,
        help="When enabled, LONG requires confirmed bullish RSI divergence and SHORT requires confirmed bearish RSI divergence. This setting is stored locally and used by the background paper engine too.",
        key="rsi_divergence_toggle",
    )
    if rsi_required != initial_rsi_required:
        settings_store.set_state("rsi_divergence_required", "1" if rsi_required else "0")
        st.success("RSI rule saved. The next engine cycle will use this setting.")
    if settings.option_selection_mode == "OTM_BAND":
        st.caption(f"Option selection: LONG buys CE {settings.option_otm_min_points:.0f}-{settings.option_otm_max_points:.0f} points above spot; SHORT buys PE the same distance below spot. No ATM fallback.")
    else:
        st.caption(f"Option selection: NEAR_ATM within {settings.option_moneyness_pct:.2%} of spot.")
    st.caption("Keep the same rule settings throughout a paper-trading evaluation week. Changing a gate starts a new experiment.")

engine = get_engine()
store = engine.store

st.title("NIFTY Level Trader")
st.caption("Rule-based NIFTY 5-minute signals • Upstox market data • paper-first • approval-gated live orders")

mode_col, refresh_col, saved_col, kill_col = st.columns([1.15, 1, 1, 1])
with mode_col:
    st.metric("Execution mode", "PAPER by default", help="Live orders require two environment flags and a per-order approval phrase.")
with refresh_col:
    refresh = st.button("Run one market cycle", type="primary", use_container_width=True)
with saved_col:
    load_saved = st.button("Load saved snapshot", use_container_width=True)
with kill_col:
    kill_on = store.get_state("kill_switch", "OFF") == "ON"
    if st.button("Deactivate kill switch" if kill_on else "Activate kill switch", use_container_width=True):
        store.set_state("kill_switch", "OFF" if kill_on else "ON")
        st.rerun()

st.caption("For continuous paper trading, run `python run_engine.py --poll-seconds 60` in a separate terminal. Use **Load saved snapshot** here; do not run a second polling cycle from the dashboard at the same time.")

result = None
if refresh:
    try:
        with st.spinner("Fetching Upstox market data and evaluating the latest completed candle..."):
            result = engine.run_once(execute_paper=True)
        st.session_state["market_result"] = result
        st.success("Market refresh completed. Paper engine has evaluated the latest completed 5-minute candle.")
    except Exception as exc:
        st.error(f"Market refresh failed: {type(exc).__name__}: {exc}")
elif load_saved:
    result = engine.load_last_snapshot()
    if result is not None:
        st.session_state["market_result"] = result
        st.success("Loaded the latest persisted market snapshot. No new API calls or trades were made.")
    else:
        st.warning("No current-session saved snapshot found. Start the paper engine or run one market cycle.")
elif "market_result" not in st.session_state:
    saved = engine.load_last_snapshot()
    if saved is not None:
        st.session_state["market_result"] = saved
        st.info("Showing the latest persisted market snapshot. Reload with **Load saved snapshot** after the background engine polls again.")

result = st.session_state.get("market_result")

if result:
    last = result["latest_candle"]
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("NIFTY spot", f"{result['spot']:,.2f}")
    bar_start, bar_end = candle_window(last["timestamp"], 5)
    k2.metric("Latest completed bar", f"{bar_start:%H:%M}–{bar_end:%H:%M}")
    rsi_value = result.get("indicator_context", {}).get("rsi")
    pcr_value = result["option_summary"].get("pcr")
    k3.metric("RSI(14)", f"{rsi_value:.1f}" if rsi_value is not None and pd.notna(rsi_value) else "N/A")
    k4.metric("PCR (nearest expiry)", f"{pcr_value:.2f}" if pcr_value is not None and pd.notna(pcr_value) else "N/A")
    k5.metric("Spot data age", f"{result['data_age_seconds']:.0f}s")
    refreshed = pd.Timestamp(result.get("timestamp") or store.get_state("last_refresh", ""))
    if pd.notna(refreshed):
        refreshed = refreshed.tz_localize(IST) if refreshed.tzinfo is None else refreshed.tz_convert(IST)
        refreshed_label = refreshed.strftime("%d %b %Y %H:%M:%S IST")
    else:
        refreshed_label = "N/A"
    st.caption(f"Last successful refresh: {refreshed_label} · Next expected 5-minute candle close: {candle_countdown()} (countdown as of this page render)")
    opt_age = result.get("options_age_seconds")
    if opt_age is not None and opt_age > 180:
        st.warning(f"Latest persisted option-chain snapshot is {opt_age:.0f}s old. Do not treat the option-chain metrics as live until a fresh snapshot is collected.")

    st.subheader("NIFTY 5-minute chart")
    try:
        import plotly.graph_objects as go
        candles = result["candles"].tail(100)
        fig = go.Figure(data=[go.Candlestick(x=candles.timestamp, open=candles.open,
                    high=candles.high, low=candles.low, close=candles.close, name="NIFTY")])
        level_rows = result["levels"].sort_values("price").to_dict("records")
        side_colors = {"SUPPORT": "#16a34a", "RESISTANCE": "#dc2626", "REFERENCE": "#6b7280"}
        # Place legible, colour-coded labels at the right edge of the chart so
        # the level name remains visible rather than relying on dotted lines only.
        previous_label_price = None
        stack = 0
        for level in level_rows:
            side = str(level.get("side", "REFERENCE")).upper()
            line_color = side_colors.get(side, side_colors["REFERENCE"])
            price = float(level["price"])
            fig.add_hline(y=price, line_dash="dot", line_color=line_color, opacity=0.85,
                          line_width=1.4)
            if previous_label_price is not None and abs(price - previous_label_price) < 12:
                stack += 1
            else:
                stack = 0
            # Alternate upward/downward offsets without wrapping when many
            # manual and swing levels cluster around the same price.
            yshift = 0 if stack == 0 else ((stack + 1) // 2) * 14 * (1 if stack % 2 else -1)
            fig.add_annotation(
                x=1.01, xref="paper", y=price, yref="y", yshift=yshift,
                text=f"{level['name']} · {price:,.2f}", showarrow=False,
                xanchor="left", align="left",
                font=dict(size=10, color="white"),
                bgcolor=line_color, bordercolor=line_color, borderpad=3,
            )
            previous_label_price = price
        fig.update_layout(
            height=570, xaxis_rangeslider_visible=False,
            margin=dict(l=10, r=155, t=35, b=10),
            xaxis_title="IST", yaxis_title="NIFTY spot",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        )
        st.plotly_chart(fig, use_container_width=True)
    except Exception as exc:
        st.warning(f"Chart unavailable: {exc}")

    left, right = st.columns([1, 1], gap="large")
    with left:
        st.subheader("Detected and manual levels")
        level_view = result["levels"].copy()
        level_view["level_type"] = level_view.side.map({
            "SUPPORT": "🟢 SUPPORT", "RESISTANCE": "🔴 RESISTANCE", "REFERENCE": "⚪ REFERENCE"
        }).fillna(level_view.side)
        level_view = level_view[["name", "price", "level_type", "source", "timestamp"]]
        st.dataframe(level_view, use_container_width=True, hide_index=True)
    with right:
        st.subheader("Indicator confirmation")
        ctx = result.get("indicator_context", {})
        st.write({"SMA(5)": ctx.get("sma_5"), "SMA(10)": ctx.get("sma_10"),
                  "Close above both SMAs": ctx.get("sma_bullish"),
                  "Close below both SMAs": ctx.get("sma_bearish"),
                  "Bullish RSI divergence": ctx.get("bullish_divergence"),
                  "Bearish RSI divergence": ctx.get("bearish_divergence"),
                  "RSI divergence required": ctx.get("rsi_divergence_required"),
                  "Fresh bullish FVG": ctx.get("long_fvg"), "Fresh bearish FVG": ctx.get("short_fvg")})
        st.caption("Entry logic: completed 5m breakout or valid retest; SMA(5/10) close alignment and a fresh unmitigated directional FVG are controlled by their filters. RSI divergence is now a persistent dashboard setting. OI/PCR remain context-only and do not gate trades.")
        st.caption(f"Signal data quality: {ctx.get('data_quality', 'UNKNOWN')}")

    diagnostics = result.get("indicator_context", {}).get("diagnostics", [])
    if diagnostics:
        st.subheader("Latest-candle rule diagnostics")
        st.caption("Every valid level/direction is scored against the complete rule set. DISABLED and NOT_REQUIRED are distinguished from PASS and FAIL.")
        diagnostic_frame = pd.DataFrame(diagnostics)
        compact_cols = [c for c in [
            "level", "level_price", "level_side", "direction", "mode", "status",
            "breakout_pass", "retest_pass", "sma_status", "fvg_status", "rsi_status",
            "target", "reward_risk", "reason",
        ] if c in diagnostic_frame.columns]
        compact_view = diagnostic_frame[compact_cols].rename(columns={
            "level": "Level", "level_price": "Level price", "level_side": "Level type",
            "direction": "Direction", "mode": "Entry mode", "status": "Decision",
            "breakout_pass": "Breakout", "retest_pass": "Retest", "sma_status": "SMA 5/10",
            "fvg_status": "Fresh FVG", "rsi_status": "RSI divergence",
            "target": "Next target", "reward_risk": "Planned R:R", "reason": "Why",
        })
        st.dataframe(compact_view, use_container_width=True, hide_index=True)
        with st.expander("Detailed condition-by-condition diagnostics"):
            detail_cols = [c for c in [
                "previous_close", "current_close", "entry_mode_enabled", "sma_filter_enabled",
                "sma_pass", "sma_5", "sma_10", "fvg_filter_enabled", "fvg_pass",
                "fvg_lower", "fvg_upper", "rsi_divergence_observed", "rsi_required",
                "rsi_pass", "rsi_value", "target_pass", "minimum_reward_risk", "reward_risk_pass",
            ] if c in diagnostic_frame.columns]
            st.dataframe(diagnostic_frame[detail_cols], use_container_width=True, hide_index=True)

    if result.get("option_errors"):
        st.warning("One or more option-chain expiries could not be refreshed. The remaining valid expiries are still shown; do not treat missing expiries as zero liquidity.")
        with st.expander("Option-chain fetch errors"):
            st.dataframe(pd.DataFrame(result["option_errors"]), use_container_width=True, hide_index=True)

    if result.get("events"):
        st.subheader("Execution events")
        st.dataframe(pd.DataFrame(result["events"]), use_container_width=True, hide_index=True)

    st.subheader("Latest signals")
    if result["signals"]:
        signal_frame = _expand_contract_details(pd.DataFrame(result["signals"]), source="signal")
        signal_cols = [c for c in [
            "direction", "entry_mode", "level_name", "status", "expiry", "option_type",
            "strike", "contract_otm_distance_points", "contract_ltp", "contract_bid", "contract_ask",
            "contract_spread_pct", "contract_volume", "contract_oi", "contract_delta", "contract_iv",
            "contract_theta", "liquidity_status", "reward_risk", "reason",
        ] if c in signal_frame.columns]
        st.dataframe(signal_frame[signal_cols], use_container_width=True, hide_index=True)
    else:
        st.info("No new signal passed the current entry and confluence rules on the latest completed candle.")

    st.subheader("Option-chain context")
    summary_by_expiry = result.get("option_summary_by_expiry", {})
    if summary_by_expiry and not result["options"].empty:
        expiry_options = list(summary_by_expiry)
        default_expiry = result.get("front_expiry") if result.get("front_expiry") in expiry_options else expiry_options[0]
        selected_expiry = st.selectbox(
            "Option-chain expiry", expiry_options, index=expiry_options.index(default_expiry),
            key="option_chain_expiry",
        )
        selected_frame = result["options"][result["options"].expiry.astype(str).eq(str(selected_expiry))].copy()
        selected_summary = summary_by_expiry[selected_expiry]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Call OI", f"{selected_summary.get('call_oi', 0):,.0f}")
        m2.metric("Put OI", f"{selected_summary.get('put_oi', 0):,.0f}")
        pcr_value = selected_summary.get("pcr")
        m3.metric("PCR", f"{pcr_value:.2f}" if pcr_value is not None and pd.notna(pcr_value) else "N/A")
        m4.metric("Expiry", str(selected_expiry))
        m5, m6, m7, m8 = st.columns(4)
        def oi_fmt(value):
            return f"{value:+,.0f}" if value is not None and pd.notna(value) else "N/A"
        m5.metric("Call ΔOI · day", oi_fmt(selected_summary.get("call_day_change_oi")))
        m6.metric("Put ΔOI · day", oi_fmt(selected_summary.get("put_day_change_oi")))
        m7.metric("Call ΔOI · vs prior poll", oi_fmt(selected_summary.get("call_intraday_change_oi")))
        m8.metric("Put ΔOI · vs prior poll", oi_fmt(selected_summary.get("put_intraday_change_oi")))
        m9, m10 = st.columns(2)
        m9.metric("Call ΔOI · since first app snapshot", oi_fmt(selected_summary.get("call_session_change_oi")))
        m10.metric("Put ΔOI · since first app snapshot", oi_fmt(selected_summary.get("put_session_change_oi")))
        st.caption(
            "Prior-poll ΔOI coverage: "
            f"CE {selected_summary.get('call_intraday_coverage', 0):.0f}% / "
            f"PE {selected_summary.get('put_intraday_coverage', 0):.0f}%; "
            "first-snapshot session coverage: "
            f"CE {selected_summary.get('call_session_coverage', 0):.0f}% / "
            f"PE {selected_summary.get('put_session_coverage', 0):.0f}%"
        )
        if selected_summary.get("call_intraday_change_oi") is None or selected_summary.get("put_intraday_change_oi") is None:
            st.info("Per-poll ΔOI is N/A until a second same-session snapshot is captured. Session ΔOI is measured from the first snapshot captured by this app, not necessarily the exchange open. Exchange day ΔOI uses Upstox previous OI.")

        st.markdown("#### ATM ±10-strike option ladder")
        ladder = atm_option_chain_window(selected_frame, float(result["spot"]), 10, settings)
        if ladder.empty:
            st.warning("No valid strikes are available for the selected expiry.")
        else:
            # Split the wide CE/PE ladder into readable side-by-side tables.
            # Internally spread_pct is a fraction; the UI expresses it as percent.
            ladder_display = ladder.copy()
            for col in ("CE_spread_pct", "PE_spread_pct"):
                if col in ladder_display:
                    ladder_display[col] = ladder_display[col] * 100.0
            side_cols = {
                "CE": ["strike", "ATM", "CE_ltp", "CE_bid", "CE_ask", "CE_spread_pct",
                       "CE_volume", "CE_oi", "CE_intraday_change_oi", "CE_liquidity_eligible"],
                "PE": ["strike", "ATM", "PE_ltp", "PE_bid", "PE_ask", "PE_spread_pct",
                       "PE_volume", "PE_oi", "PE_intraday_change_oi", "PE_liquidity_eligible"],
            }
            ce_col, pe_col = st.columns(2, gap="large")
            for holder, side in ((ce_col, "CE"), (pe_col, "PE")):
                with holder:
                    st.markdown(f"**{side} contracts**")
                    side_frame = ladder_display[[c for c in side_cols[side] if c in ladder_display]].copy()
                    side_frame = side_frame.rename(columns={
                        "strike": "Strike", "ATM": "ATM", f"{side}_ltp": "LTP",
                        f"{side}_bid": "Bid", f"{side}_ask": "Ask",
                        f"{side}_spread_pct": "Spread %", f"{side}_volume": "Volume",
                        f"{side}_oi": "OI", f"{side}_intraday_change_oi": "Poll ΔOI",
                        f"{side}_liquidity_eligible": "Liquidity PASS",
                    })
                    st.dataframe(side_frame, use_container_width=True, hide_index=True)

        with st.expander("All option quotes and contract-liquidity checks"):
            quality = add_option_quality_metrics(selected_frame, settings)
            view_cols = [c for c in [
                "strike", "option_type", "ltp", "bid", "ask", "spread", "spread_pct",
                "bid_qty", "ask_qty", "volume", "oi", "day_change_oi", "intraday_change_oi",
                "session_change_oi", "otm_distance_points", "iv", "delta", "theta", "gamma", "vega",
                "liquidity_score", "liquidity_eligible",
                "liquidity_status",
            ] if c in quality.columns]
            quality_view = quality[view_cols].sort_values(["strike", "option_type"]).copy()
            if "spread_pct" in quality_view:
                quality_view["spread_pct"] = quality_view["spread_pct"] * 100.0
            quality_view = quality_view.rename(columns={"spread_pct": "spread_pct_of_ask"})
            st.dataframe(quality_view, use_container_width=True, hide_index=True)
            st.caption("Liquidity PASS means valid bid/ask, premium, spread, volume, OI and configured strike-distance rule all passed. In OTM_BAND mode the signed CE/PE distance must be within the configured 150–200 point band. Spread % is (ask − bid) / ask × 100. Greeks are quote-time estimates, not guarantees.")
            eligible = quality[quality["liquidity_eligible"]].copy()
            if not eligible.empty:
                st.markdown(f"#### Eligible OTM contracts in {selected_expiry}")
                eligible_cols = [c for c in [
                    "expiry", "option_type", "strike", "otm_distance_points", "ltp", "bid", "ask",
                    "spread_pct", "volume", "oi", "delta", "iv", "theta", "liquidity_score",
                ] if c in eligible.columns]
                eligible = eligible.sort_values(["liquidity_score", "spread_pct"], ascending=[False, True]).head(20)
                eligible_view = eligible[eligible_cols].copy()
                if "spread_pct" in eligible_view:
                    eligible_view["spread_pct"] = eligible_view["spread_pct"] * 100.0
                st.dataframe(eligible_view, use_container_width=True, hide_index=True)
                st.caption("These are quote-qualified contracts, not trade signals. A strategy breakout and risk checks must still pass.")
    else:
        st.info("No option-chain snapshot is available. Check the Upstox option-chain API and contract master response.")

# Manual level management
st.divider()
st.subheader("Manual support / resistance")
with st.form("manual_level_form", clear_on_submit=True):
    col1,col2,col3,col4 = st.columns([1.2,1,1,2])
    with col1: level_name = st.text_input("Level name", value="MANUAL")
    with col2: level_price = st.number_input("Price", min_value=0.0, step=1.0, format="%.2f")
    with col3: level_side = st.selectbox("Side", ["SUPPORT", "RESISTANCE", "REFERENCE"])
    with col4: level_notes = st.text_input("Notes", value="")
    submitted = st.form_submit_button("Add level")
    if submitted and level_price > 0:
        store.add_manual_level(datetime.now(IST).date().isoformat(), level_name.strip() or "MANUAL", level_price, level_side, level_notes)
        st.success("Manual level saved for today's session.")

manuals = store.manual_levels(datetime.now(IST).date().isoformat())
if manuals:
    manual_by_id = {int(x["id"]): x for x in manuals}
    selected_manual_id = st.selectbox("Deactivate an active manual level", options=list(manual_by_id),
        format_func=lambda key: f"{key} · {manual_by_id[key]['name']} · {manual_by_id[key]['price']:.2f} · {manual_by_id[key]['side']}")
    if st.button("Deactivate selected level"):
        store.deactivate_level(int(selected_manual_id))
        st.rerun()

# Paper book, live approvals, and reconciliation
st.divider()
paper, live, audit = st.tabs(["Paper trades", "Live approvals & reconciliation", "Trade journal"])
with paper:
    p_open = store.open_trades("PAPER")
    st.metric("Open paper positions", len(p_open))
    if not p_open.empty:
        open_view = _expand_contract_details(p_open, source="trade")
        open_cols = [c for c in [
            "direction", "contract_expiry", "contract_option_type", "contract_strike",
            "contract_otm_distance_points", "entry_price", "contract_bid", "contract_ask",
            "contract_delta", "contract_iv", "contract_theta", "quantity", "premium_stop",
            "spot_stop", "spot_target", "opened_at",
        ] if c in open_view.columns]
        st.dataframe(open_view[open_cols], use_container_width=True, hide_index=True)
    p_all = store.trades("CLOSED")
    if not p_all.empty:
        closed_view = _expand_contract_details(p_all[p_all.mode == "PAPER"], source="trade")
        closed_cols = [c for c in [
            "direction", "contract_expiry", "contract_option_type", "contract_strike",
            "contract_otm_distance_points", "entry_price", "exit_price", "quantity",
            "gross_pnl", "charges", "net_pnl", "exit_reason", "opened_at", "exited_at",
        ] if c in closed_view.columns]
        st.dataframe(closed_view[closed_cols], use_container_width=True, hide_index=True)
    st.caption("Paper entries use ask + configured slippage; exits use executable bid − slippage. The configured 35% premium stop is a modeled stop risk, not a guaranteed maximum loss: long-option maximum loss remains the full premium paid if a stop cannot execute.")

with live:
    st.warning("Live order placement is off by default. Arming requires LIVE_TRADING_ENABLED=1, ALLOW_LIVE_ORDERS=1 and UPSTOX_ACCESS_TOKEN in your local .env. Each order, including exits, requires a matching approval phrase.")
    live_signals = store.recent_signals(100)
    if not live_signals.empty:
        ready = live_signals[live_signals.status == "READY"]
        if not ready.empty:
            ready = _expand_contract_details(ready, source="signal")
            ready_by_id = {x["signal_id"]: x for x in ready.to_dict("records")}
            selected_signal_id = st.selectbox("Signal to stage for live approval", options=list(ready_by_id),
                format_func=lambda key: (
                    f"{ready_by_id[key]['created_at']} · {ready_by_id[key]['direction']} · "
                    f"{ready_by_id[key]['expiry']} {ready_by_id[key]['strike']} {ready_by_id[key]['option_type']} · "
                    f"{float(ready_by_id[key].get('contract_otm_distance_points') or 0):.0f} pts OTM · "
                    f"premium ₹{float(ready_by_id[key].get('contract_ask') or 0):.2f} · "
                    f"delta {float(ready_by_id[key].get('contract_delta') or 0):.2f}"
                ))
            if st.button("Stage live entry for approval", disabled=not settings.live_is_armed):
                try:
                    out = engine.prepare_live_order(selected_signal_id)
                    st.write(out)
                except Exception as exc:
                    st.error(f"Could not stage order: {exc}")
    pending = store.pending_orders()
    if not pending.empty:
        st.markdown("**Orders awaiting your explicit approval**")
        for order in pending.to_dict("records"):
            st.code(order["approval_text"])
            entered = st.text_input("Type the exact approval phrase", key=f"approve_{order['order_ref']}")
            if st.button("Approve and submit this one order", key=f"submit_{order['order_ref']}", disabled=not settings.live_is_armed):
                try:
                    out = approve_and_submit(order["order_ref"], entered, cfg=settings, store=store, client=engine.client)
                    st.success(f"Submitted. Broker order id: {out.get('broker_order_id')}")
                except Exception as exc:
                    st.error(f"Order not submitted: {type(exc).__name__}: {exc}")
    submitted_orders = []
    with store.connect() as con:
        rows = con.execute("SELECT * FROM orders WHERE status IN ('SUBMITTED','PARTIALLY_FILLED','OPEN','CANCEL_REQUESTED','SUBMISSION_UNKNOWN','SUBMIT_ERROR') ORDER BY rowid DESC").fetchall()
        submitted_orders = [dict(x) for x in rows]
    if submitted_orders:
        st.markdown("**Broker order reconciliation**")
        for order in submitted_orders:
            st.write(f"{order['side']} {order['quantity']} · {order['instrument_key']} · {order['broker_order_id'] or 'broker id unknown'} · {order['status']}")
            if order["status"] == "SUBMISSION_UNKNOWN" and not order["broker_order_id"]:
                try:
                    unknown_payload = json.loads(order.get("payload") or "{}")
                except json.JSONDecodeError:
                    unknown_payload = {}
                possible_ids = unknown_payload.get("broker_order_ids") or []
                if possible_ids:
                    st.error("Upstox returned multiple child order IDs. This application cannot safely aggregate a multi-child order into one managed position. Do not retry; inspect each order in the Upstox order book and reconcile the aggregate filled quantity.")
                    st.code("\n".join(str(value) for value in possible_ids))
                else:
                    st.error("Submission result is ambiguous. Inspect the Upstox order book manually; do not retry this order until you establish whether it reached the broker.")
            if order.get("broker_order_id"):
                c_sync, c_cancel = st.columns(2)
                with c_sync:
                    if st.button("Refresh broker status", key=f"sync_{order['order_ref']}"):
                        try:
                            st.write(sync_live_order(order["order_ref"], store=store, client=engine.client, cfg=settings))
                        except Exception as exc:
                            st.error(f"Reconciliation failed: {exc}")
                with c_cancel:
                    if order["status"] in {"SUBMITTED", "PARTIALLY_FILLED", "OPEN"} and st.button("Request broker cancellation", key=f"cancel_{order['order_ref']}"):
                        try:
                            response = engine.client.cancel_order(str(order["broker_order_id"]))
                            try:
                                payload = json.loads(order.get("payload") or "{}")
                            except json.JSONDecodeError:
                                payload = {}
                            payload["cancel_response"] = response
                            store.update_order(order["order_ref"], status="CANCEL_REQUESTED", payload=json.dumps(payload, default=str))
                            st.warning("Cancellation request sent. Refresh broker status until Upstox confirms a terminal state.")
                        except Exception as exc:
                            st.error(f"Cancellation request failed: {type(exc).__name__}: {exc}")
            if order["status"] == "PARTIALLY_FILLED":
                st.warning("This order still has a working remainder. Request cancellation and reconcile the terminal broker status before the application treats the filled quantity as a managed position.")
    live_open = store.open_trades("LIVE")
    if not live_open.empty:
        st.markdown("**Open live positions**")
        st.dataframe(live_open, use_container_width=True, hide_index=True)
        live_by_id = {x["trade_id"]: x for x in live_open.to_dict("records")}
        selected_trade_id = st.selectbox("Live position to stage for exit", options=list(live_by_id),
            format_func=lambda key: f"{key} · {live_by_id[key]['instrument_key']} · qty {live_by_id[key]['quantity']}")
        if st.button("Stage live exit for approval", disabled=not settings.live_is_armed):
            try:
                st.write(engine.prepare_live_exit(selected_trade_id))
            except Exception as exc:
                st.error(f"Could not stage exit: {exc}")

with audit:
    sigs = store.recent_signals(200)
    if not sigs.empty:
        st.markdown("**Signal audit trail**")
        st.dataframe(sigs, use_container_width=True, hide_index=True)
    orders = pd.read_sql_query("SELECT * FROM orders ORDER BY rowid DESC LIMIT 200", store.connect())
    if not orders.empty:
        st.markdown("**Order audit trail**")
        st.dataframe(orders, use_container_width=True, hide_index=True)
    engine_events = store.recent_events(500)
    if not engine_events.empty:
        st.markdown("**Persistent engine / operational events**")
        st.dataframe(engine_events, use_container_width=True, hide_index=True)
    diagnostics = store.recent_diagnostics(500)
    if not diagnostics.empty:
        st.markdown("**Persisted rule diagnostics (one-week review dataset)**")
        st.dataframe(diagnostics, use_container_width=True, hide_index=True)
    trades = store.trades(limit=300)
    if not trades.empty:
        st.markdown("**All trades**")
        st.dataframe(trades, use_container_width=True, hide_index=True)

# Research backtest
st.divider()
st.subheader("Historical signal backtest")
st.caption("Upload 5-minute NIFTY spot OHLC CSV. The engine simulates spot-level stops and next-level targets. It does not claim option-premium returns without historical option-chain and contract OHLC data.")
spot_file = st.file_uploader("NIFTY 5-minute candles CSV", type=["csv"], key="spot_csv")
daily_file = st.file_uploader("Optional daily OHLC CSV for PDH/PDL/PDC", type=["csv"], key="daily_csv")
if spot_file and st.button("Run spot signal backtest"):
    try:
        test_candles = pd.read_csv(spot_file, parse_dates=["timestamp"])
        test_daily = pd.read_csv(daily_file, parse_dates=["timestamp"]) if daily_file else pd.DataFrame()
        effective_settings = engine.strategy_settings()
        trades, metrics = backtest_spot(test_candles, test_daily, effective_settings)
        st.json(metrics)
        if not trades.empty:
            st.dataframe(trades, use_container_width=True, hide_index=True)
    except Exception as exc:
        st.error(f"Backtest failed: {type(exc).__name__}: {exc}")

st.caption(f"Local DB: {settings.db_path} · Last successful market refresh: {store.get_state('last_refresh', 'not yet refreshed')}")
