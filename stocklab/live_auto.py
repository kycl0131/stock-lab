"""Autonomous REAL-account runner: explicit config, typed arming, idempotent cycles, one order per market/cycle.

Capability is not activation. Nothing here sends an order unless ALL of these hold at that instant:
  1. a validated config exists (`auto config`, typed confirmation; append-only), with an explicit total KRW capital
     cap, per-order/per-position caps, daily and total loss triggers, costs, universe, calendars and proposer;
  2. the newest arming event is an ARM (`auto arm`, typed confirmation) that is unexpired and bound to the newest
     config and the newest `live cap` row of each market, and there is no ALL halt (checked in Python and SQL);
  3. the market's own ledger gates pass (halt, unresolved orders, caps, broker cash/holdings, SELL entitlement).
Any newer config or cap, `auto disarm`, a loss trigger, an UNKNOWN order outcome or a terminal error ends the
authority; the running process never re-arms itself and never widens a limit. Dry-run cycles (`--dry-run`) record
decisions and would-be orders only.

Cycle (per market, inside the regular-session window of the configured official calendar):
  claim cycle key (market, session date, slot, mode)  -> a key runs at most once, even across restarts
  reconcile every unresolved ticket of the market      -> still unresolved: abstain (no model call)
  evidence (live_evidence, current session bars only)  -> any gap: abstain
  P&L/exposure marks, loss triggers                    -> trigger: disarm, abstain
  MODEL only: time-series forecast (ts_forecast)      -> needs `proposer.forecast` pins; any gap: HOLD, no call
  proposal (model or baseline; the other recorded)     -> immutable decision record, with the model snapshot and
                                                          the research-only candidate (live_research; never planned)
  risk plan (live_risk) -> fresh re-quote -> ticket + intent in one transaction -> send_auto (single transmission)
Rate limits: >= 120 s between cycles, <= 1 model call per cycle and <= max_calls_per_day per rolling 24 h, <= 1 order
per market per cycle and <= max_orders_per_day per market session.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

from .domain import ValidationError, canonical, digest, now
from . import live_ai, live_calendar as cal, live_evidence as ev, live_orders as lo, live_research, live_risk as risk
from . import news, ts_forecast as ts

CONFIG_SCHEMA = "stocklab-auto-config-v1"
MAX_UNIVERSE = 8
MIN_INTERVAL_S, MAX_INTERVAL_S = 120, 3600
EXCHANGES = {"KR": ("KRX",), "US": ("ND", "NY", "NA")}
COST_KEYS = {"KR": ("buy_fee_bps", "sell_fee_bps", "sell_tax_bps", "slippage_bps"),
             "US": ("buy_fee_bps", "sell_fee_bps", "sell_tax_bps", "slippage_bps", "fx_cost_bps")}
MIN_BUFFERS = {"KR": (5, 15), "US": (5, 10)}   # minutes after open / before close (KRX closing auction 15:20)

UNVERIFIED = [
    "실제 응답으로 검증되지 않은 필드: ka10080/usa06011 분봉 순서·시각, ka10004 bid_req_base_tm, usa20101 dt/bid_tm, "
    "ka10100 state/auditInfo 문구, usa20100 trd_susp_tp, ust21180 목록 키·slby_tp_nm·rsrv_tp 문구·시간대.",
    "US 시각대는 신선도로만 판별합니다(KST/미국 동부 중 정확히 하나만 최근이어야 함). 로컬 PC 시계가 틀리면 모두 관망합니다.",
    "캘린더는 사용자가 공식 공지에서 옮겨 적은 값이며, 임시 휴장·조기 폐장 변경은 설정을 다시 저장해야 반영됩니다.",
    "거래정지·VI 등 장 상태 API가 없어, 신선한 체결·호가와 종목 상태 필드로만 간접 확인합니다.",
    "비용·세금·환전 bps는 사용자가 입력한 추정치이며 실제 체결 비용이 아닙니다. 손실 기준은 트리거일 뿐 손실 상한이 아닙니다.",
    "전략(모델 제안·기준 규칙)의 수익성은 입증되지 않았습니다.",
    "사람이 미확인 잔량 주문을 종결하면 원장과 실물 잔고가 다를 수 있어 자동 실행을 중단합니다. 자동 복구 절차는 없습니다.",
]
STEPS_BEFORE_LIVE = [
    "1) `live cap`으로 사용할 시장의 누적·주문당 한도와 현금 비율을 명시 (확인 문구).",
    "2) `auto template`로 틀을 받아 종목·총 자본 상한·주문/종목 한도·일/누적 손실 기준·비용·공식 캘린더·제안자를 직접 채움.",
    "3) `auto config --file ...` 저장 (확인 문구). `auto run-once --dry-run`/`auto watch --dry-run`으로 판단 기록만 관찰.",
    "4) 실제 응답으로 위 미검증 필드를 확인(`broker quote` 등, 사람이 직접), 소액 수동 `live` 주문·`live reconcile`로 KR/US 대사 확인.",
    "5) `auto arm --hours N` (확인 문구) 후 `auto watch`. 중지는 `auto disarm` 또는 `live halt`.",
]


class _Abstain(Exception):
    def __init__(self, reason, **detail):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def _utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- config

def _int(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValidationError(f"{name}: {low}~{high} 사이 정수여야 합니다 (기본값 없음).")
    return value


def _bps(value, name, high=1000):
    try:
        number = Decimal(value) if isinstance(value, str) else None
    except InvalidOperation:
        number = None
    if number is None or not number.is_finite() or not 0 <= number <= high:
        raise ValidationError(f"{name}: 0~{high} 사이 숫자 문자열(bps)이어야 합니다.")
    return value


def _keys(value, keys, name):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValidationError(f"{name}: 필드가 정확히 {sorted(keys)} 이어야 합니다.")
    return value


def validate_config(cfg) -> dict:
    """Strict validation. No field has a default; unknown or missing fields are errors."""
    _keys(cfg, ("schema", "capital_cap_krw", "max_total_loss_krw", "arm_max_hours", "cycle", "proposer",
                "baseline", "markets"), "config")
    if cfg["schema"] != CONFIG_SCHEMA:
        raise ValidationError(f"schema는 {CONFIG_SCHEMA} 이어야 합니다.")
    capital = _int(cfg["capital_cap_krw"], "capital_cap_krw", 1000, 100_000_000)
    total_loss = _int(cfg["max_total_loss_krw"], "max_total_loss_krw", 1, capital)
    _int(cfg["arm_max_hours"], "arm_max_hours", 1, 168)
    cycle = _keys(cfg["cycle"], ("interval_seconds", "lookback_bars", "max_bar_age_seconds", "max_quote_age_seconds",
                                 "collar_bps", "max_spread_bps"), "cycle")
    _int(cycle["interval_seconds"], "interval_seconds", MIN_INTERVAL_S, MAX_INTERVAL_S)
    # <= 12 observed symbols x 31 bars (the forecaster's window) keeps the model snapshot inside live_ai's 64 KB
    # input budget; collect() still rejects an oversized snapshot.
    _int(cycle["lookback_bars"], "lookback_bars", ev.MIN_BARS, ts.WINDOW_BARS)
    _int(cycle["max_bar_age_seconds"], "max_bar_age_seconds", 60, 900)
    _int(cycle["max_quote_age_seconds"], "max_quote_age_seconds", 10, 300)
    _int(cycle["collar_bps"], "collar_bps", 5, 300)
    _int(cycle["max_spread_bps"], "max_spread_bps", 1, 300)
    proposer = cfg["proposer"]
    if not isinstance(proposer, dict) or proposer.get("kind") not in ("MODEL", "BASELINE"):
        raise ValidationError("proposer.kind는 MODEL 또는 BASELINE 입니다.")
    if proposer["kind"] == "BASELINE":
        _keys(proposer, ("kind",), "proposer")
    else:
        # LIVE model proposals go only to the Codex CLI under a ChatGPT subscription login. Earlier `openai`
        # (API key) and `anthropic` configs are rejected; save a new config (which also invalidates arming).
        if proposer.get("provider") not in live_ai.PROVIDERS:
            raise ValidationError(f"proposer.provider는 {live_ai.PROVIDERS} 만 허용합니다(openai API·anthropic 설정 "
                                  "거부). `auto template`으로 새 설정을 저장하세요.")
        # A LIVE MODEL decision requires both pinned time-series artifacts and point-in-time news evidence. Missing
        # or stale inputs must stop the cycle before Codex is called, rather than silently changing the strategy.
        _keys(proposer, ("kind", "provider", "model", "max_calls_per_day", "timeout_seconds", "forecast", "news"),
              "proposer")
        if not isinstance(proposer["model"], str) or not re.fullmatch(live_ai.MODEL_ID_PATTERN, proposer["model"]) \
                or proposer["model"].startswith("claude"):
            raise ValidationError("proposer.model에 Codex CLI에서 쓸 정확한 모델 ID를 적으세요.")
        _int(proposer["max_calls_per_day"], "max_calls_per_day", 1, 200)
        _int(proposer["timeout_seconds"], "timeout_seconds", *live_ai.TIMEOUT_SECONDS_RANGE)
    base = _keys(cfg["baseline"], ("entry_bps", "exit_bps"), "baseline")
    _int(base["entry_bps"], "entry_bps", 1, 5000)
    _int(base["exit_bps"], "exit_bps", 1, 5000)
    markets = cfg["markets"]
    if not isinstance(markets, dict) or not markets or not set(markets) <= {"KR", "US"}:
        raise ValidationError("markets는 KR/US 중 하나 이상입니다.")
    enabled = 0
    for market, m in markets.items():
        _keys(m, ("enabled", "universe", "max_order_krw", "max_position_krw", "max_daily_loss_krw",
                  "max_orders_per_day", "costs", "open_buffer_minutes", "close_buffer_minutes", "calendar"),
              f"markets.{market}")
        if not isinstance(m["enabled"], bool):
            raise ValidationError(f"markets.{market}.enabled는 true/false 입니다.")
        enabled += m["enabled"]
        universe = m["universe"]
        if not isinstance(universe, list) or not 1 <= len(universe) <= MAX_UNIVERSE:
            raise ValidationError(f"markets.{market}.universe는 1~{MAX_UNIVERSE}개 종목입니다.")
        seen = set()
        for item in universe:
            _keys(item, ("symbol", "exchange"), "universe item")
            pattern = r"[0-9]{6}" if market == "KR" else r"[A-Z]{1,5}"
            if not isinstance(item["symbol"], str) or not re.fullmatch(pattern, item["symbol"]) \
                    or item["exchange"] not in EXCHANGES[market] or item["symbol"] in seen:
                raise ValidationError(f"markets.{market}.universe 종목/거래소가 올바르지 않거나 중복입니다.")
            seen.add(item["symbol"])
        order = _int(m["max_order_krw"], f"{market}.max_order_krw", 1000, capital)
        _int(m["max_position_krw"], f"{market}.max_position_krw", order, capital)
        _int(m["max_daily_loss_krw"], f"{market}.max_daily_loss_krw", 1, total_loss)
        _int(m["max_orders_per_day"], f"{market}.max_orders_per_day", 1, 20)
        costs = _keys(m["costs"], COST_KEYS[market], f"{market}.costs")
        for key in COST_KEYS[market]:
            _bps(costs[key], f"{market}.costs.{key}")
        _int(m["open_buffer_minutes"], f"{market}.open_buffer_minutes", MIN_BUFFERS[market][0], 120)
        _int(m["close_buffer_minutes"], f"{market}.close_buffer_minutes", MIN_BUFFERS[market][1], 120)
        cal.validate_calendar(market, m["calendar"])
    if not enabled:
        raise ValidationError("활성화된 시장이 없습니다.")
    if proposer["kind"] == "MODEL":
        ts.validate_forecast_config(proposer["forecast"],
                                    {mk: [u["symbol"] for u in m["universe"]] for mk, m in markets.items() if m["enabled"]},
                                    cycle["lookback_bars"])
        news.validate_config(proposer["news"], {mk: m["universe"] for mk, m in markets.items() if m["enabled"]})
    if len(canonical(cfg).encode("utf-8")) > 200_000:
        raise ValidationError("설정 파일이 너무 큽니다.")
    return cfg


def template() -> dict:
    """Skeleton with nulls: it fails validation until every value is chosen by the user."""
    calendar = {"schema": cal.CALENDAR_SCHEMA, "market": None, "source": None, "valid_from": None, "valid_to": None,
                "sessions": [{"date": None, "open": None, "close": None}]}
    market = {"enabled": None, "universe": [{"symbol": None, "exchange": None}], "max_order_krw": None,
              "max_position_krw": None, "max_daily_loss_krw": None, "max_orders_per_day": None,
              "costs": None, "open_buffer_minutes": None, "close_buffer_minutes": None, "calendar": calendar}
    return {"schema": CONFIG_SCHEMA, "capital_cap_krw": None, "max_total_loss_krw": None, "arm_max_hours": None,
            "cycle": {"interval_seconds": None, "lookback_bars": None, "max_bar_age_seconds": None,
                      "max_quote_age_seconds": None, "collar_bps": None, "max_spread_bps": None},
            "proposer": {"kind": "MODEL | BASELINE", "provider": "codex_cli", "model": None,
                         "max_calls_per_day": None, "timeout_seconds": None,
                         "forecast": {"artifacts": {"KR": {"<symbol>": {"path": None, "sha256": None}},
                                                    "US": {"<symbol>": {"path": None, "sha256": None}}},
                                      "max_artifact_age_days": None},
                         "news": {"archive_path": None, "max_status_age_seconds": None, "lookback_hours": None,
                                  "max_items_per_symbol": None,
                                  "required_sources": {"KR": [None], "US": [None]}}},
            "baseline": {"entry_bps": None, "exit_bps": None},
            "markets": {"KR": {**market, "costs": {k: None for k in COST_KEYS["KR"]}},
                        "US": {**market, "costs": {k: None for k in COST_KEYS["US"]}}},
            "note": "모든 null을 직접 채우고 note 필드를 지우세요. 기본값은 없습니다. calendar는 공식 공지에서 옮겨 적습니다."}


def latest_config(conn):
    row = conn.execute("SELECT * FROM auto_configs ORDER BY config_id DESC LIMIT 1").fetchone()
    if row is None:
        return None, None
    cfg = json.loads(row["config_json"])
    if digest(cfg) != row["config_hash"]:
        raise ValidationError("저장된 자동 실행 설정의 해시가 맞지 않습니다.")
    return row, validate_config(cfg)


def set_config(conn, path, reason):
    """Validate a config file and append it after typed confirmation. Invalidates any current arming."""
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        raise ValidationError("설정 파일을 읽지 못했습니다(JSON).") from None
    cfg = validate_config(cfg)
    reason = lo._one_line(reason, "설정 사유")
    config_hash = digest(cfg)
    summary = [f"총 자본 상한 {cfg['capital_cap_krw']:,}원 · 누적 손실 트리거 {cfg['max_total_loss_krw']:,}원 · "
               f"제안자 {cfg['proposer']['kind']} {cfg['proposer'].get('model') or ''}"]
    for market, m in cfg["markets"].items():
        summary.append(f"{market} {'활성' if m['enabled'] else '비활성'}: 종목 "
                       f"{', '.join(u['symbol'] for u in m['universe'])} · 주문 {m['max_order_krw']:,}원 · 종목 "
                       f"{m['max_position_krw']:,}원 · 일 손실 트리거 {m['max_daily_loss_krw']:,}원 · 일 주문 "
                       f"{m['max_orders_per_day']}건 · 캘린더 {m['calendar']['valid_from']}~{m['calendar']['valid_to']}")
    summary.append("저장하면 현재 arming은 무효가 됩니다(새 설정으로 다시 arm 필요). 주문은 전송하지 않습니다.")
    lo._typed(f"SET AUTO CONFIG {config_hash[:12]}", summary)
    conn.execute("INSERT INTO auto_configs(config_json, config_hash, reason, created_at) VALUES (?,?,?,?)",
                 (canonical(cfg), config_hash, reason, now()))
    return {"config_saved": True, "config_hash": config_hash, "armed": False, "order_sent": False}


# ---------------------------------------------------------------- arming

def arming(conn, at=None) -> dict:
    """Current authority. The SQL fragment used by the ledger gates is the final word."""
    at = at or now()
    row = conn.execute("SELECT * FROM auto_arming ORDER BY arm_id DESC LIMIT 1").fetchone()
    if row is None:
        return {"armed": False, "reason": "NEVER_ARMED"}
    if row["action"] == "DISARM":
        return {"armed": False, "reason": "DISARMED", "detail": row["reason"], "at": row["created_at"]}
    reasons = []
    if row["expires_at"] <= at:
        reasons.append("EXPIRED")
    if row["config_id"] != conn.execute("SELECT MAX(config_id) FROM auto_configs").fetchone()[0]:
        reasons.append("CONFIG_CHANGED")
    for market, column in (("KR", "kr_cap_id"), ("US", "us_cap_id")):
        if row[column] != conn.execute("SELECT MAX(cap_id) FROM risk_caps WHERE market = ?", (market,)).fetchone()[0]:
            reasons.append(f"{market}_CAP_CHANGED")
    if conn.execute("SELECT 1 FROM halts WHERE scope = 'ALL'").fetchone():
        reasons.append("HALTED_ALL")
    valid = conn.execute(f"SELECT {lo._arm_valid_sql('?', '?')}", (row["arm_id"], at)).fetchone()[0]
    if reasons or not valid:
        return {"armed": False, "reason": ",".join(reasons) or "SQL_CHECK_FAILED", "arm_id": row["arm_id"]}
    return {"armed": True, "arm_id": row["arm_id"], "config_id": row["config_id"], "expires_at": row["expires_at"]}


def arm(conn, hours, reason):
    config_row, cfg = latest_config(conn)
    if cfg is None:
        raise ValidationError("자동 실행 설정이 없습니다. `auto config`로 먼저 저장하세요.")
    hours = _int(int(hours) if str(hours).isdigit() else hours, "--hours", 1, cfg["arm_max_hours"])
    reason = lo._one_line(reason, "arm 사유")
    caps = {m: lo._latest_cap(conn, m) for m in ("KR", "US")}
    enabled = [m for m, c in cfg["markets"].items() if c["enabled"]]
    missing = [m for m in enabled if caps[m] is None]
    if missing:
        raise ValidationError(f"{', '.join(missing)} 시장의 `live cap`이 없습니다. 한도 없이 arm 하지 않습니다.")
    if conn.execute("SELECT 1 FROM halts WHERE scope = 'ALL'").fetchone():
        raise ValidationError("전체(ALL) halt 상태입니다. arm 할 수 없습니다.")
    lines = [f"실제 돈으로 무인 자동 주문을 {hours}시간 허용합니다 (설정 {config_row['config_hash'][:12]}).",
             f"총 자본 상한 {cfg['capital_cap_krw']:,}원, 누적 손실 트리거 {cfg['max_total_loss_krw']:,}원."]
    for m in enabled:
        c, cap = cfg["markets"][m], caps[m]
        halted = " (이 시장은 halt 상태라 주문하지 않음)" if lo._halts(conn, m) else ""
        lines.append(f"{m}: 주문당 ≤ min({c['max_order_krw']:,}, {cap['max_order_krw']:,})원, 누적 약정 ≤ "
                     f"{cap['max_committed_krw']:,}원, 주문가능현금의 {cap['cash_fraction_bps'] / 100}% 이내, "
                     f"일 손실 트리거 {c['max_daily_loss_krw']:,}원{halted}")
    lines += ["설정·한도가 바뀌거나 disarm·손실 트리거·결과불명 주문이 생기면 자동으로 권한이 사라지며 스스로 다시 arm 하지 않습니다.",
              "수익을 보장하지 않습니다. 전략의 수익성은 검증되지 않았습니다."]
    lo._typed(f"ARM REAL AUTO {config_row['config_hash'][:12]} {hours}H", lines)
    created = _utcnow()
    try:
        conn.execute("INSERT INTO auto_arming(action, config_id, kr_cap_id, us_cap_id, expires_at, reason, created_at) "
                     "VALUES ('ARM',?,?,?,?,?,?)",
                     (config_row["config_id"], caps["KR"]["cap_id"] if caps["KR"] else None,
                      caps["US"]["cap_id"] if caps["US"] else None,
                      (created + timedelta(hours=hours)).isoformat(timespec="microseconds"), reason,
                      created.isoformat(timespec="microseconds")))
    except sqlite3.IntegrityError:
        raise ValidationError("DB 조건(최신 설정·한도·전체 중지)에 걸려 arm 하지 않았습니다.") from None
    return {"armed": True, **arming(conn), "order_sent": False}


def disarm(conn, reason, market=None):
    """Withdraw autonomous authority immediately (no confirmation needed: it only reduces risk)."""
    reason = lo._one_line(reason, "disarm 사유")
    conn.execute("INSERT INTO auto_arming(action, reason, created_at) VALUES ('DISARM', ?, ?)", (reason, now()))
    _event(conn, market, "DISARM", reason)
    return {"armed": False, "reason": reason}


def _event(conn, market, kind, detail):
    conn.execute("INSERT INTO auto_events(market, kind, detail, created_at) VALUES (?,?,?,?)",
                 (market, kind, str(detail)[:1000], now()))


def _latch_halt(conn, market, reason):
    """Terminal error: permanent market halt (no resume) plus disarm. Never raises."""
    for action in (lambda: lo.halt(conn, market, f"auto terminal error: {reason}"[:200]),
                   lambda: disarm(conn, f"terminal error {market}: {reason}"[:200], market)):
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            action()
        except Exception:
            pass


# ---------------------------------------------------------------- lock

class InstanceLock:
    """Single autonomous process per user profile (OS file lock next to the ledger)."""

    def __init__(self):
        self.path = lo._ledger_path().parent / "auto.lock"
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise ValidationError("다른 자동 실행 프로세스가 이미 실행 중입니다(단일 실행 잠금).") from None
        return self

    def __exit__(self, *exc):
        try:
            if sys.platform == "win32":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            self.handle.close()


# ---------------------------------------------------------------- cycle

def _daily_baseline(conn, market, session_date, mode):
    """Prior session P&L, or a conservative inception baseline when none exists.

    A first mark below zero counts as an immediate loss. With no prior session, a positive
    first mark is retained as the baseline so a subsequent same-session drop is not hidden.
    """
    prior = conn.execute(
        "SELECT m.pnl_krw FROM auto_marks m JOIN auto_cycles c ON c.cycle_key = m.cycle_key "
        "WHERE m.market = ? AND c.mode = ? AND m.session_date < ? "
        "ORDER BY m.session_date DESC, m.mark_id DESC LIMIT 1", (market, mode, session_date)).fetchone()
    if prior is not None:
        return Decimal(prior["pnl_krw"]), "PRIOR_SESSION_MARK"
    first = conn.execute(
        "SELECT m.pnl_krw FROM auto_marks m JOIN auto_cycles c ON c.cycle_key = m.cycle_key "
        "WHERE m.market = ? AND c.mode = ? AND m.session_date = ? "
        "ORDER BY m.mark_id LIMIT 1", (market, mode, session_date)).fetchone()
    return max(Decimal(0), Decimal(first["pnl_krw"]) if first is not None else Decimal(0)), "INCEPTION"


def _latest_mark(conn, market, mode):
    return conn.execute(
        "SELECT m.pnl_krw, m.exposure_krw, m.session_date, m.created_at FROM auto_marks m "
        "JOIN auto_cycles c ON c.cycle_key = m.cycle_key WHERE m.market = ? AND c.mode = ? "
        "ORDER BY m.mark_id DESC LIMIT 1", (market, mode)).fetchone()


def _other_mark_usable(conn, cfg, market, book, mark, at):
    """Accept the last completed session mark for held shares; flat books need no fresh quote."""
    if not book:
        return True
    if mark is None:
        return False
    try:
        mark_at = datetime.fromisoformat(mark["created_at"])
        activity = conn.execute(
            "SELECT MAX(event_at) FROM ("
            "SELECT attempted_at AS event_at FROM tickets WHERE market = ? AND attempted_at IS NOT NULL "
            "UNION ALL SELECT f.observed_at FROM fills f JOIN tickets t ON t.ticket_id = f.ticket_id WHERE t.market = ? "
            "UNION ALL SELECT f.observed_at FROM fills_us f JOIN tickets t ON t.ticket_id = f.ticket_id WHERE t.market = ? "
            "UNION ALL SELECT r.created_at FROM resolutions r JOIN tickets t ON t.ticket_id = r.ticket_id "
            "WHERE t.market = ?)", (market, market, market, market)).fetchone()[0]
        if activity and mark_at < datetime.fromisoformat(activity):
            return False
    except (ValueError, TypeError):
        return False
    if all(s["entitlement"] == 0 and s["open_sell_remainder"] == 0 and
           s["unresolved_buy_krw"] == 0 for s in book.values()):
        return True
    other_cfg = cfg["markets"].get(market)
    if other_cfg is None:
        return False
    calendar = other_cfg.get("calendar")
    if calendar is None:
        return False
    try:
        local_day = cal.utc_to_local(market, at).date()
        if not calendar["valid_from"] <= local_day.isoformat() <= calendar["valid_to"]:
            return False
        completed = []
        for entry in calendar["sessions"]:
            day = datetime.fromisoformat(entry["date"]).date()
            close_time = datetime.strptime(entry["close"], "%H:%M").time()
            close_utc = cal.local_to_utc(market, day, close_time)
            if close_utc <= at:
                completed.append((entry, close_utc))
        if not completed:
            return False
        entry, close_utc = completed[-1]
        permitted_end = close_utc - timedelta(minutes=other_cfg["close_buffer_minutes"])
        earliest = permitted_end - timedelta(seconds=cfg["cycle"]["interval_seconds"] + 600)
        return mark["session_date"] == entry["date"] and earliest <= mark_at <= close_utc
    except (cal.CalendarError, ValueError, KeyError, TypeError):
        return False


def _model_cost_krw(conn, market, mode) -> Decimal:
    """Sum of recorded per-decision costs; earlier API-billed rows keep their stored amounts."""
    rows = conn.execute("SELECT d.model_cost_krw FROM auto_decisions d JOIN auto_cycles c ON c.cycle_key = d.cycle_key "
                        "WHERE c.market = ? AND c.mode = ?", (market, mode)).fetchall()
    return sum((Decimal(r[0]) for r in rows), Decimal(0))


def _recent_model_calls(conn, since) -> int:
    """Count model-call attempts across both modes; the quota bounds subscription usage globally.

    Older rows without a `called` flag count conservatively. A cycle skipped because the
    quota was already exhausted has `called: false` and must not extend the cooldown.
    """
    rows = conn.execute("SELECT model_meta_json FROM auto_decisions WHERE proposer = 'MODEL' AND created_at > ?",
                        (since,)).fetchall()
    return sum(json.loads(r["model_meta_json"]).get("called") is not False for r in rows)


def _reconcile_first(conn, market, mode):
    """Read-only reconciliation of every unresolved ticket in the market before any proposal."""
    notes = []
    for ticket_id in lo._in_flight(conn, market):
        row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        linked = conn.execute("SELECT 1 FROM order_links WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if row["state"] == "ACCEPTED" or linked:
            try:
                result = lo.reconcile(conn, ticket_id)
                notes.append({"ticket_id": ticket_id, "status": result["broker_status"],
                              "filled_qty": result["filled_qty"], "closed": result["ticket_closed"]})
            except ValidationError as exc:
                notes.append({"ticket_id": ticket_id, "error": str(exc)[:200]})
                _event(conn, market, "RECONCILE_FAILED", f"{ticket_id}: {str(exc)[:300]}")
        else:
            notes.append({"ticket_id": ticket_id, "error": "NO_ORDER_IDENTITY_NEEDS_HUMAN"})
            _event(conn, market, "UNKNOWN_ORDER_NEEDS_HUMAN", ticket_id)
            if mode == "LIVE" and arming(conn)["armed"]:
                disarm(conn, f"unknown order outcome {ticket_id}: human check required", market)
    return notes


def _evidence_symbols(cfg_market, book):
    symbols = {u["symbol"]: u["exchange"] for u in cfg_market["universe"]}
    for symbol, s in book.items():   # held bot inventory is always observed (SELL-only if outside the universe)
        if s["entitlement"] > 0 or s["open_sell_remainder"] > 0:
            symbols.setdefault(symbol, s["exchange"])
    if len(symbols) > 12:
        raise _Abstain("TOO_MANY_SYMBOLS_TO_OBSERVE")
    return sorted(symbols.items())


def _cycle_body(conn, *, key, market, mode, arm_id, cfg, session, clock):
    from .kiwoom_bridge import KiwoomReadOnly
    m, glob, proposer = cfg["markets"][market], {**cfg["cycle"], "capital_cap_krw": cfg["capital_cap_krw"]}, cfg["proposer"]
    out = {"reconciled": _reconcile_first(conn, market, mode)}
    if lo._in_flight(conn, market):
        raise _Abstain("UNRESOLVED_ORDER_IN_MARKET", **out)
    uncertain = lo._uncertain_human_closes(conn, market)
    if uncertain:
        if mode == "LIVE" and arming(conn)["armed"]:
            disarm(conn, f"{market} human-closed order has unverified remainder", market)
        raise _Abstain("HUMAN_CLOSED_ORDER_REQUIRES_BROKER_REVIEW", ticket_ids=uncertain, **out)
    orders_today = conn.execute(
        "SELECT COUNT(*) FROM auto_intents i JOIN auto_cycles c ON c.cycle_key = i.cycle_key "
        "WHERE c.market = ? AND c.session_date = ?", (market, session["session_date"])).fetchone()[0]
    if orders_today >= m["max_orders_per_day"]:
        raise _Abstain("DAILY_ORDER_LIMIT", **out)

    book = risk.ledger_book(conn, market)
    symbols = _evidence_symbols(m, book)
    # Read-only clients are used one at a time and closed (a newly issued token may invalidate an older one).
    client = KiwoomReadOnly("real")
    try:
        evidence = ev.collect(client, market, symbols, sellable={s: b["entitlement"] > 0 for s, b in book.items()},
                              lookback=glob["lookback_bars"], max_bar_age_s=glob["max_bar_age_seconds"],
                              max_quote_age_s=glob["max_quote_age_seconds"],
                              session=(session["open_utc"], session["close_utc"]), clock=clock)
    except ValidationError as exc:
        raise _Abstain(f"EVIDENCE:{str(exc)[:160]}", **out) from None
    finally:
        client.close()
    try:
        broker = lo._read_broker(market)
    except ValidationError as exc:
        raise _Abstain(f"BROKER_CASH:{str(exc)[:160]}", **out) from None
    facts = evidence["facts"]

    # ---- P&L, exposure, loss triggers (BUY needs all of them; SELL of bot inventory does not)
    buy_allowed, block = True, None
    fx = broker["fx"] if market == "US" else None
    valuation = None
    try:
        valuation = risk.valuation(book, {s: f["bid"] for s, f in facts.items()}, m["costs"], market=market, fx=fx,
                                   model_cost_krw=_model_cost_krw(conn, market, mode))
    except risk.RiskUnknown as exc:
        buy_allowed, block = False, f"PNL_UNKNOWN:{exc}"
    if valuation is not None:
        conn.execute("INSERT INTO auto_marks(market, session_date, cycle_key, pnl_krw, exposure_krw, detail_json, "
                     "created_at) VALUES (?,?,?,?,?,?,?)",
                     (market, session["session_date"], key, str(valuation["pnl_krw"]), str(valuation["exposure_krw"]),
                      canonical(valuation["positions"]), now()))
        baseline, baseline_kind = _daily_baseline(conn, market, session["session_date"], mode)
        daily = valuation["pnl_krw"] - baseline
        other = "US" if market == "KR" else "KR"
        other_mark = _latest_mark(conn, other, mode)
        other_book = risk.ledger_book(conn, other)
        other_active = any(s["buy_qty"] or s["open_sell_remainder"] or s["unresolved_buy_krw"]
                           for s in other_book.values())
        other_uncertain = lo._uncertain_human_closes(conn, other)
        out["pnl"] = {"market_pnl_krw": str(valuation["pnl_krw"]), "daily_pnl_krw": str(daily),
                      "daily_baseline_krw": str(baseline), "daily_baseline_kind": baseline_kind,
                      "exposure_krw": str(valuation["exposure_krw"])}
        if daily <= -m["max_daily_loss_krw"]:
            _event(conn, market, "DAILY_LOSS_TRIGGER", out["pnl"])
            if mode == "LIVE":
                disarm(conn, f"{market} daily loss trigger {daily} KRW", market)
            raise _Abstain("DAILY_LOSS_TRIGGER", **out)
        if other_uncertain or (other_active and not _other_mark_usable(conn, cfg, other, other_book, other_mark, clock())):
            buy_allowed, block = False, "OTHER_MARKET_PNL_STALE_OR_UNKNOWN"
        else:
            total = valuation["pnl_krw"] + (Decimal(other_mark["pnl_krw"]) if other_mark else 0)
            exposure_total = valuation["exposure_krw"] + (Decimal(other_mark["exposure_krw"]) if other_mark else 0)
            out["pnl"].update({"total_pnl_krw": str(total), "exposure_total_krw": str(exposure_total)})
            if total <= -cfg["max_total_loss_krw"]:
                _event(conn, market, "TOTAL_LOSS_TRIGGER", out["pnl"])
                if mode == "LIVE":
                    disarm(conn, f"total loss trigger {total} KRW", market)
                raise _Abstain("TOTAL_LOSS_TRIGGER", **out)

    # ---- proposal (immutable record before any order)
    snapshot = evidence["model_snapshot"]
    base = live_ai.validate_proposal(live_ai.baseline(snapshot, entry_bps=cfg["baseline"]["entry_bps"],
                                                      exit_bps=cfg["baseline"]["exit_bps"]), snapshot)
    if proposer["kind"] == "BASELINE":
        proposal, meta, kind = base, {"proposer": live_ai.BASELINE_VERSION}, "BASELINE"
    else:
        kind = "MODEL"
        since = (clock() - timedelta(hours=24)).isoformat()
        calls = _recent_model_calls(conn, since)
        if calls >= proposer["max_calls_per_day"]:
            proposal, meta = {**live_ai.HOLD, "reason": "daily model call limit reached; abstain"}, \
                {"error": "DAILY_MODEL_CALL_LIMIT", "called": False}
        else:
            # Time-series forecast first, from the same validated market-data snapshot and quotes. Any problem
            # (no pinned artifact, hash mismatch, stale artifact, too few or gapped bars) is a HOLD with no call.
            try:
                forecast, forecast_audit = ts.live_forecast(
                    snapshot, proposer.get("forecast"), quotes={s: {"bid": f["bid"], "ask": f["ask"]}
                                                                for s, f in facts.items()},
                    costs=m["costs"], max_bar_age_s=glob["max_bar_age_seconds"], session_date=session["session_date"])
            except ValidationError as exc:
                forecast, forecast_error = None, f"FORECAST:{str(exc)[:160]}"
            except Exception as exc:  # defensive: a forecaster bug must not become a model call or an order
                forecast, forecast_error = None, "FORECAST:UNEXPECTED_" + re.sub(r"[^A-Za-z0-9_]", "",
                                                                              type(exc).__name__)[:60]
            # Point-in-time news from the separately collected archive (no network here). This input is required;
            # a missing, failed or stale feed is a HOLD with no call; a fresh feed with no matching items is empty.
            news_obj, news_audit, news_error = None, None, None
            if forecast is not None:
                try:
                    news_obj, news_audit = news.live_evidence(proposer["news"], snapshot, now=clock())
                except ValidationError as exc:
                    news_error = f"NEWS:{str(exc)[:160]}"
                except Exception as exc:  # defensive: a news bug must not become a model call or an order
                    news_error = "NEWS:UNEXPECTED_" + re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:60]
            if forecast is None:
                proposal, meta = {**live_ai.HOLD, "reason": "time-series forecast unavailable; abstain"}, \
                    {"error": forecast_error, "called": False}
            elif news_error is not None:
                proposal, meta = {**live_ai.HOLD, "reason": "news feed missing, failed or stale; abstain"}, \
                    {"error": news_error, "called": False, "forecast": forecast, "forecast_artifacts": forecast_audit}
            else:
                proposal, meta = live_ai.decide(snapshot, provider=proposer["provider"], model=proposer["model"],
                                                timeout=proposer["timeout_seconds"], forecast=forecast,
                                                news=news_obj)
                meta.update({"called": True, "forecast": forecast, "forecast_artifacts": forecast_audit})
                if news_obj is not None:
                    meta.update({"news": news_obj, "news_audit": news_audit})
    # Subscription calls are recorded as 0 KRW: no per-call bill, but not free (subscription limits apply).
    cost = Decimal(0)
    # Research-only candidate on the same snapshot and quotes: recorded in the immutable decision row, never
    # passed to risk.plan, never a ticket. Stored under model_meta_json so no schema change is needed.
    research = live_research.evaluate_safely(snapshot, {s: {"bid": f["bid"], "ask": f["ask"]}
                                                        for s, f in facts.items()}, m["costs"])
    conn.execute("INSERT INTO auto_decisions(cycle_key, evidence_version, evidence_hash, evidence_json, proposer, "
                 "proposal_json, model_meta_json, baseline_json, model_cost_krw, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (key, ev.EVIDENCE_VERSION, evidence["stored"]["hash"], canonical(evidence["stored"]), kind,
                  canonical(proposal), canonical({**meta, "research_candidate": research}), canonical(base),
                  str(cost), now()))
    out.update({"evidence_hash": evidence["stored"]["hash"], "proposal": proposal, "baseline": base,
                "model_error": meta.get("error"),
                "research_candidate": {"version": research["version"], "proposal": research["proposal"],
                                       "input_hash": research["input_hash"], "error": research["error"]}})

    # ---- deterministic plan
    cap = lo._latest_cap(conn, market)
    universe = {u["symbol"] for u in m["universe"]}
    exposure_total = Decimal(out.get("pnl", {}).get("exposure_total_krw", "0"))
    kwargs = dict(market=market, proposal=proposal, universe=universe, book=book, cfg=m, glob=glob,
                  cash_ccy=broker["cash"], fx=fx, cap=cap, committed_krw=lo._committed(conn, market),
                  exposure_total_krw=exposure_total, buy_allowed=buy_allowed, buy_block_reason=block)
    planned = risk.plan(fact=facts.get(proposal["symbol"]), **kwargs)
    out["plan"] = {"reason": planned["reason"], "order": planned["order"], "calc": planned["calc"]}
    if planned["order"] is None:
        return "COMPLETED", out
    if mode == "DRY_RUN":
        out["would_order"] = planned["order"]
        return "COMPLETED", out

    # ---- LIVE: re-quote, re-plan with the fresh quote, then ticket + intent + single transmission
    order = planned["order"]
    client = KiwoomReadOnly("real")
    try:
        fresh = ev.requote(client, market, order["symbol"], order["exchange"],
                           max_quote_age_s=glob["max_quote_age_seconds"], clock=clock)
    except ValidationError as exc:
        raise _Abstain(f"REQUOTE:{str(exc)[:160]}", **out) from None
    finally:
        client.close()
    replanned = risk.plan(fact={**facts[order["symbol"]], "bid": fresh["bid"], "ask": fresh["ask"]}, **kwargs)
    out["replan"] = {"reason": replanned["reason"], "order": replanned["order"]}
    if replanned["order"] is None or replanned["order"]["side"] != order["side"]:
        raise _Abstain(f"REPLAN:{replanned['reason']}", **out)
    order = replanned["order"]
    authority = arming(conn)
    if not authority["armed"] or authority["arm_id"] != arm_id:
        raise _Abstain(f"NOT_ARMED:{authority['reason'] if not authority['armed'] else 'ARMING_REPLACED'}", **out)
    intent = {"intent_key": f"{key}:{order['side']}:{order['symbol']}", "cycle_key": key, "arm_id": arm_id,
              "risk_json": canonical(replanned["calc"])}
    try:
        ticket = lo.prepare(conn, market=market, side=order["side"], symbol=order["symbol"],
                            exchange=order["exchange"], quantity=order["quantity"],
                            limit_price=order["limit_price"], intent=intent)
    except ValidationError as exc:
        raise _Abstain(f"PREPARE:{str(exc)[:160]}", **out) from None
    out["ticket_id"] = ticket["ticket_id"]
    try:
        sent = lo.send_auto(conn, ticket["ticket_id"])
    except ValidationError as exc:   # refused before ATTEMPTED: nothing was sent, the ticket expires
        raise _Abstain(f"SEND_REFUSED:{str(exc)[:160]}", **out) from None
    out["send"] = {k: sent.get(k) for k in ("state", "outcome", "outcome_recorded", "reserved_krw",
                                            "broker_order_ref_masked", "order_identity_linked")}
    if sent.get("state") != "ACCEPTED":
        _event(conn, market, "ORDER_OUTCOME_UNKNOWN", f"{ticket['ticket_id']} {sent.get('outcome')}")
        disarm(conn, f"order outcome not accepted ({ticket['ticket_id']}): human check required", market)
    return "COMPLETED", out


def run_cycle(conn, market, *, mode, clock=_utcnow):
    """One cycle for one market. Returns a JSON-safe summary; never sends in DRY_RUN."""
    config_row, cfg = latest_config(conn)
    if cfg is None:
        return {"market": market, "skipped": "NO_CONFIG"}
    m = cfg["markets"].get(market)
    if not m or not m["enabled"]:
        return {"market": market, "skipped": "MARKET_NOT_ENABLED"}
    at = clock()
    try:
        session = cal.session_state(market, m["calendar"], at, open_buffer_min=m["open_buffer_minutes"],
                                    close_buffer_min=m["close_buffer_minutes"])
    except cal.CalendarError as exc:
        return {"market": market, "skipped": f"CALENDAR_UNCERTAIN:{exc}"}
    if session["state"] != "TRADING_WINDOW":
        return {"market": market, "skipped": session["state"], "session": session}
    if lo._halts(conn, market):
        return {"market": market, "skipped": "HALTED"}
    arm_id = None
    if mode == "LIVE":
        authority = arming(conn)
        if not authority["armed"]:
            return {"market": market, "skipped": f"NOT_ARMED:{authority['reason']}"}
        arm_id = authority["arm_id"]
    start = datetime.fromisoformat(session["window_start_utc"])
    slot = int((at - start).total_seconds() // cfg["cycle"]["interval_seconds"])
    key = f"{market}:{session['session_date']}:{slot:04d}:{mode}"
    if conn.execute("SELECT 1 FROM auto_cycles WHERE cycle_key = ?", (key,)).fetchone():
        return {"market": market, "skipped": "CYCLE_ALREADY_RAN", "cycle_key": key}
    try:
        conn.execute("INSERT INTO auto_cycles(cycle_key, market, session_date, mode, config_id, arm_id, status, started_at) "
                     "VALUES (?,?,?,?,?,?, 'STARTED', ?)",
                     (key, market, session["session_date"], mode, config_row["config_id"], arm_id, now()))
    except sqlite3.IntegrityError:
        return {"market": market, "skipped": "CYCLE_REFUSED_BY_DB", "cycle_key": key}
    try:
        result, outcome = _cycle_body(conn, key=key, market=market, mode=mode, arm_id=arm_id, cfg=cfg,
                                      session=session, clock=clock)
    except _Abstain as exc:
        result, outcome = "ABSTAINED", {"reason": exc.reason, **exc.detail}
    except ValidationError as exc:   # a refused or unestablished fact (e.g. SDK/connection): no order this cycle
        result, outcome = "ABSTAINED", {"reason": f"REFUSED:{str(exc)[:200]}"}
    except Exception as exc:  # terminal: unexpected state or bug; latch
        name = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:60]
        result, outcome = "FAILED", {"reason": f"TERMINAL_{name}"}
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            _event(conn, market, "TERMINAL_ERROR", name)
        except sqlite3.Error:
            pass
        if mode == "LIVE":
            _latch_halt(conn, market, name)
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.execute("UPDATE auto_cycles SET status = ?, outcome_json = ?, finished_at = ? WHERE cycle_key = ?",
                     (result, canonical(_json_safe(outcome)), now(), key))
    except sqlite3.Error:
        try:
            _event(conn, market, "CYCLE_FINISH_NOT_RECORDED", key)
        except sqlite3.Error:
            pass
    return {"market": market, "cycle_key": key, "status": result, "outcome": _json_safe(outcome)}


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def run_once(conn, *, dry_run, market=None, clock=_utcnow):
    mode = "DRY_RUN" if dry_run else "LIVE"
    with InstanceLock():
        markets = [market] if market else ["KR", "US"]
        return {"mode": mode, "results": [run_cycle(conn, mk, mode=mode, clock=clock) for mk in markets]}


def watch(conn, *, dry_run, max_cycles=None, clock=_utcnow, sleep=time.sleep):
    """Bounded loop: one run per interval; LIVE stops as soon as authority is gone (never re-arms)."""
    mode = "DRY_RUN" if dry_run else "LIVE"
    history = []
    with InstanceLock():
        while True:
            _, cfg = latest_config(conn)
            if cfg is None:
                return {"mode": mode, "stopped": "NO_CONFIG", "cycles": history[-20:]}
            if mode == "LIVE":
                authority = arming(conn)
                if not authority["armed"]:
                    return {"mode": mode, "stopped": f"NOT_ARMED:{authority['reason']}", "cycles": history[-20:]}
            results = [run_cycle(conn, mk, mode=mode, clock=clock) for mk in ("KR", "US")]
            history.append({"at": clock().isoformat(timespec="seconds"), "results": results})
            print(json.dumps(history[-1], ensure_ascii=False), flush=True)
            if max_cycles is not None and len(history) >= max_cycles:
                return {"mode": mode, "stopped": "MAX_CYCLES", "cycles": history[-20:]}
            interval = cfg["cycle"]["interval_seconds"]
            sleep(max(30, interval - int(clock().timestamp()) % interval))


# ---------------------------------------------------------------- status

def _decision_summary(row) -> dict:
    """One decision row for `auto status`; readable for v1 rows without a stored snapshot or research record."""
    meta = json.loads(row["model_meta_json"])
    research = meta.get("research_candidate")
    try:
        replayable = ev.stored_model_snapshot(row["evidence_json"]) is not None
    except ValidationError:
        replayable = False
    return {"cycle_key": row["cycle_key"], "proposer": row["proposer"], "proposal": json.loads(row["proposal_json"]),
            "baseline": json.loads(row["baseline_json"]), "model_provider": meta.get("provider"),
            "model": meta.get("model"), "model_billing": meta.get("billing"), "model_usage": meta.get("usage"),
            "model_error": meta.get("error"), "forecast_gate": meta.get("forecast_gate"),
            "news_items": meta.get("news_items"), "news_hash": meta.get("news_hash"),
            "model_cost_krw": row["model_cost_krw"], "evidence_version": row["evidence_version"],
            "evidence_hash": row["evidence_hash"], "snapshot_replayable": replayable,
            "research_candidate": None if not isinstance(research, dict) else {
                "version": research.get("version"), "proposal": research.get("proposal"),
                "input_hash": research.get("input_hash"), "error": research.get("error"),
                "live_eligible": research.get("live_eligible")}}


def status(conn):
    """Offline status: authority, config summary, recent cycles/decisions/intents/events. No network."""
    config_row, cfg = latest_config(conn)
    authority = arming(conn)
    cycles = [dict(r) for r in conn.execute(
        "SELECT cycle_key, mode, status, started_at, finished_at, outcome_json FROM auto_cycles "
        "ORDER BY started_at DESC LIMIT 20")]
    for c in cycles:
        c["outcome"] = json.loads(c.pop("outcome_json")) if c.get("outcome_json") else None
    decisions = [_decision_summary(r)
                 for r in conn.execute("SELECT * FROM auto_decisions ORDER BY created_at DESC LIMIT 20")]
    intents = [dict(r) for r in conn.execute(
        "SELECT i.intent_key, i.ticket_id, i.created_at, t.state FROM auto_intents i JOIN tickets t "
        "ON t.ticket_id = i.ticket_id ORDER BY i.created_at DESC LIMIT 20")]
    events = [dict(r) for r in conn.execute("SELECT * FROM auto_events ORDER BY event_id DESC LIMIT 30")]
    marks = {mode: {m: (dict(r) if (r := _latest_mark(conn, m, mode)) else None) for m in ("KR", "US")}
             for mode in ("LIVE", "DRY_RUN")}
    return {"real_money": True, "network_used": False,
            "capability": "코드상 무인 자동 주문 경로가 있습니다(설정·arm·한도·게이트 통과 시에만).",
            "activation": {"orders_possible_now": authority["armed"], **authority},
            "config": None if cfg is None else {
                "config_id": config_row["config_id"], "config_hash": config_row["config_hash"],
                "saved_at": config_row["created_at"], "capital_cap_krw": cfg["capital_cap_krw"],
                "max_total_loss_krw": cfg["max_total_loss_krw"], "proposer": cfg["proposer"],
                "markets": {k: {"enabled": v["enabled"], "universe": v["universe"],
                                "max_order_krw": v["max_order_krw"], "max_position_krw": v["max_position_krw"],
                                "max_daily_loss_krw": v["max_daily_loss_krw"],
                                "max_orders_per_day": v["max_orders_per_day"],
                                "calendar": {"source": v["calendar"]["source"], "valid_from": v["calendar"]["valid_from"],
                                             "valid_to": v["calendar"]["valid_to"]}}
                            for k, v in cfg["markets"].items()}},
            "latest_marks": marks, "cycles": cycles, "decisions": decisions, "intents": intents, "events": events,
            "unverified": UNVERIFIED, "steps_before_live": STEPS_BEFORE_LIVE,
            "no_alpha_claim": "모델 제안과 기준 규칙 모두 수익성이 입증되지 않았습니다. 비교 기록일 뿐입니다.",
            "research_only": f"{live_research.CANDIDATE_VERSION}는 기록 전용 연구 후보입니다. 위험 엔진·주문에 쓰이지 "
                             "않고 제안자로 선택할 수 없으며, 비용 차감 수익이 검증되지 않았습니다."}
