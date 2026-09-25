"""Offline checks for the historical signal test. Temporary CSV fixtures only; no network, broker, keys or orders."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from stocklab import historical_eval as he, live_ai, live_research

KR_OPEN = datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc)      # Monday 09:00 KST
KR_OPEN_2 = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)
US_OPEN = datetime(2026, 9, 28, 13, 30, tzinfo=timezone.utc)    # Monday 09:30 EDT
HEADER = ",".join(he.COLUMNS)


def uptrend(count, start=10000):
    closes, price = [], start
    for i in range(count):
        price += 30 if i % 2 else 40
        closes.append(price)
    return closes


def rows(start, closes, *, market="KR", symbol="005930", source=he.DEFAULT_SOURCE, skip=()):
    out, previous = [], None
    for i, close in enumerate(closes):
        if i in skip:
            previous = close
            continue
        opened = previous if previous is not None else close
        at = (start + timedelta(minutes=i)).isoformat()
        out.append(f"{market},{symbol},{at},{opened},{max(opened, close) + 5},{min(opened, close) - 5},{close},1000,{source}")
        previous = close
    return out


def write(tmp, lines, name="bars.csv"):
    path = Path(tmp) / name
    path.write_text("\n".join([HEADER, *lines]) + "\n", encoding="utf-8")
    return path


class HistoricalEvalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def load(self, lines, **kw):
        return he.load_bars(write(self.tmp.name, lines), **kw)

    def test_window_and_horizon_match_the_research_rule(self):
        self.assertEqual(he.WINDOW_BARS, live_research.WINDOW_RETURNS + 1)
        self.assertEqual(he.HOLD_MINUTES, live_research.HORIZON_MINUTES)
        self.assertEqual(he._baseline_params(), {"entry_bps": 50, "exit_bps": 50})

    def test_input_validation_rejects_bad_rows(self):
        good = rows(KR_OPEN, uptrend(3))
        at = KR_OPEN.isoformat()
        cases = {
            "naive": good[:1] + [good[1].replace((KR_OPEN + timedelta(minutes=1)).isoformat(), "2026-09-28T00:01:00")],
            "offset": good[:1] + [good[1].replace("+00:00", "+09:00")],
            "duplicate": [good[0], good[0]],
            "descending": [good[1], good[0]],
            "seconds": [good[0].replace(at, "2026-09-28T00:00:30+00:00")],
            "nonpositive": [good[0].replace("10040,1000", "0,1000")],
            "inconsistent": ["KR,005930,%s,100,90,80,95,1000,%s" % (at, he.DEFAULT_SOURCE)],
            "volume": [good[0].replace(",1000,", ",-1,")],
            "synthetic": rows(KR_OPEN, uptrend(2), source="synthetic-v2"),
            "other_source": rows(KR_OPEN, uptrend(2), source="some-vendor"),
            "two_symbols": [good[0], rows(KR_OPEN + timedelta(minutes=1), [10100], symbol="000660")[0]],
            "bad_symbol": rows(KR_OPEN, uptrend(2), symbol="AAPL"),
            "pre_market": rows(KR_OPEN - timedelta(minutes=1), uptrend(2)),
            "weekend": rows(datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc), uptrend(2)),
        }
        for name, lines in cases.items():
            with self.subTest(name), self.assertRaises(he.InputError):
                self.load(lines)
        with self.assertRaises(he.InputError):   # header must match exactly (e.g. no synthetic column)
            path = Path(self.tmp.name) / "extra.csv"
            path.write_text(HEADER + ",synthetic\n" + good[0] + ",false\n", encoding="utf-8")
            he.load_bars(path)

    def test_outside_session_rows_are_only_dropped_explicitly_and_counted(self):
        lines = rows(KR_OPEN - timedelta(minutes=2), uptrend(20))
        data = self.load(lines, drop_outside_session=True)
        self.assertEqual((data["rows"], data["rows_dropped_outside_session"], len(data["bars"])), (20, 2, 18))
        us = self.load(rows(US_OPEN, uptrend(3), market="US", symbol="AAPL"))
        self.assertEqual(us["bars"][0]["session"], "2026-09-28")

    def test_no_lookahead_decision_depends_only_on_bars_through_t(self):
        data = self.load(rows(KR_OPEN, uptrend(40)))
        bars, t = data["bars"], 20
        window = bars[t - he.WINDOW_BARS + 1:t + 1]
        snap = he.decision_snapshot("KR", "005930", window)
        stamps = [o["event_at"] for o in snap["candidates"][0]["observations"]]
        self.assertEqual(snap["as_of"], bars[t]["at"].isoformat())
        self.assertTrue(all(s <= snap["as_of"] for s in stamps))
        self.assertFalse(snap["candidates"][0]["sellable"])
        quote = he.assumed_quote(bars[t]["close"], Decimal("10"))
        self.assertEqual((quote["ask"] - quote["bid"]) / bars[t]["close"] * 10000, Decimal("10"))
        costs = he.DEFAULT_COSTS["KR"]
        before = {m: he.decide(m, "KR", "005930", window, costs, Decimal("10")) for m in he.MODELS}
        # Crash every later bar: decisions at t must not change.
        crashed = [dict(b, open=Decimal(1), high=Decimal(1), low=Decimal(1), close=Decimal(1)) for b in bars[t + 1:]]
        future_changed = bars[:t + 1] + crashed
        after = {m: he.decide(m, "KR", "005930", future_changed[t - he.WINDOW_BARS + 1:t + 1], costs, Decimal("10"))
                 for m in he.MODELS}
        self.assertEqual(before, after)
        self.assertEqual(before["baseline"][0], "BUY")
        self.assertEqual(before["research"], ("BUY", None))
        # Changing the future only changes outcomes, never which bars signalled.
        a = he.simulate("baseline", data, costs, Decimal("10"))
        b = he.simulate("baseline", {**data, "bars": future_changed}, costs, Decimal("10"))
        first_after_t = lambda r: [x["decision_at"] for x in r["trade_list"] if x["decision_at"] <= bars[t]["at"].isoformat()]
        self.assertEqual(first_after_t(a), first_after_t(b))

    def test_gaps_and_sessions_are_never_bridged(self):
        n = 40
        whole = self.load(rows(KR_OPEN, uptrend(n)))
        self.assertEqual(he.evaluate(whole)["windows"]["eligible_windows"], n - 15)
        gapped = self.load(rows(KR_OPEN, uptrend(n), skip={20}))   # runs of 20 and 19 bars
        self.assertEqual(he.evaluate(gapped)["windows"]["eligible_windows"], (20 - 15) + (19 - 15))
        for trade in he.evaluate(gapped)["models"]["baseline"]["trade_list"]:
            self.assertFalse(trade["decision_at"] < (KR_OPEN + timedelta(minutes=20)).isoformat() < trade["exit_at"])
        # Two sessions: each counted separately, nothing crosses the overnight break.
        two = self.load(rows(KR_OPEN + timedelta(minutes=370), uptrend(20)) + rows(KR_OPEN_2, uptrend(20)))
        report = he.evaluate(two)
        self.assertEqual(report["input"]["sessions"], 2)
        self.assertEqual(report["windows"]["eligible_windows"], 2 * (20 - 15))
        # Same-session is checked explicitly, not only via the minute spacing.
        fake = [{"at": KR_OPEN, "session": "a"}, {"at": KR_OPEN + timedelta(minutes=1), "session": "b"}]
        self.assertFalse(he._linked(fake, 0))
        # Too-short sessions give no windows at all (no filling).
        short = he.evaluate(self.load(rows(KR_OPEN, uptrend(15))))
        self.assertEqual(short["windows"]["eligible_windows"], 0)

    def test_cost_arithmetic(self):
        entry, exit_ = he.side_costs_bps("KR", he.DEFAULT_COSTS["KR"], Decimal("10"))
        self.assertEqual((entry, exit_), (Decimal("16.5"), Decimal("36.5")))
        result = he.roundtrip(Decimal(10000), Decimal(10100), entry, exit_)
        self.assertEqual(result["gross"], Decimal("0.01"))
        self.assertEqual(result["net"], Decimal(10100) * Decimal("0.99635") / (Decimal(10000) * Decimal("1.00165")) - 1)
        us_entry, us_exit = he.side_costs_bps("US", he.DEFAULT_COSTS["US"], Decimal("10"))
        self.assertEqual((us_entry, us_exit), (Decimal("50"), Decimal("50")))
        self.assertEqual(he.side_costs_bps("KR", he.DEFAULT_COSTS["KR"], Decimal("20"))[0], Decimal("21.5"))
        # Same keys the research rule's hurdle reads; its hurdle equals entry + exit cost.
        quote = he.assumed_quote(Decimal(10000), Decimal("10"))
        hurdle = live_research.hurdle_bps("US", quote["bid"], quote["ask"], he.DEFAULT_COSTS["US"])["hurdle_bps"]
        self.assertEqual(hurdle, us_entry + us_exit)
        with self.assertRaises(he.InputError):
            he.evaluate(self.load(rows(KR_OPEN, uptrend(20))), spread_bps=Decimal(0))
        sample = self.load(rows(KR_OPEN, uptrend(20)))
        adjusted = he.evaluate(sample, cost_overrides={"slippage_bps": "0"})
        self.assertEqual(adjusted["assumptions"]["costs_bps"]["slippage_bps"], "0")
        self.assertEqual(adjusted["assumptions"]["entry_cost_bps"], "6.5")
        with self.assertRaises(he.InputError):
            he.evaluate(sample, cost_overrides={"buy_fee_bps": "-1"})
        with self.assertRaises(he.InputError):
            he.evaluate(sample, cost_overrides={"slippage_bps": "NaN"})

    def test_trades_do_not_overlap(self):
        data = self.load(rows(KR_OPEN, uptrend(60)))
        for model in he.MODELS:
            result = he.simulate(model, data, he.DEFAULT_COSTS["KR"], Decimal("10"))
            trades = result["trade_list"]
            self.assertGreater(len(trades), 3)
            for trade in trades:
                decided = datetime.fromisoformat(trade["decision_at"])
                self.assertEqual(datetime.fromisoformat(trade["entry_at"]), decided + timedelta(minutes=1))
                self.assertEqual(datetime.fromisoformat(trade["exit_at"]), decided + timedelta(minutes=5))
            for prev, nxt in zip(trades, trades[1:]):
                self.assertGreater(nxt["decision_at"], prev["exit_at"])
            self.assertEqual(result["evaluated_decisions"] + result["skipped_while_trade_open"], 60 - 15)
            self.assertEqual(result["other_action"], 0)
            self.assertEqual(result["rule_errors"], 0)

    def test_zero_trades_report_null_win_rate(self):
        report = he.evaluate(self.load(rows(KR_OPEN, [10000] * 30)))
        for model in report["models"].values():
            self.assertEqual(model["trades"], 0)
            self.assertIsNone(model["net_win_rate"])
            self.assertIsNone(model["mean_net_bps"])
            self.assertIsNone(model["median_net_bps"])
            self.assertEqual((model["compounded_return_pct"], model["max_drawdown_pct"]), ("0.0000", "0.0000"))
            self.assertFalse(model["minimum_historical_sample_gate_passed"])
            self.assertFalse(model["future_accuracy_validated"])
            self.assertEqual(model["HOLD"], 15)

    def test_cli_runs_offline_without_orders_network_or_model(self):
        csv_path = write(self.tmp.name, rows(KR_OPEN, uptrend(40)) + rows(KR_OPEN_2, uptrend(40)))
        out = Path(self.tmp.name) / "report.json"

        def refuse(*_a, **_k):
            raise AssertionError("network access attempted")
        with patch.object(socket.socket, "connect", refuse), patch.object(live_ai, "_run", refuse), \
                patch.object(live_ai, "propose", refuse), redirect_stdout(io.StringIO()):
            self.assertEqual(he.main(["--input", str(csv_path), "--output", str(out)]), 0)
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["kind"], "OFFLINE_HISTORICAL_SIGNAL_TEST")
        self.assertEqual(report["model_versions"]["baseline"]["version"], live_ai.BASELINE_VERSION)
        self.assertEqual(report["model_versions"]["research"]["version"], live_research.CANDIDATE_VERSION)
        self.assertEqual(report["assumptions"]["costs_bps"], he.DEFAULT_COSTS["KR"])
        for model in report["models"].values():
            self.assertGreater(model["trades"], 0)
            self.assertFalse(model["minimum_historical_sample_gate_passed"])   # < 30 trades, < 20 sessions
            self.assertFalse(model["future_accuracy_validated"])
            self.assertIsNotNone(model["net_win_rate"])
        self.assertNotIn(self.tmp.name.replace("\\", "/"), out.read_text(encoding="utf-8").replace("\\\\", "/"))
        # Output must never overwrite the input; failures return 2 without writing.
        with redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
            self.assertEqual(he.main(["--input", str(csv_path), "--output", str(csv_path)]), 2)
        self.assertTrue(csv_path.read_text(encoding="utf-8").startswith(HEADER))

    def test_module_imports_no_broker_order_or_risk_code(self):
        code = ("import sys, stocklab.historical_eval; "
                "print(','.join(sorted(m for m in sys.modules if m.startswith('stocklab'))))")
        loaded = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                                cwd=Path(__file__).resolve().parents[1]).stdout.strip().split(",")
        for forbidden in ("kiwoom_bridge", "kiwoom_order", "live_orders", "live_risk", "live_auto", "live_evidence",
                          "live_reconcile", "engine", "pilot", "paper_pilot", "server"):
            self.assertNotIn(f"stocklab.{forbidden}", loaded)
        source = Path(he.__file__).read_text(encoding="utf-8")
        for token in ("propose(", "decide(snapshot", "urlopen", "environ", "kiwoom_", "Kiwoom", "sqlite"):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
