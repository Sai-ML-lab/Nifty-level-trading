from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from nifty_trader.upstox import normalize_candles
from nifty_trader.indicators import add_indicators, latest_fvg, rsi_divergence
from nifty_trader.levels import build_levels, next_target
from nifty_trader.option_chain import normalize_option_chain, chain_summary, choose_expiry
from nifty_trader.db import Store
from nifty_trader.risk import size_long_option, market_is_open
from nifty_trader.execution import stage_paper_trade, stage_live_order, approve_and_submit

IST = ZoneInfo("Asia/Kolkata")


def cfg(**overrides):
    values = dict(
        rsi_period=14, pivot_left=2, pivot_right=2,
        sma_filter_enabled=False, fvg_filter_enabled=False, rsi_divergence_required=False,
        immediate_entry_enabled=True, retest_entry_enabled=True, retest_bars=3,
        breakout_buffer_points=1.0, retest_tolerance_points=5.0,
        atr_stop_multiplier=1.0, min_reward_risk=0.5,
        risk_per_trade_inr=500, max_lots=2, max_position_value_inr=15000,
        min_premium_inr=30, max_premium_inr=500, max_spread_pct=0.10,
        min_option_volume=100, min_option_oi=1000, option_moneyness_pct=0.02,
        slippage_points=0.5, live_is_armed=False, live_product="I",
        stale_data_seconds=360, max_trades_per_day=2, max_daily_loss_inr=2000,
        estimated_round_trip_cost_per_lot=100,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_normalize_candles_sorts_dedupes_and_timezone():
    now = pd.Timestamp.now(tz=IST).floor("5min")
    rows = [
        [(now - pd.Timedelta(minutes=10)).isoformat(), 10, 12, 9, 11, 100, 5],
        [(now - pd.Timedelta(minutes=5)).isoformat(), 11, 13, 10, 12, 200, 7],
        [(now - pd.Timedelta(minutes=10)).isoformat(), 10, 12, 9, 10.5, 120, 6],
    ]
    df = normalize_candles(rows)
    assert len(df) == 2
    assert df.timestamp.is_monotonic_increasing
    assert str(df.timestamp.dt.tz) == "Asia/Kolkata"
    assert df.iloc[0].close == 10.5


def test_build_levels_previous_day_and_opening_range():
    day1 = pd.Timestamp("2026-11-26 09:15", tz=IST)
    day2 = pd.Timestamp("2026-11-27 09:15", tz=IST)
    daily = pd.DataFrame([
        {"timestamp": day1, "open": 100, "high": 120, "low": 90, "close": 115, "volume": 1, "oi": 0},
        {"timestamp": day2, "open": 115, "high": 125, "low": 110, "close": 122, "volume": 1, "oi": 0},
    ])
    intraday = pd.DataFrame([
        {"timestamp": day2 + pd.Timedelta(minutes=i*5), "open": 115+i, "high": 118+i,
         "low": 113+i, "close": 116+i, "volume": 10, "oi": 0} for i in range(5)
    ])
    levels = build_levels(intraday, daily)
    by_name = {x.name: x.price for x in levels.itertuples()}
    assert by_name["PDH"] == 120
    assert by_name["PDL"] == 90
    assert by_name["PDC"] == 115
    assert by_name["ORH"] == 120
    assert by_name["ORL"] == 113


def test_next_target_directional():
    levels = pd.DataFrame({"price": [100, 110, 120]})
    assert next_target(levels, 105, "LONG") == 110
    assert next_target(levels, 115, "SHORT") == 110
    assert next_target(levels, 125, "LONG") is None


def test_fvg_detection():
    rows = [
        (1, 10, 12, 9, 11),
        (2, 11, 13, 10, 12),
        (3, 14, 15, 13, 14.5),  # gap above first high 12
    ]
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
    fvg = latest_fvg(df, "LONG")
    assert fvg is not None
    assert fvg["lower"] == 12
    assert fvg["upper"] == 13


def test_confirmed_pivots_ignore_unconfirmed_right_edge():
    ts = pd.date_range("2026-11-27 09:15", periods=10, freq="5min", tz=IST)
    df = pd.DataFrame({"timestamp": ts,
        "open": [10]*10, "high": [11,12,13,10,9,10,11,12,13,14],
        "low": [9,8,7,9,10,9,8,9,10,11], "close": [10]*10,
        "volume": [1]*10, "oi": [0]*10})
    out = add_indicators(df, 3, 2, 2)
    assert bool(out.loc[2, "pivot_low"]) is True
    assert bool(out.loc[9, "pivot_high"]) is False


def test_option_chain_normalization_pcr_and_snapshot_fields():
    chain = [{"expiry": "2026-12-03", "strike_price": 25000, "underlying_spot_price": 25010,
              "call_options": {"instrument_key": "CE1", "market_data": {"ltp": 100, "volume": 1000, "oi": 2000, "prev_oi": 1500, "bid_price": 99, "ask_price": 101}, "option_greeks": {"delta": 0.5}},
              "put_options": {"instrument_key": "PE1", "market_data": {"ltp": 90, "volume": 1500, "oi": 4000, "prev_oi": 3000, "bid_price": 89, "ask_price": 91}, "option_greeks": {"delta": -0.5}}}]
    df = normalize_option_chain(chain)
    summ = chain_summary(df)
    assert len(df) == 2
    assert summ["pcr"] == 2.0
    assert summ["call_change_oi"] == 500
    assert summ["put_change_oi"] == 1000


def test_option_selector_scores_liquidity_spread_and_premium():
    c = cfg()
    frame = pd.DataFrame([
        {"expiry": "2026-12-03", "option_type": "CE", "strike": 25000, "spot": 25000,
         "instrument_key": "good", "ltp": 100, "bid": 99, "ask": 101, "spread_pct": 0.0198,
         "volume": 10000, "oi": 100000, "score": 0, "lot_size": 65},
        {"expiry": "2026-12-03", "option_type": "CE", "strike": 25100, "spot": 25000,
         "instrument_key": "bad", "ltp": 100, "bid": 80, "ask": 120,
         "volume": 100, "oi": 100, "lot_size": 65},
    ])
    selected = choose_expiry({"2026-12-03": frame}, 25000, "LONG", c)
    assert selected is not None
    assert selected["instrument_key"] == "good"


def test_store_persists_manual_levels_and_signal_idempotently(tmp_path):
    store = Store(tmp_path / "test.sqlite")
    store.add_manual_level("2026-11-27", "Desk S1", 25000, "SUPPORT")
    assert store.manual_levels("2026-11-27")[0]["name"] == "Desk S1"
    signal = {"signal_id": "s1", "created_at": "2026-11-27T10:00:00+05:30", "session_date": "2026-11-27",
              "direction": "LONG", "entry_mode": "IMMEDIATE", "level_name": "PDH", "level_price": 25000,
              "spot_entry": 25010, "spot_stop": 24980, "spot_target": 25100, "reward_risk": 3,
              "status": "READY", "reason": "breakout"}
    assert store.save_signal(signal) is True
    assert store.save_signal(signal) is False
    assert len(store.recent_signals()) == 1


def test_sizing_rejects_one_lot_above_risk_or_value_cap():
    c = cfg(risk_per_trade_inr=200, max_position_value_inr=15000, max_lots=2)
    result = size_long_option(premium=100, lot_size=65, cfg=c)
    assert not result.allowed
    assert result.lots == 0


def test_sizing_caps_lots():
    result = size_long_option(premium=50, lot_size=25, cfg=cfg(risk_per_trade_inr=1000, max_lots=1))
    assert result.allowed
    assert result.lots == 1
    assert result.quantity == 25


def test_paper_trade_entry_exit_ledger(tmp_path):
    store = Store(tmp_path / "paper.sqlite")
    sig = {"signal_id": "sig", "direction": "LONG", "spot_entry": 25000,
           "spot_stop": 24950, "spot_target": 25100}
    option = {"instrument_key": "NSE_FO|1", "expiry": "2026-12-03", "ask": 100, "bid": 99}
    out = stage_paper_trade(sig, option, 25, cfg(risk_per_trade_inr=2000), store)
    assert out["status"] == "OPEN"
    duplicate = stage_paper_trade(sig, option, 25, cfg(risk_per_trade_inr=2000), store)
    assert duplicate["status"] == "ALREADY_EXISTS"
    assert len(store.trades()) == 1
    open_trade = store.open_trades("PAPER").iloc[0].to_dict()
    from nifty_trader.execution import close_paper_trade
    result = close_paper_trade(open_trade, 120, "target", cfg(), store)
    assert result["net_pnl"] > 0
    assert store.open_trades("PAPER").empty


def test_live_order_staging_is_blocked_unless_armed(tmp_path):
    store = Store(tmp_path / "live.sqlite")
    sig = {"signal_id": "sig", "direction": "LONG", "spot_entry": 25000,
           "spot_stop": 24950, "spot_target": 25100}
    option = {"instrument_key": "NSE_FO|1", "expiry": "2026-12-03", "ask": 100, "bid": 99}
    out = stage_live_order(sig, option, 25, cfg(live_is_armed=False), store)
    assert out["status"] == "REJECTED"


def test_live_approval_phrase_is_bound_to_order(tmp_path, monkeypatch):
    store = Store(tmp_path / "live2.sqlite")
    sig = {"signal_id": "sig", "direction": "LONG", "spot_entry": 25000,
           "spot_stop": 24950, "spot_target": 25100}
    option = {"instrument_key": "NSE_FO|1", "expiry": "2026-12-03", "ask": 100, "bid": 99,
              "lot_size": 25}
    c = cfg(live_is_armed=True, risk_per_trade_inr=2000)
    out = stage_live_order(sig, option, 25, c, store)
    class FakeClient:
        def place_limit_order(self, **kwargs):
            return {"status": "success", "data": {"order_id": "oid-1"}}
    import nifty_trader.risk as risk
    monkeypatch.setattr(risk, "market_is_open", lambda now=None: True)
    with pytest.raises(PermissionError):
        approve_and_submit(out["order"]["order_ref"], "APPROVE WRONG", cfg=c, store=store, client=FakeClient())
    submitted = approve_and_submit(out["order"]["order_ref"], out["order"]["approval_text"], cfg=c, store=store, client=FakeClient())
    assert submitted["status"] == "SUBMITTED"
    assert submitted["broker_order_id"] == "oid-1"


def test_nse_session_hours():
    assert market_is_open(datetime(2026, 11, 27, 10, 0, tzinfo=IST))
    assert not market_is_open(datetime(2026, 11, 27, 16, 0, tzinfo=IST))
    assert not market_is_open(datetime(2026, 11, 28, 10, 0, tzinfo=IST))


def signal_candles(breakout_at_end=True, retest=False):
    ts = pd.date_range("2026-11-27 09:15", periods=24, freq="5min", tz=IST)
    rows = []
    for i, stamp in enumerate(ts):
        close = 99.0
        high, low = 100.0, 98.0
        if retest and i == 22:
            close, high, low = 102.0, 103.0, 101.0
        if retest and i == 23:
            close, high, low = 100.5, 102.0, 99.5
        if not retest and i == 23:
            close, high, low = 102.0, 103.0, 99.0
        rows.append({"timestamp": stamp, "open": close, "high": high, "low": low,
                     "close": close, "volume": 100, "oi": 0})
    return pd.DataFrame(rows)


def test_immediate_breakout_signal_is_generated():
    from nifty_trader.strategy import evaluate_signals
    candles = signal_candles()
    levels = pd.DataFrame([
        {"name": "R1", "price": 100.0, "side": "RESISTANCE", "source": "MANUAL"},
        {"name": "R2", "price": 110.0, "side": "RESISTANCE", "source": "MANUAL"},
    ])
    signals, _, _ = evaluate_signals(candles, levels, cfg())
    assert any(x["direction"] == "LONG" and x["entry_mode"] == "IMMEDIATE" for x in signals)


def test_retest_entry_is_separate_from_immediate_entry():
    from nifty_trader.strategy import evaluate_signals
    candles = signal_candles(retest=True)
    # The last candle pulls back to the broken level and closes back above it.
    levels = pd.DataFrame([
        {"name": "R1", "price": 100.0, "side": "RESISTANCE", "source": "MANUAL"},
        {"name": "R2", "price": 110.0, "side": "RESISTANCE", "source": "MANUAL"},
    ])
    signals, _, _ = evaluate_signals(candles, levels, cfg())
    assert any(x["direction"] == "LONG" and x["entry_mode"] == "RETEST" for x in signals)


def test_expired_live_approval_cannot_be_submitted(tmp_path):
    import json
    from datetime import timedelta
    from nifty_trader.execution import stage_live_order, approve_and_submit
    store = Store(tmp_path / "expired.sqlite")
    c = cfg(live_is_armed=True, risk_per_trade_inr=2000)
    sig = {"signal_id": "expired-sig", "direction": "LONG", "spot_entry": 25000,
           "spot_stop": 24950, "spot_target": 25100}
    option = {"instrument_key": "NSE_FO|2", "expiry": "2026-12-03", "ask": 100, "bid": 99, "lot_size": 25}
    out = stage_live_order(sig, option, 25, c, store)
    order_ref = out["order"]["order_ref"]
    with store.connect() as con:
        row = con.execute("SELECT payload FROM orders WHERE order_ref=?", (order_ref,)).fetchone()
    payload = json.loads(row["payload"])
    payload["expires_at"] = (datetime.now(IST) - timedelta(seconds=5)).isoformat()
    store.update_order(order_ref, payload=json.dumps(payload))
    class FakeClient:
        def place_limit_order(self, **kwargs):
            raise AssertionError("Expired approval must not reach the broker")
    with pytest.raises(TimeoutError):
        approve_and_submit(order_ref, out["order"]["approval_text"], cfg=c, store=store, client=FakeClient())
    with store.connect() as con:
        status = con.execute("SELECT status FROM orders WHERE order_ref=?", (order_ref,)).fetchone()["status"]
    assert status == "EXPIRED"


def test_live_entry_reconciliation_creates_audited_trade(tmp_path, monkeypatch):
    from nifty_trader.execution import stage_live_order, approve_and_submit, sync_live_order
    import nifty_trader.risk as risk
    monkeypatch.setattr(risk, "market_is_open", lambda now=None: True)
    store = Store(tmp_path / "live-fill.sqlite")
    c = cfg(live_is_armed=True, risk_per_trade_inr=2000)
    sig = {"signal_id": "live-fill", "direction": "LONG", "spot_entry": 25000,
           "spot_stop": 24950, "spot_target": 25100, "created_at": datetime.now(IST).isoformat()}
    option = {"instrument_key": "NSE_FO|3", "expiry": "2026-12-03", "ask": 100, "bid": 99, "lot_size": 25}
    staged = stage_live_order(sig, option, 25, c, store)
    class FakeClient:
        def place_limit_order(self, **kwargs):
            return {"status": "success", "data": {"order_id": "live-oid"}}
        def order_history(self, order_id):
            return {"status": "success", "data": [{"status": "complete", "filled_quantity": 25, "average_price": 101}]}
    client = FakeClient()
    approve_and_submit(staged["order"]["order_ref"], staged["order"]["approval_text"], cfg=c, store=store, client=client)
    result = sync_live_order(staged["order"]["order_ref"], store=store, client=client)
    assert result["status"] == "FILLED"
    trades = store.open_trades("LIVE")
    assert len(trades) == 1
    assert trades.iloc[0].entry_price == 101
    assert trades.iloc[0].lot_size == 25


def test_cancelled_partially_filled_entry_becomes_managed_live_position(tmp_path, monkeypatch):
    import nifty_trader.risk as risk
    from nifty_trader.execution import stage_live_order, approve_and_submit, sync_live_order
    monkeypatch.setattr(risk, "market_is_open", lambda now=None: True)
    store = Store(tmp_path / "partial-entry.sqlite")
    c = cfg(live_is_armed=True, risk_per_trade_inr=2000)
    sig = {"signal_id": "partial-entry", "direction": "LONG", "spot_entry": 25000,
           "spot_stop": 24950, "spot_target": 25100}
    option = {"instrument_key": "NSE_FO|partial", "expiry": "2026-12-03", "ask": 100,
              "bid": 99, "lot_size": 25}
    staged = stage_live_order(sig, option, 25, c, store)

    class FakeClient:
        def place_limit_order(self, **kwargs):
            return {"status": "success", "data": {"order_id": "partial-buy"}}
        def order_history(self, order_id):
            return {"status": "success", "data": [{"status": "cancelled", "filled_quantity": 25,
                                                        "average_price": 100.5}]}

    client = FakeClient()
    approve_and_submit(staged["order"]["order_ref"], staged["order"]["approval_text"],
                       cfg=c, store=store, client=client)
    result = sync_live_order(staged["order"]["order_ref"], cfg=c, store=store, client=client)
    assert result["status"] == "CANCELLED"
    trades = store.open_trades("LIVE")
    assert len(trades) == 1
    assert int(trades.iloc[0].quantity) == 25


def test_cancelled_partial_exit_reduces_remaining_live_quantity(tmp_path):
    from nifty_trader.execution import stage_live_exit, sync_live_order
    store = Store(tmp_path / "partial-exit.sqlite")
    c = cfg(live_is_armed=True)
    trade = {"trade_id": "live-trade", "signal_id": "partial-signal", "mode": "LIVE",
             "direction": "LONG", "instrument_key": "NSE_FO|partial-exit", "expiry": "2026-12-03",
             "entry_price": 100.0, "quantity": 65, "lot_size": 65, "spot_entry": 25000,
             "spot_stop": 24950, "spot_target": 25100, "premium_stop": 65.0,
             "status": "OPEN", "opened_at": datetime.now(IST).isoformat(), "payload": "{}"}
    store.save_trade(trade)
    staged = stage_live_exit(trade, 120.0, cfg=c, store=store)
    order_ref = staged["order"]["order_ref"]
    store.update_order(order_ref, status="SUBMITTED", broker_order_id="partial-sell")

    class FakeClient:
        def order_history(self, order_id):
            return {"status": "success", "data": [{"status": "cancelled", "filled_quantity": 25,
                                                        "average_price": 120.0}]}

    result = sync_live_order(order_ref, cfg=c, store=store, client=FakeClient())
    assert result["status"] == "CANCELLED"
    live_open = store.open_trades("LIVE")
    assert len(live_open) == 1
    assert int(live_open.iloc[0].quantity) == 40
    assert float(live_open.iloc[0].gross_pnl) == 500.0


def test_unknown_submission_blocks_another_live_entry(tmp_path):
    from nifty_trader.risk import pretrade_gate
    store = Store(tmp_path / "unknown-submit.sqlite")
    c = cfg(live_is_armed=True)
    order = {"order_ref": "uncertain", "signal_id": "uncertain-signal", "mode": "LIVE",
             "side": "BUY", "instrument_key": "NSE_FO|unknown", "quantity": 25,
             "limit_price": 100, "status": "SUBMISSION_UNKNOWN", "payload": "{}"}
    store.save_order(order)
    ok, reason = pretrade_gate(cfg=c, store=store, session_date="2026-11-27", mode="LIVE",
                               now=datetime(2026, 11, 27, 10, 0, tzinfo=IST), data_age_seconds=0)
    assert not ok
    assert "reconciliation" in reason.lower()


def test_live_order_rechecks_fresh_spot_and_quote_before_broker_submit(tmp_path, monkeypatch):
    import nifty_trader.risk as risk
    from nifty_trader.execution import stage_live_order, approve_and_submit
    monkeypatch.setattr(risk, "market_is_open", lambda now=None: True)
    store = Store(tmp_path / "live-revalidate.sqlite")
    c = cfg(live_is_armed=True, risk_per_trade_inr=2000, instrument_key="NSE_INDEX|Nifty 50",
            max_approval_price_drift_pct=0.02)
    now = pd.Timestamp.now(tz=IST)
    signal_ts = now.floor("5min")
    signal = {"signal_id": "fresh-revalidate", "created_at": signal_ts.isoformat(),
              "direction": "LONG", "level_price": 25000, "spot_entry": 25010,
              "spot_stop": 24950, "spot_target": 25100}
    option = {"instrument_key": "NSE_FO|fresh", "expiry": now.date().isoformat(),
              "ask": 101, "bid": 99, "lot_size": 25}
    staged = stage_live_order(signal, option, 25, c, store)

    class FreshClient:
        def __init__(self, ask):
            self.ask = ask
            self.submitted = False
        def intraday_candles(self, instrument_key, interval):
            return pd.DataFrame([{"timestamp": signal_ts - pd.Timedelta(minutes=5),
                                  "open": 25005, "high": 25020, "low": 25000,
                                  "close": 25010, "volume": 100, "oi": 0}])
        def option_chain(self, instrument_key, expiry):
            return [{"expiry": expiry, "strike_price": 25000, "underlying_spot_price": 25010,
                     "call_options": {"instrument_key": "NSE_FO|fresh", "market_data": {
                         "ltp": self.ask, "volume": 5000, "oi": 50000, "prev_oi": 45000,
                         "bid_price": self.ask - 2, "ask_price": self.ask}}, "put_options": {}}]
        def place_limit_order(self, **kwargs):
            self.submitted = True
            return {"status": "success", "data": {"order_id": "fresh-oid"}}

    client = FreshClient(ask=102)
    result = approve_and_submit(staged["order"]["order_ref"], staged["order"]["approval_text"],
                                cfg=c, store=store, client=client)
    assert result["status"] == "SUBMITTED"
    assert client.submitted


def test_live_order_rejects_stale_quote_drift_before_submission(tmp_path, monkeypatch):
    import nifty_trader.risk as risk
    from nifty_trader.execution import stage_live_order, approve_and_submit
    monkeypatch.setattr(risk, "market_is_open", lambda now=None: True)
    store = Store(tmp_path / "live-price-drift.sqlite")
    now = pd.Timestamp.now(tz=IST)
    c = cfg(live_is_armed=True, risk_per_trade_inr=2000, instrument_key="NSE_INDEX|Nifty 50",
            max_approval_price_drift_pct=0.02)
    signal = {"signal_id": "price-drift", "created_at": now.floor("5min").isoformat(),
              "direction": "LONG", "level_price": 25000, "spot_entry": 25010,
              "spot_stop": 24950, "spot_target": 25100}
    option = {"instrument_key": "NSE_FO|drift", "expiry": now.date().isoformat(),
              "ask": 100, "bid": 99, "lot_size": 25}
    staged = stage_live_order(signal, option, 25, c, store)

    class DriftClient:
        submitted = False
        def intraday_candles(self, instrument_key, interval):
            ts = now.floor("5min")
            return pd.DataFrame([{"timestamp": ts - pd.Timedelta(minutes=5),
                                  "open": 25005, "high": 25020, "low": 25000,
                                  "close": 25010, "volume": 100, "oi": 0}])
        def option_chain(self, instrument_key, expiry):
            return [{"expiry": expiry, "strike_price": 25000, "underlying_spot_price": 25010,
                     "call_options": {"instrument_key": "NSE_FO|drift", "market_data": {
                         "ltp": 110, "volume": 5000, "oi": 50000, "prev_oi": 45000,
                         "bid_price": 109, "ask_price": 110}}, "put_options": {}}]
        def place_limit_order(self, **kwargs):
            self.submitted = True
            return {"status": "success", "data": {"order_id": "should-not-send"}}

    client = DriftClient()
    with pytest.raises(RuntimeError, match="quote moved"):
        approve_and_submit(staged["order"]["order_ref"], staged["order"]["approval_text"],
                           cfg=c, store=store, client=client)
    assert not client.submitted
    with store.connect() as con:
        status = con.execute("SELECT status FROM orders WHERE order_ref=?",
                             (staged["order"]["order_ref"],)).fetchone()["status"]
    assert status == "STALE_APPROVAL"


def test_pretrade_gate_enforces_last_entry_time(tmp_path, monkeypatch):
    from nifty_trader.risk import pretrade_gate
    import nifty_trader.risk as risk
    monkeypatch.setattr(risk, "market_is_open", lambda now=None: True)
    store = Store(tmp_path / "cutoff.sqlite")
    c = cfg(last_entry_time="15:00")
    ok, reason = pretrade_gate(cfg=c, store=store, session_date="2026-11-27", mode="PAPER",
                               now=datetime(2026, 11, 27, 15, 2, tzinfo=IST), data_age_seconds=0)
    assert not ok
    assert "last-entry" in reason.lower()


def test_long_breakout_does_not_use_a_support_level_as_resistance():
    from nifty_trader.strategy import evaluate_signals
    candles = signal_candles()
    levels = pd.DataFrame([
        {"name": "S1", "price": 100.0, "side": "SUPPORT", "source": "MANUAL"},
        {"name": "R2", "price": 110.0, "side": "RESISTANCE", "source": "MANUAL"},
    ])
    signals, _, _ = evaluate_signals(candles, levels, cfg())
    assert not any(x["level_name"] == "S1" and x["direction"] == "LONG" for x in signals)


def test_fvg_is_not_fresh_after_price_trades_into_gap():
    # Bullish gap is [12, 13]; a later low of 12.5 partially mitigates it.
    rows = [
        (1, 10, 12, 9, 11),
        (2, 11, 12.5, 10, 12),
        (3, 14, 15, 13, 14.5),
        (4, 13.5, 14, 12.5, 13),
    ]
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
    assert latest_fvg(df, "LONG") is None


def test_fvg_is_not_formed_across_missing_five_minute_bar():
    ts = [pd.Timestamp("2026-11-27 09:15", tz=IST),
          pd.Timestamp("2026-11-27 09:25", tz=IST),
          pd.Timestamp("2026-11-27 09:30", tz=IST)]
    df = pd.DataFrame({"timestamp": ts, "open": [10, 11, 14],
                       "high": [12, 12.5, 15], "low": [9, 10, 13],
                       "close": [11, 12, 14.5]})
    assert latest_fvg(df, "LONG") is None


def test_option_intraday_oi_is_unknown_until_second_same_session_snapshot():
    from nifty_trader.option_chain import attach_snapshot_change
    current = pd.DataFrame([{"instrument_key": "CE1", "oi": 2500}])
    first = attach_snapshot_change(current, None)
    assert pd.isna(first.iloc[0].intraday_change_oi)
    assert chain_summary(pd.concat([
        first.assign(option_type="CE", strike=25000, day_change_oi=float("nan")),
    ]))["call_intraday_change_oi"] is None

    previous = pd.DataFrame([{"instrument_key": "CE1", "oi": 2100}])
    second = attach_snapshot_change(current, previous)
    assert second.iloc[0].intraday_change_oi == 400


def test_previous_option_snapshot_is_scoped_to_current_session(tmp_path):
    store = Store(tmp_path / "snapshots.sqlite")
    old = pd.DataFrame([{"timestamp": "2026-11-26T15:25:00+05:30", "expiry": "2026-12-01",
                         "instrument_key": "CE1", "option_type": "CE", "strike": 25000,
                         "ltp": 100, "volume": 1000, "oi": 2000, "bid": 99, "ask": 101}])
    today = pd.DataFrame([{"timestamp": "2026-11-27T10:00:00+05:30", "expiry": "2026-12-01",
                           "instrument_key": "CE1", "option_type": "CE", "strike": 25000,
                           "ltp": 105, "volume": 1100, "oi": 2200, "bid": 104, "ask": 106}])
    store.save_option_snapshot(old)
    assert store.previous_option_snapshot(["CE1"], session_date="2026-11-27").empty
    store.save_option_snapshot(today)
    prior = store.previous_option_snapshot(["CE1"], session_date="2026-11-27")
    assert len(prior) == 1
    assert prior.iloc[0].oi == 2200


def test_next_target_respects_support_resistance_side_labels():
    lv = pd.DataFrame([
        {"name": "wrong-side-support", "price": 105, "side": "SUPPORT"},
        {"name": "R1", "price": 110, "side": "RESISTANCE"},
        {"name": "wrong-side-resistance", "price": 95, "side": "RESISTANCE"},
        {"name": "S1", "price": 90, "side": "SUPPORT"},
    ])
    assert next_target(lv, 100, "LONG") == 110
    assert next_target(lv, 100, "SHORT") == 90


def test_sma_filter_requires_close_above_both_not_sma_stacking():
    from nifty_trader.strategy import evaluate_signals
    candles = signal_candles()
    # Make SMA(5) lower than SMA(10), while the breakout close remains above both.
    candles.loc[18:22, ["open", "high", "low", "close"]] = [90, 91, 89, 90]
    candles.loc[23, ["open", "high", "low", "close"]] = [101, 103, 100, 102]
    levels = pd.DataFrame([
        {"name": "R1", "price": 100.0, "side": "RESISTANCE"},
        {"name": "R2", "price": 120.0, "side": "RESISTANCE"},
    ])
    signals, frame, context = evaluate_signals(
        candles, levels, cfg(sma_filter_enabled=True, fvg_filter_enabled=False))
    assert frame.iloc[-1].sma_5 < frame.iloc[-1].sma_10
    assert frame.iloc[-1].close > frame.iloc[-1].sma_5
    assert frame.iloc[-1].close > frame.iloc[-1].sma_10
    assert any(x["direction"] == "LONG" for x in signals)
    assert context["sma_bullish"] is True


def test_strategy_blocks_gapped_recent_five_minute_data():
    from nifty_trader.strategy import evaluate_signals
    candles = signal_candles()
    candles.loc[20, "timestamp"] = candles.loc[20, "timestamp"] + pd.Timedelta(minutes=5)
    levels = pd.DataFrame([{"name": "R1", "price": 100, "side": "RESISTANCE"}])
    signals, _, context = evaluate_signals(candles, levels, cfg())
    assert signals == []
    assert context["data_quality"] == "GAPPED_5M_DATA"


def test_position_manager_detects_stop_in_older_unprocessed_post_entry_bar():
    from nifty_trader.engine import TradingEngine
    opened = pd.Timestamp("2026-11-27 10:02", tz=IST)
    trade = {"opened_at": opened.isoformat(), "direction": "LONG",
             "spot_stop": 95.0, "spot_target": 110.0}
    bars = pd.DataFrame([
        # This candle began before entry and is ignored even though its low crossed stop.
        {"timestamp": pd.Timestamp("2026-11-27 10:00", tz=IST), "open": 100, "high": 112, "low": 90, "close": 105},
        {"timestamp": pd.Timestamp("2026-11-27 10:05", tz=IST), "open": 105, "high": 108, "low": 100, "close": 103},
        # Older than latest, but after entry: it must still be evaluated.
        {"timestamp": pd.Timestamp("2026-11-27 10:10", tz=IST), "open": 103, "high": 105, "low": 94, "close": 96},
        {"timestamp": pd.Timestamp("2026-11-27 10:15", tz=IST), "open": 96, "high": 100, "low": 95, "close": 99},
    ])
    reason, trigger_ts = TradingEngine._first_spot_exit_trigger(trade, bars)
    assert reason == "spot_stop"
    assert trigger_ts.startswith("2026-11-27T10:10")


def test_position_manager_ignores_partially_formed_entry_candle():
    from nifty_trader.engine import TradingEngine
    trade = {"opened_at": "2026-11-27T10:02:00+05:30", "direction": "LONG",
             "spot_stop": 95.0, "spot_target": 110.0}
    bars = pd.DataFrame([{"timestamp": pd.Timestamp("2026-11-27 10:00", tz=IST),
                          "open": 100, "high": 111, "low": 94, "close": 102}])
    assert TradingEngine._first_spot_exit_trigger(trade, bars) == (None, None)


def test_position_manager_uses_stop_if_stop_and_target_hit_same_bar():
    from nifty_trader.engine import TradingEngine
    trade = {"opened_at": "2026-11-27T10:00:00+05:30", "direction": "LONG",
             "spot_stop": 95.0, "spot_target": 110.0}
    bars = pd.DataFrame([{"timestamp": pd.Timestamp("2026-11-27 10:00", tz=IST),
                          "open": 100, "high": 112, "low": 94, "close": 105}])
    assert TradingEngine._first_spot_exit_trigger(trade, bars)[0] == "spot_stop"


def test_risk_size_reports_full_premium_as_maximum_loss():
    sized = size_long_option(premium=50, lot_size=25,
                             cfg=cfg(risk_per_trade_inr=1000, max_position_value_inr=15000,
                                     max_lots=1, premium_stop_loss_pct=0.35))
    assert sized.allowed
    assert sized.estimated_risk == pytest.approx(437.5)
    assert sized.maximum_premium_loss == pytest.approx(1250)


def test_live_approval_is_atomically_claimed_once(tmp_path, monkeypatch):
    import nifty_trader.risk as risk
    monkeypatch.setattr(risk, "market_is_open", lambda now=None: True)
    store = Store(tmp_path / "atomic-approval.sqlite")
    c = cfg(live_is_armed=True, risk_per_trade_inr=2000)
    sig = {"signal_id": "atomic", "direction": "LONG", "spot_entry": 25000,
           "spot_stop": 24950, "spot_target": 25100}
    opt = {"instrument_key": "NSE_FO|atomic", "expiry": "2026-12-03", "ask": 100,
           "bid": 99, "lot_size": 25}
    staged = stage_live_order(sig, opt, 25, c, store)
    ref = staged["order"]["order_ref"]
    assert store.claim_order_for_submission(ref, datetime.now(IST).isoformat()) is True
    assert store.claim_order_for_submission(ref, datetime.now(IST).isoformat()) is False


def test_backtest_flattens_open_spot_trade_at_1520(tmp_path, monkeypatch):
    import nifty_trader.backtest as bt
    ts = pd.date_range("2026-11-27 09:15", periods=75, freq="5min", tz=IST)
    candles = pd.DataFrame({"timestamp": ts, "open": 100.0, "high": 101.0,
                            "low": 99.0, "close": 100.0})
    # Trigger one deterministic synthetic signal at the first eligible prefix.
    def fake_eval(hist, levels, config):
        if len(hist) == 21:
            return ([{"signal_id": "time-exit", "direction": "LONG", "entry_mode": "IMMEDIATE",
                      "level_name": "R1", "level_price": 98.0, "spot_stop": 95.0,
                      "spot_target": 110.0, "reward_risk": 2.0}], hist, {})
        return [], hist, {}
    monkeypatch.setattr(bt, "evaluate_signals", fake_eval)
    c = cfg(pivot_left=2, pivot_right=2, last_entry_time="15:00", max_trades_per_day=2)
    trades, _ = bt.backtest_spot(candles, pd.DataFrame(), c,
                                 manual_levels=[{"name": "R1", "price": 98.0, "side": "RESISTANCE"}])
    assert len(trades) == 1
    assert trades.iloc[0].exit_reason == "TIME_EXIT"
    assert trades.iloc[0].exit_timestamp.time() == datetime.strptime("15:15", "%H:%M").time()


def test_backtest_uses_configured_daily_trade_limit_by_default(monkeypatch):
    import nifty_trader.backtest as bt
    ts = pd.date_range("2026-11-27 09:15", periods=75, freq="5min", tz=IST)
    candles = pd.DataFrame({"timestamp": ts, "open": 100.0, "high": 101.0,
                            "low": 99.0, "close": 100.0})
    def fake_eval(hist, levels, config):
        if len(hist) in {21, 31}:
            sid = f"signal-{len(hist)}"
            return ([{"signal_id": sid, "direction": "LONG", "entry_mode": "IMMEDIATE",
                      "level_name": "R1", "level_price": 98.0, "spot_stop": 95.0,
                      "spot_target": 110.0, "reward_risk": 2.0}], hist, {})
        return [], hist, {}
    monkeypatch.setattr(bt, "evaluate_signals", fake_eval)
    candles.loc[23, "low"] = 94.0  # stop the first trade before the second setup
    c = cfg(pivot_left=2, pivot_right=2, last_entry_time="15:00", max_trades_per_day=1)
    trades, _ = bt.backtest_spot(candles, pd.DataFrame(), c,
                                 manual_levels=[{"name": "R1", "price": 98.0, "side": "RESISTANCE"}])
    assert len(trades) == 1


def _engine_cfg(tmp_path, **overrides):
    values = dict(
        db_path=str(tmp_path / "engine.sqlite3"), data_dir=str(tmp_path / "data"),
        instrument_key="NSE_INDEX|Nifty 50", market_data_token="", trading_token="",
        base_url="https://api.upstox.com", order_base_url="https://api-hft.upstox.com",
        pivot_left=3, pivot_right=3, rsi_period=14,
        sma_filter_enabled=True, fvg_filter_enabled=False, rsi_divergence_required=False,
        immediate_entry_enabled=True, retest_entry_enabled=True, retest_bars=3,
        breakout_buffer_points=2.0, retest_tolerance_points=8.0,
        atr_stop_multiplier=1.0, min_reward_risk=1.0,
        stale_data_seconds=180, max_signal_age_seconds=180, last_entry_time="15:00",
        max_trades_per_day=2, max_daily_loss_inr=2000,
        risk_per_trade_inr=2000, max_lots=1, max_position_value_inr=15000,
        premium_stop_loss_pct=0.35, slippage_points=0.5,
        min_premium_inr=30, max_premium_inr=500, max_spread_pct=0.08,
        min_option_volume=1000, min_option_oi=10000, option_moneyness_pct=0.01,
        estimated_round_trip_cost_per_lot=100, max_approval_price_drift_pct=0.02,
        live_is_armed=False, live_product="I",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _engine_market(now):
    last_start = pd.Timestamp(now).floor("5min") - pd.Timedelta(minutes=5)
    stamps = pd.date_range(end=last_start, periods=21, freq="5min")
    rows = []
    for i, stamp in enumerate(stamps):
        close = 25000.0
        high, low = 25005.0, 24995.0
        if i == len(stamps) - 1:
            close, high, low = 25010.0, 25020.0, 25000.0
        rows.append({"timestamp": stamp, "open": 25000.0, "high": high, "low": low,
                     "close": close, "volume": 10000, "oi": 0})
    candles = pd.DataFrame(rows)
    yesterday = (pd.Timestamp(now).normalize() - pd.Timedelta(days=1)).tz_convert(IST)
    daily = pd.DataFrame([{"timestamp": yesterday, "open": 24900.0, "high": 25200.0,
                           "low": 24800.0, "close": 24950.0, "volume": 100000, "oi": 0}])
    return candles, daily


class _FakeEngineClient:
    def __init__(self, candles, daily):
        self.candles = candles
        self.daily = daily

    def intraday_candles(self, instrument_key, interval):
        assert instrument_key == "NSE_INDEX|Nifty 50" and interval == 5
        return self.candles.copy()

    def historical_candles(self, instrument_key, start, end, unit, interval):
        return self.daily.copy()

    def option_contracts(self, instrument_key):
        return [
            {"expiry": "2026-12-01", "instrument_key": "CE-25000", "lot_size": 25},
            {"expiry": "2026-12-01", "instrument_key": "PE-25000", "lot_size": 25},
        ]

    def option_chain(self, instrument_key, expiry):
        return [{
            "expiry": expiry, "strike_price": 25000, "underlying_spot_price": 25010,
            "call_options": {"instrument_key": "CE-25000", "market_data": {
                "ltp": 100, "volume": 5000, "oi": 50000, "prev_oi": 40000,
                "bid_price": 99, "ask_price": 101}},
            "put_options": {"instrument_key": "PE-25000", "market_data": {
                "ltp": 100, "volume": 5000, "oi": 50000, "prev_oi": 40000,
                "bid_price": 99, "ask_price": 101}},
        }]


def test_full_engine_pipeline_opens_one_paper_trade_and_persists_diagnostics(tmp_path, monkeypatch):
    from nifty_trader.engine import TradingEngine
    import nifty_trader.risk as risk

    monkeypatch.setattr(risk, "market_is_open", lambda now=None: True)
    # Anchor the synthetic latest completed bar just 10 seconds behind now;
    # a test running mid-bar must not expect a stale signal to be traded.
    now = pd.Timestamp.now(tz=IST).floor("5min") + pd.Timedelta(seconds=10)
    candles, daily = _engine_market(now)
    c = _engine_cfg(tmp_path)
    engine = TradingEngine(c, client=_FakeEngineClient(candles, daily))
    session = now.date().isoformat()
    engine.store.add_manual_level(session, "Desk R1", 25000, "RESISTANCE")
    engine.store.add_manual_level(session, "Desk R2", 25100, "RESISTANCE")

    first = engine.run_once(now=now.to_pydatetime(), execute_paper=True)
    assert first["indicator_context"]["data_quality"] == "OK"
    assert first["option_summary"]["call_intraday_change_oi"] is None
    assert any(s["status"] == "READY" for s in first["signals"])
    assert len(engine.store.open_trades("PAPER")) == 1
    assert len(engine.store.recent_diagnostics()) > 0

    # Same completed candle cannot create a duplicate position on the next poll.
    second = engine.run_once(now=(now + pd.Timedelta(seconds=60)).to_pydatetime(), execute_paper=True)
    assert len(engine.store.trades()) == 1
    assert len(engine.store.open_trades("PAPER")) == 1
    assert second["option_summary"]["call_intraday_change_oi"] == 0

    # Streamlit can load the daemon's persisted snapshot without another API call.
    loaded = engine.load_last_snapshot(now=(now + pd.Timedelta(seconds=60)).to_pydatetime())
    assert loaded is not None
    assert loaded["snapshot_source"] == "persisted"
    assert loaded["spot"] == 25010.0
    assert not loaded["options"].empty


def test_paper_position_stop_is_not_silently_ignored_without_bid(tmp_path):
    from nifty_trader.engine import TradingEngine

    now = pd.Timestamp.now(tz=IST).floor("5min")
    candles = pd.DataFrame([{
        "timestamp": now - pd.Timedelta(minutes=5), "open": 25000.0,
        "high": 25002.0, "low": 24980.0, "close": 24985.0,
        "volume": 100, "oi": 0,
    }])
    c = _engine_cfg(tmp_path)
    engine = TradingEngine(c, client=object())
    opened = (now - pd.Timedelta(minutes=15)).isoformat()
    engine.store.save_trade({
        "trade_id": "paper-no-bid", "signal_id": "s-no-bid", "mode": "PAPER",
        "direction": "LONG", "instrument_key": "CE-25000", "expiry": "2026-12-01",
        "entry_price": 100.0, "quantity": 25, "lot_size": 25, "spot_entry": 25000.0,
        "spot_stop": 24990.0, "spot_target": 25100.0, "premium_stop": 65.0,
        "status": "OPEN", "opened_at": opened,
    })
    events = engine._manage_paper_positions(candles, pd.DataFrame(), now)
    assert events[0]["type"] == "PAPER_EXIT_BLOCKED_NO_BID"
    assert len(engine.store.open_trades("PAPER")) == 1


def test_paper_position_overnight_safety_exit_uses_available_bid(tmp_path):
    from nifty_trader.engine import TradingEngine

    now = pd.Timestamp.now(tz=IST).normalize() + pd.Timedelta(hours=10)
    c = _engine_cfg(tmp_path)
    engine = TradingEngine(c, client=object())
    opened = (now - pd.Timedelta(days=1)).isoformat()
    engine.store.save_trade({
        "trade_id": "paper-overnight", "signal_id": "s-overnight", "mode": "PAPER",
        "direction": "LONG", "instrument_key": "CE-25000", "expiry": "2026-12-01",
        "entry_price": 100.0, "quantity": 25, "lot_size": 25, "spot_entry": 25000.0,
        "spot_stop": 24900.0, "spot_target": 25100.0, "premium_stop": 65.0,
        "status": "OPEN", "opened_at": opened,
    })
    quote = pd.DataFrame([{"instrument_key": "CE-25000", "bid": 80.0}])
    events = engine._manage_paper_positions(pd.DataFrame(), quote, now)
    assert events[0]["reason"] == "overnight_safety_exit"
    assert engine.store.open_trades("PAPER").empty



def test_option_chain_persists_poll_and_session_deltas_without_duplicate_quotes(tmp_path):
    from nifty_trader.option_chain import attach_snapshot_change, chain_summary

    store = Store(tmp_path / "oi_history.sqlite")
    first = pd.DataFrame([{
        "timestamp": "2026-11-27T09:30:00+05:30", "expiry": "2026-12-01",
        "instrument_key": "CE1", "option_type": "CE", "strike": 25000,
        "ltp": 100, "volume": 1000, "oi": 10000, "prev_oi": 9000,
        "day_change_oi": 1000, "bid": 99, "ask": 101,
    }])
    store.save_option_snapshot(first)
    prev = store.previous_option_snapshot(["CE1"], "2026-11-27")
    base = store.first_option_snapshot(["CE1"], "2026-11-27")
    assert prev.iloc[0].oi == 10000
    assert base.iloc[0].oi == 10000

    second = first.copy()
    second["timestamp"] = "2026-11-27T09:31:00+05:30"
    second["oi"] = 10250
    second["day_change_oi"] = 1250
    current = attach_snapshot_change(second, prev, base)
    assert current.iloc[0].intraday_change_oi == 250
    assert current.iloc[0].session_change_oi == 250
    assert current.iloc[0].day_change_oi == 1250
    store.save_option_snapshot(current)

    summary = chain_summary(current)
    assert summary["call_intraday_change_oi"] == 250
    assert summary["call_session_change_oi"] == 250

    # Rewriting the same timestamp/instrument updates the row instead of
    # creating two possible prior snapshots with non-deterministic order.
    replacement = current.copy()
    replacement["oi"] = 10275
    store.save_option_snapshot(replacement)
    with store.connect() as con:
        count = con.execute("SELECT COUNT(*) n FROM option_snapshots WHERE instrument_key='CE1'").fetchone()["n"]
    assert count == 2
    latest = store.latest_option_snapshots("2026-11-27")
    assert float(latest.iloc[0].oi) == 10275


def test_atm_plus_minus_ten_chain_view_has_spread_volume_oi_and_atm_marker():
    from nifty_trader.option_chain import atm_option_chain_window
    records = []
    for strike in range(24900, 25101, 10):
        for kind, prefix in (("CE", "C"), ("PE", "P")):
            records.append({
                "timestamp": "2026-11-27T10:00:00+05:30", "expiry": "2026-12-01",
                "instrument_key": f"{prefix}{strike}", "option_type": kind,
                "strike": strike, "spot": 25000, "ltp": 100, "bid": 99,
                "ask": 101, "bid_qty": 50, "ask_qty": 60, "volume": 5000,
                "oi": 50000, "day_change_oi": 1000, "intraday_change_oi": 100,
                "session_change_oi": 250, "iv": 12.5, "delta": 0.5,
            })
    window = atm_option_chain_window(pd.DataFrame(records), 25000, 10, cfg())
    assert len(window) == 21
    atm = window[window.ATM]
    assert len(atm) == 1
    assert atm.iloc[0].strike == 25000
    assert "CE_spread_pct" in window.columns
    assert "PE_volume" in window.columns
    assert "CE_session_change_oi" in window.columns
    assert bool(atm.iloc[0].CE_liquidity_eligible) is True


def test_option_quality_reports_spread_volume_oi_and_quote_failures():
    from nifty_trader.option_chain import add_option_quality_metrics
    frame = pd.DataFrame([
        {"option_type": "CE", "strike": 25000, "spot": 25000, "ltp": 100,
         "bid": 99, "ask": 101, "volume": 5000, "oi": 50000},
        {"option_type": "PE", "strike": 25000, "spot": 25000, "ltp": 100,
         "bid": 70, "ask": 100, "volume": 10, "oi": 20},
    ])
    out = add_option_quality_metrics(frame, cfg())
    assert bool(out.iloc[0].liquidity_eligible)
    assert not bool(out.iloc[1].liquidity_eligible)
    assert "spread filter" in out.iloc[1].liquidity_status
    assert "volume filter" in out.iloc[1].liquidity_status
    assert out.iloc[0].spread == 2
    assert out.iloc[0].spread_pct == pytest.approx(2 / 101)


def test_signal_diagnostics_explain_every_gate_and_configurable_rsi_requirement():
    from nifty_trader.strategy import evaluate_signals
    candles = signal_candles()
    levels = pd.DataFrame([
        {"name": "R1", "price": 100.0, "side": "RESISTANCE"},
        {"name": "R2", "price": 110.0, "side": "RESISTANCE"},
    ])
    signals, _, context = evaluate_signals(
        candles, levels,
        cfg(sma_filter_enabled=True, fvg_filter_enabled=True, rsi_divergence_required=True),
    )
    assert signals == []
    diag = pd.DataFrame(context["diagnostics"])
    breakout = diag[(diag.level == "R1") & (diag.direction == "LONG")].iloc[0]
    assert breakout.status == "FILTERED"
    assert bool(breakout.breakout_pass)
    assert breakout.sma_status == "PASS"
    assert breakout.fvg_status == "FAIL"
    assert breakout.rsi_status == "FAIL"
    assert "FVG confirmation failed" in breakout.reason
    assert "RSI divergence gate failed" in breakout.reason


def test_runtime_rsi_gate_is_loaded_by_background_engine(tmp_path):
    from nifty_trader.engine import TradingEngine
    c = _engine_cfg(tmp_path)
    engine = TradingEngine(c, client=object())
    assert engine.strategy_settings().rsi_divergence_required is False
    engine.store.set_state("rsi_divergence_required", "1")
    assert engine.strategy_settings().rsi_divergence_required is True
    engine.store.set_state("rsi_divergence_required", "0")
    assert engine.strategy_settings().rsi_divergence_required is False


def test_opening_range_requires_exact_first_three_five_minute_bars():
    # Three rows exist in the opening window, but 09:20 is missing and one row
    # is off-grid; the old count-only implementation would accept this range.
    base = pd.Timestamp("2026-11-27 09:15", tz=IST)
    stamps = [base, base + pd.Timedelta(minutes=10), base + pd.Timedelta(minutes=12)]
    intraday = pd.DataFrame([
        {"timestamp": stamp, "open": 100, "high": 110 + i, "low": 90 - i,
         "close": 100, "volume": 1, "oi": 0}
        for i, stamp in enumerate(stamps)
    ])
    levels = build_levels(intraday, pd.DataFrame())
    assert "ORH" not in set(levels.name)
    assert "ORL" not in set(levels.name)


def test_swing_pivot_is_not_created_across_missing_five_minute_bar():
    base = pd.Timestamp("2026-11-27 09:15", tz=IST)
    # Candidate high at 09:25 would be a pivot if the 09:30 bar existed.
    # The gap at 09:30 invalidates the local pivot window.
    stamps = [base + pd.Timedelta(minutes=x) for x in (0, 5, 10, 20, 25, 30)]
    highs = [100, 102, 120, 101, 99, 98]
    lows = [95, 96, 97, 96, 95, 94]
    intraday = pd.DataFrame([
        {"timestamp": stamp, "open": 99, "high": highs[i], "low": lows[i],
         "close": 99, "volume": 1, "oi": 0}
        for i, stamp in enumerate(stamps)
    ])
    levels = build_levels(intraday, pd.DataFrame(), pivot_left=1, pivot_right=1)
    assert not ((levels.source == "SWING") & (levels.timestamp == stamps[2])).any()


def test_option_snapshot_json_is_strict_and_nonfinite_values_become_null(tmp_path):
    import json
    store = Store(tmp_path / "strict_json.sqlite")
    frame = pd.DataFrame([{
        "timestamp": "2026-11-27T09:30:00+05:30", "expiry": "2026-12-01",
        "instrument_key": "CE1", "option_type": "CE", "strike": 25000,
        "ltp": 100.0, "volume": 100, "oi": 1000, "bid": 99.0, "ask": 101.0,
        "intraday_change_oi": float("nan"), "session_change_oi": float("nan"),
    }])
    store.save_option_snapshot(frame)
    snapshot = store.latest_option_snapshots("2026-11-27")
    assert pd.isna(snapshot.iloc[0].intraday_change_oi)
    with store.connect() as con:
        raw = con.execute("SELECT payload FROM option_snapshots WHERE instrument_key='CE1'").fetchone()[0]
    assert '"intraday_change_oi":null' in raw
    json.loads(raw, parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))


def test_option_chain_expiry_failure_does_not_discard_other_expiries(tmp_path):
    from nifty_trader.engine import TradingEngine

    class PartialChainClient(_FakeEngineClient):
        def option_contracts(self, instrument_key):
            return [
                {"expiry": "2026-12-01", "instrument_key": "CE-25000", "lot_size": 25},
                {"expiry": "2026-12-08", "instrument_key": "CE-25100", "lot_size": 25},
            ]

        def option_chain(self, instrument_key, expiry):
            if expiry == "2026-12-01":
                raise RuntimeError("simulated expiry-specific timeout")
            return [{
                "expiry": expiry, "strike_price": 25000, "underlying_spot_price": 25000,
                "call_options": {"instrument_key": "CE-25100", "market_data": {
                    "ltp": 100, "volume": 5000, "oi": 50000, "prev_oi": 40000,
                    "bid_price": 99, "ask_price": 101}},
                "put_options": {"instrument_key": "PE-25100", "market_data": {
                    "ltp": 100, "volume": 5000, "oi": 50000, "prev_oi": 40000,
                    "bid_price": 99, "ask_price": 101}},
            }]

    now = pd.Timestamp("2026-11-27 10:00:00", tz=IST)
    engine = TradingEngine(_engine_cfg(tmp_path), client=PartialChainClient(*_engine_market(now)))
    chains, all_options = engine._collect_option_chains(25000, now)
    assert list(chains) == ["2026-12-08"]
    assert not all_options.empty
    assert len(engine._option_errors) == 1
    assert "simulated expiry-specific timeout" in engine._option_errors[0]["error"]


def test_chart_view_uses_all_level_categories_and_named_labels():
    # Contract test for the presentation rules maintained in Streamlit source.
    from pathlib import Path
    source = Path("streamlit_app.py").read_text(encoding="utf-8")
    assert '"SUPPORT": "#16a34a"' in source
    assert '"RESISTANCE": "#dc2626"' in source
    assert '"REFERENCE": "#6b7280"' in source
    assert "text=f\"{level['name']} · {price:,.2f}\"" in source
    assert 'st.set_page_config(page_title="NIFTY Level Trader", page_icon="📈", layout="wide")' in source


def test_operational_events_persist_and_are_retrievable(tmp_path):
    store = Store(tmp_path / "events.sqlite")
    store.save_event({"type": "PAPER_EXIT_BLOCKED_NO_BID", "trade_id": "t1",
                      "trigger": "spot_stop", "reason": "bid was missing"},
                     event_timestamp="2026-11-27T10:00:00+05:30")
    events = store.recent_events()
    assert len(events) == 1
    assert events.iloc[0].event_type == "PAPER_EXIT_BLOCKED_NO_BID"
    assert events.iloc[0].trade_id == "t1"
    assert '"trigger":"spot_stop"' in events.iloc[0].payload


def test_weekly_review_export_contains_operational_events(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    store = Store(tmp_path / "review.sqlite3")
    store.save_event({"type": "ENGINE_ERROR", "error": "synthetic"},
                     event_timestamp="2026-11-27T10:00:00+05:30")
    output = tmp_path / "review"
    subprocess.run([
        sys.executable, "scripts/export_paper_review.py", "--db", str(store.path),
        "--output", str(output),
    ], check=True, capture_output=True, text=True, cwd=Path.cwd(),
       env={**os.environ, "PYTHONPATH": str(Path.cwd())})
    events = pd.read_csv(output / "engine_events.csv")
    summary = json.loads((output / "summary.json").read_text())
    assert len(events) == 1
    assert events.iloc[0].event_type == "ENGINE_ERROR"
    assert summary["engine_event_counts"]["ENGINE_ERROR"] == 1


def test_chain_summary_reports_delta_oi_snapshot_coverage():
    from nifty_trader.option_chain import chain_summary
    frame = pd.DataFrame([
        {"option_type": "CE", "strike": 25000, "oi": 1000,
         "intraday_change_oi": 100, "session_change_oi": 100},
        {"option_type": "CE", "strike": 25100, "oi": 2000,
         "intraday_change_oi": float("nan"), "session_change_oi": 50},
        {"option_type": "PE", "strike": 25000, "oi": 1500,
         "intraday_change_oi": 50, "session_change_oi": float("nan")},
    ])
    summary = chain_summary(frame)
    assert summary["call_intraday_coverage"] == 50.0
    assert summary["put_intraday_coverage"] == 100.0
    assert summary["call_session_coverage"] == 100.0
    assert summary["put_session_coverage"] == 0.0


def test_replaying_trade_insert_does_not_reset_closed_trade(tmp_path):
    store = Store(tmp_path / "trade_idempotency.sqlite")
    base = {
        "trade_id": "trade-1", "signal_id": "signal-1", "mode": "PAPER",
        "direction": "LONG", "instrument_key": "CE1", "expiry": "2026-12-01",
        "entry_price": 100, "quantity": 25, "lot_size": 25,
        "spot_entry": 25000, "spot_stop": 24900, "spot_target": 25200,
        "premium_stop": 65, "status": "OPEN", "opened_at": "2026-11-27T10:00:00+05:30",
    }
    assert store.save_trade(base) is True
    store.update_trade("trade-1", status="CLOSED", exit_price=120,
                       exited_at="2026-11-27T10:30:00+05:30", exit_reason="target", net_pnl=400)
    assert store.save_trade({**base, "status": "OPEN", "entry_price": 99}) is False
    saved = store.trades().iloc[0]
    assert saved.status == "CLOSED"
    assert saved.entry_price == 100
    assert saved.net_pnl == 400


def test_live_approval_parses_upstox_v3_order_ids_list(tmp_path, monkeypatch):
    store = Store(tmp_path / "live-v3.sqlite")
    sig = {"signal_id": "sig-v3", "direction": "LONG", "spot_entry": 25000,
           "spot_stop": 24950, "spot_target": 25100}
    option = {"instrument_key": "NSE_FO|V3", "expiry": "2026-12-03", "ask": 100,
              "bid": 99, "lot_size": 25}
    settings = cfg(live_is_armed=True, risk_per_trade_inr=2000)
    staged = stage_live_order(sig, option, 25, settings, store)

    class V3Client:
        def place_limit_order(self, **kwargs):
            return {"status": "success", "data": {"order_ids": ["v3-order-123"]}}

    import nifty_trader.risk as risk
    monkeypatch.setattr(risk, "market_is_open", lambda now=None: True)
    submitted = approve_and_submit(
        staged["order"]["order_ref"], staged["order"]["approval_text"],
        cfg=settings, store=store, client=V3Client())
    assert submitted["status"] == "SUBMITTED"
    assert submitted["broker_order_id"] == "v3-order-123"
    with store.connect() as con:
        row = con.execute("SELECT * FROM orders WHERE order_ref=?", (staged["order"]["order_ref"],)).fetchone()
    assert row["status"] == "SUBMITTED"
    assert row["broker_order_id"] == "v3-order-123"


def test_multiple_upstox_v3_order_ids_lock_submission_as_unknown(tmp_path, monkeypatch):
    store = Store(tmp_path / "live-v3-multi.sqlite")
    sig = {"signal_id": "sig-v3-multi", "direction": "LONG", "spot_entry": 25000,
           "spot_stop": 24950, "spot_target": 25100}
    option = {"instrument_key": "NSE_FO|V3M", "expiry": "2026-12-03", "ask": 100,
              "bid": 99, "lot_size": 25}
    settings = cfg(live_is_armed=True, risk_per_trade_inr=2000)
    staged = stage_live_order(sig, option, 25, settings, store)

    class SlicedClient:
        def place_limit_order(self, **kwargs):
            return {"status": "success", "data": {"order_ids": ["child-1", "child-2"]}}

    import nifty_trader.risk as risk
    monkeypatch.setattr(risk, "market_is_open", lambda now=None: True)
    with pytest.raises(RuntimeError, match="2 child order IDs"):
        approve_and_submit(
            staged["order"]["order_ref"], staged["order"]["approval_text"],
            cfg=settings, store=store, client=SlicedClient())
    with store.connect() as con:
        row = con.execute("SELECT * FROM orders WHERE order_ref=?", (staged["order"]["order_ref"],)).fetchone()
    assert row["status"] == "SUBMISSION_UNKNOWN"
    assert row["broker_order_id"] is None
    import json
    payload = json.loads(row["payload"])
    assert payload["broker_order_ids"] == ["child-1", "child-2"]


def test_upstox_cancel_uses_v3_hft_endpoint():
    class Response:
        ok = True
        status_code = 200
        text = ""
        def json(self):
            return {"status": "success", "data": {"order_id": "cancel-me"}}

    class Session:
        def __init__(self):
            self.calls = []
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return Response()

    from nifty_trader.upstox import UpstoxClient
    session = Session()
    client = UpstoxClient("analytics", "access", session=session)
    result = client.cancel_order("cancel-me")
    assert result["data"]["order_id"] == "cancel-me"
    method, url, kwargs = session.calls[0]
    assert method == "DELETE"
    assert url == "https://api-hft.upstox.com/v3/order/cancel"
    assert kwargs["params"] == {"order_id": "cancel-me"}
    assert kwargs["headers"]["Authorization"] == "Bearer access"



def _otm_band_fixture():
    rows = []
    for option_type, strikes in (
        ("CE", [25700, 25849, 25850, 25875, 25900, 25901]),
        ("PE", [25700, 25551, 25550, 25525, 25500, 25499]),
    ):
        for index, strike in enumerate(strikes):
            prefix = "CE" if option_type == "CE" else "PE"
            rows.append({
                "timestamp": "2026-11-27T10:00:00+05:30",
                "expiry": "2026-12-03",
                "instrument_key": f"{prefix}-{strike}",
                "option_type": option_type,
                "strike": strike,
                "spot": 25700.0,
                "ltp": 100.0,
                "bid": 99.0,
                "ask": 101.0,
                "bid_qty": 100 + index,
                "ask_qty": 100 + index,
                "volume": 5000,
                "oi": 50000,
                "delta": 0.25 if option_type == "CE" else -0.25,
                "iv": 14.0,
                "theta": -4.5,
                "gamma": 0.001,
                "vega": 8.0,
            })
    return pd.DataFrame(rows)


def test_directional_otm_band_includes_only_150_to_200_points():
    from nifty_trader.option_chain import add_option_quality_metrics
    frame = _otm_band_fixture()
    settings = cfg(option_selection_mode="OTM_BAND",
                   option_otm_min_points=150, option_otm_max_points=200)
    out = add_option_quality_metrics(frame, settings)

    ce = out[out.option_type == "CE"].set_index("strike")
    pe = out[out.option_type == "PE"].set_index("strike")

    assert bool(ce.loc[25850, "liquidity_eligible"])
    assert bool(ce.loc[25875, "liquidity_eligible"])
    assert bool(ce.loc[25900, "liquidity_eligible"])
    assert not bool(ce.loc[25849, "liquidity_eligible"])
    assert not bool(ce.loc[25901, "liquidity_eligible"])
    assert not bool(ce.loc[25700, "liquidity_eligible"])

    assert bool(pe.loc[25550, "liquidity_eligible"])
    assert bool(pe.loc[25525, "liquidity_eligible"])
    assert bool(pe.loc[25500, "liquidity_eligible"])
    assert not bool(pe.loc[25551, "liquidity_eligible"])
    assert not bool(pe.loc[25499, "liquidity_eligible"])
    assert not bool(pe.loc[25700, "liquidity_eligible"])

    assert ce.loc[25850, "otm_distance_points"] == 150
    assert pe.loc[25500, "otm_distance_points"] == 200


def test_expiry_selector_never_falls_back_to_atm_when_otm_band_empty():
    settings = cfg(option_selection_mode="OTM_BAND",
                   option_otm_min_points=150, option_otm_max_points=200)
    frame = _otm_band_fixture()
    only_near_atm = frame[frame.strike.isin([25700, 25750, 25650])].copy()
    selected = choose_expiry(
        {"2026-12-03": only_near_atm}, 25700, "LONG", settings,
        as_of=pd.Timestamp("2026-11-27 10:00:00", tz=IST),
    )
    assert selected is None


def test_expiry_selector_returns_contract_in_correct_directional_otm_band():
    settings = cfg(option_selection_mode="OTM_BAND",
                   option_otm_min_points=150, option_otm_max_points=200)
    frame = _otm_band_fixture()
    chains = {"2026-12-03": frame}
    as_of = pd.Timestamp("2026-11-27 10:00:00", tz=IST)

    call = choose_expiry(chains, 25700, "LONG", settings, as_of=as_of)
    put = choose_expiry(chains, 25700, "SHORT", settings, as_of=as_of)

    assert call is not None and call["option_type"] == "CE"
    assert 150 <= call["otm_distance_points"] <= 200
    assert put is not None and put["option_type"] == "PE"
    assert 150 <= put["otm_distance_points"] <= 200
    assert call["delta"] > 0
    assert put["delta"] < 0


def test_otm_band_invalid_configuration_fails_closed():
    from nifty_trader.option_chain import add_option_quality_metrics
    frame = _otm_band_fixture()
    settings = cfg(option_selection_mode="OTM_BAND",
                   option_otm_min_points=220, option_otm_max_points=150)
    with pytest.raises(ValueError, match="Invalid OTM band"):
        add_option_quality_metrics(frame, settings)


def test_normalize_option_chain_keeps_full_greeks_for_selected_contract():
    payload = [{
        "expiry": "2026-12-03",
        "strike_price": 25850,
        "underlying_spot_price": 25700,
        "call_options": {
            "instrument_key": "CE-25850",
            "market_data": {"ltp": 100, "volume": 5000, "oi": 50000,
                            "bid_price": 99, "ask_price": 101},
            "option_greeks": {"delta": 0.25, "iv": 14, "theta": -4.5,
                              "gamma": 0.001, "vega": 8.0},
        },
        "put_options": {},
    }]
    normalized = normalize_option_chain(payload)
    row = normalized.iloc[0]
    assert row.delta == pytest.approx(0.25)
    assert row.iv == pytest.approx(14)
    assert row.theta == pytest.approx(-4.5)
    assert row.gamma == pytest.approx(0.001)
    assert row.vega == pytest.approx(8.0)


def test_market_clock_displays_completed_bar_window_and_next_close():
    from nifty_trader.market_clock import candle_window, next_candle_close, candle_countdown
    start, end = candle_window("2026-11-27T14:05:00+05:30")
    assert start.strftime("%H:%M") == "14:05"
    assert end.strftime("%H:%M") == "14:10"

    now = pd.Timestamp("2026-11-27 14:11:00", tz=IST)
    assert next_candle_close(now).strftime("%H:%M") == "14:15"
    assert candle_countdown(now) == "14:15 IST (4m 00s)"


def test_market_clock_advances_when_now_is_exactly_on_candle_boundary():
    from nifty_trader.market_clock import next_candle_close
    now = pd.Timestamp("2026-11-27 14:15:00", tz=IST)
    assert next_candle_close(now).strftime("%H:%M") == "14:20"


def test_streamlit_displays_refresh_time_candle_window_and_contract_distance():
    from pathlib import Path
    source = Path("streamlit_app.py").read_text(encoding="utf-8")
    assert "Last successful refresh:" in source
    assert "Next expected 5-minute candle close:" in source
    assert 'candle_window(last["timestamp"], 5)' in source
    assert "contract_otm_distance_points" in source
    assert "Eligible OTM contracts" in source
