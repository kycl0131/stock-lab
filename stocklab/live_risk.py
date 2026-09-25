"""Deterministic risk engine: a validated proposal + fresh evidence + ledger + broker cash -> at most one
limit order, or a stated reason for none. No model output reaches this module except action/symbol.

Money rules (all Decimal; KRW amounts floored/ceiled explicitly):
  Cost basis   from recorded fills only (all_fills: cumulative quantity and broker average price per ticket).
               US cost in KRW uses the broker FX rate recorded on each BUY ticket at its attempt. A filled
               ticket without that rate makes cost basis unknown -> BUY blocked (fail closed).
  Exposure     sum over symbols of entitlement x average KRW cost + full reservation of every unresolved BUY.
  P&L (KRW)    current-KRW value of sell fill proceeds and remaining shares at the best bid
               - BUY fill cost converted at the recorded entry FX - estimated costs
               (configured fee/tax/slippage/FX bps) - recorded model costs.
               Shares of a SELL that a human closed without a full fill are valued at 0 (conservative).
               US USD proceeds are valued at the current broker FX rate; entry FX effects enter through
               the stored BUY cost. This remains an estimate until actual FX/fees are reconciled.
  BUY size     floor(min(config per-order, cap per-order, cap remaining cumulative, capital cap - exposure,
               config per-position, broker cash x cap cash fraction) / (limit x (1 + cost buffer) [x FX])).
  Prices       BUY at the best ask, SELL at the best bid, only if the spread and the distance from the last
               trade are inside the configured bps collars. US prices are rounded to cents away from the
               passive side (BUY up, SELL down) and re-checked against the collar.
  SELL size    the whole bot entitlement for the symbol (never existing holdings; broker quantity re-checked
               at send). BUY only when the bot holds none of the symbol (one lot per symbol).
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .domain import ValidationError
from .kiwoom_order import MAX_QUANTITY, OrderTerms
from .live_orders import COST_BUFFER_BPS, FX_PLAUSIBLE_KRW_PER_USD, FX_CROSS_CHECK_BPS

RISK_VERSION = "stocklab-live-risk-v1"
BPS = Decimal(10000)


class RiskUnknown(Exception):
    """A required quantity (cost basis, mark, FX) is not established: new BUYs are blocked."""


def _d(value) -> Decimal:
    return Decimal(str(value))


def ledger_book(conn, market) -> dict:
    """Per-symbol ledger aggregates from attempted tickets and confirmed fills (market currency)."""
    book = {}
    rows = conn.execute(
        "SELECT t.ticket_id, t.side, t.symbol, t.exchange, t.quantity, t.reserved_krw, t.fx_rate, "
        "(SELECT MAX(f.cum_qty) FROM all_fills f WHERE f.ticket_id = t.ticket_id) AS filled, "
        "(SELECT f.avg_price FROM all_fills f WHERE f.ticket_id = t.ticket_id ORDER BY f.cum_qty DESC LIMIT 1) AS avg, "
        "EXISTS(SELECT 1 FROM resolutions r WHERE r.ticket_id = t.ticket_id) AS resolved "
        "FROM tickets t WHERE t.market = ? AND t.state != 'PREPARED' ORDER BY t.created_at", (market,)).fetchall()
    for r in rows:
        s = book.setdefault(r["symbol"], {"exchange": r["exchange"], "buy_qty": 0, "buy_cost": Decimal(0),
                                          "buy_cost_krw": Decimal(0), "sell_ticket_qty": 0, "sell_filled_qty": 0,
                                          "sell_proceeds": Decimal(0), "open_sell_remainder": 0,
                                          "unresolved_buy_krw": 0, "cost_unknown": False})
        filled = int(r["filled"] or 0)
        value = filled * _d(r["avg"]) if filled else Decimal(0)
        if r["side"] == "BUY":
            s["buy_qty"] += filled
            s["buy_cost"] += value
            if filled:
                if market == "KR":
                    s["buy_cost_krw"] += value
                elif r["fx_rate"]:
                    s["buy_cost_krw"] += value * _d(r["fx_rate"])
                else:
                    s["cost_unknown"] = True
            if not r["resolved"]:
                s["unresolved_buy_krw"] += int(r["reserved_krw"] or 0)
        else:
            s["sell_ticket_qty"] += r["quantity"]
            s["sell_filled_qty"] += filled
            s["sell_proceeds"] += value
            if not r["resolved"]:
                s["open_sell_remainder"] += r["quantity"] - filled
    for s in book.values():
        s["entitlement"] = s["buy_qty"] - s["sell_ticket_qty"]
    return book


def valuation(book, marks, costs, *, market, fx=None, model_cost_krw=Decimal(0)) -> dict:
    """P&L and exposure in KRW. `marks`: symbol -> best bid (market currency). Raises RiskUnknown."""
    fee_buy, fee_sell = _d(costs["buy_fee_bps"]) / BPS, _d(costs["sell_fee_bps"]) / BPS
    tax, slip = _d(costs["sell_tax_bps"]) / BPS, _d(costs["slippage_bps"]) / BPS
    fx_cost = _d(costs.get("fx_cost_bps", "0")) / BPS
    if market == "US" and fx is None:
        raise RiskUnknown("FX_UNKNOWN")
    rate = Decimal(1) if market == "KR" else fx
    pnl_krw_total, exposure, positions = Decimal(0), Decimal(0), {}
    for symbol, s in book.items():
        if s["cost_unknown"]:
            raise RiskUnknown(f"{symbol}:COST_BASIS_UNKNOWN")
        if s["entitlement"] < 0:
            raise RiskUnknown(f"{symbol}:NEGATIVE_ENTITLEMENT")
        held = s["entitlement"] + s["open_sell_remainder"]
        if held and symbol not in marks:
            raise RiskUnknown(f"{symbol}:MARK_MISSING")
        mark_value = held * marks[symbol] if held else Decimal(0)
        exit_value = s["sell_proceeds"] + mark_value
        exit_cost = s["sell_proceeds"] * (fee_sell + tax) + mark_value * (fee_sell + tax + slip)
        buy_fee_krw = s["buy_cost_krw"] * fee_buy
        fx_cost_krw = (s["buy_cost_krw"] + exit_value * rate) * fx_cost
        symbol_pnl = exit_value - s["buy_cost"] - exit_cost
        symbol_pnl_krw = (exit_value - exit_cost) * rate - s["buy_cost_krw"] - buy_fee_krw - fx_cost_krw
        pnl_krw_total += symbol_pnl_krw
        avg_cost_krw = s["buy_cost_krw"] / s["buy_qty"] if s["buy_qty"] else Decimal(0)
        exposure += s["entitlement"] * avg_cost_krw + s["unresolved_buy_krw"]
        positions[symbol] = {"entitlement": s["entitlement"], "held_for_mark": held,
                             "pnl_ccy_before_buy_fee": f"{symbol_pnl:.4f}",
                             "pnl_krw_estimate": f"{symbol_pnl_krw:.2f}",
                             "avg_cost_krw": f"{avg_cost_krw:.2f}"}
    pnl_krw = pnl_krw_total - model_cost_krw
    return {"pnl_krw": pnl_krw.quantize(Decimal("1"), rounding=ROUND_FLOOR),
            "exposure_krw": exposure.quantize(Decimal("1"), rounding=ROUND_CEILING),
            "fx": None if fx is None else f"{fx:f}", "positions": positions}


def _cent(value: Decimal, up: bool) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_CEILING if up else ROUND_FLOOR)


def _bps(a: Decimal, b: Decimal) -> Decimal:
    return abs(a - b) / b * BPS


def plan(*, market, proposal, universe, fact, book, cfg, glob, cash_ccy, fx, cap, committed_krw,
         exposure_total_krw, buy_allowed, buy_block_reason=None) -> dict:
    """At most one order for this market/cycle. Returns {"order": dict|None, "reason": str, "calc": dict}."""
    action, symbol = proposal["action"], proposal["symbol"]
    calc = {"version": RISK_VERSION, "action": action, "symbol": symbol}
    if action == "HOLD":
        return {"order": None, "reason": "HOLD", "calc": calc}
    held = book.get(symbol, {}).get("entitlement", 0)
    if fact is None:
        return {"order": None, "reason": "NO_EVIDENCE_FOR_SYMBOL", "calc": calc}
    bid, ask, last = fact["bid"], fact["ask"], fact["last"]
    spread = (ask - bid) / ((ask + bid) / 2) * BPS
    calc.update({"bid": f"{bid:f}", "ask": f"{ask:f}", "last": f"{last:f}", "spread_bps": f"{spread:.1f}"})
    if spread > glob["max_spread_bps"]:
        return {"order": None, "reason": "SPREAD_TOO_WIDE", "calc": calc}

    if action == "SELL":
        if held <= 0:
            return {"order": None, "reason": "NO_BOT_INVENTORY", "calc": calc}
        limit = bid if market == "KR" else _cent(bid, up=False)
        if _bps(limit, last) > glob["collar_bps"]:
            return {"order": None, "reason": "PRICE_OUTSIDE_COLLAR", "calc": calc}
        quantity = min(held, MAX_QUANTITY)
    else:
        if not buy_allowed:
            return {"order": None, "reason": buy_block_reason or "BUY_BLOCKED", "calc": calc}
        if symbol not in universe:
            return {"order": None, "reason": "SYMBOL_NOT_IN_CONFIGURED_UNIVERSE", "calc": calc}
        if held > 0 or book.get(symbol, {}).get("unresolved_buy_krw"):
            return {"order": None, "reason": "ALREADY_HOLDING_SYMBOL", "calc": calc}
        if cap is None:
            return {"order": None, "reason": "CAP_NOT_CONFIGURED", "calc": calc}
        limit = ask if market == "KR" else _cent(ask, up=True)
        if _bps(limit, last) > glob["collar_bps"]:
            return {"order": None, "reason": "PRICE_OUTSIDE_COLLAR", "calc": calc}
        if market == "US":
            low, high = FX_PLAUSIBLE_KRW_PER_USD
            if fx is None or not low <= fx <= high:
                return {"order": None, "reason": "FX_UNKNOWN", "calc": calc}
            quote_fx = fact.get("fx_quote")
            if quote_fx is None or _bps(quote_fx, fx) > FX_CROSS_CHECK_BPS:
                return {"order": None, "reason": "FX_SOURCES_DISAGREE", "calc": calc}
        rate = Decimal(1) if market == "KR" else fx
        if cash_ccy is None or cash_ccy < 0:
            return {"order": None, "reason": "BROKER_CASH_UNKNOWN", "calc": calc}
        cash_krw = (cash_ccy * rate).quantize(Decimal("1"), rounding=ROUND_FLOOR)
        limits = {"config_max_order_krw": Decimal(cfg["max_order_krw"]),
                  "config_max_position_krw": Decimal(cfg["max_position_krw"]),
                  "cap_max_order_krw": Decimal(cap["max_order_krw"]),
                  "cap_remaining_cumulative_krw": Decimal(cap["max_committed_krw"] - committed_krw),
                  "capital_cap_remaining_krw": Decimal(glob["capital_cap_krw"]) - exposure_total_krw,
                  "broker_cash_fraction_krw": cash_krw * cap["cash_fraction_bps"] // 10000}
        budget = min(limits.values())
        per_share = limit * (1 + Decimal(COST_BUFFER_BPS[market]) / BPS) * rate
        quantity = int((budget / per_share).to_integral_value(rounding=ROUND_FLOOR)) if budget > 0 else 0
        calc.update({k: f"{v:f}" for k, v in limits.items()})
        calc.update({"budget_krw": f"{budget:f}", "per_share_krw_with_buffer": f"{per_share:.2f}",
                     "fx": None if fx is None else f"{fx:f}"})
        if quantity < 1:
            return {"order": None, "reason": "BUDGET_BELOW_ONE_SHARE", "calc": calc}
        quantity = min(quantity, MAX_QUANTITY)
    try:
        terms = OrderTerms(market=market, side=action, symbol=symbol, exchange=fact["exchange"],
                           quantity=quantity, limit_price=f"{limit:f}")
    except ValidationError:   # tick/cent/range problem: no order, not an error
        return {"order": None, "reason": "ORDER_TERMS_INVALID", "calc": calc}
    calc.update({"quantity": terms.quantity, "limit_price": terms.limit_price})
    return {"order": terms.payload(), "reason": "ORDER", "calc": calc}
