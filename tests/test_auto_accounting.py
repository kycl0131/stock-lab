"""Offline accounting checks. Minimal in-memory tables; no credentials or broker calls."""
from __future__ import annotations

from decimal import Decimal
import json
import sqlite3
import unittest

from stocklab import live_auto as auto


class AutoAccountingTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE auto_cycles(cycle_key TEXT PRIMARY KEY, market TEXT, mode TEXT);
            CREATE TABLE auto_marks(mark_id INTEGER PRIMARY KEY, cycle_key TEXT, market TEXT,
                                    session_date TEXT, pnl_krw TEXT, exposure_krw TEXT, created_at TEXT);
            CREATE TABLE auto_decisions(cycle_key TEXT, proposer TEXT, model_cost_krw TEXT,
                                        model_meta_json TEXT, created_at TEXT);
        """)

    def tearDown(self):
        self.conn.close()

    def record(self, key, mode, day, pnl, cost="0", *, market="KR", called=True):
        at = day + "T12:00:00+00:00"
        self.conn.execute("INSERT INTO auto_cycles VALUES(?,?,?)", (key, market, mode))
        self.conn.execute("INSERT INTO auto_marks(cycle_key,market,session_date,pnl_krw,exposure_krw,created_at) "
                          "VALUES(?,?,?,?,?,?)", (key, market, day, str(pnl), "0", at))
        self.conn.execute("INSERT INTO auto_decisions VALUES(?,?,?,?,?)",
                          (key, "MODEL", str(cost), json.dumps({"called": called}), at))

    def test_live_and_dry_run_costs_and_marks_are_separate(self):
        self.record("dry", "DRY_RUN", "2026-09-23", "-300", "40")
        self.record("live", "LIVE", "2026-09-23", "10", "2")
        self.assertEqual(auto._model_cost_krw(self.conn, "KR", "LIVE"), Decimal("2"))
        self.assertEqual(auto._model_cost_krw(self.conn, "KR", "DRY_RUN"), Decimal("40"))
        self.assertEqual(auto._latest_mark(self.conn, "KR", "LIVE")["pnl_krw"], "10")
        self.assertEqual(auto._latest_mark(self.conn, "KR", "DRY_RUN")["pnl_krw"], "-300")

    def test_prior_session_baseline_counts_overnight_gap(self):
        self.record("prior", "LIVE", "2026-09-22", "20")
        self.record("dry", "DRY_RUN", "2026-09-23", "999")
        self.record("current", "LIVE", "2026-09-23", "-80")
        baseline, source = auto._daily_baseline(self.conn, "KR", "2026-09-23", "LIVE")
        self.assertEqual((baseline, source), (Decimal("20"), "PRIOR_SESSION_MARK"))
        self.assertEqual(Decimal("-80") - baseline, Decimal("-100"))

    def test_inception_baseline_counts_first_loss_and_later_fall(self):
        self.record("loss-first", "LIVE", "2026-09-23", "-50")
        self.assertEqual(auto._daily_baseline(self.conn, "KR", "2026-09-23", "LIVE"),
                         (Decimal("0"), "INCEPTION"))
        self.record("loss-later", "LIVE", "2026-09-23", "-100")
        self.assertEqual(auto._daily_baseline(self.conn, "KR", "2026-09-23", "LIVE")[0], Decimal("0"))
        self.record("gain-first", "DRY_RUN", "2026-09-23", "50")
        self.record("gain-fades", "DRY_RUN", "2026-09-23", "-25")
        self.assertEqual(auto._daily_baseline(self.conn, "KR", "2026-09-23", "DRY_RUN")[0], Decimal("50"))

    def test_global_model_quota_does_not_count_quota_skips(self):
        self.record("dry-call", "DRY_RUN", "2026-09-23", "0", called=True)
        self.record("live-call", "LIVE", "2026-09-23", "0", called=True)
        self.record("skipped", "LIVE", "2026-09-23", "0", called=False)
        self.assertEqual(auto._recent_model_calls(self.conn, "2026-09-22T00:00:00+00:00"), 2)
        self.assertEqual(auto._recent_model_calls(self.conn, "2026-09-24T00:00:00+00:00"), 0)


if __name__ == "__main__":
    unittest.main()
