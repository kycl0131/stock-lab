"""OFFLINE historical SIGNAL test of the current rules on local public minute bars. Places no orders.

    python -m stocklab.historical_eval --input bars.csv --output report.json [--spread-bps 10]

This answers "what if the historical BUY signals had been filled?" for ONE market and ONE symbol. It is a
signal test, not a reproduction of the autonomous trading engine: there is no SELL path, no live order,
risk, sizing, portfolio, calendar or reconciliation simulation, and no broker, network or model call.

Input CSV columns (exactly): market,symbol,at_utc,open,high,low,close,volume,source. Rows must be one market
and one symbol, strictly ascending unique whole-minute UTC timestamps (explicit +00:00 or Z), positive and
consistent OHLC, a non-negative integer volume, one non-synthetic source (default
`kiwoom-real-readonly-minute`) and regular-session bars only (KR 09:00-15:30 KST, US 09:30-16:00
America/New_York on weekdays; holidays and early closes are not modelled - a session is simply the local
date of the bars present). Missing minutes are never filled and nothing crosses sessions.

Decision at the close of bar t, for each rule separately:
  snapshot  only the WINDOW_BARS most recent closes t-10..t, all exactly one minute apart in one session,
            candidate sellable=false (long-only entry signals; SELL cannot be proposed)
  quote     ASSUMED bid/ask = close_t x (1 -/+ spread/2); a fixed illustrative spread, not a historical quote
  rules     live_ai.baseline (its default entry/exit thresholds) and live_research.evaluate_safely, called
            directly; neither is reimplemented here
Hypothetical long round trip on BUY: entry at the open of bar t+1, exit at the close of bar t+5; scored only if
bars t..t+5 are contiguous in one session. Decisions at bars t+1..t+5 are skipped (no overlapping trades).
  entry price x (1 + (spread/2 + buy_fee + slippage [+ fx for US]) / 10000)
  exit  price x (1 - (spread/2 + sell_fee + sell_tax + slippage [+ fx for US]) / 10000)
Fee, tax, slippage, FX and spread values are illustrative inputs, not verified user fee rates or quotes.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import inspect
import json
import re
import statistics
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

from . import live_ai, live_research
from .domain import ValidationError
from .live_calendar import CalendarError, utc_to_local

REPORT_VERSION = "stocklab-historical-signal-eval-v1"
COLUMNS = ("market", "symbol", "at_utc", "open", "high", "low", "close", "volume", "source")
DEFAULT_SOURCE = "kiwoom-real-readonly-minute"
SYNTHETIC_MARKERS = re.compile(r"synthetic|demo|mock|fake|fixture|sample|simulat", re.IGNORECASE)
REGULAR_HOURS = {"KR": (dtime(9, 0), dtime(15, 30)), "US": (dtime(9, 30), dtime(16, 0))}
SYMBOL_PATTERN = {"KR": r"[0-9]{6}", "US": r"[A-Z]{1,5}"}
# validate_snapshot requires an exchange code; neither rule reads it.
SNAPSHOT_EXCHANGE = {"KR": "KRX", "US": "ND"}
WINDOW_BARS = 11          # = live_research.WINDOW_RETURNS + 1 (checked by tests)
HOLD_MINUTES = 5          # = live_research.HORIZON_MINUTES (checked by tests)
BAR = timedelta(minutes=1)
BPS = Decimal(10000)
DEFAULT_SPREAD_BPS = Decimal("10")
# Illustrative per-side cost assumptions in bps, keyed exactly as live_research.hurdle_bps expects.
DEFAULT_COSTS = {
    "KR": {"buy_fee_bps": "1.5", "sell_fee_bps": "1.5", "sell_tax_bps": "20", "slippage_bps": "10",
           "fx_cost_bps": "0"},
    "US": {"buy_fee_bps": "25", "sell_fee_bps": "25", "sell_tax_bps": "0", "slippage_bps": "10",
           "fx_cost_bps": "10"},
}
MIN_TRADES, MIN_SESSIONS = 30, 20
MODELS = ("baseline", "research")
_PRICE = re.compile(r"[0-9]+(\.[0-9]+)?")
_VOLUME = re.compile(r"[0-9]+")


class InputError(ValidationError):
    pass


def _q(value: Decimal, places: str = "0.0001") -> str:
    return f"{value.quantize(Decimal(places))}"


def _price(value: str, name: str, line: int) -> Decimal:
    if not _PRICE.fullmatch(value or ""):
        raise InputError(f"line {line}: {name} is not a plain positive decimal")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise InputError(f"line {line}: {name} is invalid") from None
    if number <= 0:
        raise InputError(f"line {line}: {name} must be positive")
    return number


def _stamp(value: str, line: int) -> datetime:
    try:
        at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise InputError(f"line {line}: at_utc is not an ISO-8601 timestamp") from None
    if at.tzinfo is None or at.utcoffset() is None:
        raise InputError(f"line {line}: at_utc must be timezone-aware")
    if at.utcoffset() != timedelta(0):
        raise InputError(f"line {line}: at_utc must be UTC (+00:00 or Z)")
    if at.second or at.microsecond:
        raise InputError(f"line {line}: at_utc must be a whole minute")
    return at.astimezone(timezone.utc)


def regular_session(market: str, at: datetime) -> str | None:
    """Local session date (YYYY-MM-DD) if `at` is inside regular hours on a weekday, else None."""
    try:
        local = utc_to_local(market, at)
    except CalendarError:
        return None
    opened, closed = REGULAR_HOURS[market]
    if local.weekday() >= 5 or not opened <= local.time() <= closed:
        return None
    return local.date().isoformat()


def load_bars(path, *, expected_source: str = DEFAULT_SOURCE, drop_outside_session: bool = False) -> dict:
    """Validated ascending bars of one market/symbol. Raises InputError; never fills or reorders anything."""
    if SYNTHETIC_MARKERS.search(expected_source or "") or not expected_source:
        raise InputError("expected source must name a real (non-synthetic) data source")
    raw = Path(path).read_bytes()
    reader = csv.DictReader(raw.decode("utf-8-sig").splitlines())
    if tuple(reader.fieldnames or ()) != COLUMNS:
        raise InputError(f"CSV header must be exactly: {','.join(COLUMNS)}")
    bars, dropped, market, symbol, previous = [], 0, None, None, None
    for line, row in enumerate(reader, start=2):
        if None in row or any(v is None for v in row.values()):
            raise InputError(f"line {line}: wrong number of columns")
        source = row["source"]
        if SYNTHETIC_MARKERS.search(source):
            raise InputError(f"line {line}: synthetic or demo data is rejected")
        if source != expected_source:
            raise InputError(f"line {line}: source must be {expected_source!r}")
        if market is None:
            market, symbol = row["market"], row["symbol"]
            if market not in REGULAR_HOURS:
                raise InputError(f"line {line}: market must be KR or US")
            if not re.fullmatch(SYMBOL_PATTERN[market], symbol):
                raise InputError(f"line {line}: symbol is invalid for {market}")
        elif (row["market"], row["symbol"]) != (market, symbol):
            raise InputError(f"line {line}: exactly one market and one symbol per file")
        at = _stamp(row["at_utc"], line)
        if previous is not None and at <= previous:
            raise InputError(f"line {line}: at_utc must be strictly ascending and unique")
        previous = at
        o, h, l, c = (_price(row[k], k, line) for k in ("open", "high", "low", "close"))
        if not (l <= min(o, c) and h >= max(o, c) and l <= h):
            raise InputError(f"line {line}: OHLC is inconsistent")
        if not _VOLUME.fullmatch(row["volume"] or ""):
            raise InputError(f"line {line}: volume must be a non-negative integer")
        session = regular_session(market, at)
        if session is None:
            if not drop_outside_session:
                raise InputError(f"line {line}: bar is outside the {market} regular session "
                                 "(use --drop-outside-session to drop such rows explicitly)")
            dropped += 1
            continue
        bars.append({"at": at, "session": session, "open": o, "high": h, "low": l, "close": c,
                     "volume": int(row["volume"])})
    if not bars:
        raise InputError("no regular-session bars")
    return {"market": market, "symbol": symbol, "source": expected_source, "bars": bars,
            "rows": len(bars) + dropped, "rows_dropped_outside_session": dropped,
            "sha256": hashlib.sha256(raw).hexdigest()}


def _linked(bars: list, i: int) -> bool:
    """Bar i+1 follows bar i by exactly one minute in the same session."""
    return bars[i + 1]["at"] - bars[i]["at"] == BAR and bars[i + 1]["session"] == bars[i]["session"]


def contiguity(bars: list) -> tuple[list[bool], list[bool]]:
    """has_history[t]: bars t-10..t contiguous; has_path[t]: bars t..t+5 contiguous (same session)."""
    n = len(bars)
    back = [0] * n            # number of contiguous links ending at t
    for t in range(1, n):
        back[t] = back[t - 1] + 1 if _linked(bars, t - 1) else 0
    ahead = [0] * n           # number of contiguous links starting at t
    for t in range(n - 2, -1, -1):
        ahead[t] = ahead[t + 1] + 1 if _linked(bars, t) else 0
    return [b >= WINDOW_BARS - 1 for b in back], [a >= HOLD_MINUTES for a in ahead]


def decision_snapshot(market: str, symbol: str, window: list) -> dict:
    """Model snapshot from exactly the WINDOW_BARS bars ending at the decision bar (nothing later)."""
    if len(window) != WINDOW_BARS:
        raise ValidationError("decision window must hold exactly WINDOW_BARS bars")
    return {"schema": live_ai.PROMPT_VERSION, "market": market, "as_of": window[-1]["at"].isoformat(),
            "candidates": [{"symbol": symbol, "exchange": SNAPSHOT_EXCHANGE[market], "sellable": False,
                            "observations": [{"id": f"{symbol}-{b['at']:%Y%m%d%H%M}", "event_at": b["at"].isoformat(),
                                              "price": f"{b['close']:f}", "volume": str(b["volume"])}
                                             for b in window]}]}


def assumed_quote(close: Decimal, spread_bps: Decimal) -> dict:
    """Explicit assumption: a fixed round-trip spread centred on the decision bar's close."""
    half = spread_bps / 2 / BPS
    return {"bid": close * (1 - half), "ask": close * (1 + half)}


def decide(model: str, market: str, symbol: str, window: list, costs: dict, spread_bps: Decimal) -> tuple[str, str | None]:
    """(action, error) for one decision; uses only `window`."""
    snapshot = decision_snapshot(market, symbol, window)
    if model == "baseline":
        return live_ai.baseline(snapshot)["action"], None
    record = live_research.evaluate_safely(snapshot, {symbol: assumed_quote(window[-1]["close"], spread_bps)}, costs)
    return record["proposal"]["action"], record["error"]


def side_costs_bps(market: str, costs: dict, spread_bps: Decimal) -> tuple[Decimal, Decimal]:
    """(entry, exit) one-way cost in bps of the traded price."""
    half = spread_bps / 2
    fx = Decimal(costs["fx_cost_bps"]) if market == "US" else Decimal(0)
    entry = half + Decimal(costs["buy_fee_bps"]) + Decimal(costs["slippage_bps"]) + fx
    exit_ = half + Decimal(costs["sell_fee_bps"]) + Decimal(costs["sell_tax_bps"]) + Decimal(costs["slippage_bps"]) + fx
    return entry, exit_


def roundtrip(entry_open: Decimal, exit_close: Decimal, entry_cost_bps: Decimal, exit_cost_bps: Decimal) -> dict:
    gross = exit_close / entry_open - 1
    net = exit_close * (1 - exit_cost_bps / BPS) / (entry_open * (1 + entry_cost_bps / BPS)) - 1
    return {"gross": gross, "net": net}


def simulate(model: str, data: dict, costs: dict, spread_bps: Decimal) -> dict:
    market, symbol, bars = data["market"], data["symbol"], data["bars"]
    has_history, has_path = contiguity(bars)
    entry_cost, exit_cost = side_costs_bps(market, costs, spread_bps)
    counts = {"evaluated_decisions": 0, "BUY": 0, "HOLD": 0, "other_action": 0, "rule_errors": 0,
              "skipped_while_trade_open": 0}
    trades, next_allowed = [], 0
    for t in range(len(bars)):
        if not (has_history[t] and has_path[t]):
            continue
        if t < next_allowed:
            counts["skipped_while_trade_open"] += 1
            continue
        action, error = decide(model, market, symbol, bars[t - WINDOW_BARS + 1:t + 1], costs, spread_bps)
        counts["evaluated_decisions"] += 1
        counts["rule_errors"] += error is not None
        counts[action if action in ("BUY", "HOLD") else "other_action"] += 1
        if action != "BUY":
            continue
        entry, exit_bar = bars[t + 1], bars[t + HOLD_MINUTES]
        result = roundtrip(entry["open"], exit_bar["close"], entry_cost, exit_cost)
        trades.append({"session": bars[t]["session"], "decision_at": bars[t]["at"].isoformat(),
                       "entry_at": entry["at"].isoformat(), "exit_at": exit_bar["at"].isoformat(),
                       "entry_open": f"{entry['open']:f}", "exit_close": f"{exit_bar['close']:f}",
                       "gross": result["gross"], "net": result["net"]})
        next_allowed = t + HOLD_MINUTES + 1
    return {**counts, **summarize(trades)}


def summarize(trades: list) -> dict:
    net_bps = [t["net"] * BPS for t in trades]
    equity, peak, max_dd = Decimal(1), Decimal(1), Decimal(0)
    for t in trades:
        equity *= 1 + t["net"]
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
    sessions = len({t["session"] for t in trades})
    reasons = []
    if len(trades) < MIN_TRADES:
        reasons.append(f"fewer than {MIN_TRADES} trades")
    if sessions < MIN_SESSIONS:
        reasons.append(f"fewer than {MIN_SESSIONS} independent sessions with trades")
    return {
        "trades": len(trades), "trade_sessions": sessions,
        "gross_winning_trades": sum(t["gross"] > 0 for t in trades),
        "gross_win_rate": _q(Decimal(sum(t["gross"] > 0 for t in trades)) / len(trades)) if trades else None,
        "net_winning_trades": sum(t["net"] > 0 for t in trades),
        "net_win_rate": _q(Decimal(sum(t["net"] > 0 for t in trades)) / len(trades)) if trades else None,
        "mean_net_bps": _q(sum(net_bps, Decimal(0)) / len(net_bps)) if trades else None,
        "median_net_bps": _q(statistics.median(net_bps)) if trades else None,
        "compounded_return_pct": _q((equity - 1) * 100),
        "max_drawdown_pct": _q(max_dd * 100),
        "minimum_historical_sample_gate_passed": not reasons,
        "future_accuracy_validated": False,
        "insufficient_reasons": reasons,
        "trade_list": [{**{k: v for k, v in t.items() if k not in ("gross", "net")},
                        "gross_bps": _q(t["gross"] * BPS), "net_bps": _q(t["net"] * BPS)} for t in trades],
    }


def _baseline_params() -> dict:
    params = inspect.signature(live_ai.baseline).parameters
    return {"entry_bps": params["entry_bps"].default, "exit_bps": params["exit_bps"].default}


def evaluate(data: dict, *, spread_bps: Decimal = DEFAULT_SPREAD_BPS, cost_overrides: dict | None = None) -> dict:
    spread_bps = Decimal(spread_bps)
    if not spread_bps.is_finite() or not 0 < spread_bps < 1000:
        raise InputError("spread must be between 0 and 1000 bps (exclusive)")
    market, bars = data["market"], data["bars"]
    costs = dict(DEFAULT_COSTS[market])
    if cost_overrides:
        for name, raw in cost_overrides.items():
            if name not in costs:
                raise InputError(f"unknown cost field: {name}")
            try:
                value = Decimal(raw)
            except (InvalidOperation, TypeError):
                raise InputError(f"{name} must be a number of bps") from None
            if not value.is_finite() or not 0 <= value < 1000:
                raise InputError(f"{name} must be between 0 and 1000 bps")
            costs[name] = f"{value:f}"
    has_history, has_path = contiguity(bars)
    entry_cost, exit_cost = side_costs_bps(market, costs, spread_bps)
    with localcontext() as ctx:
        ctx.prec = 28
        models = {m: simulate(m, data, costs, spread_bps) for m in MODELS}
    return {
        "report_version": REPORT_VERSION, "kind": "OFFLINE_HISTORICAL_SIGNAL_TEST",
        "notice": ("Hypothetical fills of historical long-entry signals under illustrative cost and spread "
                   "assumptions. Not an exact reproduction of the autonomous trading engine (no SELL path, "
                   "order, risk, sizing or portfolio simulation), not verified fee rates or historical quotes, "
                   "and not evidence of future performance. No broker, network or model call was made."),
        "input": {"market": market, "symbol": data["symbol"], "source": data["source"], "sha256": data["sha256"],
                  "rows": data["rows"], "rows_dropped_outside_session": data["rows_dropped_outside_session"],
                  "bars_used": len(bars), "first_at": bars[0]["at"].isoformat(), "last_at": bars[-1]["at"].isoformat(),
                  "sessions": len({b["session"] for b in bars})},
        "windows": {"decision_bars_with_history": sum(has_history),
                    "eligible_windows": sum(h and p for h, p in zip(has_history, has_path))},
        "assumptions": {
            "window_bars": WINDOW_BARS, "hold_minutes": HOLD_MINUTES, "bar_seconds": 60,
            "sellable": False, "long_only": True, "sell_path": "none",
            "decision": "at the close of bar t using only bars t-10..t",
            "entry": "open of bar t+1", "exit": "close of bar t+5",
            "scoring": "only if bars t..t+5 are contiguous one-minute bars in the same regular session",
            "overlap": "decisions at bars t+1..t+5 are skipped after a BUY",
            "regular_hours_local": {m: [a.strftime("%H:%M"), b.strftime("%H:%M")] for m, (a, b) in REGULAR_HOURS.items()},
            "session_calendar": "not modelled; a session is the local date of the bars present",
            "quote": "assumed bid/ask = close_t x (1 -/+ spread/2); not a historical quote",
            "spread_bps_round_trip": f"{spread_bps:f}",
            "costs_bps": costs,
            "entry_cost_bps": f"{entry_cost:f}", "exit_cost_bps": f"{exit_cost:f}",
            "cost_note": "illustrative inputs, not verified user fee rates, taxes, slippage or FX costs",
            "snapshot_exchange_placeholder": SNAPSHOT_EXCHANGE[market],
            "compounding": "sequential one-unit hypothetical equity, product of (1 + net return)",
            "min_trades": MIN_TRADES, "min_sessions": MIN_SESSIONS,
        },
        "model_versions": {"baseline": {"version": live_ai.BASELINE_VERSION, "params": _baseline_params()},
                           "research": {"version": live_research.CANDIDATE_VERSION,
                                        "params": live_research.PARAMS}},
        "models": models,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stocklab.historical_eval",
                                     description="Offline historical signal test on local minute bars (no orders).")
    parser.add_argument("--input", required=True, help="local CSV: " + ",".join(COLUMNS))
    parser.add_argument("--output", required=True, help="JSON report path")
    parser.add_argument("--spread-bps", default=str(DEFAULT_SPREAD_BPS),
                        help="assumed round-trip spread around the decision close (default 10)")
    for name in DEFAULT_COSTS["KR"]:
        parser.add_argument("--" + name.replace("_", "-"), default=None,
                            help=f"override illustrative {name} using the user's verified fee schedule, in bps")
    parser.add_argument("--expected-source", default=DEFAULT_SOURCE, help=f"required source value (default {DEFAULT_SOURCE})")
    parser.add_argument("--drop-outside-session", action="store_true",
                        help="drop (and count) rows outside regular hours instead of rejecting the file")
    args = parser.parse_args(argv)
    try:
        if Path(args.output).resolve() == Path(args.input).resolve():
            raise InputError("output must differ from input")
        try:
            spread = Decimal(args.spread_bps)
        except InvalidOperation:
            raise InputError("spread must be a number of bps") from None
        data = load_bars(args.input, expected_source=args.expected_source,
                         drop_outside_session=args.drop_outside_session)
        overrides = {name: getattr(args, name) for name in DEFAULT_COSTS["KR"] if getattr(args, name) is not None}
        report = evaluate(data, spread_bps=spread, cost_overrides=overrides)
    except (ValidationError, OSError, UnicodeDecodeError) as exc:
        print(f"historical_eval: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{report['kind']} {report['input']['market']} {report['input']['symbol']}: "
          f"{report['input']['rows']} rows, {report['input']['sessions']} sessions, "
          f"{report['windows']['eligible_windows']} eligible windows")
    for name, m in report["models"].items():
        print(f"  {name}: BUY {m['BUY']} / HOLD {m['HOLD']}, trades {m['trades']}, net win rate {m['net_win_rate']}, "
              f"mean net {m['mean_net_bps']} bps, hypothetical one-unit compounded "
              f"{m['compounded_return_pct']}%, hypothetical max DD {m['max_drawdown_pct']}%"
              + ("" if m["minimum_historical_sample_gate_passed"] else " [SMALL SAMPLE]"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
