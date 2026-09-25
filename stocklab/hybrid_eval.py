"""OFFLINE historical test of the two-stage path: time-series forecast, optionally followed by Codex. No orders.

    python -m stocklab.hybrid_eval --input bars.csv --output artifacts/hybrid.json                 # forecasts only
    python -m stocklab.hybrid_eval --input bars.csv --output artifacts/hybrid.json \
        --model <exact Codex model id> --max-codex-calls 20                                      # + Codex calls

Split     the forecaster (ts_forecast.train) is fitted only on complete sessions strictly before the test sessions:
          the last --holdout-sessions (default 5) or every session after --train-end-session. Its parameters are
          fixed before any test bar is read; nothing is tuned on the test sessions.
Schedule  predetermined from bar availability only (never prices or outcomes): in each test session the first bar t
          with a full contiguous 31-bar window and a full contiguous 30-minute exit path is a decision point, the
          next one is at t+31 or later, and so on. Decision points never overlap.
Model-only  BUY iff predicted_net_bps > 0 (the LIVE forecast gate), else HOLD.
Hybrid    at a model-only BUY, while fewer than --max-codex-calls calls were made, the exact LIVE proposer
          live_ai.decide(..., forecast=...) runs once on an anonymised snapshot (symbol replaced, dates shifted to
          2000-01-03, prices rescaled to 100 at the window start, volumes relative to the window mean) with the
          forecast; the same gate then applies. Where the model-only rule HOLDs the gate would reject any Codex BUY
          (SELL is not offered: sellable=false), so no call is made. The first Codex error is a HOLD and stops all
          later calls (no retry); the hybrid arm is then reported as incomplete.
Scoring   BUY: entry at the open of t+1, exit at the close of t+30, with historical_eval's illustrative spread and
          cost assumptions. The fixed 30-minute exit is a test convention, not a Codex SELL; there is no broker,
          order, risk, sizing, portfolio or calendar simulation.
News      optional (--news-archive, a news_collect archive). At each decision point the LIVE rule applies with the
          decision time in place of now: every required source needs an OK archive-committed run within
          --news-max-age-seconds, and only records with max(available_at, run.committed_at) <= decision time are
          used. Covered points are reported; with Codex, a covered model-only BUY gets three calls with the same
          snapshot and forecast: without news, with news instructions/coverage but no items, and with actual items.
          The four arms (including model-only) use exactly those covered windows. These are independent single
          samples, not a causal or profitability estimate. With no covered point, no news result is produced. News
          titles are not anonymised: they can reveal the issuer and period to the model.
    python -m stocklab.hybrid_eval --input bars.csv --output artifacts/hybrid.json --model <id> \
        --max-codex-calls 40 --news-archive data/news/archive.json --news-sources gdelt,opendart
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path
import re
import sys

from . import historical_eval as he, live_ai, news as nw, ts_forecast as ts
from .domain import ValidationError

REPORT_VERSION = "stocklab-hybrid-eval-v1"
DEFAULT_HOLDOUT_SESSIONS = 5
MAX_CODEX_CALLS = 200
ANON_SYMBOL = {"KR": "000000", "US": "XXXX"}
ANON_DATE = date(2000, 1, 3)
NOTICE = ("Offline historical test. The forecaster was fitted on earlier sessions only and the decision schedule "
          "was fixed from bar availability. Hypothetical fills under illustrative cost and spread assumptions; the "
          "fixed 30-minute exit is a test convention, not a Codex SELL and not the live trading engine (no broker, "
          "order, risk, sizing or portfolio simulation). A few sessions are a small sample: this is not evidence of "
          "future performance or profitability.")


def split(bars, *, holdout_sessions=None, train_end_session=None) -> tuple[list[str], list[str]]:
    """(train, test) session lists; every training session precedes every test session."""
    sessions = sorted({b["session"] for b in bars})
    if train_end_session is not None:
        train, test = ts.split_sessions(bars, train_end_session)
    else:
        n = DEFAULT_HOLDOUT_SESSIONS if holdout_sessions is None else holdout_sessions
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n < len(sessions):
            raise he.InputError("--holdout-sessions must be at least 1 and leave earlier sessions for training")
        train, test = sessions[:-n], sessions[-n:]
    if not train or not test or max(train) >= min(test):
        raise he.InputError("need training sessions strictly before at least one test session")
    return train, test


def schedule(bars, test_sessions) -> list[int]:
    back, ahead = ts.link_counts(bars)
    points, next_allowed = [], 0
    for t, bar in enumerate(bars):
        if t >= next_allowed and bar["session"] in test_sessions and back[t] >= ts.WINDOW_BARS - 1 \
                and ahead[t] >= ts.HORIZON_MINUTES:
            points.append(t)
            next_allowed = t + ts.HORIZON_MINUTES + 1
    return points


def anon_shift(window: list) -> timedelta:
    return timedelta(days=(date.fromisoformat(window[-1]["session"]) - ANON_DATE).days)


def anonymized_snapshot(market: str, window: list) -> dict:
    """Snapshot of exactly the decision window with identity and calendar date removed."""
    shift = anon_shift(window)
    base = window[0]["close"]
    mean_volume = Decimal(sum(b["volume"] for b in window)) / len(window)
    symbol = ANON_SYMBOL[market]
    observations = [{"id": f"{symbol}-{i:02d}", "event_at": (b["at"] - shift).isoformat(),
                     "price": f"{(b['close'] / base * 100).quantize(Decimal('0.0001'))}",
                     "volume": str(int((Decimal(b["volume"]) * 1000 / mean_volume).to_integral_value()))
                     if mean_volume > 0 else "0"}
                    for i, b in enumerate(window)]
    return {"schema": live_ai.PROMPT_VERSION, "market": market, "as_of": observations[-1]["event_at"],
            "candidates": [{"symbol": symbol, "exchange": he.SNAPSHOT_EXCHANGE[market], "sellable": False,
                            "observations": observations}]}


def _trade(bars, t, entry_cost, exit_cost) -> dict:
    entry, exit_bar = bars[t + 1], bars[t + ts.HORIZON_MINUTES]
    result = he.roundtrip(entry["open"], exit_bar["close"], entry_cost, exit_cost)
    return {"session": bars[t]["session"], "decision_at": bars[t]["at"].isoformat(),
            "entry_at": entry["at"].isoformat(), "exit_at": exit_bar["at"].isoformat(),
            "entry_open": f"{entry['open']:f}", "exit_close": f"{exit_bar['close']:f}",
            "gross": result["gross"], "net": result["net"]}


def news_at(news_cfg: dict, market: str, symbol: str, window: list, snapshot_as_of: str) -> tuple:
    """(news object or None, reason) for the decision bar window[-1], under the LIVE freshness rule with the
    decision time as `now`. The object is anonymised like the snapshot (symbol label, same date shift)."""
    try:
        obj, _ = nw.evidence_object(
            news_cfg["archive"], market=market, candidates=[(symbol, ANON_SYMBOL[market])],
            sources=news_cfg["sources"], as_of=window[-1]["at"], as_of_text=snapshot_as_of,
            max_age_s=news_cfg["max_status_age_seconds"], lookback_s=news_cfg["lookback_hours"] * 3600,
            max_items_per_symbol=news_cfg["max_items_per_symbol"], shift=anon_shift(window))
        return obj, None
    except nw.NewsError as exc:
        return None, str(exc).split(":", 2)[-1][:80]


def _call(decide, codex, snapshot, forecast, news, state, record, prefix):
    """One LIVE proposer call; the first error stops every later call. Returns the action (HOLD on error)."""
    proposal, meta = decide(snapshot, provider="codex_cli", model=codex["model"], timeout=codex["timeout"],
                            forecast=forecast, **({"news": news} if news is not None else {}))
    state["calls"] += 1
    if meta.get("error"):
        state["errors"] += 1
        state["stopped"] = "CODEX_ERROR"
        record[f"{prefix}_error"] = str(meta["error"])[:200]
        return "HOLD"
    record[f"{prefix}_forecast_gate"] = meta.get("forecast_gate")
    return proposal["action"]


def evaluate(data: dict, *, train_sessions, test_sessions, spread_bps, costs, codex=None,
             decide=None, news_cfg=None) -> dict:
    """`codex`: None, or {"model", "max_calls", "timeout"}. `decide` defaults to the LIVE live_ai.decide.
    `news_cfg`: None, or {"archive", "archive_sha256", "sources", "max_status_age_seconds", "lookback_hours",
    "max_items_per_symbol"}; it never changes the split, schedule, costs or model-only decisions."""
    decide = decide or live_ai.decide
    market, symbol, bars = data["market"], data["symbol"], data["bars"]
    artifact = ts.train(data, train_sessions)          # training sessions only
    entry_cost, exit_cost = he.side_costs_bps(market, costs, spread_bps)
    roundtrip_cost = entry_cost + exit_cost
    points = schedule(bars, set(test_sessions))
    state = {"calls": 0, "errors": 0, "stopped": None}
    model_trades, window_trades, hybrid_trades, decisions, predicted, realized = [], [], [], [], [], []
    news_arms = {"model_only": [], "codex_without_news": [], "codex_news_empty": [], "codex_with_news": []}
    pairwise_actions = {"codex_without_news_to_empty": {}, "codex_empty_to_news": {}}
    news_windows, uncovered = 0, {}
    for t in points:
        gross = ts.predict(artifact, ts.window_features(bars, t))
        entry = ts.forecast_entry(artifact, symbol, gross, roundtrip_cost)
        predicted.append(gross)
        realized.append(ts.target_bps(bars[t]["close"], bars[t + ts.HORIZON_MINUTES]["close"]))
        model_action = "BUY" if Decimal(entry["predicted_net_bps"]) > 0 else "HOLD"
        trade = _trade(bars, t, entry_cost, exit_cost) if model_action == "BUY" else None
        if trade:
            model_trades.append(trade)
        record = {"decision_at": bars[t]["at"].isoformat(), "session": bars[t]["session"],
                  "predicted_gross_bps": entry["predicted_gross_bps"],
                  "predicted_net_bps": entry["predicted_net_bps"], "model_only": model_action}
        window = bars[t - ts.WINDOW_BARS + 1:t + 1]
        snapshot = anonymized_snapshot(market, window)
        news_obj = None
        if news_cfg is not None:
            news_obj, reason = news_at(news_cfg, market, symbol, window, snapshot["as_of"])
            record["news"] = {"covered": news_obj is not None, "items": len(news_obj["items"]) if news_obj else 0,
                              "reason": reason}
            if reason:
                uncovered[reason] = uncovered.get(reason, 0) + 1
        if codex is not None and state["stopped"] is None:
            hybrid_action, empty_news_action, news_action = "HOLD", "HOLD", "HOLD"
            needed = (1 + (2 if news_obj is not None else 0)) if model_action == "BUY" else 0
            if needed and state["calls"] + needed > codex["max_calls"]:
                state["stopped"] = "MAX_CODEX_CALLS_REACHED"   # this and later decisions are outside the window
            elif needed:
                forecast = ts.forecast_object([ts.forecast_entry(artifact, ANON_SYMBOL[market], gross,
                                                                 roundtrip_cost)])
                hybrid_action = _call(decide, codex, snapshot, forecast, None, state, record, "codex")
                if news_obj is not None and state["stopped"] is None:
                    empty_news = {**news_obj, "items": []}
                    empty_news_action = _call(decide, codex, snapshot, forecast, empty_news, state, record,
                                              "codex_news_empty")
                if news_obj is not None and state["stopped"] is None:
                    news_action = _call(decide, codex, snapshot, forecast, news_obj, state, record, "codex_news")
            # A failed/partial call is not an observed HOLD and must not enter a performance comparison. In
            # particular, do not count a news window unless both Codex arms completed on that same decision.
            if state["stopped"] is None:
                record["hybrid"] = hybrid_action
                if trade:
                    window_trades.append(trade)
                if hybrid_action == "BUY":
                    hybrid_trades.append(trade)
                if news_obj is not None:                 # same covered window for all completed arms
                    news_windows += 1
                    record["hybrid_with_empty_news"] = empty_news_action
                    record["hybrid_with_news"] = news_action
                    for arm, action in (("model_only", model_action), ("codex_without_news", hybrid_action),
                                        ("codex_news_empty", empty_news_action), ("codex_with_news", news_action)):
                        if action == "BUY":
                            news_arms[arm].append(trade)
                    for pair, first, second in (("codex_without_news_to_empty", hybrid_action, empty_news_action),
                                                ("codex_empty_to_news", empty_news_action, news_action)):
                        transition = "UNCHANGED" if first == second else f"{first}_TO_{second}"
                        pairwise_actions[pair][transition] = pairwise_actions[pair].get(transition, 0) + 1
            elif trade and any(key in record for key in
                               ("codex_error", "codex_news_empty_error", "codex_news_error")):
                # Keep the model-only baseline for the attempted decision, but leave failed Codex arms unscored.
                window_trades.append(trade)
        decisions.append(record)
    covered = sum(1 for d in decisions if d.get("news", {}).get("covered"))
    with localcontext() as ctx:
        ctx.prec = 28
        results = {"model_only": he.summarize(model_trades)}
        if codex is not None:
            results["model_only_same_window"] = he.summarize(window_trades)
            results["hybrid"] = he.summarize(hybrid_trades)
        if codex is not None and news_cfg is not None and news_windows:
            results["news_window"] = {arm: he.summarize(trades) for arm, trades in news_arms.items()}
    news_report = None
    if news_cfg is not None:
        records = news_cfg["archive"]["records"]
        news_report = {
            "archive_sha256": news_cfg["archive_sha256"], "sources": list(news_cfg["sources"]),
            "max_status_age_seconds": news_cfg["max_status_age_seconds"],
            "lookback_hours": news_cfg["lookback_hours"], "max_items_per_symbol": news_cfg["max_items_per_symbol"],
            "archive_records_for_symbol": sum(1 for r in records if (r["market"], r["symbol"]) == (market, symbol)),
            "earliest_retrieved_at": min((r["retrieved_at"] for r in records
                                          if (r["market"], r["symbol"]) == (market, symbol)), default=None),
            "decision_points_covered": covered, "decision_points_uncovered": len(points) - covered,
            "uncovered_reasons": dict(sorted(uncovered.items())),
            "coverage_status": ("NO_POINT_IN_TIME_COVERAGE" if covered == 0 else
                                "FULL" if covered == len(points) else "PARTIAL"),
            "comparison": ("NOT_RUN_NO_POINT_IN_TIME_COVERAGE" if covered == 0 else
                           "NOT_RUN_WITHOUT_CODEX" if codex is None else
                           "NO_COVERED_POINT_IN_CODEX_WINDOW" if not news_windows else "RUN"),
            "covered_windows_compared": news_windows,
            "paired_action_transitions": pairwise_actions,
            "comparison_scope_note": "The empty-news arm holds the NEWS instructions and coverage constant while "
                                     "removing items; actual and empty inputs are separate single-sample Codex calls. "
                                     "Differences are diagnostic, not a causal or profitability estimate.",
            "point_in_time_rule": "record used only if max(available_at, owning run committed_at) <= decision time; "
                                  "available_at = max(provider time, our retrieved_at), so neither late retrieval nor "
                                  "archive publication is backfilled; each required source needs an OK run committed by "
                                  "the decision time within max_status_age_seconds",
            "model_only_note": "the model-only rule does not read news; Codex calls are restricted to model-only "
                               "BUY windows with positive predicted net return, so news cannot bypass the forecast "
                               "gate but may change BUY/HOLD within eligible windows",
            "anonymization_note": "news titles and summaries are not anonymised and can reveal the issuer and period"}
    return {
        "report_version": REPORT_VERSION, "kind": "OFFLINE_HISTORICAL_HYBRID_TEST", "notice": NOTICE,
        "input": {"market": market, "symbol": symbol, "source": data["source"], "sha256": data["sha256"],
                  "rows": data["rows"], "rows_dropped_outside_session": data["rows_dropped_outside_session"],
                  "sessions": len({b["session"] for b in bars})},
        "split": {"train_first_session": min(train_sessions), "train_last_session": max(train_sessions),
                  "train_sessions": len(train_sessions), "test_sessions": sorted(test_sessions),
                  "rule": "train strictly before test; no test bar used for fitting, scaling or thresholds"},
        "model": {"version": artifact["model_version"], "content_sha256": artifact["content_sha256"],
                  "train": artifact["train"], "residual": artifact["residual"],
                  "ridge_lambda_per_row": artifact["ridge_lambda_per_row"], "features": artifact["features"]},
        "forecast_diagnostic_on_decisions": {**ts.diagnostics(predicted, realized),
                                             "realized": "close_t to close_t+30, gross"},
        "assumptions": {
            "window_bars": ts.WINDOW_BARS, "horizon_minutes": ts.HORIZON_MINUTES, "sellable": False,
            "schedule": "first eligible bar per session, then the next eligible bar >= t+31 (availability only)",
            "decision": "at the close of bar t using only bars t-30..t",
            "entry": "open of bar t+1", "exit": "close of bar t+30 (fixed-horizon test convention, not a SELL)",
            "model_only_rule": "BUY iff predicted_net_bps > 0",
            "spread_bps_round_trip": f"{spread_bps:f}", "costs_bps": costs,
            "entry_cost_bps": f"{entry_cost:f}", "exit_cost_bps": f"{exit_cost:f}",
            "roundtrip_cost_bps_in_forecast": f"{roundtrip_cost:f}",
            "cost_note": "illustrative inputs, not verified fee rates, taxes, slippage, FX or historical quotes",
            "compounding": "sequential one-unit hypothetical equity, product of (1 + net return)"},
        "codex": None if codex is None else {
            "model": codex["model"], "max_calls": codex["max_calls"], "calls": state["calls"],
            "errors": state["errors"], "stopped": state["stopped"],
            "hybrid_complete": state["stopped"] is None, "anonymized_inputs": True,
            "window_note": "model_only_same_window covers exactly the decisions the hybrid arm evaluated"},
        "news": news_report,
        "decision_points": len(points),
        "results": results,
        "decisions": decisions,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stocklab.hybrid_eval",
                                     description="Offline historical time-series (+ optional Codex) test; no orders.")
    parser.add_argument("--input", required=True, help="local CSV: " + ",".join(he.COLUMNS))
    parser.add_argument("--output", required=True, help="JSON report path (keep it under git-ignored artifacts/)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--holdout-sessions", type=int, default=None,
                       help=f"test on the last N sessions (default {DEFAULT_HOLDOUT_SESSIONS})")
    group.add_argument("--train-end-session", default=None, help="train on sessions <= this date, test on later ones")
    parser.add_argument("--spread-bps", default=str(he.DEFAULT_SPREAD_BPS))
    for name in he.DEFAULT_COSTS["KR"]:
        parser.add_argument("--" + name.replace("_", "-"), default=None, help=f"override illustrative {name} (bps)")
    parser.add_argument("--expected-source", default=he.DEFAULT_SOURCE)
    parser.add_argument("--drop-outside-session", action="store_true")
    parser.add_argument("--model", default=None, help="exact Codex model ID; without it no Codex call is made")
    parser.add_argument("--max-codex-calls", type=int, default=None, help=f"required with --model (1..{MAX_CODEX_CALLS})")
    parser.add_argument("--timeout-seconds", type=int, default=180, help="per Codex call (30..600)")
    parser.add_argument("--news-archive", default=None, help="news_collect archive (point-in-time filtered)")
    parser.add_argument("--news-sources", default=None,
                        help="comma list, default all sources of the market (KR gdelt,opendart; US gdelt,sec_edgar)")
    parser.add_argument("--news-max-age-seconds", type=int, default=900, help="collector freshness (60..3600)")
    parser.add_argument("--news-lookback-hours", type=int, default=24, help="item window (1..168)")
    parser.add_argument("--news-max-items", type=int, default=5, help="items per decision (1..5)")
    args = parser.parse_args(argv)
    try:
        ts.check_private_output(args.output)
        if Path(args.output).resolve() == Path(args.input).resolve():
            raise he.InputError("output must differ from input")
        codex = None
        if args.model is not None:
            if not re.fullmatch(live_ai.MODEL_ID_PATTERN, args.model) or args.model.startswith("claude"):
                raise he.InputError("--model must be an exact Codex CLI model ID")
            if args.max_codex_calls is None or not 1 <= args.max_codex_calls <= MAX_CODEX_CALLS:
                raise he.InputError(f"--max-codex-calls (1..{MAX_CODEX_CALLS}) is required with --model")
            low, high = live_ai.TIMEOUT_SECONDS_RANGE
            if not low <= args.timeout_seconds <= high:
                raise he.InputError(f"--timeout-seconds must be {low}..{high}")
            codex = {"model": args.model, "max_calls": args.max_codex_calls, "timeout": args.timeout_seconds}
        elif args.max_codex_calls is not None:
            raise he.InputError("--max-codex-calls needs --model")
        data = he.load_bars(args.input, expected_source=args.expected_source,
                            drop_outside_session=args.drop_outside_session)
        try:
            spread_raw = Decimal(args.spread_bps)
        except InvalidOperation:
            raise he.InputError("spread must be a number of bps") from None
        overrides = {n: getattr(args, n) for n in he.DEFAULT_COSTS["KR"] if getattr(args, n) is not None}
        spread, costs = he.resolve_costs(data["market"], spread_raw, overrides)
        train_sessions, test_sessions = split(data["bars"], holdout_sessions=args.holdout_sessions,
                                              train_end_session=args.train_end_session)
        news_cfg = None
        if args.news_archive is not None:
            sources = (args.news_sources.split(",") if args.news_sources else list(nw.MARKET_SOURCES[data["market"]]))
            if not sources or len(set(sources)) != len(sources) \
                    or not set(sources) <= set(nw.MARKET_SOURCES[data["market"]]):
                raise he.InputError(f"--news-sources must be from {nw.MARKET_SOURCES[data['market']]}")
            nw.validate_config({"archive_path": str(Path(args.news_archive).resolve()),
                                "max_status_age_seconds": args.news_max_age_seconds,
                                "lookback_hours": args.news_lookback_hours,
                                "max_items_per_symbol": args.news_max_items,
                                "required_sources": {data["market"]: sources}}, {data["market"]: [data["symbol"]]})
            archive, archive_sha = nw.read_archive(args.news_archive)
            news_cfg = {"archive": archive, "archive_sha256": archive_sha, "sources": sources,
                        "max_status_age_seconds": args.news_max_age_seconds,
                        "lookback_hours": args.news_lookback_hours, "max_items_per_symbol": args.news_max_items}
        elif args.news_sources is not None:
            raise he.InputError("--news-sources needs --news-archive")
        report = evaluate(data, train_sessions=train_sessions, test_sessions=test_sessions, spread_bps=spread,
                          costs=costs, codex=codex, news_cfg=news_cfg)
    except (ValidationError, OSError, UnicodeDecodeError) as exc:
        print(f"hybrid_eval: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{report['kind']} {report['input']['market']} {report['input']['symbol']}: train "
          f"{report['split']['train_sessions']} sessions, test {len(report['split']['test_sessions'])} sessions, "
          f"{report['decision_points']} decision points")
    flat = {**{k: v for k, v in report["results"].items() if k != "news_window"},
            **{f"news_window.{k}": v for k, v in report["results"].get("news_window", {}).items()}}
    for name, m in flat.items():
        print(f"  {name}: trades {m['trades']}, net win rate {m['net_win_rate']}, mean net {m['mean_net_bps']} bps, "
              f"hypothetical compounded {m['compounded_return_pct']}%, max DD {m['max_drawdown_pct']}%"
              + ("" if m["minimum_historical_sample_gate_passed"] else " [SMALL SAMPLE]"))
    if report["news"] is not None:
        n = report["news"]
        print(f"  news: {n['coverage_status']}, covered {n['decision_points_covered']}/{report['decision_points']} "
              f"decision points, comparison {n['comparison']} ({n['covered_windows_compared']} windows)")
    if report["codex"] is not None:
        c = report["codex"]
        print(f"  codex: {c['calls']} calls, {c['errors']} errors, stopped={c['stopped']}")
        if c["stopped"] == "CODEX_ERROR":
            print("hybrid_eval: a Codex call failed; later calls were not made (hybrid arm incomplete)",
                  file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
