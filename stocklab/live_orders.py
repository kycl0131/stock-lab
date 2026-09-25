"""REAL order tickets for the KR/US pilot (`live prepare|send|reconcile|close|cap|status|halt`) and the ledger
that the armed autonomous runner (live_auto.py) uses through the same gates.

Separate SQLite file in the user's LocalAppData/StockLab directory, never the paper/demo DB. Nothing here runs
in the background, schedules, retries or resends. Network use: `send` (fresh broker cash or holdings, then one
order) and `reconcile` (read-only kt00007 / ust21180 order inquiry). Everything else is offline.
`send` is authorized either by the typed human phrase (`live send`) or, via `send_auto`, by an order intent of
an armed autonomous cycle; the SQL gate re-checks that arming inside the ATTEMPTED transition.

State machine (enforced in code AND by SQLite triggers; rows are never deleted or rewritten):
    PREPARED --send--> ATTEMPTED --> ACCEPTED (return_code 0 + ord_no; NOT a fill)
                                  -> UNKNOWN  (any other outcome; may still have reached the broker)
A crash while ATTEMPTED leaves it ATTEMPTED, which is treated exactly like UNKNOWN.
An ATTEMPTED/ACCEPTED/UNKNOWN ticket blocks every new order (BUY or SELL) in its market until it has a
row in `resolutions`:
    BROKER_FILLED  kt00007 (KR) / ust21180 (US) shows the linked order fully filled
    HUMAN_CLOSED   `live close` after typed confirmation; nothing is resent, confirmed fills are kept,
                   a BUY keeps its whole reservation and a SELL consumes its whole quantity (conservative).

Schema v2 (added in place to v1 files, one transaction, no ticket/halt/meta row is changed):
    risk_caps         append-only explicit caps per market; no cap = no BUY. The v1 fixed KRW 50,000
                      gate is removed (the user revoked it); legacy reservations still count.
    attempt_evidence  the broker figures the gate used, written with the ATTEMPTED transition
    submission_claims one-use transmission opportunity per ticket, spent before the network call
    order_links       (market, KST attempt date, order number) identity, unique; from the send response
                      or a human-supplied number that the order inquiry confirms as the only matching order
    fills             append-only cumulative confirmed fill quantities per KR ticket (kt00007)
    resolutions       terminal outcome per ticket
Schema v3 (same in-place rules; replaces four v2 triggers):
    fills_us          append-only cumulative confirmed fills per US ticket (ust21180); view all_fills = both
    auto_configs      append-only validated autonomous configurations (no secrets, no account data)
    auto_arming       append-only ARM/DISARM events; an ARM binds the latest config and the latest cap per
                      market and expires. Any newer config, cap, DISARM or ALL-halt invalidates it.
    auto_cycles       one row per (market, session date, slot, mode); the key is consumed even on crash
    auto_decisions    immutable evidence snapshot, proposal, baseline and model metadata per cycle (evidence v2 JSON
                      also holds the model snapshot; model_meta_json holds the research-only candidate record)
    auto_intents      at most one order per cycle, bound to exactly one ticket and to the arming
    auto_marks        append-only P&L/exposure marks for loss triggers; auto_events audit log
SELL entitlement = confirmed BUY fills of this program for the symbol - quantity of every non-PREPARED
SELL ticket for it, for both markets. A ticket with an autonomous intent can only become ATTEMPTED while its
arming is still the valid latest arming (checked in SQL).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
import os
import re
import secrets
import sqlite3
import sys
import time

from .domain import ValidationError, canonical, digest, now
from .kiwoom_order import OrderTerms, build_request, mask_order_no
from . import live_reconcile as rec

SCHEMA_MARKER = "stocklab-live-orders-v1"   # DB identity and part of every terms hash; never changes
V2_MARKER = "stocklab-live-orders-v2"
V3_MARKER = "stocklab-live-orders-v3"

def _ledger_path() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if not root or not Path(root).is_absolute():
        raise ValidationError("LOCALAPPDATA 절대 경로를 확인하지 못해 실전 주문 기록을 열 수 없습니다.")
    return Path(root).resolve() / "StockLab" / "live-orders.db"
COST_BUFFER_BPS = {"KR": 100, "US": 150}  # conservative fees/tax/FX-drift buffer on gross BUY value
TICKET_TTL_SECONDS = 300
READ_MAX_AGE_SECONDS = 60
HUMAN_CLOSE_MIN_AGE_SECONDS = 600          # an ATTEMPTED send may still be running before this
ORDER_TIME_WINDOW = (-120, 600)            # broker ord_tm vs attempted_at, seconds (clock skew / latency)
KST = timezone(timedelta(hours=9))
FX_PLAUSIBLE_KRW_PER_USD = (Decimal("900"), Decimal("2500"))
FX_CROSS_CHECK_BPS = Decimal(300)
IN_FLIGHT = ("ATTEMPTED", "ACCEPTED", "UNKNOWN")
LIMITATIONS = [
    "ACCEPTED는 접수일 뿐 체결이 아닙니다. 체결은 `live reconcile`(KR kt00007, US ust21180)로 확인된 수량만 기록합니다.",
    "전량 체결이 확인되면 자동 종결됩니다. 거부·취소·기간만료·부분체결 후 잔량 소멸은 공식 명세에 코드가 없어 "
    "추정하지 않습니다. 키움 앱/HTS에서 확인한 뒤 `live close`로 사람이 종결해야 합니다(재전송 없음).",
    "US 대사(ust21180)는 실제 응답으로 검증되지 않았습니다. 목록 키·매수/매도 문구·시간대가 명세와 다르면 대사가 "
    "실패하고 주문은 계속 차단됩니다(추정하지 않음).",
    "SELL은 이 프로그램의 BUY 주문에서 확인된 체결 수량만큼만 가능합니다. 증권사 전체 보유수량이 "
    "원장 시범 보유수량과 다르면 기존 보유분·외부 매매가 섞였을 수 있어 전송하지 않습니다. "
    "증권사 매매가능수량이 원장 수량보다 적어도 거부합니다.",
    "BUY 한도는 `live cap`으로 명시한 값만 사용합니다(누적 약정 한도·주문당 한도·주문가능현금 비율). 설정 전에는 BUY가 차단됩니다. "
    "누적 한도는 레거시 예약을 포함하며 매도 대금으로 늘어나지 않습니다.",
    "비용 버퍼(KR 1%, US 1.5%)는 추정치입니다. 실제 수수료·세금·환전 비용이나 체결 환율을 확정하지 않습니다.",
    "미확인 잔량이 있는 티켓을 사람이 종결하면 자동매매는 해당 시장을 멈추고, 다른 시장의 자동 신규 매수도 차단합니다. "
    "원장과 실물 잔고를 자동으로 복구하는 절차는 없습니다.",
    "`live` 수동 티켓에는 캘린더·시세 신선도·손실 기준 검사가 없습니다(자동 실행 `auto`에만 있음). 취소·정정 주문 기능은 없습니다.",
]

# v1 objects (unchanged, minus the two v1 triggers that v2 replaces).
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS live_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS halts(
  halt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL CHECK(scope IN ('KR','US','ALL')),
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tickets(
  ticket_id TEXT PRIMARY KEY CHECK(ticket_id GLOB 'LT-*'),
  market TEXT NOT NULL CHECK(market IN ('KR','US')),
  side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
  symbol TEXT NOT NULL,
  exchange TEXT NOT NULL,
  quantity INTEGER NOT NULL CHECK(quantity > 0),
  limit_price TEXT NOT NULL,
  request_json TEXT NOT NULL,
  terms_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  cost_buffer_bps INTEGER NOT NULL CHECK(cost_buffer_bps >= 100),
  state TEXT NOT NULL CHECK(state IN ('PREPARED','ATTEMPTED','ACCEPTED','UNKNOWN')),
  attempted_at TEXT,
  reserved_krw INTEGER CHECK(reserved_krw IS NULL OR reserved_krw > 0),
  fx_rate TEXT,
  outcome_at TEXT,
  outcome TEXT,
  broker_order_no TEXT,
  CHECK(state = 'PREPARED' OR attempted_at IS NOT NULL),
  CHECK(state = 'PREPARED' OR side = 'SELL' OR reserved_krw IS NOT NULL),
  CHECK(state != 'ACCEPTED' OR (broker_order_no IS NOT NULL AND length(broker_order_no) > 0)),
  CHECK(state != 'UNKNOWN' OR broker_order_no IS NULL));

CREATE TRIGGER IF NOT EXISTS meta_no_update BEFORE UPDATE ON live_meta
BEGIN SELECT RAISE(ABORT, 'live_meta is immutable'); END;
CREATE TRIGGER IF NOT EXISTS meta_no_delete BEFORE DELETE ON live_meta
BEGIN SELECT RAISE(ABORT, 'live_meta is immutable'); END;
CREATE TRIGGER IF NOT EXISTS halt_no_update BEFORE UPDATE ON halts
BEGIN SELECT RAISE(ABORT, 'halts are permanent'); END;
CREATE TRIGGER IF NOT EXISTS halt_no_delete BEFORE DELETE ON halts
BEGIN SELECT RAISE(ABORT, 'halts are permanent; there is no resume'); END;
CREATE TRIGGER IF NOT EXISTS ticket_no_delete BEFORE DELETE ON tickets
BEGIN SELECT RAISE(ABORT, 'tickets are permanent'); END;

CREATE TRIGGER IF NOT EXISTS ticket_insert BEFORE INSERT ON tickets
WHEN NEW.state != 'PREPARED' OR NEW.attempted_at IS NOT NULL OR NEW.reserved_krw IS NOT NULL
  OR NEW.fx_rate IS NOT NULL OR NEW.outcome_at IS NOT NULL OR NEW.outcome IS NOT NULL
  OR NEW.broker_order_no IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'new tickets start PREPARED'); END;
CREATE TRIGGER IF NOT EXISTS ticket_insert_halted BEFORE INSERT ON tickets
WHEN EXISTS(SELECT 1 FROM halts WHERE scope IN (NEW.market, 'ALL'))
BEGIN SELECT RAISE(ABORT, 'market halted'); END;

CREATE TRIGGER IF NOT EXISTS ticket_immutable BEFORE UPDATE ON tickets
WHEN NEW.ticket_id IS NOT OLD.ticket_id OR NEW.market IS NOT OLD.market OR NEW.side IS NOT OLD.side
  OR NEW.symbol IS NOT OLD.symbol OR NEW.exchange IS NOT OLD.exchange OR NEW.quantity IS NOT OLD.quantity
  OR NEW.limit_price IS NOT OLD.limit_price OR NEW.request_json IS NOT OLD.request_json
  OR NEW.terms_hash IS NOT OLD.terms_hash OR NEW.created_at IS NOT OLD.created_at
  OR NEW.expires_at IS NOT OLD.expires_at OR NEW.cost_buffer_bps IS NOT OLD.cost_buffer_bps
BEGIN SELECT RAISE(ABORT, 'ticket terms are immutable'); END;
CREATE TRIGGER IF NOT EXISTS ticket_transition BEFORE UPDATE ON tickets
WHEN NOT ((OLD.state = 'PREPARED' AND NEW.state = 'ATTEMPTED')
       OR (OLD.state = 'ATTEMPTED' AND NEW.state IN ('ACCEPTED', 'UNKNOWN')))
BEGIN SELECT RAISE(ABORT, 'illegal ticket state transition; attempted orders are never resent'); END;
CREATE TRIGGER IF NOT EXISTS ticket_attempt_fields BEFORE UPDATE ON tickets
WHEN OLD.state != 'PREPARED' AND (NEW.attempted_at IS NOT OLD.attempted_at
  OR NEW.reserved_krw IS NOT OLD.reserved_krw OR NEW.fx_rate IS NOT OLD.fx_rate)
BEGIN SELECT RAISE(ABORT, 'attempt record is immutable'); END;
CREATE TRIGGER IF NOT EXISTS ticket_attempt_clean BEFORE UPDATE ON tickets
WHEN NEW.state = 'ATTEMPTED' AND (NEW.outcome_at IS NOT NULL OR NEW.outcome IS NOT NULL
  OR NEW.broker_order_no IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'outcome recorded only after the attempt'); END;
"""

# v1 triggers replaced by v2: the blanket SELL ban and the fixed KRW 50,000 gate.
V1_REPLACED = ("ticket_insert_no_sell", "ticket_attempt_gate")

# v2 triggers replaced by v3 (US fills, US SELL, autonomous arming check).
V2_REPLACED = ("fill_insert", "resolution_insert", "ticket_insert_sell_v2", "ticket_attempt_gate_v2")

# SQL for "confirmed pilot shares of NEW.symbol still attributable to this program", excluding NEW.
_ENTITLEMENT_SQL = """(
  (SELECT COALESCE(SUM(m.q), 0) FROM (SELECT MAX(f.cum_qty) AS q FROM all_fills f JOIN tickets b ON b.ticket_id = f.ticket_id
     WHERE b.market = NEW.market AND b.symbol = NEW.symbol AND b.side = 'BUY' GROUP BY f.ticket_id) m)
  - (SELECT COALESCE(SUM(s.quantity), 0) FROM tickets s WHERE s.market = NEW.market AND s.symbol = NEW.symbol
     AND s.side = 'SELL' AND s.state != 'PREPARED' AND s.ticket_id != NEW.ticket_id))"""


def _arm_valid_sql(arm_ref: str, at_ref: str) -> str:
    """SQL: arming `arm_ref` is the newest arming event, an ARM, unexpired at `at_ref`, bound to the newest
    config and to the newest cap of each market, and there is no ALL halt. Same rule as live_auto.arming()."""
    return f"""EXISTS(SELECT 1 FROM auto_arming a WHERE a.arm_id = {arm_ref} AND a.action = 'ARM'
      AND a.arm_id = (SELECT MAX(arm_id) FROM auto_arming) AND a.expires_at > {at_ref}
      AND a.config_id = (SELECT MAX(config_id) FROM auto_configs)
      AND a.kr_cap_id IS (SELECT MAX(cap_id) FROM risk_caps WHERE market = 'KR')
      AND a.us_cap_id IS (SELECT MAX(cap_id) FROM risk_caps WHERE market = 'US')
      AND NOT EXISTS(SELECT 1 FROM halts WHERE scope = 'ALL'))"""


SCHEMA_V2 = f"""
CREATE TABLE IF NOT EXISTS risk_caps(
  cap_id INTEGER PRIMARY KEY AUTOINCREMENT,
  market TEXT NOT NULL CHECK(market IN ('KR','US')),
  max_committed_krw INTEGER NOT NULL CHECK(max_committed_krw >= 0),
  max_order_krw INTEGER NOT NULL CHECK(max_order_krw >= 0 AND max_order_krw <= max_committed_krw),
  cash_fraction_bps INTEGER NOT NULL CHECK(cash_fraction_bps BETWEEN 0 AND 10000),
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS cap_no_update BEFORE UPDATE ON risk_caps
BEGIN SELECT RAISE(ABORT, 'cap history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS cap_no_delete BEFORE DELETE ON risk_caps
BEGIN SELECT RAISE(ABORT, 'cap history is append-only'); END;

CREATE TABLE IF NOT EXISTS attempt_evidence(
  ticket_id TEXT PRIMARY KEY REFERENCES tickets(ticket_id),
  side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
  cap_id INTEGER REFERENCES risk_caps(cap_id),
  available_cash_krw INTEGER CHECK(available_cash_krw IS NULL OR available_cash_krw >= 0),
  cash_limit_krw INTEGER CHECK(cash_limit_krw IS NULL OR cash_limit_krw >= 0),
  broker_tradeable_qty INTEGER CHECK(broker_tradeable_qty IS NULL OR broker_tradeable_qty >= 0),
  ledger_entitlement_qty INTEGER CHECK(ledger_entitlement_qty IS NULL OR ledger_entitlement_qty >= 0),
  broker_read_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  CHECK(side = 'SELL' OR (cap_id IS NOT NULL AND available_cash_krw IS NOT NULL AND cash_limit_krw IS NOT NULL
                          AND broker_tradeable_qty IS NULL AND ledger_entitlement_qty IS NULL)),
  CHECK(side = 'BUY' OR (cap_id IS NULL AND broker_tradeable_qty IS NOT NULL AND ledger_entitlement_qty IS NOT NULL)));
CREATE TRIGGER IF NOT EXISTS evidence_insert BEFORE INSERT ON attempt_evidence
WHEN NOT EXISTS(SELECT 1 FROM tickets t WHERE t.ticket_id = NEW.ticket_id AND t.state = 'PREPARED' AND t.side = NEW.side)
BEGIN SELECT RAISE(ABORT, 'evidence only for the PREPARED ticket being attempted'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_update BEFORE UPDATE ON attempt_evidence
BEGIN SELECT RAISE(ABORT, 'attempt evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_delete BEFORE DELETE ON attempt_evidence
BEGIN SELECT RAISE(ABORT, 'attempt evidence is immutable'); END;

CREATE TABLE IF NOT EXISTS submission_claims(
  ticket_id TEXT PRIMARY KEY REFERENCES tickets(ticket_id),
  claimed_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS claim_insert BEFORE INSERT ON submission_claims
WHEN NOT EXISTS(SELECT 1 FROM tickets t JOIN attempt_evidence e ON e.ticket_id = t.ticket_id
                WHERE t.ticket_id = NEW.ticket_id AND t.state = 'ATTEMPTED' AND t.outcome IS NULL)
BEGIN SELECT RAISE(ABORT, 'submission claim requires an attempted ticket with safety evidence'); END;
CREATE TRIGGER IF NOT EXISTS claim_no_update BEFORE UPDATE ON submission_claims
BEGIN SELECT RAISE(ABORT, 'submission claims are immutable'); END;
CREATE TRIGGER IF NOT EXISTS claim_no_delete BEFORE DELETE ON submission_claims
BEGIN SELECT RAISE(ABORT, 'submission claims are immutable'); END;

CREATE TABLE IF NOT EXISTS resolutions(
  ticket_id TEXT PRIMARY KEY REFERENCES tickets(ticket_id),
  kind TEXT NOT NULL CHECK(kind IN ('BROKER_FILLED','HUMAN_CLOSED')),
  filled_qty INTEGER NOT NULL CHECK(filled_qty >= 0),
  note TEXT NOT NULL,
  created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS order_links(
  ticket_id TEXT PRIMARY KEY REFERENCES tickets(ticket_id),
  market TEXT NOT NULL CHECK(market IN ('KR','US')),
  order_date TEXT NOT NULL CHECK(length(order_date) = 8 AND order_date NOT GLOB '*[^0-9]*'),
  broker_order_no TEXT NOT NULL CHECK(length(broker_order_no) BETWEEN 1 AND 12
      AND broker_order_no NOT GLOB '*[^0-9]*' AND broker_order_no NOT GLOB '0*'),
  source TEXT NOT NULL CHECK(source IN ('SEND_RESPONSE','HUMAN_ORDER_NO_VERIFIED')),
  created_at TEXT NOT NULL,
  UNIQUE(market, order_date, broker_order_no));
CREATE TRIGGER IF NOT EXISTS link_insert BEFORE INSERT ON order_links
WHEN NOT EXISTS(SELECT 1 FROM tickets t WHERE t.ticket_id = NEW.ticket_id AND t.market = NEW.market AND (
       (NEW.source = 'SEND_RESPONSE' AND t.state = 'ACCEPTED' AND ltrim(t.broker_order_no, '0') = NEW.broker_order_no)
    OR (NEW.source = 'HUMAN_ORDER_NO_VERIFIED' AND t.state IN ('ATTEMPTED', 'UNKNOWN'))))
  OR EXISTS(SELECT 1 FROM resolutions r WHERE r.ticket_id = NEW.ticket_id)
  OR EXISTS(SELECT 1 FROM tickets o WHERE o.market = NEW.market AND o.ticket_id != NEW.ticket_id
            AND ltrim(o.broker_order_no, '0') = NEW.broker_order_no
            AND ((o.market = 'KR' AND strftime('%Y%m%d', o.attempted_at, '+9 hours') = NEW.order_date)
                 OR (o.market = 'US' AND
                     (strftime('%Y%m%d', o.attempted_at, '-4 hours') = NEW.order_date OR
                      strftime('%Y%m%d', o.attempted_at, '-5 hours') = NEW.order_date))))
BEGIN SELECT RAISE(ABORT, 'order identity refused'); END;
CREATE TRIGGER IF NOT EXISTS link_no_update BEFORE UPDATE ON order_links
BEGIN SELECT RAISE(ABORT, 'order links are immutable'); END;
CREATE TRIGGER IF NOT EXISTS link_no_delete BEFORE DELETE ON order_links
BEGIN SELECT RAISE(ABORT, 'order links are immutable'); END;

CREATE TABLE IF NOT EXISTS fills(
  fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
  cum_qty INTEGER NOT NULL CHECK(cum_qty > 0),
  avg_price TEXT NOT NULL,
  source_api TEXT NOT NULL CHECK(source_api = 'kt00007'),
  observed_at TEXT NOT NULL,
  evidence_hash TEXT NOT NULL,
  UNIQUE(ticket_id, cum_qty));
CREATE TRIGGER IF NOT EXISTS fill_no_update BEFORE UPDATE ON fills
BEGIN SELECT RAISE(ABORT, 'fills are append-only'); END;
CREATE TRIGGER IF NOT EXISTS fill_no_delete BEFORE DELETE ON fills
BEGIN SELECT RAISE(ABORT, 'fills are append-only'); END;

CREATE TRIGGER IF NOT EXISTS resolution_no_update BEFORE UPDATE ON resolutions
BEGIN SELECT RAISE(ABORT, 'resolutions are permanent'); END;
CREATE TRIGGER IF NOT EXISTS resolution_no_delete BEFORE DELETE ON resolutions
BEGIN SELECT RAISE(ABORT, 'resolutions are permanent'); END;
"""

# v3: US fills, US SELL, autonomous configuration/arming/cycle records. The gate below replaces
# ticket_attempt_gate_v2 with the same checks plus (a) entitlement from both fill tables, (b) SELL for
# both markets, (c) for tickets with an autonomous intent, a still-valid arming at the attempt instant.
SCHEMA_V3 = f"""
CREATE TABLE IF NOT EXISTS fills_us(
  fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
  cum_qty INTEGER NOT NULL CHECK(cum_qty > 0),
  avg_price TEXT NOT NULL,
  source_api TEXT NOT NULL CHECK(source_api = 'ust21180'),
  observed_at TEXT NOT NULL,
  evidence_hash TEXT NOT NULL,
  UNIQUE(ticket_id, cum_qty));
CREATE TRIGGER IF NOT EXISTS fill_us_no_update BEFORE UPDATE ON fills_us
BEGIN SELECT RAISE(ABORT, 'fills are append-only'); END;
CREATE TRIGGER IF NOT EXISTS fill_us_no_delete BEFORE DELETE ON fills_us
BEGIN SELECT RAISE(ABORT, 'fills are append-only'); END;
CREATE VIEW IF NOT EXISTS all_fills AS
  SELECT ticket_id, cum_qty, avg_price, source_api, observed_at FROM fills
  UNION ALL SELECT ticket_id, cum_qty, avg_price, source_api, observed_at FROM fills_us;

CREATE TRIGGER IF NOT EXISTS fill_insert_v3 BEFORE INSERT ON fills
WHEN NOT EXISTS(SELECT 1 FROM order_links l JOIN tickets t ON t.ticket_id = l.ticket_id
                WHERE l.ticket_id = NEW.ticket_id AND t.market = 'KR')
  OR EXISTS(SELECT 1 FROM resolutions r WHERE r.ticket_id = NEW.ticket_id)
  OR NEW.cum_qty > COALESCE((SELECT quantity FROM tickets WHERE ticket_id = NEW.ticket_id), 0)
  OR NEW.cum_qty <= (SELECT COALESCE(MAX(cum_qty), 0) FROM all_fills WHERE ticket_id = NEW.ticket_id)
BEGIN SELECT RAISE(ABORT, 'fill refused: needs KR order identity, open ticket, increasing cumulative quantity'); END;
CREATE TRIGGER IF NOT EXISTS fill_us_insert BEFORE INSERT ON fills_us
WHEN NOT EXISTS(SELECT 1 FROM order_links l JOIN tickets t ON t.ticket_id = l.ticket_id
                WHERE l.ticket_id = NEW.ticket_id AND t.market = 'US')
  OR EXISTS(SELECT 1 FROM resolutions r WHERE r.ticket_id = NEW.ticket_id)
  OR NEW.cum_qty > COALESCE((SELECT quantity FROM tickets WHERE ticket_id = NEW.ticket_id), 0)
  OR NEW.cum_qty <= (SELECT COALESCE(MAX(cum_qty), 0) FROM all_fills WHERE ticket_id = NEW.ticket_id)
BEGIN SELECT RAISE(ABORT, 'fill refused: needs US order identity, open ticket, increasing cumulative quantity'); END;

CREATE TRIGGER IF NOT EXISTS resolution_insert_v3 BEFORE INSERT ON resolutions
WHEN NOT EXISTS(SELECT 1 FROM tickets t WHERE t.ticket_id = NEW.ticket_id AND t.state IN ('ATTEMPTED','ACCEPTED','UNKNOWN'))
  OR NEW.filled_qty != (SELECT COALESCE(MAX(cum_qty), 0) FROM all_fills WHERE ticket_id = NEW.ticket_id)
  OR (NEW.kind = 'BROKER_FILLED' AND (
         NEW.filled_qty != COALESCE((SELECT quantity FROM tickets WHERE ticket_id = NEW.ticket_id), -1)
      OR NOT EXISTS(SELECT 1 FROM order_links l WHERE l.ticket_id = NEW.ticket_id)))
BEGIN SELECT RAISE(ABORT, 'resolution refused'); END;

CREATE TABLE IF NOT EXISTS auto_configs(
  config_id INTEGER PRIMARY KEY AUTOINCREMENT,
  config_json TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS auto_config_no_update BEFORE UPDATE ON auto_configs
BEGIN SELECT RAISE(ABORT, 'auto configs are append-only'); END;
CREATE TRIGGER IF NOT EXISTS auto_config_no_delete BEFORE DELETE ON auto_configs
BEGIN SELECT RAISE(ABORT, 'auto configs are append-only'); END;

CREATE TABLE IF NOT EXISTS auto_arming(
  arm_id INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL CHECK(action IN ('ARM','DISARM')),
  config_id INTEGER REFERENCES auto_configs(config_id),
  kr_cap_id INTEGER REFERENCES risk_caps(cap_id),
  us_cap_id INTEGER REFERENCES risk_caps(cap_id),
  expires_at TEXT,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  CHECK(action = 'DISARM' OR (config_id IS NOT NULL AND expires_at IS NOT NULL)),
  CHECK(action = 'ARM' OR (config_id IS NULL AND expires_at IS NULL AND kr_cap_id IS NULL AND us_cap_id IS NULL)));
CREATE TRIGGER IF NOT EXISTS auto_arm_insert BEFORE INSERT ON auto_arming
WHEN NEW.action = 'ARM' AND (
     NEW.config_id IS NOT (SELECT MAX(config_id) FROM auto_configs)
  OR NEW.kr_cap_id IS NOT (SELECT MAX(cap_id) FROM risk_caps WHERE market = 'KR')
  OR NEW.us_cap_id IS NOT (SELECT MAX(cap_id) FROM risk_caps WHERE market = 'US')
  OR NEW.expires_at <= NEW.created_at
  OR EXISTS(SELECT 1 FROM halts WHERE scope = 'ALL'))
BEGIN SELECT RAISE(ABORT, 'arming refused: must bind the latest config and caps'); END;
CREATE TRIGGER IF NOT EXISTS auto_arm_no_update BEFORE UPDATE ON auto_arming
BEGIN SELECT RAISE(ABORT, 'arming history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS auto_arm_no_delete BEFORE DELETE ON auto_arming
BEGIN SELECT RAISE(ABORT, 'arming history is append-only'); END;

CREATE TABLE IF NOT EXISTS auto_cycles(
  cycle_key TEXT PRIMARY KEY,
  market TEXT NOT NULL CHECK(market IN ('KR','US')),
  session_date TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('DRY_RUN','LIVE')),
  config_id INTEGER NOT NULL REFERENCES auto_configs(config_id),
  arm_id INTEGER REFERENCES auto_arming(arm_id),
  status TEXT NOT NULL CHECK(status IN ('STARTED','COMPLETED','ABSTAINED','FAILED')),
  outcome_json TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  CHECK(mode = 'DRY_RUN' OR arm_id IS NOT NULL));
CREATE TRIGGER IF NOT EXISTS auto_cycle_insert BEFORE INSERT ON auto_cycles
WHEN NEW.status != 'STARTED' OR NEW.finished_at IS NOT NULL OR NEW.outcome_json IS NOT NULL
  OR NEW.config_id IS NOT (SELECT MAX(config_id) FROM auto_configs)
  OR (NEW.mode = 'LIVE' AND NOT {_arm_valid_sql('NEW.arm_id', 'NEW.started_at')})
BEGIN SELECT RAISE(ABORT, 'cycle refused'); END;
CREATE TRIGGER IF NOT EXISTS auto_cycle_update BEFORE UPDATE ON auto_cycles
WHEN OLD.status != 'STARTED' OR NEW.status = 'STARTED' OR NEW.finished_at IS NULL
  OR NEW.cycle_key IS NOT OLD.cycle_key OR NEW.market IS NOT OLD.market OR NEW.session_date IS NOT OLD.session_date
  OR NEW.mode IS NOT OLD.mode OR NEW.config_id IS NOT OLD.config_id OR NEW.arm_id IS NOT OLD.arm_id
  OR NEW.started_at IS NOT OLD.started_at
BEGIN SELECT RAISE(ABORT, 'a cycle finishes once'); END;
CREATE TRIGGER IF NOT EXISTS auto_cycle_no_delete BEFORE DELETE ON auto_cycles
BEGIN SELECT RAISE(ABORT, 'cycles are permanent'); END;

CREATE TABLE IF NOT EXISTS auto_decisions(
  cycle_key TEXT PRIMARY KEY REFERENCES auto_cycles(cycle_key),
  evidence_version TEXT NOT NULL,
  evidence_hash TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  proposer TEXT NOT NULL CHECK(proposer IN ('MODEL','BASELINE')),
  proposal_json TEXT NOT NULL,
  model_meta_json TEXT NOT NULL,
  baseline_json TEXT NOT NULL,
  model_cost_krw TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS auto_decision_insert BEFORE INSERT ON auto_decisions
WHEN NOT EXISTS(SELECT 1 FROM auto_cycles c WHERE c.cycle_key = NEW.cycle_key AND c.status = 'STARTED')
BEGIN SELECT RAISE(ABORT, 'decision needs a running cycle'); END;
CREATE TRIGGER IF NOT EXISTS auto_decision_no_update BEFORE UPDATE ON auto_decisions
BEGIN SELECT RAISE(ABORT, 'decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS auto_decision_no_delete BEFORE DELETE ON auto_decisions
BEGIN SELECT RAISE(ABORT, 'decisions are immutable'); END;

CREATE TABLE IF NOT EXISTS auto_intents(
  intent_key TEXT PRIMARY KEY,
  cycle_key TEXT NOT NULL UNIQUE REFERENCES auto_cycles(cycle_key),
  ticket_id TEXT NOT NULL UNIQUE REFERENCES tickets(ticket_id),
  arm_id INTEGER NOT NULL REFERENCES auto_arming(arm_id),
  risk_json TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS auto_intent_insert BEFORE INSERT ON auto_intents
WHEN NOT EXISTS(SELECT 1 FROM auto_cycles c JOIN tickets t ON t.ticket_id = NEW.ticket_id
                WHERE c.cycle_key = NEW.cycle_key AND c.mode = 'LIVE' AND c.status = 'STARTED'
                  AND c.arm_id = NEW.arm_id AND c.market = t.market AND t.state = 'PREPARED')
  OR NOT EXISTS(SELECT 1 FROM auto_decisions d WHERE d.cycle_key = NEW.cycle_key)
  OR NOT {_arm_valid_sql('NEW.arm_id', 'NEW.created_at')}
BEGIN SELECT RAISE(ABORT, 'order intent refused'); END;
CREATE TRIGGER IF NOT EXISTS auto_intent_no_update BEFORE UPDATE ON auto_intents
BEGIN SELECT RAISE(ABORT, 'intents are immutable'); END;
CREATE TRIGGER IF NOT EXISTS auto_intent_no_delete BEFORE DELETE ON auto_intents
BEGIN SELECT RAISE(ABORT, 'intents are immutable'); END;

CREATE TABLE IF NOT EXISTS auto_marks(
  mark_id INTEGER PRIMARY KEY AUTOINCREMENT,
  market TEXT NOT NULL CHECK(market IN ('KR','US')),
  session_date TEXT NOT NULL,
  cycle_key TEXT NOT NULL REFERENCES auto_cycles(cycle_key),
  pnl_krw TEXT NOT NULL,
  exposure_krw TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS auto_mark_no_update BEFORE UPDATE ON auto_marks
BEGIN SELECT RAISE(ABORT, 'marks are append-only'); END;
CREATE TRIGGER IF NOT EXISTS auto_mark_no_delete BEFORE DELETE ON auto_marks
BEGIN SELECT RAISE(ABORT, 'marks are append-only'); END;

CREATE TABLE IF NOT EXISTS auto_events(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  market TEXT CHECK(market IS NULL OR market IN ('KR','US')),
  kind TEXT NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS auto_event_no_update BEFORE UPDATE ON auto_events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS auto_event_no_delete BEFORE DELETE ON auto_events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

CREATE TABLE IF NOT EXISTS sell_ownership_checks(
  ticket_id TEXT PRIMARY KEY REFERENCES tickets(ticket_id),
  broker_held_qty INTEGER NOT NULL CHECK(broker_held_qty >= 0),
  checked_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS ownership_insert BEFORE INSERT ON sell_ownership_checks
WHEN NOT EXISTS(SELECT 1 FROM tickets t JOIN attempt_evidence e ON e.ticket_id = t.ticket_id
                WHERE t.ticket_id = NEW.ticket_id AND t.state = 'PREPARED' AND t.side = 'SELL'
                  AND e.ledger_entitlement_qty = NEW.broker_held_qty)
BEGIN SELECT RAISE(ABORT, 'sell ownership must equal broker holdings and pilot entitlement'); END;
CREATE TRIGGER IF NOT EXISTS ownership_no_update BEFORE UPDATE ON sell_ownership_checks
BEGIN SELECT RAISE(ABORT, 'sell ownership checks are immutable'); END;
CREATE TRIGGER IF NOT EXISTS ownership_no_delete BEFORE DELETE ON sell_ownership_checks
BEGIN SELECT RAISE(ABORT, 'sell ownership checks are immutable'); END;

CREATE TRIGGER IF NOT EXISTS ticket_attempt_gate_v3 BEFORE UPDATE ON tickets
WHEN NEW.state = 'ATTEMPTED' AND (
     NEW.attempted_at >= OLD.expires_at
  OR EXISTS(SELECT 1 FROM halts WHERE scope IN (NEW.market, 'ALL'))
  OR EXISTS(SELECT 1 FROM tickets t WHERE t.market = NEW.market AND t.ticket_id != NEW.ticket_id
            AND t.state IN ('ATTEMPTED', 'ACCEPTED', 'UNKNOWN')
            AND NOT EXISTS(SELECT 1 FROM resolutions r WHERE r.ticket_id = t.ticket_id))
  OR NOT EXISTS(SELECT 1 FROM attempt_evidence e WHERE e.ticket_id = NEW.ticket_id AND e.side = NEW.side)
  OR (NEW.side = 'BUY' AND (
        NEW.reserved_krw IS NULL
     OR COALESCE((SELECT MAX(cap_id) FROM risk_caps WHERE market = NEW.market), -1)
        != COALESCE((SELECT e.cap_id FROM attempt_evidence e JOIN risk_caps r ON r.cap_id = e.cap_id
                     WHERE e.ticket_id = NEW.ticket_id AND r.market = NEW.market), -2)
     OR NEW.reserved_krw > COALESCE((SELECT cash_limit_krw FROM attempt_evidence WHERE ticket_id = NEW.ticket_id), -1)
     OR NEW.reserved_krw > COALESCE((SELECT r.max_order_krw FROM risk_caps r JOIN attempt_evidence e ON e.cap_id = r.cap_id
                                     WHERE e.ticket_id = NEW.ticket_id), -1)
     OR (SELECT COALESCE(SUM(t.reserved_krw), 0) FROM tickets t WHERE t.market = NEW.market AND t.side = 'BUY'
         AND t.state != 'PREPARED' AND t.ticket_id != NEW.ticket_id) + NEW.reserved_krw
        > COALESCE((SELECT r.max_committed_krw FROM risk_caps r JOIN attempt_evidence e ON e.cap_id = r.cap_id
                    WHERE e.ticket_id = NEW.ticket_id), -1)))
  OR (NEW.side = 'SELL' AND (
        NEW.reserved_krw IS NOT NULL
     OR NOT EXISTS(SELECT 1 FROM sell_ownership_checks o JOIN attempt_evidence e ON e.ticket_id = o.ticket_id
                   WHERE o.ticket_id = NEW.ticket_id AND o.broker_held_qty = e.ledger_entitlement_qty
                     AND o.broker_held_qty = {_ENTITLEMENT_SQL})
     OR NEW.quantity > COALESCE((SELECT broker_tradeable_qty FROM attempt_evidence WHERE ticket_id = NEW.ticket_id), -1)
     OR NEW.quantity > COALESCE((SELECT ledger_entitlement_qty FROM attempt_evidence WHERE ticket_id = NEW.ticket_id), -1)
     OR NEW.quantity > {_ENTITLEMENT_SQL}))
  OR (EXISTS(SELECT 1 FROM auto_intents i WHERE i.ticket_id = NEW.ticket_id)
      AND NOT EXISTS(SELECT 1 FROM auto_intents i WHERE i.ticket_id = NEW.ticket_id
                     AND {_arm_valid_sql('i.arm_id', 'NEW.attempted_at')}))
  OR (EXISTS(SELECT 1 FROM auto_intents i WHERE i.ticket_id = NEW.ticket_id)
      AND EXISTS(SELECT 1 FROM resolutions r JOIN tickets t ON t.ticket_id = r.ticket_id
                 WHERE (t.market = NEW.market OR NEW.side = 'BUY')
                   AND r.kind = 'HUMAN_CLOSED' AND r.filled_qty < t.quantity)))
BEGIN SELECT RAISE(ABORT, 'attempt refused by safety gate'); END;
"""

V3_TRIGGERS = ("cap_no_update", "cap_no_delete", "evidence_insert", "evidence_no_update", "evidence_no_delete",
               "claim_insert", "claim_no_update", "claim_no_delete",
               "link_insert", "link_no_update", "link_no_delete", "fill_insert_v3", "fill_no_update", "fill_no_delete",
               "fill_us_insert", "fill_us_no_update", "fill_us_no_delete",
               "resolution_insert_v3", "resolution_no_update", "resolution_no_delete",
               "ticket_attempt_gate_v3", "ticket_transition", "ticket_immutable", "ticket_attempt_fields",
               "ticket_attempt_clean", "ticket_insert", "ticket_insert_halted", "ticket_no_delete",
               "halt_no_update", "halt_no_delete", "meta_no_update", "meta_no_delete",
               "auto_config_no_update", "auto_config_no_delete", "auto_arm_insert", "auto_arm_no_update",
               "auto_arm_no_delete", "auto_cycle_insert", "auto_cycle_update", "auto_cycle_no_delete",
               "auto_decision_insert", "auto_decision_no_update", "auto_decision_no_delete",
               "auto_intent_insert", "auto_intent_no_update", "auto_intent_no_delete",
               "auto_mark_no_update", "auto_mark_no_delete", "auto_event_no_update", "auto_event_no_delete",
               "ownership_insert", "ownership_no_update", "ownership_no_delete")
V3_TABLES = ("live_meta", "halts", "tickets", "risk_caps", "attempt_evidence", "submission_claims",
             "resolutions", "order_links", "fills", "fills_us", "auto_configs", "auto_arming", "auto_cycles",
             "auto_decisions", "auto_intents", "auto_marks", "auto_events", "sell_ownership_checks")


# ---------------------------------------------------------------- database

def _is_live_db(path: Path) -> bool:
    try:
        probe = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = probe.execute("SELECT value FROM live_meta WHERE key = 'schema'").fetchone()
        finally:
            probe.close()
    except sqlite3.Error:
        return False
    return bool(row) and row[0] == SCHEMA_MARKER


def _canonical_schema_sql():
    """SQLite's own parsed DDL for the canonical live schema, without touching the user's ledger."""
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(SCHEMA_V1 + SCHEMA_V2 + SCHEMA_V3)
        return {(r[0], r[1]): r[2] for r in reference.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table','trigger','view') "
            "AND name NOT LIKE 'sqlite_%'")}
    finally:
        reference.close()


def _verify_schema(conn, expected):
    actual = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table','trigger','view') "
        "AND name NOT LIKE 'sqlite_%'")}
    names = set(actual)
    missing = [n for n in V3_TABLES if ("table", n) not in names] + \
              [n for n in V3_TRIGGERS if ("trigger", n) not in names] + \
              ([] if ("view", "all_fills") in names else ["all_fills"])
    leftover = [n for n in V1_REPLACED + V2_REPLACED if ("trigger", n) in names]
    meta = dict(conn.execute("SELECT key, value FROM live_meta").fetchall())
    changed = [key for key in expected if actual.get(key) != expected[key]]
    if missing or leftover or changed or set(actual) != set(expected) \
            or meta.get("schema") != SCHEMA_MARKER or meta.get("schema_v2") != V2_MARKER \
            or meta.get("schema_v3") != V3_MARKER:
        raise ValidationError("실전 주문 DB 구조를 확인하지 못했습니다(마이그레이션 불확실). 아무 작업도 하지 않습니다.")


def open_db():
    """Open the per-user ledger and migrate v1 -> v2 -> v3 in one transaction.

    The migration creates missing objects and refreshes canonical trigger bodies; it never updates
    or deletes a ticket, halt, meta, cap or fill row. Any failure rolls back completely.
    """
    p = _ledger_path()
    if p.exists() and p.stat().st_size > 0:
        if not _is_live_db(p):
            raise ValidationError("이 파일은 실전 주문 전용 DB가 아닙니다. 모의/연구 DB는 사용할 수 없습니다.")
    elif Path(f"{p}-wal").exists() or Path(f"{p}-journal").exists():
        raise ValidationError("DB 옆에 남은 저널 파일이 있습니다. 상태를 확인하기 전에는 열지 않습니다.")
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous = FULL")
    try:
        # One script so executescript's implicit COMMIT cannot split the schema transaction.
        conn.executescript(
            "BEGIN IMMEDIATE;" + SCHEMA_V1 +
            f"INSERT OR IGNORE INTO live_meta(key, value) VALUES ('schema', '{SCHEMA_MARKER}');" +
            "".join(f"DROP TRIGGER IF EXISTS {name};" for name in V1_REPLACED) + SCHEMA_V2 +
            f"INSERT OR IGNORE INTO live_meta(key, value) VALUES ('schema_v2', '{V2_MARKER}');"
            f"INSERT OR IGNORE INTO live_meta(key, value) VALUES ('schema_v2_at', '{now()}');" +
            "".join(f"DROP TRIGGER IF EXISTS {name};" for name in V2_REPLACED) +
            "DROP TRIGGER IF EXISTS ticket_attempt_gate_v3;" + SCHEMA_V3 +
            f"INSERT OR IGNORE INTO live_meta(key, value) VALUES ('schema_v3', '{V3_MARKER}');"
            f"INSERT OR IGNORE INTO live_meta(key, value) VALUES ('schema_v3_at', '{now()}');")
        expected = _canonical_schema_sql()
        existing = {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE type = 'trigger'")}
        for (kind, name), sql in expected.items():
            if kind == "trigger" and existing.get((kind, name)) != sql:
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
                conn.execute(sql)
        _verify_schema(conn, expected)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
        raise
    return conn


def _expired(row, at=None) -> bool:
    return (at or now()) >= row["expires_at"]


def _resolution(conn, ticket_id):
    return conn.execute("SELECT * FROM resolutions WHERE ticket_id = ?", (ticket_id,)).fetchone()


def _display_state(conn, row) -> str:
    if row["state"] == "PREPARED" and _expired(row):
        return "EXPIRED"
    resolved = _resolution(conn, row["ticket_id"]) if row["state"] in IN_FLIGHT else None
    if resolved:
        return "FILLED" if resolved["kind"] == "BROKER_FILLED" else "CLOSED_BY_HUMAN"
    if row["state"] == "ATTEMPTED":
        return "ATTEMPTED_OUTCOME_UNKNOWN"
    return row["state"]


def _halts(conn, market):
    return conn.execute("SELECT scope, reason, created_at FROM halts WHERE scope IN (?, 'ALL') ORDER BY halt_id",
                        (market,)).fetchall()


def _in_flight(conn, market, exclude=None):
    """Attempted tickets without a resolution: they block every new order in the market."""
    return [r["ticket_id"] for r in conn.execute(
        "SELECT ticket_id FROM tickets t WHERE market = ? AND state IN ('ATTEMPTED','ACCEPTED','UNKNOWN') "
        "AND NOT EXISTS(SELECT 1 FROM resolutions r WHERE r.ticket_id = t.ticket_id) "
        "AND ticket_id IS NOT ? ORDER BY created_at", (market, exclude))]


def _open_prepared(conn, market, exclude=None):
    at = now()
    return [r["ticket_id"] for r in conn.execute(
        "SELECT ticket_id FROM tickets WHERE market = ? AND state = 'PREPARED' AND expires_at > ? "
        "AND ticket_id IS NOT ?", (market, at, exclude))]


def _committed(conn, market) -> int:
    """Cumulative BUY reservations (legacy v1 tickets included); never released by sales or closes."""
    row = conn.execute("SELECT COALESCE(SUM(reserved_krw), 0) FROM tickets WHERE market = ? AND side = 'BUY' "
                       "AND state != 'PREPARED'", (market,)).fetchone()
    return int(row[0])


def _latest_cap(conn, market):
    return conn.execute("SELECT * FROM risk_caps WHERE market = ? ORDER BY cap_id DESC LIMIT 1", (market,)).fetchone()


def _filled(conn, ticket_id) -> int:
    return int(conn.execute("SELECT COALESCE(MAX(cum_qty), 0) FROM all_fills WHERE ticket_id = ?",
                            (ticket_id,)).fetchone()[0])


def _entitlement(conn, market, symbol, exclude=None) -> int:
    """Same arithmetic as the SQL gate: confirmed pilot BUY fills minus every attempted SELL quantity."""
    bought = conn.execute(
        "SELECT COALESCE(SUM(m.q), 0) FROM (SELECT MAX(f.cum_qty) AS q FROM all_fills f JOIN tickets b "
        "ON b.ticket_id = f.ticket_id WHERE b.market = ? AND b.symbol = ? AND b.side = 'BUY' GROUP BY f.ticket_id) m",
        (market, symbol)).fetchone()[0]
    sold = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM tickets WHERE market = ? AND symbol = ? AND side = 'SELL' "
        "AND state != 'PREPARED' AND ticket_id IS NOT ?", (market, symbol, exclude)).fetchone()[0]
    return int(bought) - int(sold)


def _uncertain_human_closes(conn, market):
    """Tickets whose unconfirmed remainder can invalidate inventory and P&L."""
    return [r[0] for r in conn.execute(
        "SELECT t.ticket_id FROM resolutions r JOIN tickets t ON t.ticket_id = r.ticket_id "
        "WHERE t.market = ? AND r.kind = 'HUMAN_CLOSED' AND r.filled_qty < t.quantity "
        "ORDER BY r.created_at", (market,))]


def _blockers(conn, market, side, ticket_id=None, symbol=None):
    reasons = []
    if _halts(conn, market):
        reasons.append("HALTED")
    if _in_flight(conn, market, ticket_id):
        reasons.append("UNRECONCILED_ORDER_IN_MARKET")
    if _open_prepared(conn, market, ticket_id):
        reasons.append("ANOTHER_OPEN_TICKET_IN_MARKET")
    if side == "BUY":
        cap = _latest_cap(conn, market)
        if cap is None:
            reasons.append("CAP_NOT_CONFIGURED")
        elif _committed(conn, market) >= cap["max_committed_krw"] or cap["max_order_krw"] == 0 \
                or cap["cash_fraction_bps"] == 0:
            reasons.append("CAP_EXHAUSTED")
    elif symbol is not None and _entitlement(conn, market, symbol, ticket_id) <= 0:
        reasons.append("NO_PILOT_INVENTORY")
    return reasons


BLOCKER_TEXT = {
    "HALTED": "이 시장(또는 전체)이 halt 상태입니다. 해제(resume) 기능은 없습니다.",
    "UNRECONCILED_ORDER_IN_MARKET": "이 시장에 종결되지 않은 주문이 있습니다. `live reconcile` 또는 앱 확인 후 `live close`로 종결하세요.",
    "ANOTHER_OPEN_TICKET_IN_MARKET": "이 시장에 유효한 다른 티켓이 있습니다. 만료(5분) 후 다시 준비하세요.",
    "CAP_NOT_CONFIGURED": "이 시장의 BUY 한도가 설정되지 않았습니다. `live cap`으로 명시적으로 설정하기 전에는 매수하지 않습니다.",
    "CAP_EXHAUSTED": "이 시장의 명시적 BUY 한도(누적·주문당·현금비율)가 소진되었거나 0입니다.",
    "NO_PILOT_INVENTORY": "이 종목에 이 프로그램의 확인된 매수 체결 수량이 없습니다. 기존 보유분은 매도하지 않습니다.",
}


def _refuse(reasons):
    raise ValidationError(" / ".join(BLOCKER_TEXT[r] for r in reasons))


def _one_line(text, name):
    if not isinstance(text, str) or not text.strip() or len(text) > 200 or any(ord(ch) < 32 for ch in text):
        raise ValidationError(f"{name}를 200자 이내 한 줄로 입력하세요.")
    return text.strip()


# ---------------------------------------------------------------- money

def _ceil_krw(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _floor_krw(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _with_buffer(terms: OrderTerms) -> Decimal:
    """Gross limit value plus the cost buffer, in the market currency."""
    return terms.gross() * (1 + Decimal(COST_BUFFER_BPS[terms.market]) / 10000)


def _row_terms(row) -> OrderTerms:
    return OrderTerms(market=row["market"], side=row["side"], symbol=row["symbol"], exchange=row["exchange"],
                      quantity=row["quantity"], limit_price=row["limit_price"])


def _terms_hash(ticket_id, created_at, expires_at, terms, request) -> str:
    return digest({"schema": SCHEMA_MARKER, "ticket_id": ticket_id, "created_at": created_at,
                   "expires_at": expires_at, "terms": terms.payload(), "request": request,
                   "cost_buffer_bps": COST_BUFFER_BPS[terms.market]})


def _verify_integrity(row) -> OrderTerms:
    terms = _row_terms(row)
    request = build_request(terms)
    if canonical(request) != row["request_json"] or row["cost_buffer_bps"] != COST_BUFFER_BPS[terms.market] or \
            _terms_hash(row["ticket_id"], row["created_at"], row["expires_at"], terms, request) != row["terms_hash"]:
        raise ValidationError("티켓 내용이 저장된 해시와 일치하지 않습니다. 전송하지 않습니다.")
    return terms


def confirmation_phrase(row) -> str:
    return f"SEND REAL {row['side']} {row['market']} {row['symbol']} {row['ticket_id']} {row['terms_hash'][:12]}"


def _age_seconds(iso) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()


def _auto_session_open(conn, market) -> bool:
    """Recheck the configured regular-session window at the actual transmission boundary."""
    from . import live_auto, live_calendar
    _, cfg = live_auto.latest_config(conn)
    if cfg is None or market not in cfg["markets"] or not cfg["markets"][market]["enabled"]:
        return False
    m = cfg["markets"][market]
    state = live_calendar.session_state(market, m["calendar"], datetime.now(timezone.utc),
                                        open_buffer_min=m["open_buffer_minutes"],
                                        close_buffer_min=m["close_buffer_minutes"])
    return state["state"] == "TRADING_WINDOW"


# ---------------------------------------------------------------- commands

def prepare(conn, *, market, side, symbol, exchange, quantity, limit_price, intent=None):
    """Create a 5-minute ticket. Offline: no broker, keyring or network access. Cannot send.

    `intent` (autonomous runner only): {"intent_key", "cycle_key", "arm_id", "risk_json"}; the order intent is
    inserted in the same transaction, so a ticket and its cycle binding exist together or not at all.
    """
    if market == "KR" and exchange is None:
        exchange = "KRX"
    if exchange is None:
        raise ValidationError("미국 주문은 --exchange ND|NY|NA를 명시하세요.")
    terms = OrderTerms(market=market, side=side, symbol=symbol, exchange=exchange,
                       quantity=quantity, limit_price=str(limit_price))
    request = build_request(terms)
    estimate = remaining = entitlement = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        blockers = _blockers(conn, market, side, symbol=terms.symbol)
        if blockers:
            _refuse(blockers)
        if side == "BUY":
            cap = _latest_cap(conn, market)
            remaining = cap["max_committed_krw"] - _committed(conn, market)
            if market == "KR":
                estimate = _ceil_krw(_with_buffer(terms))
            else:
                # Lower bound with the lowest plausible rate; the binding check uses a fresh broker rate at send.
                estimate = _ceil_krw(_with_buffer(terms) * FX_PLAUSIBLE_KRW_PER_USD[0])
            if estimate > remaining or estimate > cap["max_order_krw"]:
                raise ValidationError(f"예약 매수금액(비용 버퍼 포함) {estimate:,}원이 남은 누적 한도 {remaining:,}원 "
                                      f"또는 주문당 한도 {cap['max_order_krw']:,}원을 넘습니다.")
        else:
            entitlement = _entitlement(conn, market, terms.symbol)
            if terms.quantity > entitlement:
                raise ValidationError(f"매도 수량 {terms.quantity}주가 이 프로그램의 확인된 시범 보유 {entitlement}주를 넘습니다.")
        created = datetime.now(timezone.utc)
        created_at = created.isoformat(timespec="microseconds")
        expires_at = (created + timedelta(seconds=TICKET_TTL_SECONDS)).isoformat(timespec="microseconds")
        ticket_id = "LT-" + created.strftime("%Y%m%d") + "-" + secrets.token_hex(6).upper()
        terms_hash = _terms_hash(ticket_id, created_at, expires_at, terms, request)
        conn.execute(
            "INSERT INTO tickets(ticket_id, market, side, symbol, exchange, quantity, limit_price, request_json, "
            "terms_hash, created_at, expires_at, cost_buffer_bps, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'PREPARED')",
            (ticket_id, market, side, terms.symbol, terms.exchange, terms.quantity, terms.limit_price,
             canonical(request), terms_hash, created_at, expires_at, COST_BUFFER_BPS[market]))
        if intent is not None:
            conn.execute("INSERT INTO auto_intents(intent_key, cycle_key, ticket_id, arm_id, risk_json, created_at) "
                         "VALUES (?,?,?,?,?,?)", (intent["intent_key"], intent["cycle_key"], ticket_id,
                                                  intent["arm_id"], intent["risk_json"], now()))
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        raise ValidationError("DB 안전 조건(중지·중복·자동 실행 권한)에 걸려 티켓을 만들지 않았습니다.") from None
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    currency = "KRW" if market == "KR" else "USD"
    result = {"ticket_id": ticket_id, "state": "PREPARED", "order_sent": False,
              "terms": terms.payload(), "order_type": "LIMIT (보통/지정가)", "currency": currency,
              "gross_limit_value": f"{terms.gross():f} {currency}",
              "terms_hash": terms_hash, "created_at": created_at, "expires_at": expires_at,
              "next_step": f"python -m stocklab live send --ticket {ticket_id}  (5분 이내, 확인 문구 직접 입력)"}
    if side == "BUY":
        result.update({"cost_buffer_bps": COST_BUFFER_BPS[market],
                       "reservation_krw": estimate if market == "KR" else None,
                       "reservation_krw_lower_bound": estimate if market == "US" else None,
                       "remaining_cap_krw_before": remaining,
                       "note": "주문을 전송하지 않았습니다. 전송 시 실제 주문가능현금·(US)증권사 환율로 현금비율 한도를 다시 계산합니다."})
    else:
        result.update({"pilot_entitlement_qty_before": entitlement,
                       "note": "주문을 전송하지 않았습니다. 전송 직전에 증권사 매매가능수량과 원장 수량을 다시 확인합니다."})
    return result


def _read_broker(market):
    """Fresh, complete broker cash reads through the read-only client. Returns only derived inputs."""
    from .kiwoom_bridge import KiwoomReadOnly
    from .real_dashboard import _pages, _summary_field, _usd_row, _amount
    client = KiwoomReadOnly("real")
    try:
        if market == "KR":
            cash, holdings = client.cash("KR"), client.balance("KR")
            deposit = None
        else:
            cash, holdings, deposit = client.cash("US"), client.balance("US"), client.us_deposit_detail()
    finally:
        client.close()
    if _pages(holdings) is None:
        raise ValidationError("보유 잔고 조회가 불완전합니다. 전송하지 않습니다.")
    if market == "KR":
        values = [_summary_field(cash, "ord_alow_amt"), _summary_field(cash, "100stk_ord_alow_amt")]
        return {"cash": None if None in values else min(values), "fx": None}
    row = _usd_row(cash)
    values = [_amount(row.get("fc_entra")), _amount(row.get("fc_ord_alowa"))] if row else [None]
    return {"cash": None if None in values else min(values), "fx": _fx_rate(deposit)}


def _read_holding(market, symbol):
    """Fresh tradeable/held quantity for one symbol: KR kt00018 (합산, KRX), US ust21070 sell_alowq/poss_qty."""
    from .kiwoom_bridge import KiwoomReadOnly
    client = KiwoomReadOnly("real")
    try:
        result = client.balance(market)
    finally:
        client.close()
    try:
        return rec.kr_holding(result, symbol) if market == "KR" else rec.us_holding(result, symbol)
    except rec.InquiryError as exc:
        raise ValidationError(f"보유 수량을 확정하지 못했습니다 ({exc}). 전송하지 않습니다.") from None


def _fx_rate(result):
    """usd_exch_rate from ust21160 (formatted like '1,507.70'); every page must agree; plausible range only."""
    from .real_dashboard import _pages
    pages = _pages(result)
    if pages is None:
        return None
    values = set()
    for page in pages:
        if "usd_exch_rate" not in page:
            continue
        raw = page["usd_exch_rate"]
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,3}(,[0-9]{3})*(\.[0-9]+)?|[0-9]+(\.[0-9]+)?",
                                                        raw.strip()):
            return None
        values.add(Decimal(raw.strip().replace(",", "")))
    if len(values) != 1:
        return None
    rate = values.pop()
    low, high = FX_PLAUSIBLE_KRW_PER_USD
    return rate if low <= rate <= high else None


def _cross_checked_us_fx(symbol, exchange, account_rate):
    """Require the broker's account and symbol-quote FX rates to agree before a US BUY."""
    from .kiwoom_bridge import KiwoomReadOnly
    from .live_evidence import parse_us_status
    if account_rate is None:
        raise ValidationError("미국 주문의 증권사 계좌 환율을 확인하지 못했습니다. 전송하지 않습니다.")
    client = KiwoomReadOnly("real")
    try:
        quote_rate = parse_us_status(client.quote("US", symbol, exchange), symbol, exchange)["fx_quote"]
    finally:
        client.close()
    low, high = FX_PLAUSIBLE_KRW_PER_USD
    if not low <= quote_rate <= high or abs(quote_rate - account_rate) * 10000 > account_rate * FX_CROSS_CHECK_BPS:
        raise ValidationError("미국 계좌 환율과 시세 환율이 일치하지 않습니다. 전송하지 않습니다.")
    return max(account_rate, quote_rate)


def _typed(phrase, banner):
    if not sys.stdin or not sys.stdin.isatty():
        raise ValidationError("확인 문구는 대화형 터미널에서 입력해야 합니다 (파이프·리디렉션 거부).")
    print("=" * 72)
    for line in banner:
        print(" " + line)
    print("=" * 72)
    print("계속하려면 아래 문구를 정확히 입력하세요 (그 외 입력은 취소):")
    print(f"  {phrase}")
    try:
        typed = input("> ")
    except (EOFError, KeyboardInterrupt):
        raise ValidationError("확인이 취소되었습니다. 아무것도 변경·전송하지 않았습니다.") from None
    if typed != phrase:
        raise ValidationError("확인 문구가 일치하지 않습니다. 아무것도 변경·전송하지 않았습니다.")


def _confirm(row):
    currency = "KRW" if row["market"] == "KR" else "USD"
    _typed(confirmation_phrase(row), [
        "실제 돈으로 키움 실전 계좌에 주문을 전송합니다 (api.kiwoom.com). 취소·정정 기능은 없습니다.",
        f"티켓 {row['ticket_id']}  만료 {row['expires_at'][:19]} UTC",
        f"{row['market']} {row['side']} {row['symbol']} ({row['exchange']})  수량 {row['quantity']}주  "
        f"지정가 {row['limit_price']} {currency}",
        f"terms_hash {row['terms_hash']}",
        "접수(ACCEPTED)는 체결이 아닙니다. 결과가 불명확하면 재전송하지 않고 UNKNOWN으로 남깁니다."])


class SubmitRefused(ValidationError):
    """verify_attempt_for_submit refused: nothing was transmitted."""


def verify_attempt_for_submit(conn, ticket_id, terms):
    """Last check inside KiwoomRealOrderClient.submit, immediately before transmission."""
    try:
        main = [r for r in conn.execute("PRAGMA database_list").fetchall() if r[1] == "main"]
        if len(main) != 1 or not main[0][2] or Path(main[0][2]).resolve() != _ledger_path():
            raise SubmitRefused("실전 주문 원장 DB가 아닙니다.")
        row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if row is None or row["state"] != "ATTEMPTED" or row["outcome"] is not None:
            raise SubmitRefused("ATTEMPTED 상태의 원장 티켓이 아닙니다.")
        if not isinstance(terms, OrderTerms) or _verify_integrity(row).payload() != terms.payload():
            raise SubmitRefused("전송 조건이 원장 티켓과 다릅니다.")
        attempt_age = _age_seconds(row["attempted_at"])
        if not 0 <= attempt_age <= READ_MAX_AGE_SECONDS:
            raise SubmitRefused("ATTEMPTED 기록이 오래되었습니다.")
        evidence = conn.execute("SELECT * FROM attempt_evidence WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if evidence is None or evidence["side"] != row["side"]:
            raise SubmitRefused("안전 조건 근거가 기록되지 않았습니다.")
        read_age = _age_seconds(evidence["broker_read_at"])
        if not 0 <= read_age <= READ_MAX_AGE_SECONDS:
            raise SubmitRefused("증권사 잔고·현금 확인이 오래되었습니다.")
        if conn.execute("SELECT 1 FROM submission_claims WHERE ticket_id = ?", (ticket_id,)).fetchone():
            raise SubmitRefused("이 티켓의 전송 기회는 이미 사용되었습니다.")
        if _resolution(conn, ticket_id) or _halts(conn, row["market"]) or _in_flight(conn, row["market"], ticket_id):
            raise SubmitRefused("종결·중지·다른 미종결 주문이 있습니다.")
        if row["side"] == "BUY":
            cap = _latest_cap(conn, row["market"])
            if cap is None or cap["cap_id"] != evidence["cap_id"] or row["reserved_krw"] is None:
                raise SubmitRefused("매수 한도가 변경되었거나 예약액이 없습니다.")
            if row["reserved_krw"] > min(cap["max_order_krw"], evidence["cash_limit_krw"]):
                raise SubmitRefused("매수 주문 금액이 한도를 넘습니다.")
            if _committed(conn, row["market"]) > cap["max_committed_krw"]:
                raise SubmitRefused("누적 매수 한도가 초과되었습니다.")
        if row["side"] == "SELL":
            ownership = conn.execute("SELECT broker_held_qty FROM sell_ownership_checks WHERE ticket_id = ?",
                                     (ticket_id,)).fetchone()
            if ownership is None or ownership["broker_held_qty"] != evidence["ledger_entitlement_qty"]:
                raise SubmitRefused("기존 보유분과 시범 매수분을 구분할 수 없습니다.")
            if row["quantity"] > min(_entitlement(conn, row["market"], row["symbol"], ticket_id),
                                     evidence["broker_tradeable_qty"]):
                raise SubmitRefused("시범 보유 수량이 매도 수량보다 적습니다.")
        intent = conn.execute("SELECT arm_id FROM auto_intents WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if intent is not None and (_uncertain_human_closes(conn, row["market"])
                                   or (row["side"] == "BUY" and _uncertain_human_closes(
                                       conn, "US" if row["market"] == "KR" else "KR"))):
            raise SubmitRefused("사람이 종결한 주문의 미확인 잔량이 있어 자동 전송을 거부합니다.")
        if intent is not None and not conn.execute(
                f"SELECT {_arm_valid_sql('?', '?')}", (intent["arm_id"], now())).fetchone()[0]:
            raise SubmitRefused("자동 실행 권한(arming)이 더 이상 유효하지 않습니다.")
        if intent is not None and not _auto_session_open(conn, row["market"]):
            raise SubmitRefused("자동 주문 전송 시점이 정규장 허용 창 밖입니다.")
    except SubmitRefused:
        raise
    except Exception:
        raise SubmitRefused("원장 확인에 실패했습니다.") from None


def claim_attempt_for_submit(conn, ticket_id, terms):
    """Durably spend this ticket's one transmission opportunity before touching the network.

    If the process crashes after this commit, the order outcome is unknown and must be
    reconciled. A second direct submit call cannot send the same ATTEMPTED ticket again.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        verify_attempt_for_submit(conn, ticket_id, terms)
        conn.execute("INSERT INTO submission_claims(ticket_id, claimed_at) VALUES (?, ?)", (ticket_id, now()))
        conn.execute("COMMIT")
    except BaseException as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if isinstance(exc, SubmitRefused):
            raise
        raise SubmitRefused("원장에 단일 전송 기록을 남기지 못했습니다.") from None


def send(conn, ticket_id):
    """Human path: typed confirmation in an interactive terminal authorizes the single transmission."""
    return _send(conn, ticket_id, _confirm)


def _auto_authorize(conn):
    def authorize(row):
        intent = conn.execute("SELECT arm_id FROM auto_intents WHERE ticket_id = ?", (row["ticket_id"],)).fetchone()
        if intent is None:
            raise ValidationError("자동 실행 주문 의도(intent)가 없는 티켓은 자동 전송하지 않습니다.")
        if not conn.execute(f"SELECT {_arm_valid_sql('?', '?')}", (intent["arm_id"], now())).fetchone()[0]:
            raise ValidationError("자동 실행 권한(arming)이 유효하지 않습니다(해제·만료·설정/한도 변경·전체 중지).")
        if not _auto_session_open(conn, row["market"]):
            raise ValidationError("자동 주문 전송 시점이 정규장 허용 창 밖입니다.")
    return authorize


def send_auto(conn, ticket_id):
    """Autonomous path: the ticket's order intent and a still-valid arming authorize the transmission.

    Same broker re-reads, gates, ATTEMPTED record, one-use submission claim and outcome handling as `send`.
    The SQL gate re-checks the arming at the ATTEMPTED transition and submit re-checks it again.
    """
    return _send(conn, ticket_id, _auto_authorize(conn))


def _send(conn, ticket_id, authorize):
    from .kiwoom_order import KiwoomRealOrderClient, OrderOutcomeUnknown
    row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise ValidationError("티켓이 없습니다.")
    if row["state"] != "PREPARED":
        raise ValidationError(f"이 티켓은 이미 전송 시도되었습니다 ({_display_state(conn, row)}). 어떤 경우에도 재전송하지 않습니다.")
    if _expired(row):
        raise ValidationError("티켓이 만료되었습니다. 새로 prepare 하세요.")
    market, side = row["market"], row["side"]
    terms = _verify_integrity(row)
    blockers = _blockers(conn, market, side, ticket_id, row["symbol"])
    if blockers:
        _refuse(blockers)

    authorize(row)

    # Fresh broker state after confirmation; any failure leaves the ticket PREPARED and sends nothing.
    fx_rate = reservation = None
    if side == "BUY":
        cap = _latest_cap(conn, market)
        broker = _read_broker(market)
        read_at, read_done = now(), time.monotonic()
        need = _with_buffer(terms)
        if broker["cash"] is None or broker["cash"] < 0:
            raise ValidationError("주문가능 현금을 확인하지 못했습니다. 전송하지 않습니다.")
        if market == "KR":
            reservation = _ceil_krw(need)
            cash_krw = _floor_krw(broker["cash"])
        else:
            fx_rate = _cross_checked_us_fx(terms.symbol, terms.exchange, broker["fx"])
            reservation = _ceil_krw(need * fx_rate)
            cash_krw = _floor_krw(broker["cash"] * fx_rate)
        if broker["cash"] < need:
            raise ValidationError("실제 주문가능 현금이 비용 버퍼 포함 주문금액보다 적습니다. 전송하지 않습니다.")
        cash_limit = cash_krw * cap["cash_fraction_bps"] // 10000
        if reservation > cash_limit:
            raise ValidationError(f"예약 매수금액 {reservation:,}원이 주문가능현금의 설정 비율 한도 {cash_limit:,}원을 넘습니다. "
                                  "전송하지 않습니다.")
        evidence = {"side": "BUY", "cap_id": cap["cap_id"], "available_cash_krw": cash_krw,
                    "cash_limit_krw": cash_limit, "broker_tradeable_qty": None, "ledger_entitlement_qty": None}
    else:
        tradeable, held = _read_holding(market, row["symbol"])
        read_at, read_done = now(), time.monotonic()
        entitlement = _entitlement(conn, market, row["symbol"], ticket_id)
        if held != entitlement:
            raise ValidationError("증권사 보유 수량과 이 프로그램의 확인된 보유 수량이 다릅니다. "
                                  "기존 보유분 또는 외부 매매가 섞였을 수 있어 매도하지 않습니다.")
        if tradeable < entitlement:
            raise ValidationError(f"원장 시범 보유 {entitlement}주가 증권사 매매가능수량 {tradeable}주보다 많습니다(불일치). "
                                  "전송하지 않습니다. 키움 앱/HTS에서 원인을 확인하세요.")
        if row["quantity"] > entitlement:
            raise ValidationError("매도 수량이 확인된 시범 보유 수량을 넘습니다. 전송하지 않습니다.")
        evidence = {"side": "SELL", "cap_id": None, "available_cash_krw": None, "cash_limit_krw": None,
                    "broker_tradeable_qty": tradeable, "ledger_entitlement_qty": entitlement}

    client = KiwoomRealOrderClient()
    try:
        client.prepare_connection()
        if time.monotonic() - read_done > READ_MAX_AGE_SECONDS:
            raise ValidationError("잔고 조회 후 시간이 너무 지났습니다. 다시 send 하세요 (티켓 만료 전).")
        attempted_at = now()
        if market == "US":
            try:
                _identity_date(market, attempted_at)
            except ValidationError:
                raise ValidationError("미국 주문의 거래일을 확정하지 못했습니다. 전송하지 않습니다.") from None
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
            if current["state"] != "PREPARED" or _expired(current, attempted_at):
                raise ValidationError("티켓 상태가 바뀌었거나 만료되었습니다. 전송하지 않습니다.")
            blockers = _blockers(conn, market, side, ticket_id, row["symbol"])
            if blockers:
                _refuse(blockers)
            if side == "BUY":
                latest = _latest_cap(conn, market)
                if latest["cap_id"] != evidence["cap_id"]:
                    raise ValidationError("확인 중 한도가 변경되었습니다. 다시 send 하세요.")
                remaining = latest["max_committed_krw"] - _committed(conn, market)
                if reservation > remaining or reservation > latest["max_order_krw"]:
                    raise ValidationError(f"예약 매수금액 {reservation:,}원이 남은 누적 한도 {remaining:,}원 또는 주문당 한도를 "
                                          "넘습니다. 전송하지 않습니다.")
            elif row["quantity"] > _entitlement(conn, market, row["symbol"], ticket_id):
                raise ValidationError("시범 보유 수량이 바뀌었습니다. 전송하지 않습니다.")
            conn.execute(
                "INSERT INTO attempt_evidence(ticket_id, side, cap_id, available_cash_krw, cash_limit_krw, "
                "broker_tradeable_qty, ledger_entitlement_qty, broker_read_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (ticket_id, evidence["side"], evidence["cap_id"], evidence["available_cash_krw"],
                 evidence["cash_limit_krw"], evidence["broker_tradeable_qty"], evidence["ledger_entitlement_qty"],
                 read_at, attempted_at))
            if side == "SELL":
                conn.execute("INSERT INTO sell_ownership_checks(ticket_id, broker_held_qty, checked_at) VALUES (?,?,?)",
                             (ticket_id, held, read_at))
            marked = conn.execute(
                "UPDATE tickets SET state = 'ATTEMPTED', attempted_at = ?, reserved_krw = ?, fx_rate = ? "
                "WHERE ticket_id = ? AND state = 'PREPARED'",
                (attempted_at, reservation, None if fx_rate is None else f"{fx_rate:f}", ticket_id))
            if marked.rowcount != 1:
                raise ValidationError("티켓을 ATTEMPTED로 표시하지 못했습니다. 전송하지 않습니다.")
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise ValidationError("DB 안전 조건(한도·보유수량·중지·중복·만료)에 걸려 전송하지 않았습니다.") from None
        except BaseException:
            conn.execute("ROLLBACK")
            raise

        # ---- point of no return: the ticket is ATTEMPTED and will never be sent again ----
        try:
            # A concurrent halt may have landed just after ATTEMPTED committed.
            # This narrows the window; it cannot cancel a request already in flight.
            if _halts(conn, market):
                order_no, state, outcome = None, "UNKNOWN", "HALTED_AFTER_ATTEMPT"
            else:
                order_no = client.submit(terms, ledger=conn, ticket_id=ticket_id)
                state, outcome = "ACCEPTED", "BROKER_ACCEPTED"
        except SubmitRefused:
            order_no, state, outcome = None, "UNKNOWN", "REFUSED_BEFORE_TRANSMIT"
        except OrderOutcomeUnknown as exc:
            order_no, state, outcome = None, "UNKNOWN", exc.category
        except BaseException as exc:
            order_no, state = None, "UNKNOWN"
            outcome = "LOCAL_" + re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:60]
    finally:
        try:
            client.close()
        except Exception:
            pass  # never let cleanup prevent recording the outcome below

    recorded = True
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE tickets SET state = ?, outcome_at = ?, outcome = ?, broker_order_no = ? "
                     "WHERE ticket_id = ? AND state = 'ATTEMPTED'", (state, now(), outcome, order_no, ticket_id))
        conn.execute("COMMIT")
    except sqlite3.Error:
        recorded = False
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
    linked = False
    if recorded and state == "ACCEPTED":
        linked = _link_from_response(conn, ticket_id)
    result = {"ticket_id": ticket_id, "side": side, "state": state if recorded else "ATTEMPTED_OUTCOME_UNKNOWN",
              "outcome": outcome, "outcome_recorded": recorded, "reserved_krw": reservation,
              "fx_rate_krw_per_usd": None if fx_rate is None else f"{fx_rate:f}",
              "filled": "UNKNOWN - 접수는 체결이 아닙니다. `live reconcile --ticket` 으로 체결을 확인하세요." + (
                  "" if market == "KR" else " (US ust21180 대사는 실제 응답으로 미검증)"),
              "resend": "재전송하지 않습니다. 이 주문이 종결될 때까지 이 시장의 신규 주문은 차단됩니다."}
    if state == "ACCEPTED":
        result["broker_order_ref_masked"] = mask_order_no(order_no)
        result["order_identity_linked"] = linked
    elif outcome == "REFUSED_BEFORE_TRANSMIT":
        result["warning"] = ("전송 직전 원장 재확인에서 거부되어 주문을 보내지 않았습니다. 티켓은 UNKNOWN으로 남으며 "
                             "앱에서 주문이 없음을 확인한 뒤 `live close`로 종결하세요.")
    else:
        result["warning"] = ("주문이 증권사에 도달했을 수 있습니다. 다시 보내지 말고 키움 앱/HTS의 주문·체결 내역을 확인하세요. "
                             "주문번호가 보이면 `live reconcile --ticket ... --order-no ...`로 연결할 수 있습니다.")
    if not recorded:
        result["warning_db"] = "결과를 DB에 기록하지 못했습니다. 티켓은 ATTEMPTED로 남아 결과불명과 똑같이 취급됩니다."
    return result


def _kst(iso):
    return datetime.fromisoformat(iso).astimezone(KST)


def _identity_date(market, attempted_at):
    """Market trading date for a broker order number, independent of the inquiry timestamp basis."""
    if market == "KR":
        return _kst(attempted_at).strftime("%Y%m%d")
    from .live_calendar import utc_to_local
    return utc_to_local("US", datetime.fromisoformat(attempted_at)).strftime("%Y%m%d")


def _link_from_response(conn, ticket_id) -> bool:
    """Best effort: record (market, trading date, order no) identity. reconcile retries."""
    try:
        row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("INSERT INTO order_links(ticket_id, market, order_date, broker_order_no, source, created_at) "
                         "VALUES (?,?,?,?, 'SEND_RESPONSE', ?)",
                         (ticket_id, row["market"], _identity_date(row["market"], row["attempted_at"]),
                          rec.order_no(row["broker_order_no"]), now()))
            conn.execute("COMMIT")
            return True
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    except Exception:
        return False


def reconcile(conn, ticket_id, order_no=None):
    """Read-only order inquiry for one attempted ticket (KR kt00007, US ust21180); records identity, confirmed
    cumulative fills and full-fill closure.

    Never sends, cancels or resends. Unresolvable outcomes leave the ticket blocking its market.
    """
    from .kiwoom_bridge import KiwoomReadOnly
    row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise ValidationError("티켓이 없습니다.")
    if row["state"] not in IN_FLIGHT:
        raise ValidationError("전송 시도되지 않은 티켓은 대사할 것이 없습니다.")
    if _resolution(conn, ticket_id):
        raise ValidationError("이미 종결된 티켓입니다.")
    market = row["market"]
    terms = _verify_integrity(row)
    link = conn.execute("SELECT * FROM order_links WHERE ticket_id = ?", (ticket_id,)).fetchone()
    supplied = None
    if order_no is not None:
        if not isinstance(order_no, str) or not re.fullmatch(r"[0-9]{1,12}", order_no.strip()) \
                or not order_no.strip().lstrip("0"):
            raise ValidationError("--order-no는 키움 앱/HTS에 표시된 숫자 주문번호여야 합니다.")
        supplied = order_no.strip().lstrip("0")
    if link:
        identity, source = link["broker_order_no"], None
        if supplied and supplied != identity:
            raise ValidationError("입력한 주문번호가 이미 연결된 주문번호와 다릅니다. 변경하지 않습니다.")
    elif row["state"] == "ACCEPTED":
        identity, source = rec.order_no(row["broker_order_no"]), "SEND_RESPONSE"
        if supplied and supplied != identity:
            raise ValidationError("입력한 주문번호가 접수 응답의 주문번호와 다릅니다. 변경하지 않습니다.")
    elif supplied:
        if row["state"] == "ATTEMPTED" and _age_seconds(row["attempted_at"]) < HUMAN_CLOSE_MIN_AGE_SECONDS:
            raise ValidationError("전송이 아직 진행 중일 수 있습니다. ATTEMPTED 후 10분이 지난 뒤 다시 시도하세요.")
        identity, source = supplied, "HUMAN_ORDER_NO_VERIFIED"
    else:
        raise ValidationError("이 티켓에는 주문번호가 없습니다(결과불명). 키움 앱/HTS에서 해당 주문번호를 확인해 "
                              "--order-no로 입력하거나, 주문이 없음을 확인했다면 `live close`로 종결하세요.")

    def window(local):
        base = local.hour * 3600 + local.minute * 60 + local.second
        span = (base + ORDER_TIME_WINDOW[0], base + ORDER_TIME_WINDOW[1])
        if span[0] < 0 or span[1] > 86399:
            raise ValidationError("전송 시각이 자정 경계에 가까워 주문일자를 확정할 수 없습니다. 앱 확인 후 `live close`를 사용하세요.")
        return span

    attempted = _kst(row["attempted_at"])
    order_date = _identity_date(market, row["attempted_at"])
    kw = {"side": row["side"], "symbol": row["symbol"], "quantity": terms.quantity, "limit_price": terms.limit_price}
    client = KiwoomReadOnly("real")
    try:
        if market == "KR":
            kw["window"] = window(attempted)
            api = "kt00007"
            result = client.kr_orders(order_date, row["side"], row["symbol"])
        else:
            from .live_calendar import CalendarError, utc_to_local
            local_candidates = [attempted]
            try:
                local_candidates.append(utc_to_local("US", datetime.fromisoformat(row["attempted_at"])))
            except CalendarError:
                pass  # KST may still be an unambiguous basis for the broker timestamp
            candidates = []
            for local in local_candidates:
                try:
                    candidates.append((local.strftime("%Y%m%d"), window(local)))
                except ValidationError:
                    pass  # the other documented time basis may still be safely usable
            if not candidates:
                raise ValidationError("전송 시각의 KST/미국 동부 기준 모두 자정 경계여서 대사할 수 없습니다.")
            kw["candidates"] = candidates
            api = "ust21180"
            dates = sorted({d for d, _ in kw["candidates"]})
            result = client.us_orders(dates[0], dates[-1], row["side"], row["exchange"], row["symbol"])
    finally:
        client.close()
    read_done, observed_at = time.monotonic(), now()
    try:
        if market == "KR":
            order = rec.kr_order(result, target_no=identity, **kw)
            unique = rec.kr_unique_candidate
        else:
            order = rec.us_order(result, target_no=identity, **kw)
            unique = rec.us_unique_candidate
        if source == "HUMAN_ORDER_NO_VERIFIED" and unique(result, **kw) != identity:
            raise rec.InquiryError("SUPPLIED_NUMBER_NOT_THE_UNIQUE_MATCH")
    except rec.InquiryError as exc:
        raise ValidationError(f"증권사 주문·체결 내역으로 이 주문을 확정하지 못했습니다 ({exc}). 아무것도 기록하지 않았고 "
                              "티켓은 계속 이 시장을 차단합니다.") from None
    if time.monotonic() - read_done > READ_MAX_AGE_SECONDS:
        raise ValidationError("조회 후 시간이 너무 지났습니다. 다시 reconcile 하세요.")

    recorded_fill = closed = False
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _resolution(conn, ticket_id):
            raise ValidationError("다른 작업이 이 티켓을 먼저 종결했습니다.")
        if source and not conn.execute("SELECT 1 FROM order_links WHERE ticket_id = ?", (ticket_id,)).fetchone():
            conn.execute("INSERT INTO order_links(ticket_id, market, order_date, broker_order_no, source, created_at) "
                         "VALUES (?,?,?,?,?,?)", (ticket_id, market, order_date, identity, source, now()))
        known = _filled(conn, ticket_id)
        if order.filled_qty < known:
            raise ValidationError(f"증권사 체결수량 {order.filled_qty}주가 원장에 기록된 {known}주보다 적습니다(불일치). "
                                  "아무것도 기록하지 않습니다. 티켓은 계속 차단합니다.")
        if order.filled_qty > known:
            evidence = digest({"api": api, "order_date": getattr(order, "order_date", order_date), "order_no": identity,
                               "cntr_qty": order.filled_qty, "ord_remnq": order.pending_qty,
                               "cntr_uv": order.avg_fill_price, "observed_at": observed_at})
            table = "fills" if market == "KR" else "fills_us"
            conn.execute(f"INSERT INTO {table}(ticket_id, cum_qty, avg_price, source_api, observed_at, evidence_hash) "
                         "VALUES (?,?,?,?,?,?)",
                         (ticket_id, order.filled_qty, order.avg_fill_price, api, observed_at, evidence))
            recorded_fill = True
        if order.status == "FILLED":
            conn.execute("INSERT INTO resolutions(ticket_id, kind, filled_qty, note, created_at) VALUES (?, 'BROKER_FILLED', ?, ?, ?)",
                         (ticket_id, order.filled_qty, f"{api} cntr_qty == ord_qty, ord_remnq == 0", now()))
            closed = True
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        raise ValidationError("DB 안전 조건(주문번호 중복·체결 누적 감소 등)에 걸려 아무것도 기록하지 않았습니다.") from None
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"ticket_id": ticket_id, "market": market, "side": row["side"], "network_used": True,
            "read_only_inquiry": api, "observed_at": observed_at, "broker_order_ref_masked": mask_order_no(identity),
            "broker_status": order.status, "ordered_qty": order.ordered_qty, "filled_qty": order.filled_qty,
            "pending_qty": order.pending_qty, "avg_fill_price": order.avg_fill_price,
            "currency": "KRW" if market == "KR" else "USD",
            "us_note": None if market == "KR" else rec.US_RECONCILE_NOTE,
            "unresolved_reason": order.reason, "fill_recorded": recorded_fill, "ticket_closed": closed,
            "note": ("전량 체결 확인으로 종결했습니다." if closed else
                     "종결되지 않았습니다. 이 시장의 신규 주문은 계속 차단됩니다. 미체결 잔량이 남아 있으면 기다렸다가 다시 "
                     "reconcile 하고, 잔량이 취소·거부·만료되었음을 앱에서 확인했다면 `live close`로 종결하세요.")}


def close(conn, ticket_id):
    """Human closure of an attempted ticket after checking the Kiwoom app. Sends and resends nothing."""
    row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise ValidationError("티켓이 없습니다.")
    if row["state"] not in IN_FLIGHT:
        raise ValidationError("전송 시도되지 않은 티켓은 닫을 필요가 없습니다(PREPARED는 5분 후 만료).")
    if _resolution(conn, ticket_id):
        raise ValidationError("이미 종결된 티켓입니다.")
    if row["state"] == "ATTEMPTED" and _age_seconds(row["attempted_at"]) < HUMAN_CLOSE_MIN_AGE_SECONDS:
        raise ValidationError("전송이 아직 진행 중일 수 있습니다. ATTEMPTED 후 10분이 지난 뒤 다시 시도하세요.")
    filled = _filled(conn, ticket_id)
    consequence = (f"BUY: 확인된 체결 {filled}주만 매도 가능 수량이 되고, 예약금 {row['reserved_krw'] or 0:,}원은 계속 누적 한도에 포함됩니다."
                   if row["side"] == "BUY" else
                   f"SELL: 주문 수량 {row['quantity']}주 전체를 매도된 것으로 간주해 시범 보유에서 계속 차감합니다.")
    _typed(f"CLOSE REAL {ticket_id} NO RESEND", [
        f"티켓 {ticket_id} ({row['market']} {row['side']} {row['symbol']} {row['quantity']}주, 상태 {_display_state(conn, row)})를 "
        "사람 판단으로 종결합니다.",
        "키움 앱/HTS에서 이 주문에 미체결 잔량이 없음(체결 완료·취소·거부·미접수)을 직접 확인했을 때만 진행하세요.",
        "잔량이 살아 있다면 먼저 앱에서 취소하세요. 이 명령은 주문을 전송·취소·재전송하지 않습니다.", consequence])
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _filled(conn, ticket_id) < row["quantity"] and conn.execute(
                "SELECT 1 FROM auto_intents i JOIN tickets t ON t.ticket_id = i.ticket_id "
                "WHERE t.state = 'ATTEMPTED' AND t.outcome IS NULL AND t.ticket_id != ? LIMIT 1",
                (ticket_id,)).fetchone():
            raise ValidationError("다른 자동 주문의 전송 결과가 기록될 때까지 사람 종결을 보류하세요.")
        conn.execute("INSERT INTO resolutions(ticket_id, kind, filled_qty, note, created_at) VALUES "
                     "(?, 'HUMAN_CLOSED', ?, 'typed confirmation after checking the Kiwoom app', ?)",
                     (ticket_id, _filled(conn, ticket_id), now()))
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        raise ValidationError("DB 안전 조건에 걸려 종결하지 않았습니다.") from None
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"ticket_id": ticket_id, "closed": "HUMAN_CLOSED", "confirmed_filled_qty": filled,
            "order_sent": False, "resend": False, "consequence": consequence}


def set_cap(conn, *, market, max_committed_krw, max_order_krw, cash_fraction_pct, reason):
    """Append an explicit BUY cap for one market after typed confirmation. Offline."""
    if market not in ("KR", "US"):
        raise ValidationError("한도는 KR 또는 US 시장별로 설정합니다.")
    try:
        committed, per_order = (int(str(v).strip()) for v in (max_committed_krw, max_order_krw))
        pct = Decimal(str(cash_fraction_pct).strip())
    except (ValueError, InvalidOperation):
        raise ValidationError("한도는 원 단위 정수, 현금 비율은 0~100 사이 숫자(소수 둘째 자리까지)로 입력하세요.") from None
    if committed < 0 or per_order < 0 or per_order > committed:
        raise ValidationError("누적 한도 ≥ 주문당 한도 ≥ 0 이어야 합니다.")
    if not pct.is_finite() or pct < 0 or pct > 100 or pct != pct.quantize(Decimal("0.01")):
        raise ValidationError("현금 비율은 0~100 사이, 소수 둘째 자리까지입니다.")
    bps = int(pct * 100)
    reason = _one_line(reason, "한도 변경 사유")
    previous = _latest_cap(conn, market)
    committed_now = _committed(conn, market)
    before = "없음(BUY 차단)" if previous is None else (
        f"누적 {previous['max_committed_krw']:,}원 / 주문당 {previous['max_order_krw']:,}원 / "
        f"{previous['cash_fraction_bps']} bps")
    _typed(f"SET REAL CAP {market} {committed} {per_order} {bps}", [
        f"{market} 시장 BUY 한도를 변경합니다 (실제 돈).",
        f"이전: {before}",
        f"새 값: 누적 약정 {committed:,}원 / 주문당 {per_order:,}원 / 주문가능현금의 {pct}% ({bps} bps)",
        f"현재 누적 약정(레거시 포함): {committed_now:,}원. 매도 대금으로 한도가 늘어나지 않습니다.",
        "계좌 잔고가 커도 이 한도를 넘어서 매수하지 않습니다."])
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT INTO risk_caps(market, max_committed_krw, max_order_krw, cash_fraction_bps, reason, created_at) "
                     "VALUES (?,?,?,?,?,?)", (market, committed, per_order, bps, reason, now()))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"market": market, "cap_set": True, "max_committed_krw": committed, "max_order_krw": per_order,
            "cash_fraction_bps": bps, "committed_krw_so_far": committed_now,
            "remaining_krw": max(0, committed - committed_now), "history_append_only": True}


def halt(conn, scope, reason):
    if scope not in ("KR", "US", "ALL"):
        raise ValidationError("halt 범위는 KR, US, ALL 중 하나입니다.")
    reason = _one_line(reason, "halt 사유")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT INTO halts(scope, reason, created_at) VALUES (?, ?, ?)", (scope, reason, now()))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"halted": scope, "persistent": True, "resume_available": False, "auto_liquidation": False,
            "note": "prepare/send가 차단됩니다. 보유 종목을 자동 매도하지 않으며, 이미 전송된 주문은 취소하지 않습니다 "
                    "(취소 기능 없음, 필요하면 키움 앱/HTS에서 직접 처리). 이미 ATTEMPTED인 전송 흐름은 중단을 보장할 수 없습니다. "
                    "reconcile/close는 halt 중에도 사용할 수 있습니다."}


def _inventory(conn, market):
    symbols = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM tickets WHERE market = ? AND state != 'PREPARED' ORDER BY symbol", (market,))]
    return {s: _entitlement(conn, market, s) for s in symbols}


def status(conn, ticket_id=None):
    """Offline view of the live-order DB. No network. Broker order numbers are masked."""
    def view(row):
        link = conn.execute("SELECT * FROM order_links WHERE ticket_id = ?", (row["ticket_id"],)).fetchone()
        resolved = _resolution(conn, row["ticket_id"])
        order_no = link["broker_order_no"] if link else row["broker_order_no"]
        return {"ticket_id": row["ticket_id"], "state": _display_state(conn, row), "market": row["market"],
                "side": row["side"], "symbol": row["symbol"], "exchange": row["exchange"],
                "quantity": row["quantity"], "limit_price": row["limit_price"],
                "created_at": row["created_at"], "expires_at": row["expires_at"],
                "attempted_at": row["attempted_at"], "reserved_krw": row["reserved_krw"],
                "fx_rate_krw_per_usd": row["fx_rate"], "outcome": row["outcome"],
                "broker_order_ref_masked": mask_order_no(order_no) if order_no else None,
                "order_identity": link["source"] if link else None,
                "confirmed_filled_qty": _filled(conn, row["ticket_id"]),
                "resolution": resolved["kind"] if resolved else None,
                "terms_hash": row["terms_hash"]}
    if ticket_id:
        row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise ValidationError("티켓이 없습니다.")
        return view(row)
    meta = dict(conn.execute("SELECT key, value FROM live_meta").fetchall())
    markets = {}
    for market in ("KR", "US"):
        committed = _committed(conn, market)
        cap = _latest_cap(conn, market)
        buy_blockers = _blockers(conn, market, "BUY")
        markets[market] = {
            "cap": None if cap is None else {"cap_id": cap["cap_id"], "max_committed_krw": cap["max_committed_krw"],
                                             "max_order_krw": cap["max_order_krw"],
                                             "cash_fraction_bps": cap["cash_fraction_bps"], "set_at": cap["created_at"]},
            "cap_history": [dict(r) for r in conn.execute(
                "SELECT cap_id, max_committed_krw, max_order_krw, cash_fraction_bps, reason, created_at FROM risk_caps "
                "WHERE market = ? ORDER BY cap_id", (market,))],
            "committed_buy_krw": committed,
            "remaining_cap_krw": None if cap is None else max(0, cap["max_committed_krw"] - committed),
            "new_buy_allowed": not buy_blockers, "buy_blockers": buy_blockers,
            "halts": [dict(h) for h in _halts(conn, market)],
            "unresolved_tickets": _in_flight(conn, market),
            "pilot_inventory_qty": _inventory(conn, market),
            "sell_supported": True,
            "fill_reconciliation": "kt00007" if market == "KR" else "ust21180 (실제 응답으로 미검증)",
            "loss_triggers": "`auto` 실행에서만 검사 (수동 `live` 티켓은 검사 없음)"}
    tickets = [view(r) for r in conn.execute("SELECT * FROM tickets ORDER BY created_at DESC LIMIT 50")]
    return {"real_money": True, "network_used": False, "schema": {"v2_since": meta.get("schema_v2_at")},
            "legacy_fixed_budget_krw_per_market": {"value": meta.get("budget_krw_per_market"), "enforced": False,
                                                   "note": "사용자가 철회함. 기록으로만 남아 있습니다."},
            "markets": markets, "tickets": tickets, "limitations": LIMITATIONS}
