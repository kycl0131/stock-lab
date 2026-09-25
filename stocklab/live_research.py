"""Research-only, cost-aware time-series candidate. It cannot place orders and is never the LIVE proposer.

Input: the same validated current-session model snapshot the LIVE proposer received (live_evidence.collect)
and the same best bid/ask facts of that cycle, plus the market's configured cost assumptions (bps). Nothing is
trained or fitted, and no output is a calibrated return forecast.

Rule `stocklab-research-costaware-drift-v1` per candidate (deterministic, Decimal arithmetic):
  window   the newest WINDOW_RETURNS one-minute simple returns r_i (bps); needs WINDOW_RETURNS + 1 same-session
           bars exactly BAR_SECONDS apart (a gap, repeat or missing bar -> HOLD)
  evidence mean m, sample standard deviation s, t = m / (s / sqrt(n)); |t| < MIN_ABS_T or s = 0 -> HOLD
  edge     naive drift extrapolation over HORIZON_MINUTES: e = m * HORIZON_MINUTES (bps). An assumption to be
           tested, not an estimate of expected return.
  hurdle   round trip on the mid: (ask - bid) / mid (buy at ask, sell at bid) + buy fee + sell fee + sell tax
           + 2 x slippage (+ 2 x fx_cost for US, KRW->USD and back)
  signal   BUY a non-sellable symbol if t >= MIN_ABS_T and e > hurdle; SELL a sellable symbol if t <= -MIN_ABS_T
           and -e > hurdle; the largest |e| - hurdle wins (ties by symbol); otherwise HOLD.

`evaluate_safely` never raises; its record is stored in the immutable decision row and nowhere else. This module
does not import the risk engine or the order ledger; live_auto never passes its proposal to risk.plan.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, localcontext
import re

from .domain import ValidationError, digest
from . import live_ai

CANDIDATE_VERSION = "stocklab-research-costaware-drift-v1"
WINDOW_RETURNS = 10
HORIZON_MINUTES = 5
MIN_ABS_T = Decimal("2")
BAR_SECONDS = 60
BPS = Decimal(10000)
PARAMS = {"window_returns": WINDOW_RETURNS, "horizon_minutes": HORIZON_MINUTES, "min_abs_t": str(MIN_ABS_T),
          "bar_seconds": BAR_SECONDS, "edge": "mean_1m_return_bps * horizon_minutes (naive, uncalibrated)",
          "hurdle": "spread_bps + buy_fee + sell_fee + sell_tax + 2*slippage + 2*fx_cost"}
LIVE_ELIGIBLE = False   # never selectable as the LIVE proposer (live_auto.validate_config accepts MODEL/BASELINE only)


def _q(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def hurdle_bps(market, bid: Decimal, ask: Decimal, costs: dict) -> dict:
    """Round-trip cost hurdle in bps of the mid, with its components."""
    if not 0 < bid < ask:
        raise ValidationError("RESEARCH_QUOTE_NOT_TWO_SIDED")
    mid = (bid + ask) / 2
    parts = {"spread_bps": (ask - bid) / mid * BPS,
             "buy_fee_bps": Decimal(costs["buy_fee_bps"]), "sell_fee_bps": Decimal(costs["sell_fee_bps"]),
             "sell_tax_bps": Decimal(costs["sell_tax_bps"]), "slippage_round_trip_bps": 2 * Decimal(costs["slippage_bps"]),
             "fx_round_trip_bps": 2 * Decimal(costs["fx_cost_bps"]) if market == "US" else Decimal(0)}
    if any(not v.is_finite() or v < 0 for v in parts.values()):
        raise ValidationError("RESEARCH_COST_INVALID")
    return {**parts, "hurdle_bps": sum(parts.values(), Decimal(0))}


def _symbol(market, candidate, quote, costs) -> dict:
    """Diagnostics for one candidate; `verdict` is CANDIDATE_BUY / CANDIDATE_SELL or a HOLD reason."""
    out = {"sellable": candidate["sellable"]}
    if quote is None:
        return {**out, "verdict": "NO_QUOTE"}
    cost = hurdle_bps(market, quote["bid"], quote["ask"], costs)
    out.update({k: _q(v) for k, v in cost.items()})
    observations = candidate["observations"][-(WINDOW_RETURNS + 1):]
    out["bars_used"] = len(observations)
    if len(observations) < WINDOW_RETURNS + 1:
        return {**out, "verdict": "INSUFFICIENT_BARS"}
    stamps = [datetime.fromisoformat(o["event_at"]) for o in observations]
    if any(b - a != timedelta(seconds=BAR_SECONDS) for a, b in zip(stamps, stamps[1:])):
        return {**out, "verdict": "NONCONTIGUOUS_BARS"}
    prices = [Decimal(o["price"]) for o in observations]
    returns = [(b / a - 1) * BPS for a, b in zip(prices, prices[1:])]
    n = Decimal(len(returns))
    mean = sum(returns, Decimal(0)) / n
    variance = sum(((r - mean) ** 2 for r in returns), Decimal(0)) / (n - 1)
    out.update({"evidence_ids": [observations[0]["id"], observations[-1]["id"]], "mean_1m_bps": _q(mean),
                "std_1m_bps": _q(variance.sqrt())})
    if variance == 0:
        return {**out, "verdict": "ZERO_VARIANCE"}
    t_stat = mean / (variance.sqrt() / n.sqrt())
    edge = mean * HORIZON_MINUTES
    net = abs(edge) - cost["hurdle_bps"]
    out.update({"t_stat": _q(t_stat), "projected_edge_bps": _q(edge), "net_edge_bps": _q(net), "_net": net})
    if abs(t_stat) < MIN_ABS_T:
        return {**out, "verdict": "WEAK_EVIDENCE"}
    if net <= 0:
        return {**out, "verdict": "BELOW_COST_HURDLE"}
    if edge > 0 and not candidate["sellable"]:
        return {**out, "verdict": "CANDIDATE_BUY"}
    if edge < 0 and candidate["sellable"]:
        return {**out, "verdict": "CANDIDATE_SELL"}
    return {**out, "verdict": "NO_ACTION_FOR_POSITION_STATE"}   # already held and rising / not held and falling


def input_hash(snapshot, quotes, costs) -> str:
    """Hash of everything the rule reads: version, parameters, snapshot, quote facts, cost assumptions."""
    return digest({"version": CANDIDATE_VERSION, "params": PARAMS, "model_snapshot_hash": digest(snapshot),
                   "quotes": {s: {"bid": f"{q['bid']:f}", "ask": f"{q['ask']:f}"} for s, q in sorted(quotes.items())},
                   "costs": costs})


def evaluate(snapshot, quotes, costs) -> dict:
    """Research record for one cycle. `quotes`: symbol -> {"bid", "ask"} (Decimal) from the same evidence."""
    safe = live_ai.validate_snapshot(snapshot)
    market = safe["market"]
    with localcontext() as ctx:   # fixed precision: results do not depend on a caller's decimal context
        ctx.prec = 28
        diagnostics = {c["symbol"]: _symbol(market, c, quotes.get(c["symbol"]), costs) for c in safe["candidates"]}
    actionable = sorted(((d.pop("_net"), s, d) for s, d in diagnostics.items() if "_net" in d), key=lambda x: x[1])
    picks = [(net, s, d) for net, s, d in actionable if d["verdict"] in ("CANDIDATE_BUY", "CANDIDATE_SELL")]
    if picks:
        net, symbol, d = max(picks, key=lambda x: x[0])   # max keeps the first (lowest symbol) on ties
        action = "BUY" if d["verdict"] == "CANDIDATE_BUY" else "SELL"
        proposal = {"action": action, "symbol": symbol, "evidence_ids": d["evidence_ids"],
                    "reason": f"{CANDIDATE_VERSION}: t {d['t_stat']}, edge {d['projected_edge_bps']} bps > "
                              f"hurdle {d['hurdle_bps']} bps (research only, uncalibrated)"}
    else:
        proposal = {**live_ai.HOLD, "reason": f"{CANDIDATE_VERSION}: no candidate with evidence above cost hurdle"}
    proposal = live_ai.validate_proposal(proposal, safe)
    return {"kind": "RESEARCH_ONLY", "live_eligible": LIVE_ELIGIBLE, "version": CANDIDATE_VERSION,
            "params": PARAMS, "input_hash": input_hash(safe, quotes, costs), "model_snapshot_hash": digest(safe),
            "costs": costs, "proposal": proposal, "diagnostics": diagnostics, "error": None}


def evaluate_safely(snapshot, quotes, costs) -> dict:
    """evaluate() that never raises: any failure is a HOLD record with an error category."""
    try:
        return evaluate(snapshot, quotes, costs)
    except ValidationError as exc:
        error = str(exc)[:200]
    except Exception as exc:  # defensive: a research bug must not affect the cycle
        error = "UNEXPECTED_" + re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:60]
    return {"kind": "RESEARCH_ONLY", "live_eligible": LIVE_ELIGIBLE, "version": CANDIDATE_VERSION,
            "params": PARAMS, "input_hash": None, "model_snapshot_hash": None, "costs": costs,
            "proposal": {**live_ai.HOLD, "reason": f"{CANDIDATE_VERSION}: evaluation failed; abstain"},
            "diagnostics": {}, "error": error}
