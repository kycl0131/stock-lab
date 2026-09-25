"""Point-in-time market evidence for autonomous decisions, from official Kiwoom READ APIs only.

Sources (all via KiwoomReadOnly("real"); no paper/demo DB, no synthetic or cached price):
  KR  ka10080 1-minute bars (cntr_tm YYYYMMDDHHmmss, KST), ka10004 best bid/ask (bid_req_base_tm HHmmss, KST
      date of the read), ka10100 status (orderWarning must be "0", auditInfo "정상", no halt/administrative
      token in `state`).
  US  usa06011 1-minute bars (cntr_tm, time zone undocumented), usa20101 best bid/ask (dt + bid_tm HH:mm, time zone
      undocumented), usa20100 status (trd_susp_tp must be "0", curr_unit "USD", base_exrt kept for an FX cross-check).
      The time zone is established by freshness alone (live_calendar.resolve_stamp); every US timestamp in one
      snapshot must resolve to the same basis.
Anything missing, malformed, stale, out of order or inconsistent raises EvidenceError and the whole market
abstains for the cycle. Only numbers and timestamps reach the model: no names, news, free text or account data.
Websocket streams are not used (REST polling at bounded rates only).

Session scope (v2): `collect` takes the current regular session [open_utc, close_utc] from the configured
calendar. Bars before the open (the previous session, pre-market) are dropped, never stitched across the
opening; a bar after the close or a quote from before the open rejects the cycle, and fewer than MIN_BARS
same-session bars reject it too. The stored evidence carries the complete model snapshot (market data and the
`sellable` flag only) so a decision can be replayed; its size is capped by MAX_STORED_SNAPSHOT_BYTES.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re

from .domain import ValidationError, canonical, digest
from .kiwoom_order import krx_tick
from . import live_ai
from .live_calendar import KST, CalendarError, local_to_utc, resolve_stamp

EVIDENCE_VERSION = "stocklab-live-evidence-v2"   # v1 records have no session bounds and no stored snapshot
MIN_BARS = 5
MAX_STORED_SNAPSHOT_BYTES = 64_000                # same budget live_ai.validate_snapshot enforces
CLOCK_SKEW_S = 60
KR_BAD_STATE = ("정지", "관리", "정리", "경고", "위험")   # halted / administrative / delisting / warning tokens


class EvidenceError(ValidationError):
    pass


def _utcnow():
    return datetime.now(timezone.utc)


def _one_page(result, *, first_page=False) -> dict:
    """The single page of a quote read, or the first page of a newest-first chart read."""
    if not isinstance(result, dict) or not isinstance(result.get("pages"), list) or not result["pages"]:
        raise EvidenceError("INCOMPLETE_READ")
    if first_page:
        if result.get("first_page_only") is not True:
            raise EvidenceError("INCOMPLETE_READ")
    elif result.get("complete") is not True or len(result["pages"]) != 1:
        raise EvidenceError("INCOMPLETE_READ")
    page = result["pages"][0]
    if not isinstance(page, dict):
        raise EvidenceError("INCOMPLETE_READ")
    return page


def _page_list(page, names) -> list:
    present = [n for n in names if n in page]
    if len(present) != 1 or not isinstance(page[present[0]], list) \
            or not all(isinstance(r, dict) for r in page[present[0]]):
        raise EvidenceError("MISSING_OR_AMBIGUOUS_LIST")
    return page[present[0]]


def _text(page, field) -> str:
    value = page.get(field)
    if not isinstance(value, str):
        raise EvidenceError(f"MISSING_{field.upper()}")
    return value.strip()


def _signed_int(value, field) -> int:
    """KR prices carry a direction sign ('부호가 포함된 숫자'); the magnitude is the price."""
    if not isinstance(value, str) or not re.fullmatch(r"[+-]?[0-9]{1,12}", value.strip()):
        raise EvidenceError(f"MALFORMED_{field.upper()}")
    return abs(int(value.strip()))


def _count(value, field) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"\+?[0-9]{1,15}", value.strip()):
        raise EvidenceError(f"MALFORMED_{field.upper()}")
    return int(value.strip().lstrip("+"))


def _signed_usd(value, field) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"[+-]?[0-9]{1,9}(\.[0-9]{1,5})?", value.strip()):
        raise EvidenceError(f"MALFORMED_{field.upper()}")
    try:
        return abs(Decimal(value.strip()))
    except InvalidOperation:
        raise EvidenceError(f"MALFORMED_{field.upper()}") from None


def _stamp14(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{14}", value.strip()):
        raise EvidenceError("MALFORMED_CNTR_TM")
    return value.strip()[:8], value.strip()[8:]


def _kst_stamp(date8, time6) -> datetime:
    try:
        return datetime.strptime(date8 + time6, "%Y%m%d%H%M%S").replace(tzinfo=KST).astimezone(timezone.utc)
    except ValueError:
        raise EvidenceError("MALFORMED_TIMESTAMP") from None


def _fresh(at: datetime, now_utc: datetime, max_age_s: int, what: str):
    if at > now_utc + timedelta(seconds=CLOCK_SKEW_S):
        raise EvidenceError(f"{what}_IN_FUTURE")
    if now_utc - at > timedelta(seconds=max_age_s):
        raise EvidenceError(f"{what}_STALE")


def session_bounds(open_utc, close_utc):
    """Validated (open, close) aware UTC datetimes; accepts ISO strings as stored by live_calendar.session_state."""
    try:
        bounds = [datetime.fromisoformat(v) if isinstance(v, str) else v for v in (open_utc, close_utc)]
    except ValueError:
        raise EvidenceError("SESSION_BOUNDS_INVALID") from None
    if not all(isinstance(b, datetime) and b.utcoffset() is not None for b in bounds) or bounds[0] >= bounds[1] \
            or bounds[1] - bounds[0] > timedelta(hours=12):
        raise EvidenceError("SESSION_BOUNDS_INVALID")
    return tuple(b.astimezone(timezone.utc) for b in bounds)


def _bars(symbol, rows, convert, now_utc, *, lookback, max_age_s, strict_order, session=None):
    """rows newest first -> ascending [(id, at, close, volume)] of the newest `lookback` rows.

    With `session` (open_utc, close_utc) only bars inside [open, close] are kept. Rows are newest first and
    checked for order, so the first bar before the open ends the current session: it and every older row
    (previous session, pre-market) are dropped. A bar after the close is inconsistent and rejects the cycle.
    """
    if len(rows) < MIN_BARS:
        raise EvidenceError("TOO_FEW_BARS")
    parsed, previous = [], None
    for index, row in enumerate(rows[:lookback]):
        at, close, volume = convert(row)
        if previous is not None and (at > previous or (strict_order and at == previous)):
            raise EvidenceError("BARS_OUT_OF_ORDER")
        previous = at
        parsed.append((f"{symbol}-{at.strftime('%Y%m%d%H%M%S')}-{index}", at, close, volume))
    _fresh(parsed[0][1], now_utc, max_age_s, "LATEST_BAR")
    if session is not None:
        open_utc, close_utc = session
        if parsed[0][1] > close_utc:
            raise EvidenceError("BAR_AFTER_SESSION_CLOSE")
        parsed = [bar for bar in parsed if bar[1] >= open_utc]
        if len(parsed) < MIN_BARS:
            raise EvidenceError("TOO_FEW_SESSION_BARS")
    return list(reversed(parsed))


# ---------------------------------------------------------------- KR parsers

def parse_kr_bars(result, symbol, now_utc, *, lookback, max_age_s, session=None):
    page = _one_page(result, first_page=True)
    if "stk_cd" in page and _text(page, "stk_cd") != symbol:
        raise EvidenceError("SYMBOL_MISMATCH")
    rows = page.get("stk_min_pole_chart_qry")
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise EvidenceError("MISSING_BARS")

    def convert(row):
        date8, time6 = _stamp14(row.get("cntr_tm"))
        close = _signed_int(row.get("cur_prc"), "cur_prc")
        if close <= 0:
            raise EvidenceError("NONPOSITIVE_PRICE")
        return _kst_stamp(date8, time6), Decimal(close), _count(row.get("trde_qty"), "trde_qty")
    return _bars(symbol, rows, convert, now_utc, lookback=lookback, max_age_s=max_age_s, strict_order=True,
                 session=session)


def parse_kr_quote(result, symbol, now_utc, *, max_age_s):
    page = _one_page(result)
    raw = _text(page, "bid_req_base_tm")
    if not re.fullmatch(r"[0-2][0-9][0-5][0-9][0-5][0-9]", raw):
        raise EvidenceError("MALFORMED_BID_REQ_BASE_TM")
    at = _kst_stamp(now_utc.astimezone(KST).strftime("%Y%m%d"), raw)   # time of day only: KST date of the read
    _fresh(at, now_utc, max_age_s, "QUOTE")
    ask, bid = _signed_int(page.get("sel_fpr_bid"), "sel_fpr_bid"), _signed_int(page.get("buy_fpr_bid"), "buy_fpr_bid")
    if not 0 < bid < ask:
        raise EvidenceError("QUOTE_NOT_TWO_SIDED")
    if ask % krx_tick(ask) or bid % krx_tick(bid):
        raise EvidenceError("QUOTE_OFF_KRX_STOCK_TICK")   # ETF/ETN or other tick table: not supported
    return {"bid": Decimal(bid), "ask": Decimal(ask), "quote_at": at, "basis": "KST"}


def parse_kr_status(result, symbol):
    page = _one_page(result)
    if _text(page, "code") != symbol:
        raise EvidenceError("SYMBOL_MISMATCH")
    if _text(page, "orderWarning") != "0":
        raise EvidenceError("ORDER_WARNING")
    if _text(page, "auditInfo") != "정상":
        raise EvidenceError("AUDIT_STATUS_NOT_NORMAL")
    if any(bad in token for token in _text(page, "state").split("|") for bad in KR_BAD_STATE):
        raise EvidenceError("STOCK_STATE_RESTRICTED")
    return {}


# ---------------------------------------------------------------- US parsers

def _us_convert(basis):
    def to_utc(date8, time6):
        try:
            if basis == "KST":
                return _kst_stamp(date8, time6)
            day = datetime.strptime(date8, "%Y%m%d").date()
            return local_to_utc("US", day, datetime.strptime(time6, "%H%M%S").time())
        except (ValueError, CalendarError):
            raise EvidenceError("MALFORMED_TIMESTAMP") from None
    return to_utc


def parse_us_bars(result, symbol, now_utc, *, lookback, max_age_s, session=None):
    page = _one_page(result, first_page=True)
    rows = _page_list(page, ("result_list", "result_lsit"))
    if not rows:
        raise EvidenceError("TOO_FEW_BARS")
    try:
        _, basis = resolve_stamp(*_stamp14(rows[0].get("cntr_tm")), now_utc, max_age_s=max_age_s)
    except CalendarError as exc:
        raise EvidenceError(f"LATEST_BAR_{exc}") from None
    to_utc = _us_convert(basis)

    def convert(row):
        at = to_utc(*_stamp14(row.get("cntr_tm")))
        close = _signed_usd(row.get("cur_prc"), "cur_prc")
        if close <= 0:
            raise EvidenceError("NONPOSITIVE_PRICE")
        return at, close, _count(row.get("trde_qty"), "trde_qty")
    # The official usa06011 example repeats a cntr_tm on consecutive rows, so equal stamps are kept as is.
    # Every row is converted with the basis resolved from the newest row, then compared with the UTC session.
    return _bars(symbol, rows, convert, now_utc, lookback=lookback, max_age_s=max_age_s, strict_order=False,
                 session=session), basis


def parse_us_quote(result, symbol, exchange, now_utc, *, max_age_s):
    page = _one_page(result)
    if _text(page, "stk_cd") != symbol or ("stex_tp" in page and _text(page, "stex_tp") != exchange):
        raise EvidenceError("SYMBOL_OR_EXCHANGE_MISMATCH")
    day, hhmm = _text(page, "dt"), _text(page, "bid_tm")
    if not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", hhmm):
        raise EvidenceError("MALFORMED_BID_TM")
    try:   # minute resolution: allow one extra minute of age
        at, basis = resolve_stamp(day, hhmm.replace(":", "") + "00", now_utc, max_age_s=max_age_s + 60)
    except CalendarError as exc:
        raise EvidenceError(f"QUOTE_{exc}") from None
    ask, bid = _signed_usd(page.get("fpr_sel_bid"), "fpr_sel_bid"), _signed_usd(page.get("fpr_buy_bid"), "fpr_buy_bid")
    if not 0 < bid < ask:
        raise EvidenceError("QUOTE_NOT_TWO_SIDED")
    return {"bid": bid, "ask": ask, "quote_at": at, "basis": basis}


def parse_us_status(result, symbol, exchange):
    page = _one_page(result)
    if _text(page, "stk_cd") != symbol or _text(page, "stex_tp") != exchange:
        raise EvidenceError("SYMBOL_OR_EXCHANGE_MISMATCH")
    if _text(page, "trd_susp_tp") != "0":
        raise EvidenceError("TRADING_SUSPENDED_OR_UNKNOWN")
    if _text(page, "curr_unit") != "USD":
        raise EvidenceError("CURRENCY_NOT_USD")
    raw = _text(page, "base_exrt")
    if not re.fullmatch(r"[0-9]{3,4}(\.[0-9]{1,2})?", raw):
        raise EvidenceError("MALFORMED_BASE_EXRT")
    return {"fx_quote": Decimal(raw)}


# ---------------------------------------------------------------- collection

def _symbol_evidence(client, market, symbol, exchange, clock, *, lookback, max_bar_age_s, max_quote_age_s, session):
    if market == "KR":
        bars = parse_kr_bars(client.kr_minute_bars(symbol), symbol, clock(), lookback=lookback,
                             max_age_s=max_bar_age_s, session=session)
        quote = parse_kr_quote(client.kr_orderbook(symbol), symbol, clock(), max_age_s=max_quote_age_s)
        status = parse_kr_status(client.kr_stock_info(symbol), symbol)
        basis = "KST"
    else:
        bars, basis = parse_us_bars(client.us_minute_bars(exchange, symbol), symbol, clock(),
                                    lookback=lookback, max_age_s=max_bar_age_s, session=session)
        quote = parse_us_quote(client.us_orderbook(exchange, symbol), symbol, exchange, clock(),
                               max_age_s=max_quote_age_s)
        status = parse_us_status(client.quote("US", symbol, exchange), symbol, exchange)
        if quote["basis"] != basis:
            raise EvidenceError("TIME_BASIS_DISAGREES")
    if not session[0] <= quote["quote_at"] <= session[1] + timedelta(seconds=CLOCK_SKEW_S):
        raise EvidenceError("QUOTE_OUTSIDE_SESSION")
    return {"exchange": exchange, "bars": bars, **quote, **status, "basis": basis, "last": bars[-1][2]}


def collect(client, market, symbols, *, sellable, lookback, max_bar_age_s, max_quote_age_s, session,
            clock=_utcnow):
    """Evidence for every (symbol, exchange) in order; raises EvidenceError on the first failure.

    `sellable`: symbol -> bool (bot-owned inventory exists); passed to the model as a flag only.
    `session`: (open_utc, close_utc) of the current regular session from the configured calendar; required,
    so evidence without session bounds cannot be built. The read instant must be inside it.
    Returns the stored snapshot (JSON-safe, including the model snapshot), its hash, the model-facing snapshot
    and in-memory facts.
    """
    if not 1 <= len(symbols) <= 12:
        raise EvidenceError("UNIVERSE_SIZE")
    session = session_bounds(*session) if isinstance(session, (tuple, list)) and len(session) == 2 else None
    if session is None:
        raise EvidenceError("SESSION_BOUNDS_INVALID")
    facts, started = {}, clock()
    if not session[0] <= started <= session[1]:
        raise EvidenceError("OUTSIDE_SESSION")
    for symbol, exchange in symbols:
        try:
            facts[symbol] = _symbol_evidence(client, market, symbol, exchange, clock, lookback=lookback,
                                             max_bar_age_s=max_bar_age_s, max_quote_age_s=max_quote_age_s,
                                             session=session)
        except EvidenceError as exc:
            raise EvidenceError(f"{symbol}:{exc}") from None
    if len({f["basis"] for f in facts.values()}) != 1:
        raise EvidenceError("TIME_BASIS_DISAGREES_ACROSS_SYMBOLS")
    as_of = clock()
    model_snapshot = {
        "schema": live_ai.PROMPT_VERSION, "market": market, "as_of": as_of.isoformat(),
        "candidates": [{"symbol": s, "exchange": f["exchange"], "sellable": bool(sellable.get(s)),
                        "observations": [{"id": i, "event_at": at.isoformat(), "price": f"{close:f}",
                                          "volume": str(volume)} for i, at, close, volume in f["bars"]]}
                       for s, f in facts.items()]}
    live_ai.validate_snapshot(model_snapshot)
    if len(canonical(model_snapshot).encode("utf-8")) > MAX_STORED_SNAPSHOT_BYTES:
        raise EvidenceError("MODEL_SNAPSHOT_TOO_LARGE_TO_STORE")
    stored = {"version": EVIDENCE_VERSION, "market": market, "started_at": started.isoformat(),
              "as_of": as_of.isoformat(), "time_basis": next(iter(facts.values()))["basis"],
              "session": {"open_utc": session[0].isoformat(), "close_utc": session[1].isoformat()},
              "symbols": {s: {"exchange": f["exchange"], "bid": f"{f['bid']:f}", "ask": f"{f['ask']:f}",
                              "quote_at": f["quote_at"].isoformat(), "last": f"{f['last']:f}",
                              "first_bar_at": f["bars"][0][1].isoformat(),
                              "last_bar_at": f["bars"][-1][1].isoformat(), "session_bars": len(f["bars"]),
                              "fx_quote": f"{f['fx_quote']:f}" if "fx_quote" in f else None}
                          for s, f in facts.items()},
              "model_snapshot_hash": digest(model_snapshot),
              # Exactly what the proposer saw (validated market data + sellable flag), for replay.
              "model_snapshot": model_snapshot}
    stored["hash"] = digest(stored)
    return {"stored": stored, "model_snapshot": model_snapshot, "facts": facts}


def stored_model_snapshot(evidence_json):
    """Replay input of a stored decision: the model snapshot, or None for v1 records that did not keep it.

    Raises EvidenceError if a stored snapshot does not match its recorded hash or fails validation.
    """
    try:
        stored = json.loads(evidence_json) if isinstance(evidence_json, str) else evidence_json
    except json.JSONDecodeError:
        raise EvidenceError("STORED_EVIDENCE_UNREADABLE") from None
    if not isinstance(stored, dict) or "model_snapshot" not in stored:
        return None
    snapshot = stored["model_snapshot"]
    if digest(snapshot) != stored.get("model_snapshot_hash"):
        raise EvidenceError("STORED_SNAPSHOT_HASH_MISMATCH")
    return live_ai.validate_snapshot(snapshot)


def requote(client, market, symbol, exchange, *, max_quote_age_s, clock=_utcnow):
    """Fresh best bid/ask for the one symbol about to be ordered (the risk engine re-prices from it)."""
    if market == "KR":
        return parse_kr_quote(client.kr_orderbook(symbol), symbol, clock(), max_age_s=max_quote_age_s)
    return parse_us_quote(client.us_orderbook(exchange, symbol), symbol, exchange, clock(), max_age_s=max_quote_age_s)
