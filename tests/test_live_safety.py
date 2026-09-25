"""Offline checks of the REAL order ledger. Never uses real credentials or network."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from stocklab import live_orders as lo, live_risk as risk, live_auto as auto, live_calendar as cal
from stocklab.domain import ValidationError, now
from stocklab.kiwoom_order import KiwoomRealOrderClient, OrderTerms, OrderOutcomeUnknown, REAL_HOST, accepted_order_no


class LiveLedgerSafetyTests(unittest.TestCase):
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

    def cap(self, market="KR", committed=100_000, per_order=50_000):
        lo.set_cap(self.conn, market=market, max_committed_krw=committed,
                   max_order_krw=per_order, cash_fraction_pct="100", reason="offline test")
        return lo._latest_cap(self.conn, market)

    def ticket(self, market="KR", side="BUY", symbol=None, price=None):
        symbol = symbol or ("005930" if market == "KR" else "AAPL")
        price = price or ("10000" if market == "KR" else "5.00")
        exchange = "KRX" if market == "KR" else "ND"
        result = lo.prepare(self.conn, market=market, side=side, symbol=symbol,
                            exchange=exchange, quantity="1", limit_price=price)
        return result["ticket_id"], OrderTerms(market, side, symbol, exchange, 1, price)

    def attempt(self, ticket_id, *, reserved=10_100, cash_limit=100_000, attempted_at=None):
        row = self.conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        side, market = row["side"], row["market"]
        cap = lo._latest_cap(self.conn, market) if side == "BUY" else None
        instant = attempted_at or now()
        self.conn.execute(
            "INSERT INTO attempt_evidence(ticket_id,side,cap_id,available_cash_krw,cash_limit_krw,"
            "broker_tradeable_qty,ledger_entitlement_qty,broker_read_at,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ticket_id, side, cap["cap_id"] if cap else None,
             100_000 if side == "BUY" else None, cash_limit if side == "BUY" else None,
             None if side == "BUY" else 1, None if side == "BUY" else 1, instant, instant))
        self.conn.execute("UPDATE tickets SET state='ATTEMPTED', attempted_at=?, reserved_krw=?, fx_rate=? "
                          "WHERE ticket_id=?", (instant, reserved if side == "BUY" else None,
                                                 "1500" if market == "US" and side == "BUY" else None, ticket_id))

    def add_intent(self, ticket_id, market="KR"):
        instant = now()
        expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.conn.execute("INSERT INTO auto_configs(config_json,config_hash,reason,created_at) VALUES(?,?,?,?)",
                          ("{}", "test-hash", "offline test", instant))
        config_id = self.conn.execute("SELECT MAX(config_id) FROM auto_configs").fetchone()[0]
        kr_cap, us_cap = (lo._latest_cap(self.conn, m) for m in ("KR", "US"))
        self.conn.execute("INSERT INTO auto_arming(action,config_id,kr_cap_id,us_cap_id,expires_at,reason,created_at) "
                          "VALUES('ARM',?,?,?,?,?,?)",
                          (config_id, kr_cap["cap_id"] if kr_cap else None, us_cap["cap_id"] if us_cap else None,
                           expiry, "offline test", instant))
        arm_id = self.conn.execute("SELECT MAX(arm_id) FROM auto_arming").fetchone()[0]
        cycle = f"{market}-offline-test"
        self.conn.execute("INSERT INTO auto_cycles(cycle_key,market,session_date,mode,config_id,arm_id,status,started_at) "
                          "VALUES(?,?,?,?,?,?,'STARTED',?)",
                          (cycle, market, instant[:10], "LIVE", config_id, arm_id, instant))
        self.conn.execute("INSERT INTO auto_decisions(cycle_key,evidence_version,evidence_hash,evidence_json,"
                          "proposer,proposal_json,model_meta_json,baseline_json,model_cost_krw,created_at) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (cycle, "test", "test", "{}", "BASELINE", "{}", "{}", "{}", "0", instant))
        self.conn.execute("INSERT INTO auto_intents(intent_key,cycle_key,ticket_id,arm_id,risk_json,created_at) "
                          "VALUES(?,?,?,?,?,?)", (cycle, cycle, ticket_id, arm_id, "{}", instant))

    def test_no_cap_means_no_buy_ticket(self):
        with self.assertRaises(ValidationError):
            self.ticket()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 0)

    def test_sql_gate_refuses_attempt_without_evidence(self):
        self.cap()
        ticket_id, _ = self.ticket()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE tickets SET state='ATTEMPTED', attempted_at=?, reserved_krw=10100 "
                              "WHERE ticket_id=?", (now(), ticket_id))
        self.assertEqual(self.conn.execute("SELECT state FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()[0],
                         "PREPARED")

    def test_sql_gate_refuses_over_cap(self):
        self.cap(per_order=20_000)
        ticket_id, _ = self.ticket()
        with self.assertRaises(sqlite3.IntegrityError):
            self.attempt(ticket_id, reserved=30_000)
        self.assertEqual(self.conn.execute("SELECT state FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()[0],
                         "PREPARED")

    def test_ticket_terms_are_immutable_and_ticket_cannot_be_deleted(self):
        self.cap()
        ticket_id, _ = self.ticket()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE tickets SET quantity=2 WHERE ticket_id=?", (ticket_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM tickets WHERE ticket_id=?", (ticket_id,))
        self.assertEqual(self.conn.execute("SELECT quantity FROM tickets WHERE ticket_id=?", (ticket_id,))
                         .fetchone()[0], 1)

    def test_one_use_claim_prevents_duplicate_network_submission(self):
        self.cap()
        ticket_id, terms = self.ticket()
        self.attempt(ticket_id)

        class FakeResponse:
            body = {"return_code": 0, "ord_no": "0001234"}

        class FakeTransport:
            count = 0

            def request(self, **_kwargs):
                self.count += 1
                return FakeResponse()

        fake = KiwoomRealOrderClient.__new__(KiwoomRealOrderClient)
        fake._base_url = lambda _mode: REAL_HOST
        fake._client = FakeTransport()
        self.assertEqual(fake.submit(terms, ledger=self.conn, ticket_id=ticket_id), "0001234")
        with self.assertRaises(lo.SubmitRefused):
            fake.submit(terms, ledger=self.conn, ticket_id=ticket_id)
        self.assertEqual(fake._client.count, 1)

    def test_halt_after_attempt_prevents_network_submission(self):
        self.cap()
        ticket_id, terms = self.ticket()
        self.attempt(ticket_id)

        class FakeTransport:
            count = 0

            def request(self, **_kwargs):
                self.count += 1
                raise AssertionError("network must remain unused")

        fake = KiwoomRealOrderClient.__new__(KiwoomRealOrderClient)
        fake._base_url = lambda _mode: REAL_HOST
        fake._client = FakeTransport()
        lo.halt(self.conn, "KR", "offline test")
        with self.assertRaises(lo.SubmitRefused):
            fake.submit(terms, ledger=self.conn, ticket_id=ticket_id)
        self.assertEqual(fake._client.count, 0)

    def test_invalid_order_terms_and_acceptance_are_refused(self):
        with self.assertRaises(ValidationError):
            OrderTerms("KR", "BUY", "005930", "KRX", 1, "2001")
        with self.assertRaises(ValidationError):
            OrderTerms("US", "BUY", "AAPL", "ND", 1, "0.99")
        for body in ({"return_code": True, "ord_no": "123"},
                     {"return_code": 0, "ord_no": "0000000"},
                     {"return_code": 0}):
            with self.assertRaises(OrderOutcomeUnknown):
                accepted_order_no(body)

    def test_buy_plan_uses_dynamic_cash_and_smallest_limit(self):
        cap = self.cap(committed=100_000, per_order=50_000)
        planned = risk.plan(
            market="KR", proposal={"action": "BUY", "symbol": "005930"}, universe={"005930"},
            fact={"bid": Decimal("9000"), "ask": Decimal("9010"), "last": Decimal("9000"),
                  "exchange": "KRX"}, book={},
            cfg={"max_order_krw": 40_000, "max_position_krw": 35_000},
            glob={"capital_cap_krw": 30_000, "max_spread_bps": 300, "collar_bps": 300},
            cash_ccy=Decimal("20000"), fx=None, cap=cap, committed_krw=0,
            exposure_total_krw=Decimal(0), buy_allowed=True)
        self.assertEqual(planned["reason"], "ORDER")
        self.assertEqual(planned["order"]["quantity"], 2)
        self.assertEqual(planned["calc"]["budget_krw"], "20000")

    def test_human_closed_partial_blocks_cross_market_auto_buy_in_sql(self):
        self.cap("KR")
        self.cap("US")
        us_id, _ = self.ticket("US")
        self.attempt(us_id, reserved=10_000)
        self.conn.execute("INSERT INTO resolutions(ticket_id,kind,filled_qty,note,created_at) "
                          "VALUES(?,'HUMAN_CLOSED',0,'offline test',?)", (us_id, now()))
        kr_id, _ = self.ticket("KR")
        self.add_intent(kr_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.attempt(kr_id)
        self.assertEqual(self.conn.execute("SELECT state FROM tickets WHERE ticket_id=?", (kr_id,)).fetchone()[0],
                         "PREPARED")

    def test_new_cap_invalidates_auto_arm(self):
        self.cap("KR")
        self.cap("US")
        ticket_id, _ = self.ticket("KR")
        self.add_intent(ticket_id)
        self.assertTrue(auto.arming(self.conn)["armed"])
        self.cap("KR", committed=90_000)
        self.assertFalse(auto.arming(self.conn)["armed"])
        self.assertIn("KR_CAP_CHANGED", auto.arming(self.conn)["reason"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.attempt(ticket_id)

    def test_no_config_cannot_run_live_cycle_or_submit(self):
        with patch.object(lo, "send_auto", side_effect=AssertionError("must not submit")):
            result = auto.run_cycle(self.conn, "KR", mode="LIVE")
        self.assertEqual(result["skipped"], "NO_CONFIG")

    def test_us_dst_boundaries_and_ambiguous_timestamp_refused(self):
        from datetime import date
        self.assertFalse(cal.us_is_dst(date(2026, 3, 7)))
        self.assertTrue(cal.us_is_dst(date(2026, 3, 8)))
        self.assertTrue(cal.us_is_dst(date(2026, 10, 31)))
        self.assertFalse(cal.us_is_dst(date(2026, 11, 1)))
        with self.assertRaises(cal.CalendarError):
            cal.utc_to_local("US", datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
        with self.assertRaises(cal.CalendarError):
            cal.resolve_stamp("20260925", "093000", datetime(2026, 9, 25, 0, 0,
                                                               tzinfo=timezone.utc), max_age_s=60)

    def test_partial_close_does_not_block_existing_attempt_outcome_record(self):
        self.cap("KR")
        self.cap("US")
        kr_id, _ = self.ticket("KR")
        self.add_intent(kr_id)
        self.attempt(kr_id)
        us_id, _ = self.ticket("US")
        self.attempt(us_id, reserved=10_000)
        self.conn.execute("INSERT INTO resolutions(ticket_id,kind,filled_qty,note,created_at) "
                          "VALUES(?,'HUMAN_CLOSED',0,'offline test',?)", (us_id, now()))
        self.conn.execute("UPDATE tickets SET state='ACCEPTED',outcome_at=?,outcome='ACCEPTED',"
                          "broker_order_no='0001234' WHERE ticket_id=?", (now(), kr_id))
        self.assertEqual(self.conn.execute("SELECT state FROM tickets WHERE ticket_id=?", (kr_id,)).fetchone()[0],
                         "ACCEPTED")

    def test_broker_order_number_may_repeat_on_later_kst_day(self):
        self.cap()
        first, _ = self.ticket()
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.attempt(first, attempted_at=yesterday)
        self.conn.execute("UPDATE tickets SET state='ACCEPTED',outcome_at=?,outcome='ACCEPTED',"
                          "broker_order_no='0001234' WHERE ticket_id=?", (now(), first))
        old_day = (datetime.fromisoformat(yesterday) + timedelta(hours=9)).strftime("%Y%m%d")
        self.conn.execute("INSERT INTO order_links(ticket_id,market,order_date,broker_order_no,source,created_at) "
                          "VALUES(?,'KR',?,'1234','SEND_RESPONSE',?)", (first, old_day, now()))
        self.conn.execute("INSERT INTO resolutions(ticket_id,kind,filled_qty,note,created_at) "
                          "VALUES(?,'HUMAN_CLOSED',0,'offline test',?)", (first, now()))

        second, _ = self.ticket()
        self.attempt(second)
        self.conn.execute("UPDATE tickets SET state='ACCEPTED',outcome_at=?,outcome='ACCEPTED',"
                          "broker_order_no='0001234' WHERE ticket_id=?", (now(), second))
        today = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
        self.assertNotEqual(old_day, today)
        self.conn.execute("INSERT INTO order_links(ticket_id,market,order_date,broker_order_no,source,created_at) "
                          "VALUES(?,'KR',?,'1234','SEND_RESPONSE',?)", (second, today, now()))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM order_links WHERE broker_order_no='1234'")
                         .fetchone()[0], 2)

    def test_us_order_number_may_repeat_on_different_eastern_days_with_same_kst_date(self):
        self.cap("US")
        first_at = datetime(2026, 9, 24, 15, 10, tzinfo=timezone.utc).isoformat()
        second_at = datetime(2026, 9, 25, 14, 0, tzinfo=timezone.utc).isoformat()
        self.assertEqual(lo._kst(first_at).date(), lo._kst(second_at).date())
        self.assertNotEqual(lo._identity_date("US", first_at), lo._identity_date("US", second_at))
        for attempted_at in (first_at, second_at):
            fixed = datetime.fromisoformat(attempted_at)
            class FrozenDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return fixed.astimezone(tz or timezone.utc)
            with patch.object(lo, "datetime", FrozenDateTime):
                ticket_id, _ = self.ticket("US")
            self.attempt(ticket_id, reserved=10_000, attempted_at=attempted_at)
            self.conn.execute("UPDATE tickets SET state='ACCEPTED',outcome_at=?,outcome='ACCEPTED',"
                              "broker_order_no='0001234' WHERE ticket_id=?", (now(), ticket_id))
            self.conn.execute("INSERT INTO order_links(ticket_id,market,order_date,broker_order_no,source,created_at) "
                              "VALUES(?,'US',?,'1234','SEND_RESPONSE',?)",
                              (ticket_id, lo._identity_date("US", attempted_at), now()))
            self.conn.execute("INSERT INTO resolutions(ticket_id,kind,filled_qty,note,created_at) "
                              "VALUES(?,'HUMAN_CLOSED',0,'offline test',?)", (ticket_id, now()))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM order_links WHERE market='US' AND broker_order_no='1234'")
                         .fetchone()[0], 2)

    def test_us_fx_cross_check_uses_conservative_rate(self):
        class FakeReadOnly:
            def __init__(self, _mode):
                pass

            def quote(self, *_args):
                return {"fake": True}

            def close(self):
                pass

        with patch("stocklab.kiwoom_bridge.KiwoomReadOnly", FakeReadOnly), \
                patch("stocklab.live_evidence.parse_us_status", return_value={"fx_quote": Decimal("1510")}):
            self.assertEqual(lo._cross_checked_us_fx("AAPL", "ND", Decimal("1500")), Decimal("1510"))
        with patch("stocklab.kiwoom_bridge.KiwoomReadOnly", FakeReadOnly), \
                patch("stocklab.live_evidence.parse_us_status", return_value={"fx_quote": Decimal("1600")}):
            with self.assertRaises(ValidationError):
                lo._cross_checked_us_fx("AAPL", "ND", Decimal("1500"))


if __name__ == "__main__":
    unittest.main()
