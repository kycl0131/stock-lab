"""Durable paper execution. There is deliberately no live-order transport."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
from pathlib import Path
import uuid

from . import paper_pilot, strategies
from .data import snapshot
from .domain import (ACTIVE, CURRENCIES, RiskPolicy, ValidationError, canonical, decimal, default_policy,
                     digest, integer, market, now, timestamp)
from .store import Store


def initialize(store: Store, mkt: str, cash, policy: RiskPolicy | None = None):
    market(mkt)
    amount = decimal(cash)
    if amount <= 0:
        raise ValidationError("Starting paper cash must be positive")
    policy = policy or default_policy(mkt)
    with store.transaction() as con:
        if con.execute("SELECT 1 FROM accounts WHERE market=?", (mkt,)).fetchone():
            raise ValidationError("Account already exists; use a new database for a new experiment")
        con.execute("INSERT INTO accounts(market,currency,cash,initial_cash,policy) VALUES(?,?,?,?,?)",
                    (mkt, CURRENCIES[mkt], str(amount), str(amount), canonical(policy.payload())))
        Store.audit(con, "ACCOUNT_INITIALIZED", {"market": mkt, "cash": str(amount), "execution": "PAPER"})


def positions(con, mkt):
    return {r["symbol"]: dict(r) for r in con.execute("SELECT * FROM positions WHERE market=? AND quantity>0", (mkt,))}


def context(store, mkt, as_of, *, lookback=5, visibility="replay", con=None):
    if con is None:
        with store.connect() as db:
            return context(store, mkt, as_of, lookback=lookback, visibility=visibility, con=db)
    data = snapshot(store, mkt, as_of, lookback=lookback, visibility=visibility, con=con)
    account = store.account(mkt, con)
    if account["clock"] and data["as_of"] < account["clock"]:
        raise ValidationError("Historical portfolio unavailable before account clock; use a fresh experiment database")
    data["portfolio"] = {"cash": account["cash"], "currency": account["currency"], "clock": account["clock"],
                         "positions": positions(con, mkt)}
    return data


def _valuation(con, mkt, cash, data, policy, at):
    quotes = {i["symbol"]: i["quote"] for i in data["instruments"]}
    held = positions(con, mkt)
    nav = decimal(cash)
    gross = Decimal(0)
    for ticker, pos in held.items():
        if pos["quantity"] <= 0:
            continue
        if ticker not in quotes:
            raise ValidationError(f"Missing valuation quote for {ticker}")
        _fresh(quotes[ticker], at, policy)
        value = pos["quantity"] * decimal(quotes[ticker]["price"])
        gross += value
        nav += value
    return quotes, held, nav, gross


def _fresh(quote, at, policy):
    age = (datetime.fromisoformat(at) - datetime.fromisoformat(quote["event_at"])).total_seconds()
    if age < 0 or age > policy.max_quote_age_seconds:
        raise ValidationError("Quote is stale or from the future")


def _limit(price, side, mkt, policy):
    step = Decimal("1") if mkt == "KR" else Decimal("0.01")
    factor = decimal(policy.slippage_bps) / 10000
    value = price * (1 + factor if side == "BUY" else 1 - factor)
    return max(step, value.quantize(step, rounding=ROUND_CEILING if side == "BUY" else ROUND_FLOOR))


def run(store: Store, *, run_key: str, mkt: str, as_of: str, mode="shadow", strategy="momentum",
        lookback=5, visibility="replay", top_k=3, weight="0.20", decision_file=None, model=None) -> dict:
    market(mkt)
    as_of = timestamp(as_of)
    if mode not in ("shadow", "paper"):
        raise ValidationError("Only shadow and paper modes exist; live routing is unavailable")
    if mode == "paper" and visibility != "replay":
        raise ValidationError("Recorded visibility currently supports shadow decisions only; paper fills use archived replay")
    if strategy not in ("momentum", "equal", "file", "anthropic"):
        raise ValidationError("Unknown strategy")
    if not isinstance(run_key, str) or not run_key.strip() or len(run_key) > 200:
        raise ValidationError("A stable run key of 1-200 characters is required")
    if as_of > now():
        raise ValidationError("Evaluation cannot be scheduled in the future")
    if strategy == "file" and not decision_file:
        raise ValidationError("file strategy requires --decision-file")
    file_text = Path(decision_file).read_text(encoding="utf-8-sig") if decision_file else None
    specification = {"market": mkt, "as_of": as_of, "mode": mode, "strategy": strategy,
                     "lookback": lookback, "visibility": visibility, "top_k": top_k, "weight": str(weight),
                     "model": model or (strategies.os.environ.get("STOCKLAB_MODEL") if strategy == "anthropic" else None),
                     "decision_file_hash": digest(file_text) if file_text is not None else None}
    request_hash = digest(specification)
    with store.transaction() as con:
        existing = con.execute("SELECT * FROM runs WHERE run_key=?", (run_key,)).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise ValidationError("Run key already belongs to a different request")
            return dict(existing)
        account = store.account(mkt, con)
        if mode == "paper" and account["clock"] and as_of < account["clock"]:
            raise ValidationError("Paper account clock cannot move backwards")
        data = context(store, mkt, as_of, lookback=lookback, visibility=visibility, con=con)
        if not data["instruments"]:
            raise ValidationError("No price evidence is available at this timestamp")
        run_id = uuid.uuid4().hex
        policy = RiskPolicy(**account["policy"])
        if mode == "paper":
            if account["halted"]:
                raise ValidationError("Paper account is halted")
            if con.execute("SELECT 1 FROM orders WHERE market=? AND status IN ('OPEN','PARTIAL')", (mkt,)).fetchone():
                raise ValidationError("Reconcile or cancel existing orders before another rebalance")
            _valuation(con, mkt, account["cash"], data, policy, as_of)
        con.execute("INSERT INTO runs(id,run_key,market,as_of,created_at,mode,strategy,status,request_hash,"
                    "snapshot_hash,snapshot,policy) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, run_key, mkt, as_of, now(), mode, strategy, "PENDING", request_hash,
                     digest(data), canonical(data), canonical(policy.payload())))
        Store.audit(con, "RUN_STARTED", {"id": run_id, "specification": specification, "snapshot_hash": digest(data)})
    decision, provider = None, None
    try:
        if strategy in ("momentum", "equal"):
            decision, provider = strategies.baseline(data, method=strategy, top_k=top_k, weight=weight,
                                                      max_age=policy.max_quote_age_seconds)
        elif strategy == "file":
            decision, provider = strategies.file_decision(file_text, data)
        else:
            decision, provider = strategies.anthropic_decision(data, policy.payload(), model=model)
        with store.transaction() as con:
            pending = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if pending["status"] != "PENDING":
                raise ValidationError("Run was abandoned while inference was in progress; no orders created")
            if mode == "paper":
                current = store.account(mkt, con)
                if current["halted"]:
                    raise ValidationError("Paper account is halted")
                if current["cash"] != account["cash"] or current["clock"] != account["clock"] or positions(con, mkt) != data["portfolio"]["positions"]:
                    raise ValidationError("Account changed during inference; no orders created")
                if con.execute("SELECT 1 FROM orders WHERE market=? AND status IN ('OPEN','PARTIAL')", (mkt,)).fetchone():
                    raise ValidationError("Reconcile or cancel existing orders before another rebalance")
                _create_orders(store, con, run_id, mkt, as_of, account, data, decision, policy)
                con.execute("UPDATE accounts SET clock=? WHERE market=?", (as_of, mkt))
            con.execute("UPDATE runs SET status='COMPLETED',decision=?,provider=? WHERE id=?",
                        (canonical(decision), canonical(provider), run_id))
            Store.audit(con, "RUN_COMPLETED", {"id": run_id, "mode": mode, "provider": provider})
    except Exception as exc:
        provider = getattr(exc, "provider", provider)
        message = str(exc) if isinstance(exc, ValidationError) else f"{type(exc).__name__}: evaluation failed"
        with store.transaction() as con:
            con.execute("UPDATE runs SET status='FAILED',error=?,decision=?,provider=? WHERE id=?",
                        (message, canonical(decision) if decision is not None else None,
                         canonical(provider) if provider is not None else None, run_id))
            Store.audit(con, "RUN_FAILED", {"id": run_id, "error": message})
        raise ValidationError(message) from None
    with store.connect() as con:
        return dict(con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())


def _create_orders(store, con, run_id, mkt, at, account, data, decision, policy):
    weights = {t["symbol"]: decimal(t["weight"]) for t in decision["targets"]}
    if any(w > decimal(policy.max_symbol_weight) for w in weights.values()):
        raise ValidationError("Decision breaches the per-symbol weight limit")
    if sum(weights.values()) > decimal(policy.max_gross_weight):
        raise ValidationError("Decision breaches the gross exposure limit")
    quotes, held, nav, gross = _valuation(con, mkt, account["cash"], data, policy, at)
    remaining_cash = decimal(account["cash"])
    reserved = Decimal(0)
    # Pilot databases only: accepted buy cost and its projected mark-to-exit loss, local currency.
    pilot = paper_pilot.load(con)
    committed, projected = Decimal(0), Decimal(0)
    candidates = []
    for ticker in sorted(set(weights) | set(held)):
        quote = quotes[ticker]
        _fresh(quote, at, policy)
        price = decimal(quote["price"])
        current_qty = held.get(ticker, {}).get("quantity", 0)
        desired = int((nav * weights.get(ticker, Decimal(0)) / price).to_integral_value(rounding=ROUND_FLOOR))
        difference = desired - current_qty
        if difference:
            candidates.append(("BUY" if difference > 0 else "SELL", ticker, abs(difference), price))
    for side, ticker, qty, price in sorted(candidates, key=lambda x: (x[0] != "SELL", x[1])):
        limit = _limit(price, side, mkt, policy)
        requested_qty = qty
        # Risk-reducing sells are exempt; buys are clipped to the configured notional cap.
        if side == "BUY":
            qty = min(qty, int(decimal(policy.max_order_notional) / limit))
        if qty == 0:
            Store.audit(con, "ORDER_SKIPPED", {"symbol": ticker, "reason": "ONE_SHARE_EXCEEDS_ORDER_LIMIT"})
            continue
        if qty < requested_qty:
            Store.audit(con, "ORDER_CLIPPED", {"symbol": ticker, "requested": requested_qty, "quantity": qty})
        notional = limit * qty
        fee = notional * decimal(policy.fee_bps) / 10000
        reason = ""
        if side == "BUY":
            if notional + fee > remaining_cash - nav * decimal(policy.cash_buffer):
                reason = reason or "INSUFFICIENT_UNRESERVED_CASH"
            if gross + reserved + notional > nav * decimal(policy.max_gross_weight):
                reason = reason or "GROSS_EXPOSURE_LIMIT"
            extra = notional + fee - qty * price * paper_pilot.exit_factor(policy)
            if pilot and not reason:
                if paper_pilot.envelope_exceeded(pilot, mkt, gross + committed, notional + fee):
                    reason = "PILOT_CAPITAL_ENVELOPE"
                else:
                    reason = paper_pilot.buy_check(store, con, pilot, mkt, at, projected + extra)
            if not reason:
                remaining_cash -= notional + fee
                reserved += notional
                committed += notional + fee
                projected += extra
        order_id = uuid.uuid4().hex
        expires = (datetime.fromisoformat(at) + timedelta(seconds=policy.order_ttl_seconds)).isoformat(timespec="microseconds")
        con.execute("INSERT INTO orders(id,intent_key,run_id,market,symbol,side,quantity,limit_price,status,reason,created_at,expires_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (order_id, f"{run_id}:{ticker}:{side}", run_id, mkt, ticker, side, qty, str(limit),
                     "REJECTED" if reason else "OPEN", reason, at, expires))
        Store.audit(con, "ORDER_REJECTED" if reason else "ORDER_OPENED", {"id": order_id, "reason": reason})


def advance(store: Store, mkt: str, as_of: str) -> list[dict]:
    """Fill against subsequent observations only, with a shared per-observation volume cap."""
    market(mkt)
    as_of = timestamp(as_of)
    if as_of > now():
        raise ValidationError("Paper clock cannot advance into the future")
    filled = []
    with store.transaction() as con:
        account = store.account(mkt, con)
        clock = account["clock"]
        if clock and as_of < clock:
            raise ValidationError("Paper account clock cannot move backwards")
        if account["halted"]:
            raise ValidationError("Paper account is halted; resume explicitly before advancing")
        policy = RiskPolicy(**account["policy"])
        pilot = paper_pilot.load(con)
        # First published observation only: later historical revisions are never new execution liquidity.
        rows = con.execute("SELECT p.* FROM prices p WHERE p.market=? AND p.event_at>? AND p.event_at<=? "
                           "AND p.available_at<=? AND NOT EXISTS (SELECT 1 FROM prices q WHERE q.market=p.market "
                           "AND q.symbol=p.symbol AND q.event_at=p.event_at AND q.available_at<p.available_at) "
                           "ORDER BY p.available_at,p.event_at,p.id", (mkt, clock or "", as_of, as_of)).fetchall()
        last_pilot_observation_at = None
        for row in rows:
            price_row = dict(row)
            if pilot and row["available_at"] != last_pilot_observation_at:
                # Record a transient drawdown even when this observation fills no order.
                paper_pilot.enforce(store, con, pilot, row["available_at"])
                last_pilot_observation_at = row["available_at"]
            capacity = int(decimal(row["volume"]) * decimal(policy.participation))
            used = con.execute("SELECT COALESCE(SUM(quantity),0) FROM fills WHERE price_id=?", (row["id"],)).fetchone()[0]
            capacity -= used
            if capacity <= 0:
                continue
            orders = con.execute("SELECT * FROM orders WHERE market=? AND symbol=? AND status IN ('OPEN','PARTIAL') "
                                 "AND created_at<? ORDER BY created_at,id", (mkt, row["symbol"], row["event_at"])).fetchall()
            for order in orders:
                # A pilot threshold may have cancelled buys earlier in this loop.
                if con.execute("SELECT status FROM orders WHERE id=?", (order["id"],)).fetchone()["status"] not in ACTIVE:
                    continue
                if order["expires_at"] <= row["available_at"]:
                    con.execute("UPDATE orders SET status='EXPIRED' WHERE id=?", (order["id"],))
                    continue
                if con.execute("SELECT 1 FROM fills WHERE order_id=? AND price_id=?", (order["id"], row["id"])).fetchone():
                    continue
                amount = min(capacity, order["quantity"] - order["filled"])
                execution = decimal(row["price"]) * (1 + (1 if order["side"] == "BUY" else -1) * decimal(policy.slippage_bps) / 10000)
                if (order["side"] == "BUY" and execution > decimal(order["limit_price"])) or (order["side"] == "SELL" and execution < decimal(order["limit_price"])):
                    continue
                fee = execution * amount * decimal(policy.fee_bps) / 10000
                account = store.account(mkt, con)
                cash = decimal(account["cash"])
                pos = positions(con, mkt).get(order["symbol"], {"quantity": 0, "cost_basis": "0"})
                if order["side"] == "BUY":
                    marks = snapshot(store, mkt, row["available_at"], con=con)
                    try:
                        quotes, _, nav, gross = _valuation(con, mkt, cash, marks, policy, row["available_at"])
                        _fresh(price_row, row["available_at"], policy)
                    except ValidationError as exc:
                        Store.audit(con, "FILL_SKIPPED", {"order_id": order["id"], "price_id": row["id"], "reason": str(exc)})
                        continue
                    after_cash = cash - execution * amount - fee
                    mark = decimal(quotes[order["symbol"]]["price"])
                    if pilot:
                        cost = execution * amount + fee
                        if paper_pilot.envelope_exceeded(pilot, mkt, gross, cost):
                            blocked = "PILOT_CAPITAL_ENVELOPE"
                        else:
                            blocked = paper_pilot.buy_check(store, con, pilot, mkt, row["available_at"],
                                                            cost - amount * mark * paper_pilot.exit_factor(policy))
                        if blocked == "PILOT_VALUATION_UNAVAILABLE":
                            Store.audit(con, "FILL_SKIPPED", {"order_id": order["id"], "price_id": row["id"], "reason": blocked})
                            continue
                        if blocked:
                            con.execute("UPDATE orders SET status='CANCELLED',reason=? WHERE id=?", (blocked, order["id"]))
                            Store.audit(con, "FILL_BLOCKED", {"order_id": order["id"], "reason": blocked})
                            continue
                    after_nav = nav + amount * (mark - execution) - fee
                    if (after_cash < after_nav * decimal(policy.cash_buffer)
                        or (pos["quantity"] + amount) * mark > after_nav * decimal(policy.max_symbol_weight)
                        or gross + amount * mark > after_nav * decimal(policy.max_gross_weight)):
                        con.execute("UPDATE orders SET status='CANCELLED',reason='FILL_RISK_LIMIT' WHERE id=?", (order["id"],))
                        Store.audit(con, "FILL_BLOCKED", {"order_id": order["id"], "reason": "FILL_RISK_LIMIT"})
                        continue
                    new_qty = pos["quantity"] + amount
                    basis = (decimal(pos["cost_basis"]) * pos["quantity"] + execution * amount + fee) / new_qty
                else:
                    if amount > pos["quantity"]:
                        raise ValidationError("Sell reservation invariant violated; transaction rolled back")
                    after_cash = cash + execution * amount - fee
                    new_qty = pos["quantity"] - amount
                    basis = decimal(pos["cost_basis"]) if new_qty else Decimal(0)
                con.execute("UPDATE accounts SET cash=? WHERE market=?", (str(after_cash), mkt))
                con.execute("INSERT INTO positions VALUES(?,?,?,?) ON CONFLICT(market,symbol) "
                            "DO UPDATE SET quantity=excluded.quantity,cost_basis=excluded.cost_basis",
                            (mkt, order["symbol"], new_qty, str(basis)))
                total = order["filled"] + amount
                con.execute("UPDATE orders SET filled=?,status=? WHERE id=?", (total, "FILLED" if total == order["quantity"] else "PARTIAL", order["id"]))
                fill = {"id": uuid.uuid4().hex, "order_id": order["id"], "price_id": row["id"], "quantity": amount,
                        "price": str(execution), "fee": str(fee), "event_at": row["available_at"]}
                con.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?)", tuple(fill.values()))
                Store.audit(con, "PAPER_FILL", fill)
                filled.append(fill)
                if pilot:
                    paper_pilot.enforce(store, con, pilot, row["available_at"])
                capacity -= amount
                if capacity <= 0:
                    break
        con.execute("UPDATE orders SET status='EXPIRED' WHERE market=? AND status IN ('OPEN','PARTIAL') AND expires_at<=?", (mkt, as_of))
        if pilot:
            _, errors = paper_pilot.enforce(store, con, pilot, as_of)
            if errors:
                Store.audit(con, "PILOT_VALUATION_UNAVAILABLE", {"as_of": as_of, "errors": errors})
        con.execute("UPDATE accounts SET clock=? WHERE market=?", (as_of, mkt))
        Store.audit(con, "PAPER_CLOCK_ADVANCED", {"market": mkt, "as_of": as_of, "fills": len(filled)})
    return filled


def cancel(store: Store, order_id: str):
    with store.transaction() as con:
        order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise ValidationError("Unknown order")
        if order["status"] in ("OPEN", "PARTIAL"):
            con.execute("UPDATE orders SET status='CANCELLED',reason='USER_CANCEL' WHERE id=?", (order_id,))
            Store.audit(con, "ORDER_CANCELLED", {"id": order_id, "filled": order["filled"]})


def abandon(store: Store, run_key: str, reason: str):
    """Invalidate an interrupted run. The same key is never retried or billed again."""
    if not reason.strip():
        raise ValidationError("A reason is required")
    with store.transaction() as con:
        row = con.execute("SELECT id,status FROM runs WHERE run_key=?", (run_key,)).fetchone()
        if not row or row["status"] != "PENDING":
            raise ValidationError("Only a PENDING run can be abandoned")
        con.execute("UPDATE runs SET status='FAILED',error=? WHERE id=?", ("ABANDONED: " + reason[:500], row["id"]))
        Store.audit(con, "RUN_ABANDONED", {"id": row["id"], "reason": reason[:500]})


def halt(store: Store, mkt: str, reason: str, *, resume=False):
    market(mkt)
    if not reason.strip():
        raise ValidationError("A reason is required")
    with store.transaction() as con:
        store.account(mkt, con)
        con.execute("UPDATE accounts SET halted=?,halt_reason=? WHERE market=?", (0 if resume else 1, reason[:500], mkt))
        if not resume:
            con.execute("UPDATE orders SET status='CANCELLED',reason='HALT' WHERE market=? AND status IN ('OPEN','PARTIAL')", (mkt,))
        Store.audit(con, "RESUMED" if resume else "HALTED", {"market": mkt, "reason": reason[:500]})
