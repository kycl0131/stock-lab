"""Local two-market paper pilot: KR and US spot, KRW 50,000 envelope per market.

Paper only. There is no broker call and no live-order path here. The planned-loss thresholds
gate NEW simulated buys; they are not guaranteed realized-loss limits and nothing is ever
liquidated automatically. Pilot records in `settings` are append-only (see store triggers).
"""
from __future__ import annotations

from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
import json

from .data import snapshot
from .domain import ACTIVE, CURRENCIES, RiskPolicy, ValidationError, canonical, decimal, now, timestamp
from .pilot import FX_RATE_RANGE, MAX_COST_BPS, PILOT_CEILING_KRW, PILOT_MAX_LOSS_KRW, PILOT_TOTAL_MAX_LOSS_KRW
from .store import Store

KEY = "paper_pilot"
BREACH_PREFIX = "paper_pilot_breach:"
MARKETS = ("KR", "US")
BPS = Decimal(10000)
CENT = Decimal("0.01")
WON = Decimal(1)
STEP = {"KR": WON, "US": CENT}
TABLES = ("accounts", "positions", "prices", "news", "runs", "orders", "fills", "experiments", "audit")

CONVENTION = [
    "KR paper cash = KRW 50,000.",
    "US paper cash (USD) = floor_to_cent(50,000 / (fx_rate * (1 + fx_cost_bps/10000))). The full KRW 50,000 is "
    "charged to the US envelope; the sub-cent remainder is recorded as unconverted_residual_krw and counts as loss.",
    "USD -> KRW valuation = USD * fx_rate * (1 - fx_cost_bps/10000): the same static setup rate, with the "
    "conversion cost charged again on the way back.",
    "Cost-adjusted NAV (local currency) = cash + sum(quantity * latest paper quote at or before as-of) * "
    "(1 - (fee_bps + slippage_bps)/10000), i.e. marked liquidation value net of the modelled sell fee and slippage.",
    "Planned loss (KRW) = KRW 50,000 - conservative NAV in KRW, per market; combined = KR + US. Negative = above envelope.",
    "A threshold is reached when planned loss >= KRW 2,500 (per market) or >= KRW 5,000 (combined), compared on exact "
    "decimals. Displayed KRW NAV is floored and displayed loss is ceiled to 1 won; US local NAV is floored to 1 cent.",
    "Capital envelope: marked gross holdings + new buy notional + buy fee must stay <= the market's initial paper cash.",
    "New paper BUY is also refused when the projected loss after it (buy cost minus its conservative marked value) "
    "would reach a threshold.",
]

LIMITATIONS = [
    "LOCAL PAPER SIMULATION ONLY. No Kiwoom order, Kiwoom mock-server call or real-account read happens. Balances "
    "are this SQLite ledger, never broker holdings.",
    "Static FX: the setup USD/KRW rate and conversion cost are used for every valuation. FX moves are not modelled and "
    "no currency is actually converted.",
    "Corporate actions (splits, dividends, rights) are not applied; quotes must be unadjusted tradable prices.",
    "Commissions and slippage are the RiskPolicy fee_bps/slippage_bps paper assumptions, not Kiwoom's actual rates. "
    "KR sell tax, US regulatory fees, minimum/flat commissions and income or capital-gains taxes are not modelled.",
    "Liquidity: fills use sampled observations with a participation cap. Queue priority, auctions, halts, price "
    "limits and tick sizes are not modelled.",
    "The KRW 2,500 per-market / KRW 5,000 combined thresholds only block new paper BUYs; once observed by "
    "advance or a buy gate they stay latched for this database. A read-only status lookup does not latch. "
    "They are NOT a guaranteed stop-loss: price gaps can overshoot them and nothing is "
    "sold automatically. Risk-reducing SELLs remain allowed.",
    "Missing or stale quotes, or an as-of before a paper clock, make valuation unavailable and block new buys. Operate "
    "the two markets in chronological order: a market whose clock is ahead blocks the other market's buys.",
    "No trading calendar: the quote-age limit (default 24h) can block buys after weekends or holidays.",
    "User-configured fee/slippage assumptions can be lower than actual execution cost. A zero-cost policy makes the "
    "cost-adjusted NAV optimistic; it does not prove the position is safe.",
]


def _plain(value: Decimal) -> str:
    return format(value, "f")


def _shown(value: Decimal, step: Decimal, rounding) -> str:
    return _plain(value.quantize(step, rounding=rounding))


def load(con) -> dict | None:
    row = con.execute("SELECT value FROM settings WHERE key=?", (KEY,)).fetchone()
    return json.loads(row["value"]) if row else None


def latched(con) -> dict:
    return {r["key"][len(BREACH_PREFIX):]: json.loads(r["value"])
            for r in con.execute("SELECT key,value FROM settings WHERE key GLOB ?", (BREACH_PREFIX + "*",))}


def exit_factor(policy: RiskPolicy) -> Decimal:
    return 1 - (decimal(policy.fee_bps) + decimal(policy.slippage_bps)) / BPS


def _krw_factor(pilot: dict, mkt: str) -> Decimal:
    if mkt == "KR":
        return Decimal(1)
    fx = pilot["fx"]
    return decimal(fx["rate_krw_per_usd"]) * (1 - decimal(fx["conversion_cost_bps"]) / BPS)


def _require_empty(con):
    extra = con.execute("SELECT count(*) FROM settings WHERE key<>'schema_version'").fetchone()[0]
    if extra or any(con.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in TABLES):
        raise ValidationError("Pilot setup requires a new, empty database; choose a new --db path")


def plan(*, fx_rate, fx_cost_bps, policies: dict | None = None) -> dict:
    """Validate inputs and derive the pilot record without touching any database."""
    fx = decimal(fx_rate)
    if not FX_RATE_RANGE[0] <= fx <= FX_RATE_RANGE[1]:
        raise ValidationError(f"fx_rate must be KRW per USD within {FX_RATE_RANGE[0]}..{FX_RATE_RANGE[1]}")
    cost = decimal(fx_cost_bps)
    if cost > MAX_COST_BPS:
        raise ValidationError(f"fx_cost_bps must be within 0..{MAX_COST_BPS}")
    usd = (PILOT_CEILING_KRW / (fx * (1 + cost / BPS))).quantize(CENT, rounding=ROUND_FLOOR)
    if usd <= 0:
        raise ValidationError("FX inputs leave no USD paper cash")
    charged = usd * fx * (1 + cost / BPS)
    starting_us_loss = PILOT_CEILING_KRW - usd * fx * (1 - cost / BPS)
    if starting_us_loss >= PILOT_MAX_LOSS_KRW:
        raise ValidationError("FX cost alone reaches the US planned-loss threshold; use a lower-cost scenario")
    cash = {"KR": PILOT_CEILING_KRW, "US": usd}
    chosen = {}
    for mkt in MARKETS:
        policy = (policies or {}).get(mkt) or RiskPolicy(max_order_notional=_plain(cash[mkt]))
        if decimal(policy.max_order_notional) > cash[mkt]:
            raise ValidationError(f"{mkt} max_order_notional cannot exceed the pilot envelope {_plain(cash[mkt])}")
        if policy.max_quote_age_seconds > 86400:
            raise ValidationError(f"{mkt} max_quote_age_seconds cannot exceed 86400 for the pilot")
        chosen[mkt] = policy
    record = {
        "version": 1, "created_at": now(), "execution": "LOCAL_PAPER_ONLY",
        "markets": {mkt: {"currency": CURRENCIES[mkt], "envelope_krw": _plain(PILOT_CEILING_KRW),
                          "initial_cash": _plain(cash[mkt]),
                          "planned_loss_threshold_krw": _plain(PILOT_MAX_LOSS_KRW)} for mkt in MARKETS},
        "combined": {"envelope_krw": _plain(PILOT_CEILING_KRW * 2),
                     "planned_loss_threshold_krw": _plain(PILOT_TOTAL_MAX_LOSS_KRW)},
        "fx": {"pair": "USD/KRW", "rate_krw_per_usd": _plain(fx), "conversion_cost_bps": _plain(cost),
               "static": True, "source": "user input at pilot setup; never fetched",
               "usd_cash": _plain(usd), "krw_charged_for_usd_cash": _plain(charged),
               "unconverted_residual_krw": _plain(PILOT_CEILING_KRW - charged),
               "starting_planned_loss_krw": _plain(starting_us_loss)},
        "convention": CONVENTION,
    }
    return {"cash": cash, "policies": chosen, "record": record}


def setup(store: Store, prepared: dict) -> dict:
    """Create both paper accounts and the immutable pilot record in one transaction."""
    cash, chosen, record = prepared["cash"], prepared["policies"], prepared["record"]
    with store.transaction() as con:
        _require_empty(con)
        for mkt in MARKETS:
            con.execute("INSERT INTO accounts(market,currency,cash,initial_cash,policy) VALUES(?,?,?,?,?)",
                        (mkt, CURRENCIES[mkt], _plain(cash[mkt]), _plain(cash[mkt]), canonical(chosen[mkt].payload())))
            Store.audit(con, "ACCOUNT_INITIALIZED", {"market": mkt, "cash": _plain(cash[mkt]), "execution": "PAPER"})
        con.execute("INSERT INTO settings(key,value) VALUES(?,?)", (KEY, canonical(record)))
        Store.audit(con, "PAPER_PILOT_INITIALIZED", record)
    return {"message": "Local two-market paper pilot created. No broker, mock server or real account is involved.",
            "database": str(store.path), "pilot": record,
            "policies": {mkt: chosen[mkt].payload() for mkt in MARKETS}, "limitations": LIMITATIONS}


def value(store: Store, con, pilot: dict, mkt: str, at: str) -> dict:
    """Cost-adjusted marked NAV of one pilot market at `at`; raises instead of guessing."""
    account = store.account(mkt, con)
    if account["clock"] and at < account["clock"]:
        raise ValidationError(f"{mkt} valuation time {at} precedes its paper clock {account['clock']}")
    policy = RiskPolicy(**account["policy"])
    held = [dict(r) for r in con.execute("SELECT * FROM positions WHERE market=? AND quantity>0 ORDER BY symbol", (mkt,))]
    quotes = {}
    if held:
        quotes = {i["symbol"]: i["quote"] for i in snapshot(store, mkt, at, lookback=1, con=con)["instruments"]}
    gross, lines = Decimal(0), []
    for pos in held:
        quote = quotes.get(pos["symbol"])
        if quote is None:
            raise ValidationError(f"Missing {mkt} paper quote for {pos['symbol']} at {at}")
        age = (datetime.fromisoformat(at) - datetime.fromisoformat(quote["event_at"])).total_seconds()
        if age < 0 or age > policy.max_quote_age_seconds:
            raise ValidationError(f"Stale {mkt} paper quote for {pos['symbol']} ({int(age)}s old at {at})")
        marked = pos["quantity"] * decimal(quote["price"])
        gross += marked
        lines.append({"symbol": pos["symbol"], "quantity": pos["quantity"], "cost_basis": pos["cost_basis"],
                      "quote_price": quote["price"], "quote_event_at": quote["event_at"],
                      "quote_age_seconds": int(age), "marked_value": _plain(marked),
                      "synthetic_quote": bool(quote["synthetic"])})
    nav = decimal(account["cash"]) + gross * exit_factor(policy)
    nav_krw = nav * _krw_factor(pilot, mkt)
    return {"account": account, "policy": policy, "cash": decimal(account["cash"]), "gross": gross,
            "nav": nav, "nav_krw": nav_krw, "loss_krw": decimal(pilot["markets"][mkt]["envelope_krw"]) - nav_krw,
            "positions": lines}


def evaluate(store: Store, con, pilot: dict, at: str) -> tuple[dict, dict]:
    values, errors = {}, {}
    for mkt in MARKETS:
        try:
            values[mkt] = value(store, con, pilot, mkt, at)
        except ValidationError as exc:
            errors[mkt] = str(exc)
    return values, errors


def _thresholds(pilot: dict) -> tuple[dict, Decimal]:
    return ({m: decimal(pilot["markets"][m]["planned_loss_threshold_krw"]) for m in MARKETS},
            decimal(pilot["combined"]["planned_loss_threshold_krw"]))


def enforce(store: Store, con, pilot: dict, at: str) -> tuple[dict, dict]:
    """Latch any threshold reached at `at` and cancel open paper BUYs it covers. Never sells."""
    values, errors = evaluate(store, con, pilot, at)
    per_market, combined = _thresholds(pilot)
    reached = {m: values[m]["loss_krw"] for m in values if values[m]["loss_krw"] >= per_market[m]}
    if not errors:
        total = sum(v["loss_krw"] for v in values.values())
        if total >= combined:
            reached["COMBINED"] = total
    for scope, loss in reached.items():
        limit = combined if scope == "COMBINED" else per_market[scope]
        payload = {"scope": scope, "at": at, "planned_loss_krw": _plain(loss), "threshold_krw": _plain(limit)}
        if con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                       (BREACH_PREFIX + scope, canonical(payload))).rowcount:
            Store.audit(con, "PILOT_THRESHOLD_REACHED", payload)
    breaches = latched(con)
    for mkt in (MARKETS if "COMBINED" in breaches else [m for m in MARKETS if m in breaches]):
        cancelled = con.execute("UPDATE orders SET status='CANCELLED',reason='PILOT_LOSS_THRESHOLD' "
                                "WHERE market=? AND side='BUY' AND status IN (?,?)", (mkt, *ACTIVE)).rowcount
        if cancelled:
            Store.audit(con, "PILOT_BUYS_CANCELLED", {"market": mkt, "orders": cancelled, "at": at})
    return values, errors


def envelope_exceeded(pilot: dict, mkt: str, gross, added) -> bool:
    return gross + added > decimal(pilot["markets"][mkt]["initial_cash"])


def buy_check(store: Store, con, pilot: dict, mkt: str, at: str, extra_local=Decimal(0)) -> str:
    """Reason a new paper BUY in `mkt` is refused at `at`, or "" when allowed.

    `extra_local` is the projected loss of the pending buy(s) in local currency:
    buy cost including fee minus conservative marked value of the shares acquired.
    """
    values, errors = enforce(store, con, pilot, at)
    breaches = latched(con)
    if mkt in breaches or "COMBINED" in breaches:
        return "PILOT_LOSS_THRESHOLD"
    if errors:
        return "PILOT_VALUATION_UNAVAILABLE"
    per_market, combined = _thresholds(pilot)
    extra = extra_local * _krw_factor(pilot, mkt)
    if (values[mkt]["loss_krw"] + extra >= per_market[mkt]
            or sum(v["loss_krw"] for v in values.values()) + extra >= combined):
        return "PILOT_PROJECTED_LOSS_THRESHOLD"
    return ""


def status(store: Store, as_of: str | None = None) -> dict:
    """Read-only valuation of both pilot markets and the KRW total at one point in time."""
    with store.connect() as con:
        pilot = load(con)
        if not pilot:
            raise ValidationError("This database has no paper pilot; create one with pilot-setup on a new --db")
        accounts = {m: store.account(m, con) for m in MARKETS}
        clocks = {m: accounts[m]["clock"] for m in MARKETS}
        if as_of is None:
            known = [c for c in clocks.values() if c]
            if not known:
                raise ValidationError("No paper clock yet; pass --as-of")
            as_of = max(known)
        as_of = timestamp(as_of)
        if as_of > now():
            raise ValidationError("Valuation cannot be in the future")
        for mkt, clock in clocks.items():
            if clock and as_of < clock:
                raise ValidationError(f"as-of precedes the {mkt} paper clock {clock}; "
                                      "historical pilot state is not reconstructed")
        values, errors = evaluate(store, con, pilot, as_of)
        breaches = latched(con)
        open_orders = {m: {side: con.execute("SELECT count(*) FROM orders WHERE market=? AND side=? AND status IN (?,?)",
                                             (m, side, *ACTIVE)).fetchone()[0] for side in ("BUY", "SELL")}
                       for m in MARKETS}
        synthetic = bool(con.execute("SELECT 1 FROM prices WHERE synthetic=1 LIMIT 1").fetchone())
    per_market, combined_limit = _thresholds(pilot)
    total = None if errors else sum(v["loss_krw"] for v in values.values())
    markets = {}
    for mkt in MARKETS:
        spec = pilot["markets"][mkt]
        entry = {"currency": spec["currency"], "initial_cash": spec["initial_cash"], "envelope_krw": spec["envelope_krw"],
                 "planned_loss_threshold_krw": spec["planned_loss_threshold_krw"], "clock": clocks[mkt],
                 "latched_breach": breaches.get(mkt), "open_orders": open_orders[mkt]}
        halted = bool(accounts[mkt]["halted"])
        entry["halted"] = halted
        if mkt in values:
            v = values[mkt]
            entry.update({"valuation": "AVAILABLE", "cash": v["account"]["cash"],
                          "gross_marked_local": _plain(v["gross"]),
                          "envelope_used_pct": _shown(v["gross"] / decimal(spec["initial_cash"]) * 100, CENT, ROUND_CEILING),
                          "cost_adjusted_nav_local": _shown(v["nav"], STEP[mkt], ROUND_FLOOR),
                          "cost_adjusted_nav_krw": _shown(v["nav_krw"], WON, ROUND_FLOOR),
                          "planned_loss_krw": _shown(v["loss_krw"], WON, ROUND_CEILING),
                          "threshold_reached": v["loss_krw"] >= per_market[mkt],
                          "exit_cost_bps_assumed": _plain(decimal(v["policy"].fee_bps) + decimal(v["policy"].slippage_bps)),
                          "positions": v["positions"]})
        else:
            entry.update({"valuation": "UNAVAILABLE", "error": errors[mkt]})
        allowed = (not errors and not halted and mkt not in breaches and "COMBINED" not in breaches
                   and values[mkt]["loss_krw"] < per_market[mkt] and total < combined_limit)
        entry["new_paper_buys"] = "ALLOWED" if allowed else "BLOCKED"
        entry["risk_reducing_paper_sells"] = "BLOCKED (account halted)" if halted else "ALLOWED (normal paper checks apply)"
        markets[mkt] = entry
    combined = {"envelope_krw": pilot["combined"]["envelope_krw"],
                "planned_loss_threshold_krw": pilot["combined"]["planned_loss_threshold_krw"],
                "latched_breach": breaches.get("COMBINED")}
    if errors:
        combined.update({"valuation": "UNAVAILABLE", "unavailable_markets": sorted(errors)})
    else:
        combined.update({"valuation": "AVAILABLE",
                         "cost_adjusted_nav_krw": _shown(sum(v["nav_krw"] for v in values.values()), WON, ROUND_FLOOR),
                         "planned_loss_krw": _shown(total, WON, ROUND_CEILING),
                         "threshold_reached": total >= combined_limit})
    return {"report": "paper_pilot_status",
            "execution": "LOCAL_PAPER_ONLY (not the Kiwoom mock server, not a real account)",
            "as_of": as_of, "synthetic_prices_present": synthetic,
            "fx": {**pilot["fx"], "label": "STATIC setup input; FX moves not modelled"},
            "markets": markets, "combined": combined,
            "convention": pilot["convention"], "limitations": LIMITATIONS}
