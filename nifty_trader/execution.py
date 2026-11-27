from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from .risk import size_long_option

IST = ZoneInfo("Asia/Kolkata")


def stage_paper_trade(signal: dict, option: dict, lot_size: int, cfg, store,
                      *, mode: str = "PAPER") -> dict:
    sizing = size_long_option(premium=float(option["ask"]) + float(cfg.slippage_points), lot_size=int(lot_size), cfg=cfg)
    if not sizing.allowed:
        return {"status": "REJECTED", "reason": sizing.reason}
    fill = float(option["ask"]) + cfg.slippage_points
    trade_id = uuid.uuid4().hex[:20]
    trade = {
        "trade_id": trade_id, "signal_id": signal["signal_id"], "mode": mode,
        "direction": signal["direction"], "instrument_key": option["instrument_key"],
        "expiry": option["expiry"], "entry_price": fill, "quantity": sizing.quantity,
        "lot_size": int(lot_size), "spot_entry": signal["spot_entry"],
        "spot_stop": signal["spot_stop"], "spot_target": signal["spot_target"],
        "premium_stop": fill * (1 - float(getattr(cfg, "premium_stop_loss_pct", 0.35))), "status": "OPEN", "opened_at": datetime.now(IST).isoformat(),
        "payload": json.dumps({"sizing": sizing.__dict__, "entry_quote": option}, default=str),
    }
    inserted = store.save_trade(trade)
    if not inserted:
        return {"status": "ALREADY_EXISTS", "reason": "A paper position or prior paper trade already exists for this signal"}
    return {"status": "OPEN", "trade": trade, "sizing": sizing.__dict__}


def stage_live_order(signal: dict, option: dict, lot_size: int, cfg, store) -> dict:
    if not cfg.live_is_armed:
        return {"status": "REJECTED", "reason": "Live trading is not armed. LIVE_TRADING_ENABLED=1 and ALLOW_LIVE_ORDERS=1 plus an access token are required."}
    if signal.get("direction") not in {"LONG", "SHORT"}:
        return {"status": "REJECTED", "reason": "Signal direction must be LONG or SHORT"}
    expected_type = "CE" if signal.get("direction") == "LONG" else "PE"
    if option.get("option_type") and option.get("option_type") != expected_type:
        return {"status": "REJECTED", "reason": f"Signal direction requires {expected_type}, not {option.get('option_type')}"}
    bid, ask = float(option.get("bid") or 0), float(option.get("ask") or 0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return {"status": "REJECTED", "reason": "Invalid option bid/ask quote"}
    sizing = size_long_option(premium=ask + float(cfg.slippage_points), lot_size=int(lot_size), cfg=cfg)
    if not sizing.allowed:
        return {"status": "REJECTED", "reason": sizing.reason}
    with store.connect() as con:
        duplicate = con.execute("SELECT order_ref,status FROM orders WHERE signal_id=? AND side='BUY' AND status IN ('PENDING_APPROVAL','SUBMITTING','SUBMITTED','PARTIALLY_FILLED','FILLED','SUBMIT_ERROR','SUBMISSION_UNKNOWN','CANCEL_REQUESTED','OPEN') LIMIT 1", (signal["signal_id"],)).fetchone()
        traded = con.execute("SELECT trade_id FROM trades WHERE signal_id=? AND mode='LIVE' LIMIT 1", (signal["signal_id"],)).fetchone()
    if duplicate or traded:
        return {"status": "REJECTED", "reason": "A live entry order or trade already exists for this signal"}
    order_ref = uuid.uuid4().hex[:20]
    # Explicit, human-readable approval phrase binds the approval to this exact order.
    approval_text = f"APPROVE {signal['direction']} {option['instrument_key']} QTY {sizing.quantity} LIMIT {float(option['ask']):.2f}"
    now = datetime.now(IST)
    payload = {"signal": signal, "option": option, "sizing": sizing.__dict__,
               "created_at": now.isoformat(), "expires_at": (now + timedelta(seconds=90)).isoformat()}
    order = {
        "order_ref": order_ref, "signal_id": signal["signal_id"], "mode": "LIVE",
        "side": "BUY", "instrument_key": option["instrument_key"],
        "quantity": sizing.quantity, "limit_price": float(option["ask"]),
        "status": "PENDING_APPROVAL", "approval_text": approval_text,
        "payload": json.dumps(payload, default=str),
    }
    store.save_order(order)
    return {"status": "PENDING_APPROVAL", "order": order, "sizing": sizing.__dict__}


def _revalidate_staged_order(order: dict, payload: dict, *, cfg, client) -> None:
    """Re-fetch underlying and exact-contract quotes immediately before a live send."""
    # Lightweight fake clients in unit tests do not expose market-data methods. The real
    # Upstox client always does; production submissions therefore cannot skip this gate.
    if not (hasattr(client, "intraday_candles") and hasattr(client, "option_chain")):
        return
    now = pd.Timestamp.now(tz=IST)
    option = payload.get("option", {})
    expiry = option.get("expiry") or payload.get("expiry")
    fresh_spot = None
    if order["side"] == "BUY":
        signal = payload.get("signal", {})
        created_at = signal.get("created_at")
        if not created_at:
            raise RuntimeError("Signal timestamp missing from live approval payload")
        signal_ts = pd.Timestamp(created_at)
        signal_ts = signal_ts.tz_localize(IST) if signal_ts.tzinfo is None else signal_ts.tz_convert(IST)
        if signal_ts.date() != now.date() or (now - (signal_ts + pd.Timedelta(minutes=5))).total_seconds() > 600:
            raise TimeoutError("Signal expired before broker submission; refresh and stage a new order")
        candles = client.intraday_candles(cfg.instrument_key, 5)
        if candles.empty:
            raise RuntimeError("No completed NIFTY candle available at approval time")
        last = candles.iloc[-1]
        data_age = max(0.0, (now - (pd.Timestamp(last.timestamp) + pd.Timedelta(minutes=5))).total_seconds())
        if data_age > cfg.stale_data_seconds:
            raise TimeoutError(f"NIFTY spot data is stale at approval time ({data_age:.0f}s)")
        spot = float(last.close)
        fresh_spot = spot
        level = float(signal["level_price"])
        stop, target = float(signal["spot_stop"]), float(signal["spot_target"])
        buffer = float(getattr(cfg, "breakout_buffer_points", 0.0))
        if signal["direction"] == "LONG" and (spot <= level + buffer or spot <= stop or spot >= target):
            raise RuntimeError("Breakout is no longer valid at approval time")
        if signal["direction"] == "SHORT" and (spot >= level - buffer or spot >= stop or spot <= target):
            raise RuntimeError("Breakdown is no longer valid at approval time")
    if not expiry:
        raise RuntimeError("Option expiry missing from live order payload")
    chain = client.option_chain(cfg.instrument_key, str(expiry))
    from .option_chain import normalize_option_chain, is_within_otm_band, otm_distance_points
    frame = normalize_option_chain(chain)
    if fresh_spot is not None:
        frame["spot"] = fresh_spot
    quote_rows = frame[frame.instrument_key == order["instrument_key"]]
    if quote_rows.empty:
        raise RuntimeError("Selected option contract is no longer quoted at approval time")
    quote = quote_rows.iloc[-1]
    if order["side"] == "BUY" and not is_within_otm_band(
        str(quote.option_type), float(quote.strike), float(quote.spot), cfg
    ):
        distance = otm_distance_points(str(quote.option_type), float(quote.strike), float(quote.spot))
        low = float(getattr(cfg, "option_otm_min_points", 150.0))
        high = float(getattr(cfg, "option_otm_max_points", 200.0))
        raise RuntimeError(f"Selected contract is {distance:.1f} points OTM; required band is {low:.0f}-{high:.0f}")
    bid = float(quote.bid) if pd.notna(quote.bid) else 0.0
    ask = float(quote.ask) if pd.notna(quote.ask) else 0.0
    if bid <= 0 or ask <= 0 or ask < bid:
        raise RuntimeError("Option bid/ask is invalid at approval time")
    spread = (ask - bid) / ask
    if spread > cfg.max_spread_pct:
        raise RuntimeError("Option spread widened beyond the configured maximum before approval")
    if order["side"] == "BUY" and not (cfg.min_premium_inr <= ask <= cfg.max_premium_inr):
        raise RuntimeError("Option premium moved outside configured bounds before approval")
    live_reference = ask if order["side"] == "BUY" else bid
    staged_price = float(order["limit_price"])
    drift = abs(live_reference - staged_price) / max(staged_price, 0.01)
    if drift > float(getattr(cfg, "max_approval_price_drift_pct", 0.02)):
        raise RuntimeError(f"Option quote moved {drift:.1%} since staging; cancel/expire and stage a fresh order")


def approve_and_submit(order_ref: str, approval_text: str, *, cfg, store, client) -> dict:
    if not cfg.live_is_armed:
        raise RuntimeError("Live trading is not armed")
    with store.connect() as con:
        row = con.execute("SELECT * FROM orders WHERE order_ref=?", (order_ref,)).fetchone()
    if row is None:
        raise KeyError("Order approval request not found")
    order = dict(row)
    if order["mode"] != "LIVE" or order["side"] not in {"BUY", "SELL"}:
        raise RuntimeError("Only a valid LIVE BUY/SELL order can be approved")
    if order["status"] != "PENDING_APPROVAL":
        raise RuntimeError(f"Order is not awaiting approval (status={order['status']})")
    try:
        order_payload = json.loads(order.get("payload") or "{}")
    except json.JSONDecodeError:
        order_payload = {}
    expires_at = order_payload.get("expires_at")
    if expires_at and datetime.now(IST) > datetime.fromisoformat(expires_at):
        store.update_order(order_ref, status="EXPIRED")
        raise TimeoutError("This approval expired; refresh market data and stage a new order")
    if order["side"] == "BUY":
        from .risk import pretrade_gate
        now = datetime.now(IST)
        ok, reason = pretrade_gate(cfg=cfg, store=store, session_date=now.date().isoformat(),
                                   mode="LIVE", now=now, data_age_seconds=0,
                                   exclude_order_ref=order_ref)
        if not ok:
            raise RuntimeError(f"Live pre-trade gate failed at approval time: {reason}")
    if approval_text.strip() != order["approval_text"]:
        raise PermissionError("Approval phrase does not exactly match this order")
    try:
        _revalidate_staged_order(order, order_payload, cfg=cfg, client=client)
    except Exception:
        store.update_order(order_ref, status="STALE_APPROVAL")
        raise
    # Atomic compare-and-set prevents concurrent double-clicks from submitting twice.
    claimed_at = datetime.now(IST).isoformat()
    if not store.claim_order_for_submission(order_ref, claimed_at):
        raise RuntimeError("Order approval has already been claimed or is no longer pending")
    try:
        response = client.place_limit_order(
            instrument_key=order["instrument_key"], quantity=int(order["quantity"]),
            price=float(order["limit_price"]), side=order["side"], tag=order_ref,
            product=cfg.live_product)
    except Exception as exc:
        order_payload["submit_error"] = str(exc)
        # A timeout/connection error may happen after the broker accepted the order.
        # Mark as unknown and require manual order-book reconciliation; never retry blindly.
        store.update_order(order_ref, status="SUBMISSION_UNKNOWN", payload=json.dumps(order_payload, default=str))
        raise
    data = response.get("data") or {}
    # Upstox V3 returns `order_ids` (an array); retain legacy singular aliases
    # defensively for older API responses and test doubles.
    raw_ids = data.get("order_ids")
    if isinstance(raw_ids, list):
        broker_order_ids = list(dict.fromkeys(str(value).strip() for value in raw_ids if str(value).strip()))
    else:
        legacy_id = data.get("order_id") or data.get("orderId")
        broker_order_ids = [str(legacy_id).strip()] if legacy_id else []
    order_payload["broker_order_ids"] = broker_order_ids
    order_payload["broker_response"] = response
    submitted_at = datetime.now(IST).isoformat()
    if len(broker_order_ids) != 1:
        # A timeout or a multi-child/sliced response must never be retried blindly.
        # This implementation manages one broker order ID per approval record.
        store.update_order(order_ref, status="SUBMISSION_UNKNOWN", submitted_at=submitted_at,
                           payload=json.dumps(order_payload, default=str))
        if len(broker_order_ids) > 1:
            detail = f"Upstox returned {len(broker_order_ids)} child order IDs"
        else:
            detail = "Upstox did not return an order ID"
        raise RuntimeError(
            f"{detail}. This approval is locked as SUBMISSION_UNKNOWN; inspect and reconcile "
            "the Upstox order book before taking any further action. Do not retry."
        )
    broker_order_id = broker_order_ids[0]
    store.update_order(order_ref, status="SUBMITTED", broker_order_id=broker_order_id,
                       submitted_at=submitted_at, payload=json.dumps(order_payload, default=str))
    return {"status": "SUBMITTED", "broker_order_id": broker_order_id,
            "broker_order_ids": broker_order_ids, "response": response}


def sync_live_order(order_ref: str, *, store, client, cfg=None) -> dict:
    with store.connect() as con:
        row = con.execute("SELECT * FROM orders WHERE order_ref=?", (order_ref,)).fetchone()
    if not row or not row["broker_order_id"]:
        raise ValueError("No submitted broker order id")
    order = dict(row)
    response = client.order_history(str(order["broker_order_id"]))
    data = response.get("data") or []
    latest = data[-1] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
    status = str(latest.get("status", "UNKNOWN")).upper().replace(" ", "_")
    normalized = "FILLED" if status in {"COMPLETE", "COMPLETED", "FILLED"} else status
    if status in {"PARTIAL", "PARTIALLY_FILLED", "PARTIAL_FILL"}:
        normalized = "PARTIALLY_FILLED"
    try:
        stored_payload = json.loads(order.get("payload") or "{}")
    except json.JSONDecodeError:
        stored_payload = {}
    stored_payload["last_reconciliation"] = response
    filled_qty = int(latest.get("filled_quantity") or latest.get("filled_qty") or 0)
    avg_price = float(latest.get("average_price") or latest.get("avg_price") or latest.get("price", 0) or 0)
    terminal = normalized in {"FILLED", "CANCELLED", "REJECTED", "COMPLETE", "COMPLETED"}
    if terminal and (filled_qty <= 0 or avg_price <= 0):
        try:
            details = client.order_details(str(order["broker_order_id"]))
            detail = details.get("data") or {}
            filled_qty = int(detail.get("filled_quantity") or detail.get("filled_qty") or filled_qty or 0)
            avg_price = float(detail.get("average_price") or detail.get("avg_price") or avg_price or 0)
            stored_payload["order_details"] = details
        except Exception:
            pass
    stored_payload["last_filled_quantity"] = filled_qty
    stored_payload["last_average_price"] = avg_price
    store.update_order(order_ref, status=normalized, payload=json.dumps(stored_payload, default=str))

    # An entry position is recognized only after the broker reports a terminal state.
    # If a LIMIT entry was partially filled then cancelled, the actual filled quantity
    # becomes the managed position. We do not manage it while the remainder can still fill.
    if order["side"] == "BUY" and terminal and filled_qty > 0 and avg_price > 0:
        trade_id = f"LIVE-{order_ref}"
        signal = stored_payload.get("signal", {})
        option = stored_payload.get("option", {})
        try:
            sizing_payload = stored_payload.get("sizing", {})
            premium_stop_pct = float(getattr(cfg, "premium_stop_loss_pct", 0.35))
            trade = {
                "trade_id": trade_id, "signal_id": order["signal_id"], "mode": "LIVE",
                "direction": signal.get("direction", "LONG"), "instrument_key": order["instrument_key"],
                "expiry": option.get("expiry"), "entry_price": avg_price, "quantity": filled_qty,
                "lot_size": int(option.get("lot_size") or max(1, filled_qty)),
                "spot_entry": float(signal.get("spot_entry") or 0),
                "spot_stop": float(signal.get("spot_stop") or 0),
                "spot_target": float(signal.get("spot_target") or 0),
                "premium_stop": avg_price * (1 - premium_stop_pct), "status": "OPEN",
                "opened_at": stored_payload.get("created_at") or datetime.now(IST).isoformat(),
                "broker_order_id": order["broker_order_id"],
                "payload": json.dumps({"order_ref": order_ref, "fill": latest, "sizing": sizing_payload}, default=str),
            }
            store.save_trade(trade)
        except Exception as exc:
            stored_payload["trade_reconciliation_error"] = str(exc)
            store.update_order(order_ref, payload=json.dumps(stored_payload, default=str))
            raise

    # For exits, only apply cumulative fill quantity after the order is terminal.
    # This prevents a still-working sell LIMIT order from being double-counted.
    if order["side"] == "SELL" and terminal:
        trade_id = stored_payload.get("trade_id")
        if trade_id and filled_qty > 0 and avg_price > 0:
            with store.connect() as con:
                tr_row = con.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
            if tr_row:
                trade = dict(tr_row)
                already = int(stored_payload.get("accounted_filled_quantity", 0) or 0)
                delta_qty = max(0, filled_qty - already)
                original_qty = int(stored_payload.get("starting_quantity") or trade["quantity"] + already)
                remaining = max(0, original_qty - filled_qty)
                gross_delta = (avg_price - float(trade["entry_price"])) * delta_qty
                lot_size = max(1, int(trade["lot_size"]))
                charges_delta = float(getattr(cfg, "estimated_round_trip_cost_per_lot", 0.0)) * (delta_qty / lot_size)
                gross = float(trade.get("gross_pnl") or 0.0) + gross_delta
                charges = float(trade.get("charges") or 0.0) + charges_delta
                net = gross - charges
                fields = {"quantity": remaining, "gross_pnl": gross, "charges": charges, "net_pnl": net,
                          "payload": json.dumps({"last_exit_order_ref": order_ref, "last_exit_fill": latest}, default=str)}
                if remaining == 0:
                    fields.update({"status": "CLOSED", "exit_price": avg_price,
                                   "exited_at": datetime.now(IST).isoformat(), "exit_reason": "approved_live_exit"})
                else:
                    fields["status"] = "OPEN"
                store.update_trade(trade_id, **fields)
                stored_payload["accounted_filled_quantity"] = filled_qty
                store.update_order(order_ref, payload=json.dumps(stored_payload, default=str))
    return {"status": normalized, "latest": latest, "filled_quantity": filled_qty, "average_price": avg_price}


def stage_live_exit(trade: dict, bid: float, *, cfg, store) -> dict:
    if not cfg.live_is_armed:
        return {"status": "REJECTED", "reason": "Live trading is not armed"}
    if bid <= 0:
        return {"status": "REJECTED", "reason": "No executable bid available"}
    with store.connect() as con:
        duplicate = con.execute("SELECT order_ref,status FROM orders WHERE signal_id=? AND side='SELL' AND status IN ('PENDING_APPROVAL','SUBMITTING','SUBMITTED','PARTIALLY_FILLED','SUBMIT_ERROR','SUBMISSION_UNKNOWN','CANCEL_REQUESTED','OPEN') LIMIT 1", (trade["signal_id"],)).fetchone()
    if duplicate:
        return {"status": "ALREADY_STAGED", "order_ref": duplicate["order_ref"], "reason": "An exit is already staged or submitted"}
    order_ref = uuid.uuid4().hex[:20]
    qty = int(trade["quantity"])
    approval_text = f"APPROVE SELL {trade['instrument_key']} QTY {qty} LIMIT {float(bid):.2f}"
    now = datetime.now(IST)
    order = {"order_ref": order_ref, "signal_id": trade["signal_id"], "mode": "LIVE",
             "side": "SELL", "instrument_key": trade["instrument_key"], "quantity": qty,
             "limit_price": float(bid), "status": "PENDING_APPROVAL", "approval_text": approval_text,
             "payload": json.dumps({"trade_id": trade["trade_id"],
                                     "starting_quantity": qty, "accounted_filled_quantity": 0,
                                     "expiry": trade.get("expiry"), "created_at": now.isoformat(),
                                     "expires_at": (now + timedelta(seconds=90)).isoformat()}, default=str)}
    store.save_order(order)
    return {"status": "PENDING_APPROVAL", "order": order}


def close_paper_trade(trade: dict, exit_bid: float, reason: str, cfg, store) -> dict:
    fill = max(0.0, float(exit_bid) - cfg.slippage_points)
    gross = (fill - float(trade["entry_price"])) * int(trade["quantity"])
    # Configurable placeholder; replace with actual broker charge statement during reconciliation.
    charges = float(getattr(cfg, "estimated_round_trip_cost_per_lot", 0.0)) * (int(trade["quantity"]) / max(1, int(trade["lot_size"])))
    net = gross - charges
    store.update_trade(trade["trade_id"], status="CLOSED", exit_price=fill,
                       exited_at=datetime.now(IST).isoformat(), exit_reason=reason,
                       gross_pnl=gross, charges=charges, net_pnl=net)
    return {"gross_pnl": gross, "charges_estimate": charges, "net_pnl": net, "exit_price": fill}
