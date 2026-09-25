"""Offline checks: current-session evidence, stored model snapshot, research-only candidate. No network, no keys."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stocklab import (live_ai, live_auto as auto, live_calendar as cal, live_evidence as ev, live_orders as lo,
                      live_research as research, live_risk as risk)
from stocklab.domain import ValidationError, canonical, digest, now

KST = timezone(timedelta(hours=9))
KR_OPEN, KR_CLOSE = datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc), datetime(2026, 9, 28, 6, 30, tzinfo=timezone.utc)
US_OPEN, US_CLOSE = datetime(2026, 9, 28, 13, 30, tzinfo=timezone.utc), datetime(2026, 9, 28, 20, 0, tzinfo=timezone.utc)
UPTREND = [10000, 10050, 10110, 10160, 10220, 10270, 10330, 10380, 10440, 10490, 10550, 10600]
KR_COSTS = {"buy_fee_bps": "1.5", "sell_fee_bps": "1.5", "sell_tax_bps": "18", "slippage_bps": "5"}
PRIVATE_VALUES = ("12345678", "0009999", "SECRETTOKEN")


def kr_rows(stamps_prices):
    """(KST datetime, price) ascending -> ka10080 rows newest first."""
    return [{"cntr_tm": at.strftime("%Y%m%d%H%M%S"), "cur_prc": f"+{price}", "trde_qty": "1000"}
            for at, price in reversed(stamps_prices)]


def kr_bars_result(rows, symbol="005930"):
    return {"pages": [{"stk_cd": symbol, "stk_min_pole_chart_qry": rows}], "first_page_only": True}


def us_bars_result(stamps_prices):
    rows = [{"cntr_tm": at, "cur_prc": f"+{price}", "trde_qty": "100"} for at, price in reversed(stamps_prices)]
    return {"pages": [{"result_list": rows}], "first_page_only": True}


def minutes(start, count, prices=None):
    prices = prices or [10000 + 10 * i for i in range(count)]
    return [(start + timedelta(minutes=i), prices[i]) for i in range(count)]


class FakeKrClient:
    """ka10080/ka10004/ka10100 shaped pages; the order book page also carries account-like fields that must
    never reach the snapshot."""

    def __init__(self, rows, quote_tm, bid="10600", ask="10610"):
        self.rows, self.quote_tm, self.bid, self.ask = rows, quote_tm, bid, ask

    def __call__(self, _mode):
        return self

    def kr_minute_bars(self, symbol):
        return kr_bars_result(self.rows, symbol)

    def kr_orderbook(self, _symbol):
        return {"complete": True, "pages": [{"bid_req_base_tm": self.quote_tm, "sel_fpr_bid": f"+{self.ask}",
                                             "buy_fpr_bid": f"-{self.bid}", "acnt_no": PRIVATE_VALUES[0],
                                             "ord_no": PRIVATE_VALUES[1], "token": PRIVATE_VALUES[2],
                                             "ord_qty": "77"}]}

    def kr_stock_info(self, symbol):
        return {"complete": True, "pages": [{"code": symbol, "orderWarning": "0", "auditInfo": "정상",
                                             "state": "증거금20%"}]}

    def close(self):
        pass


def uptrend_session_rows(prior_day=True):
    """12 same-session bars 09:49..10:00 KST plus 5 bars of the previous session's close."""
    session = minutes(datetime(2026, 9, 28, 9, 49, tzinfo=KST), 12, UPTREND)
    prior = minutes(datetime(2026, 9, 24, 15, 26, tzinfo=KST), 5, [9000] * 5) if prior_day else []
    return kr_rows(prior + session)


class SessionEvidenceTests(unittest.TestCase):
    def test_kr_previous_session_bars_are_dropped_not_stitched(self):
        now_utc = datetime(2026, 9, 28, 9, 4, 10, tzinfo=KST).astimezone(timezone.utc)
        rows = kr_rows(minutes(datetime(2026, 9, 25, 15, 25, tzinfo=KST), 6)
                       + minutes(datetime(2026, 9, 28, 9, 0, tzinfo=KST), 5))
        bars = ev.parse_kr_bars(kr_bars_result(rows), "005930", now_utc, lookback=30, max_age_s=180,
                                session=(KR_OPEN, KR_CLOSE))
        self.assertEqual(len(bars), 5)
        self.assertEqual(bars[0][1], KR_OPEN)           # the 09:00:00 bar is inside [open, close]
        self.assertTrue(all(KR_OPEN <= at <= KR_CLOSE for _, at, _, _ in bars))
        # Without session bounds (read-only verification tool) the old behaviour is unchanged.
        self.assertEqual(len(ev.parse_kr_bars(kr_bars_result(rows), "005930", now_utc, lookback=30, max_age_s=180)),
                         11)

    def test_kr_too_few_same_session_bars_rejects_the_cycle(self):
        now_utc = datetime(2026, 9, 28, 9, 3, 10, tzinfo=KST).astimezone(timezone.utc)
        rows = kr_rows(minutes(datetime(2026, 9, 25, 15, 20, tzinfo=KST), 10)
                       + minutes(datetime(2026, 9, 28, 9, 0, tzinfo=KST), 4))
        with self.assertRaisesRegex(ev.EvidenceError, "TOO_FEW_SESSION_BARS"):
            ev.parse_kr_bars(kr_bars_result(rows), "005930", now_utc, lookback=30, max_age_s=180,
                             session=(KR_OPEN, KR_CLOSE))

    def test_kr_bar_after_close_rejects(self):
        now_utc = datetime(2026, 9, 28, 15, 31, 0, tzinfo=KST).astimezone(timezone.utc)
        rows = kr_rows(minutes(datetime(2026, 9, 28, 15, 25, tzinfo=KST), 6))   # ..15:30, 15:30 is ok
        self.assertEqual(len(ev.parse_kr_bars(kr_bars_result(rows), "005930", now_utc, lookback=30, max_age_s=180,
                                              session=(KR_OPEN, KR_CLOSE))), 6)
        rows = kr_rows(minutes(datetime(2026, 9, 28, 15, 26, tzinfo=KST), 6))   # ..15:31 is after the close
        with self.assertRaisesRegex(ev.EvidenceError, "BAR_AFTER_SESSION_CLOSE"):
            ev.parse_kr_bars(kr_bars_result(rows), "005930", now_utc, lookback=30, max_age_s=180,
                             session=(KR_OPEN, KR_CLOSE))

    def test_us_kst_basis_opening_crossover(self):
        # 09:30 ET = 22:30 KST. The previous US session (Fri 16:00 ET = Sat 05:00 KST) must be dropped.
        now_utc = datetime(2026, 9, 28, 13, 36, 20, tzinfo=timezone.utc)
        prior = [(f"2026092604{55 + i:02d}00", 190) for i in range(5)]      # Sat 04:55..04:59 KST
        session = [(f"2026092822{30 + i:02d}00", 191 + i) for i in range(7)]  # Mon 22:30..22:36 KST
        bars, basis = ev.parse_us_bars(us_bars_result(prior + session), "AAPL", now_utc, lookback=30, max_age_s=180,
                                       session=(US_OPEN, US_CLOSE))
        self.assertEqual(basis, "KST")
        self.assertEqual([at for _, at, _, _ in bars][0], US_OPEN)
        self.assertEqual(len(bars), 7)
        with self.assertRaisesRegex(ev.EvidenceError, "TOO_FEW_SESSION_BARS"):
            ev.parse_us_bars(us_bars_result(prior + session[:4]), "AAPL", now_utc - timedelta(minutes=3),
                             lookback=30, max_age_s=180, session=(US_OPEN, US_CLOSE))

    def test_us_kst_basis_midnight_is_not_a_session_boundary(self):
        # 23:58..00:04 KST on consecutive KST dates are all inside one US session (13:30-20:00 UTC).
        now_utc = datetime(2026, 9, 28, 15, 4, 30, tzinfo=timezone.utc)
        stamps = [(datetime(2026, 9, 28, 23, 58, tzinfo=KST) + timedelta(minutes=i)).strftime("%Y%m%d%H%M%S")
                  for i in range(7)]
        bars, basis = ev.parse_us_bars(us_bars_result([(s, 190) for s in stamps]), "AAPL", now_utc, lookback=30,
                                       max_age_s=180, session=(US_OPEN, US_CLOSE))
        self.assertEqual((basis, len(bars)), ("KST", 7))

    def test_us_eastern_basis_opening_crossover(self):
        now_utc = datetime(2026, 9, 28, 13, 36, 20, tzinfo=timezone.utc)   # 09:36 ET
        prior = [(f"2026092515{55 + i:02d}00", 190) for i in range(5)]      # Friday 15:55..15:59 ET
        session = [(f"20260928093{i}00", 191) for i in range(7)]
        bars, basis = ev.parse_us_bars(us_bars_result(prior + session), "AAPL", now_utc, lookback=30, max_age_s=180,
                                       session=(US_OPEN, US_CLOSE))
        self.assertEqual((basis, len(bars), bars[0][1]), ("US_EASTERN", 7, US_OPEN))
        with self.assertRaisesRegex(ev.EvidenceError, "TOO_FEW_SESSION_BARS"):
            ev.parse_us_bars(us_bars_result(prior + session[:3]), "AAPL", now_utc - timedelta(minutes=4),
                             lookback=30, max_age_s=180, session=(US_OPEN, US_CLOSE))

    def test_collect_requires_valid_session_bounds_and_instant_inside(self):
        fake = FakeKrClient(uptrend_session_rows(), "100020")
        clock = lambda: datetime(2026, 9, 28, 10, 0, 30, tzinfo=KST).astimezone(timezone.utc)  # noqa: E731
        kwargs = dict(sellable={}, lookback=30, max_bar_age_s=180, max_quote_age_s=60, clock=clock)
        for bad in (None, (KR_CLOSE, KR_OPEN), ("not-a-time", KR_CLOSE), (KR_OPEN.replace(tzinfo=None), KR_CLOSE)):
            with self.assertRaisesRegex(ev.EvidenceError, "SESSION_BOUNDS_INVALID"):
                ev.collect(fake, "KR", [("005930", "KRX")], session=bad, **kwargs)
        with self.assertRaisesRegex(ev.EvidenceError, "OUTSIDE_SESSION"):
            ev.collect(fake, "KR", [("005930", "KRX")], session=(KR_OPEN - timedelta(days=1),
                                                                  KR_CLOSE - timedelta(days=1)), **kwargs)

    def test_quote_before_open_rejects(self):
        fake = FakeKrClient(uptrend_session_rows(), "100020")
        clock = lambda: datetime(2026, 9, 28, 10, 0, 30, tzinfo=KST).astimezone(timezone.utc)  # noqa: E731
        late_open = (datetime(2026, 9, 28, 1, 0, 25, tzinfo=timezone.utc), KR_CLOSE)          # 10:00:25 KST
        with patch.object(ev, "parse_kr_bars", return_value=[("005930-x-0", late_open[0], Decimal(10600), 1)]):
            with self.assertRaisesRegex(ev.EvidenceError, "QUOTE_OUTSIDE_SESSION"):
                ev.collect(fake, "KR", [("005930", "KRX")], sellable={}, lookback=30, max_bar_age_s=180,
                           max_quote_age_s=60, session=late_open, clock=clock)


def collect_uptrend(sellable=None):
    fake = FakeKrClient(uptrend_session_rows(), "100020")
    clock = lambda: datetime(2026, 9, 28, 10, 0, 30, tzinfo=KST).astimezone(timezone.utc)  # noqa: E731
    return ev.collect(fake, "KR", [("005930", "KRX")], sellable=sellable or {}, lookback=30, max_bar_age_s=180,
                      max_quote_age_s=60, session=(KR_OPEN.isoformat(), KR_CLOSE.isoformat()), clock=clock)


def all_keys(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(all_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(all_keys(v) for v in value)) if value else set()
    return set()


class SnapshotPersistenceTests(unittest.TestCase):
    ALLOWED = {"version", "market", "started_at", "as_of", "time_basis", "session", "open_utc", "close_utc",
               "symbols", "005930", "exchange", "bid", "ask", "quote_at", "last", "first_bar_at", "last_bar_at",
               "session_bars", "fx_quote", "model_snapshot_hash", "model_snapshot", "hash",
               "schema", "candidates", "symbol", "sellable", "observations", "id", "event_at", "price", "volume"}

    def test_stored_evidence_holds_complete_snapshot_without_private_fields(self):
        evidence = collect_uptrend(sellable={"005930": True})
        stored = evidence["stored"]
        self.assertEqual(stored["version"], "stocklab-live-evidence-v2")
        self.assertEqual(stored["model_snapshot"], evidence["model_snapshot"])
        self.assertEqual(stored["model_snapshot_hash"], digest(evidence["model_snapshot"]))
        self.assertEqual(len(stored["model_snapshot"]["candidates"][0]["observations"]), 12)
        self.assertTrue(stored["model_snapshot"]["candidates"][0]["sellable"])
        self.assertLessEqual(all_keys(stored), self.ALLOWED)
        text = canonical(stored)
        for value in PRIVATE_VALUES + ("ord_qty", "acnt", "cash"):
            self.assertNotIn(value, text)
        # Only same-session observations reached the proposer; the 9,000 KRW previous-session bars did not.
        prices = [o["price"] for o in stored["model_snapshot"]["candidates"][0]["observations"]]
        self.assertEqual(prices, [str(p) for p in UPTREND])
        self.assertLess(len(text.encode("utf-8")), ev.MAX_STORED_SNAPSHOT_BYTES + 4000)

    def test_replay_reads_v2_and_tolerates_v1(self):
        stored = collect_uptrend()["stored"]
        self.assertEqual(ev.stored_model_snapshot(canonical(stored)), stored["model_snapshot"])
        v1 = {k: v for k, v in stored.items() if k not in ("model_snapshot", "session")}
        self.assertIsNone(ev.stored_model_snapshot(canonical({**v1, "version": "stocklab-live-evidence-v1"})))
        self.assertIsNone(ev.stored_model_snapshot("{}"))
        tampered = json.loads(canonical(stored))
        tampered["model_snapshot"]["candidates"][0]["observations"][0]["price"] = "1"
        with self.assertRaisesRegex(ev.EvidenceError, "HASH_MISMATCH"):
            ev.stored_model_snapshot(canonical(tampered))


def snapshot(prices, *, market="KR", sellable=False, start=None, step_s=60):
    start = start or datetime(2026, 9, 28, 0, 49, tzinfo=timezone.utc)
    symbol, exchange = ("005930", "KRX") if market == "KR" else ("AAPL", "ND")
    obs = [{"id": f"{symbol}-{i}", "event_at": (start + timedelta(seconds=step_s * i)).isoformat(),
            "price": str(p), "volume": "1000"} for i, p in enumerate(prices)]
    as_of = (start + timedelta(seconds=step_s * len(prices))).isoformat()
    return {"schema": live_ai.PROMPT_VERSION, "market": market, "as_of": as_of,
            "candidates": [{"symbol": symbol, "exchange": exchange, "sellable": sellable, "observations": obs}]}


class ResearchCandidateTests(unittest.TestCase):
    QUOTE = {"005930": {"bid": Decimal("10600"), "ask": Decimal("10610")}}

    def test_hurdle_components(self):
        cost = research.hurdle_bps("KR", Decimal("10000"), Decimal("10010"), KR_COSTS)
        self.assertEqual(cost["spread_bps"], Decimal(10) / Decimal("10005") * 10000)
        self.assertEqual(cost["hurdle_bps"], cost["spread_bps"] + Decimal("1.5") + Decimal("1.5") + 18 + 10)
        us = research.hurdle_bps("US", Decimal("100"), Decimal("100.02"), {**KR_COSTS, "fx_cost_bps": "25"})
        self.assertEqual(us["fx_round_trip_bps"], Decimal(50))
        self.assertEqual(research.hurdle_bps("KR", Decimal(1), Decimal(2), KR_COSTS)["fx_round_trip_bps"], 0)

    def test_buy_when_evidence_clears_the_hurdle(self):
        record = research.evaluate(snapshot(UPTREND), self.QUOTE, KR_COSTS)
        self.assertEqual(record["proposal"]["action"], "BUY")
        self.assertEqual(record["proposal"]["symbol"], "005930")
        d = record["diagnostics"]["005930"]
        self.assertEqual(d["verdict"], "CANDIDATE_BUY")
        self.assertGreater(Decimal(d["projected_edge_bps"]), Decimal(d["hurdle_bps"]))
        self.assertEqual((record["kind"], record["live_eligible"], record["version"]),
                         ("RESEARCH_ONLY", False, research.CANDIDATE_VERSION))
        self.assertEqual(record, research.evaluate(snapshot(UPTREND), self.QUOTE, KR_COSTS))   # deterministic

    def test_hold_when_costs_exceed_edge(self):
        wide = {"005930": {"bid": Decimal("10300"), "ask": Decimal("10600")}}   # ~287 bps spread
        record = research.evaluate(snapshot(UPTREND), wide, KR_COSTS)
        self.assertEqual(record["proposal"]["action"], "HOLD")
        self.assertEqual(record["diagnostics"]["005930"]["verdict"], "BELOW_COST_HURDLE")
        costly = {**KR_COSTS, "slippage_bps": "200"}
        record = research.evaluate(snapshot(UPTREND), self.QUOTE, costly)
        self.assertEqual(record["diagnostics"]["005930"]["verdict"], "BELOW_COST_HURDLE")
        self.assertNotEqual(record["input_hash"], research.evaluate(snapshot(UPTREND), self.QUOTE, KR_COSTS)["input_hash"])

    def test_us_fx_cost_is_part_of_the_hurdle(self):
        prices = [Decimal("100") + Decimal("0.05") * i + (Decimal("0.01") if i % 2 else 0) for i in range(12)]
        quote = {"AAPL": {"bid": Decimal("100.60"), "ask": Decimal("100.61")}}
        costs = {**KR_COSTS, "sell_tax_bps": "0", "fx_cost_bps": "0"}
        self.assertEqual(research.evaluate(snapshot(prices, market="US"), quote, costs)["proposal"]["action"], "BUY")
        record = research.evaluate(snapshot(prices, market="US"), quote, {**costs, "fx_cost_bps": "100"})
        self.assertEqual(record["proposal"]["action"], "HOLD")
        self.assertEqual(record["diagnostics"]["AAPL"]["fx_round_trip_bps"], "200.00")

    def test_abstains_on_insufficient_or_irregular_evidence(self):
        cases = {"INSUFFICIENT_BARS": snapshot(UPTREND[:6]),
                 "NONCONTIGUOUS_BARS": snapshot(UPTREND, step_s=120),
                 "ZERO_VARIANCE": snapshot([10000] * 12),
                 "WEAK_EVIDENCE": snapshot([10000, 10100, 10000, 10100, 10000, 10100, 10000, 10100, 10000, 10100,
                                            10000, 10110])}
        for verdict, snap in cases.items():
            record = research.evaluate(snap, self.QUOTE, KR_COSTS)
            self.assertEqual(record["proposal"]["action"], "HOLD", verdict)
            self.assertEqual(record["diagnostics"]["005930"]["verdict"], verdict)
        self.assertEqual(research.evaluate(snapshot(UPTREND), {}, KR_COSTS)["diagnostics"]["005930"]["verdict"],
                         "NO_QUOTE")

    def test_position_state_rules(self):
        down = list(reversed(UPTREND))
        self.assertEqual(research.evaluate(snapshot(down, sellable=True), self.QUOTE, KR_COSTS)["proposal"]["action"],
                         "SELL")
        self.assertEqual(research.evaluate(snapshot(down), self.QUOTE, KR_COSTS)["proposal"]["action"], "HOLD")
        self.assertEqual(research.evaluate(snapshot(UPTREND, sellable=True), self.QUOTE, KR_COSTS)
                         ["proposal"]["action"], "HOLD")

    def test_safe_wrapper_never_raises(self):
        record = research.evaluate_safely({"schema": "bogus"}, {}, KR_COSTS)
        self.assertEqual(record["proposal"]["action"], "HOLD")
        self.assertIsNotNone(record["error"])
        record = research.evaluate_safely(snapshot(UPTREND), self.QUOTE, {"buy_fee_bps": "1"})
        self.assertEqual((record["proposal"]["action"], record["error"][:10]), ("HOLD", "UNEXPECTED"))


def kr_config():
    calendar = {"schema": cal.CALENDAR_SCHEMA, "market": "KR", "source": "offline test calendar",
                "valid_from": "2026-09-28", "valid_to": "2026-09-30",
                "sessions": [{"date": "2026-09-28", "open": "09:00", "close": "15:30"}]}
    return {"schema": auto.CONFIG_SCHEMA, "capital_cap_krw": 100_000, "max_total_loss_krw": 10_000, "arm_max_hours": 8,
            "cycle": {"interval_seconds": 300, "lookback_bars": 30, "max_bar_age_seconds": 180,
                      "max_quote_age_seconds": 60, "collar_bps": 100, "max_spread_bps": 50},
            "proposer": {"kind": "BASELINE"}, "baseline": {"entry_bps": 5000, "exit_bps": 5000},
            "markets": {"KR": {"enabled": True, "universe": [{"symbol": "005930", "exchange": "KRX"}],
                               "max_order_krw": 50_000, "max_position_krw": 50_000, "max_daily_loss_krw": 5_000,
                               "max_orders_per_day": 1, "costs": dict(KR_COSTS), "open_buffer_minutes": 5,
                               "close_buffer_minutes": 15, "calendar": calendar}}}


class ResearchCannotOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.typed = patch.object(lo, "_typed", return_value=None)
        self.typed.start()
        self.conn = lo.open_db()

    def tearDown(self):
        self.conn.close()
        self.typed.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_research_is_not_a_selectable_proposer(self):
        for kind in ("RESEARCH", research.CANDIDATE_VERSION):
            with self.assertRaises(ValidationError):
                auto.validate_config({**kr_config(), "proposer": {"kind": kind}})
        source = Path(research.__file__).read_text(encoding="utf-8")
        for forbidden in ("live_risk", "live_orders", "kiwoom", "import risk"):
            self.assertNotIn(forbidden, source.split('"""', 2)[2])

    def test_research_buy_with_baseline_hold_creates_no_order_in_live_cycle(self):
        cfg = kr_config()
        instant = now()
        self.conn.execute("INSERT INTO auto_configs(config_json,config_hash,reason,created_at) VALUES(?,?,?,?)",
                          (canonical(cfg), digest(cfg), "offline test", instant))
        lo.set_cap(self.conn, market="KR", max_committed_krw=100_000, max_order_krw=50_000,
                   cash_fraction_pct="100", reason="offline test")
        config_id = self.conn.execute("SELECT MAX(config_id) FROM auto_configs").fetchone()[0]
        self.conn.execute("INSERT INTO auto_arming(action,config_id,kr_cap_id,us_cap_id,expires_at,reason,created_at) "
                          "VALUES('ARM',?,?,NULL,?,?,?)",
                          (config_id, lo._latest_cap(self.conn, "KR")["cap_id"],
                           (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "offline test", instant))
        self.assertTrue(auto.arming(self.conn)["armed"])
        clock = lambda: datetime(2026, 9, 28, 10, 0, 30, tzinfo=KST).astimezone(timezone.utc)  # noqa: E731
        planned = []
        real_plan = risk.plan

        def spy_plan(**kwargs):
            planned.append(kwargs["proposal"])
            return real_plan(**kwargs)

        with patch("stocklab.kiwoom_bridge.KiwoomReadOnly", FakeKrClient(uptrend_session_rows(), "100020")), \
                patch.object(lo, "_read_broker", return_value={"cash": Decimal("100000"), "fx": None}), \
                patch.object(risk, "plan", side_effect=spy_plan), \
                patch.object(lo, "prepare", side_effect=AssertionError("research must not prepare a ticket")), \
                patch.object(lo, "send_auto", side_effect=AssertionError("research must not send")):
            result = auto.run_cycle(self.conn, "KR", mode="LIVE", clock=clock)
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(result["outcome"]["proposal"]["action"], "HOLD")          # baseline (the LIVE proposer)
        self.assertEqual(result["outcome"]["research_candidate"]["proposal"]["action"], "BUY")
        self.assertEqual([p["action"] for p in planned], ["HOLD"])                # only the proposer is planned
        self.assertNotIn("would_order", result["outcome"])
        for table in ("tickets", "auto_intents", "submission_claims"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        row = self.conn.execute("SELECT * FROM auto_decisions").fetchone()
        stored = json.loads(row["evidence_json"])
        self.assertEqual(row["evidence_version"], "stocklab-live-evidence-v2")
        self.assertEqual(ev.stored_model_snapshot(row["evidence_json"])["candidates"][0]["observations"][0]["price"],
                         "10000")
        self.assertEqual(stored["session"], {"open_utc": KR_OPEN.isoformat(), "close_utc": KR_CLOSE.isoformat()})
        record = json.loads(row["model_meta_json"])["research_candidate"]
        self.assertEqual((record["proposal"]["action"], record["live_eligible"]), ("BUY", False))
        self.assertEqual(record["model_snapshot_hash"], stored["model_snapshot_hash"])
        self.assertIn("hurdle_bps", record["diagnostics"]["005930"])
        summary = auto.status(self.conn)["decisions"][0]
        self.assertTrue(summary["snapshot_replayable"])
        self.assertEqual(summary["research_candidate"]["proposal"]["action"], "BUY")


if __name__ == "__main__":
    unittest.main()
