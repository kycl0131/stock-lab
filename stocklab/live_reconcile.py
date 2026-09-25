"""Strict, pure parsers for the read-only order/fill and holdings inquiries used by `live`.

No DB, network or credentials here: callers pass the dict returned by KiwoomReadOnly.read().
Only fields documented in vendor/kiwoom-official/kiwoom/_data/kiwoom_api_spec.json are read.
Anything missing, malformed, duplicated or inconsistent raises InquiryError; callers treat
that as "not established" and keep the order blocked. A balance delta is never read as a fill.

KR (supported):
  kt00007 계좌별주문체결내역상세요청 (/api/dostk/acnt), list `acnt_ord_cntr_prps_dtl`
    ord_no, ori_ord, stk_cd ('A'+6 digits), io_tp_nm ('현금매수'/'현금매도' per the spec example),
    ord_qty, ord_uv, cntr_qty, cntr_uv, ord_remnq, mdfy_cncl, ord_tm (HH:mm:ss), dmst_stex_tp.
  kt00018 계좌평가잔고내역요청 qry_tp=1 (합산), list `acnt_evlt_remn_indv_tot`: stk_cd, rmnd_qty, trde_able_qty.

  Classification of the row whose ord_no equals the ticket's order number:
    FILLED   cntr_qty == ord_qty and ord_remnq == 0                    (terminal)
    OPEN     cntr_qty < ord_qty and cntr_qty + ord_remnq == ord_qty     (working or partially filled)
    UNRESOLVED  anything else: rejection, cancellation, expiry or an unknown state. kt00007 has no
             documented rejection/cancel/expiry codes (acpt_tp and mdfy_cncl are free text), so these
             are never inferred. Such tickets stay blocked until a human closes them (`live close`).
    Any row whose ori_ord points at the order (a modify/cancel entered outside this program) makes the
    order UNRESOLVED as well.

US (supported through ust21180 only; NOT yet checked against a real response):
  ust21180 미국주식 기간별 주문내역 (/api/us/acnt), request strt_dt/end_dt (YYYYMMDD), slby_tp (1 매도, 2 매수),
  stex_tp, stk_cd, oppo_trde_tp (0 일반). The list is documented as `result_list` but the official example
  returns `result_lsit`: every page must use exactly one of the two names and all pages the same name.
  Row fields read: ord_dt, ord_no, crnc_code, stk_cd, slby_tp_nm (example text '매수'/'매도'), ord_qty, cntr_qty,
  mdfy_qty, cncl_qty, ord_remnq, ord_uv, cntr_uv, cntr_amt, ord_time (HH:mm:ss), rsrv_tp and oppo_trde_tp_nm
  (example text '일반').
  The time zone of ord_dt/ord_time is not documented. The caller passes BOTH candidate (date, window) pairs:
  the KST date/time and the US-Eastern date/time of the attempt; a row must fit one of them. Nothing is
  inferred from the choice. There is no parent-order field (unlike kt00007 ori_ord), so modifications or
  cancellations are only visible through mdfy_qty/cncl_qty on the row itself.
    FILLED        cntr_qty == ord_qty, ord_remnq == 0, mdfy_qty == 0, cncl_qty == 0, cntr_amt consistent (terminal)
    NOT_TERMINAL  cntr_qty < ord_qty: the remainder may be working, cancelled, rejected or expired. The official
                  example itself shows cntr_qty 0, ord_remnq 0, cncl_qty 0 for a 1-share order, so the remainder
                  is never classified. Confirmed cumulative cntr_qty is recorded; a human closes the ticket.
    UNRESOLVED    mdfy_qty or cncl_qty > 0.
  Holdings: ust21070 list rows stk_cd, crnc_code, poss_qty, sell_alowq.
ust21510/ust21150 are not used (result_list/result_lsit plus unenumerated status and reservation codes).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re

KR_ORDER_LIST = "acnt_ord_cntr_prps_dtl"
KR_HOLDING_LIST = "acnt_evlt_remn_indv_tot"
SIDE_TEXT = {"BUY": "현금매수", "SELL": "현금매도"}
KR_SELL_TP = {"BUY": "2", "SELL": "1"}  # kt00007 sell_tp: 0 전체, 1 매도, 2 매수
US_ORDER_LISTS = ("result_list", "result_lsit")   # documented name / name in the official example
US_SIDE_TEXT = {"BUY": "매수", "SELL": "매도"}      # ust21180 example text; slby_tp_nm has no enumeration
US_SLBY_TP = {"BUY": "2", "SELL": "1"}            # ust21180 request slby_tp: 0 전체, 1 매도, 2 매수
US_RECONCILE_NOTE = (
    "US 대사는 ust21180(기간별 주문내역)만 사용합니다. 실제 응답으로 아직 검증되지 않았습니다: 목록 키(result_list/"
    "result_lsit), 매수/매도 문구, 주문일·시각의 기준 시간대가 명세에 확정되어 있지 않아 KST·미국 동부 두 해석 중 "
    "하나에 정확히 맞는 주문만 인정하고, 전량 체결만 자동 종결합니다.")


class InquiryError(Exception):
    """The broker response does not establish the fact that was asked for."""


def pages(result):
    if not isinstance(result, dict) or result.get("complete") is not True:
        raise InquiryError("INCOMPLETE_READ")
    items = result.get("pages")
    if not isinstance(items, list) or not items or not all(isinstance(p, dict) for p in items):
        raise InquiryError("INCOMPLETE_READ")
    return items


def _rows(result, key):
    rows = []
    for page in pages(result):
        if key not in page:
            continue  # the list is documented as optional (required: N): absent means no rows on this page
        items = page[key]
        if not isinstance(items, list) or not all(isinstance(r, dict) for r in items):
            raise InquiryError("MALFORMED_LIST")
        rows += items
    return rows


def count(value, field) -> int:
    """Zero-padded share count; an optional leading '+' is allowed ("부호 포함"). Negative is rejected."""
    if not isinstance(value, str) or not re.fullmatch(r"\+?[0-9]{1,15}", value.strip()):
        raise InquiryError(f"MALFORMED_{field.upper()}")
    return int(value.strip().lstrip("+"))


def order_no(value) -> str:
    """Canonical order number: digits without zero padding ('' for none/zero)."""
    if value is None:
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{0,12}", value.strip()):
        raise InquiryError("MALFORMED_ORDER_NUMBER")
    return value.strip().lstrip("0")


def _kr_code(value) -> str:
    if not isinstance(value, str):
        raise InquiryError("MALFORMED_STK_CD")
    code = value.strip()
    if re.fullmatch(r"A[0-9]{6}", code):
        return code[1:]
    if re.fullmatch(r"[0-9]{6}", code):
        return code
    return ""  # J/Q prefixes or other instruments never match a pilot symbol


def _seconds(hhmmss) -> int:
    if not isinstance(hhmmss, str) or not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]:[0-5][0-9]", hhmmss.strip()):
        raise InquiryError("MALFORMED_ORDER_TIME")
    h, m, s = (int(x) for x in hhmmss.strip().split(":"))
    if h > 23:
        raise InquiryError("MALFORMED_ORDER_TIME")
    return h * 3600 + m * 60 + s


@dataclass(frozen=True)
class KrOrder:
    order_no: str
    status: str            # FILLED | OPEN | UNRESOLVED
    ordered_qty: int
    filled_qty: int
    pending_qty: int
    avg_fill_price: str | None
    reason: str | None


def _matches_terms(row, *, side, symbol, quantity, limit_price) -> bool:
    try:
        return (_kr_code(row.get("stk_cd")) == symbol and row.get("io_tp_nm") == SIDE_TEXT[side]
                and count(row.get("ord_qty"), "ord_qty") == quantity
                and count(row.get("ord_uv"), "ord_uv") == int(limit_price)
                and order_no(row.get("ori_ord")) == "" and (row.get("mdfy_cncl") or "").strip() == ""
                and row.get("dmst_stex_tp") == "KRX")
    except InquiryError:
        return False


def kr_order(result, *, target_no, side, symbol, quantity, limit_price, window) -> KrOrder:
    """Locate and classify exactly one kt00007 row for `target_no`.

    `window` = (earliest, latest) seconds-of-day KST in which the order time must fall. The row must match
    the ticket terms exactly; a mismatch means the number belongs to some other order (fail closed).
    """
    target = order_no(target_no)
    if not target:
        raise InquiryError("NO_ORDER_IDENTITY")
    rows = _rows(result, KR_ORDER_LIST)
    own = [r for r in rows if order_no(r.get("ord_no")) == target]
    if not own:
        raise InquiryError("ORDER_NOT_FOUND")
    if len(own) != 1:
        raise InquiryError("ORDER_NUMBER_NOT_UNIQUE")
    row = own[0]
    if not _matches_terms(row, side=side, symbol=symbol, quantity=quantity, limit_price=limit_price):
        raise InquiryError("ORDER_TERMS_MISMATCH")
    at = _seconds(row.get("ord_tm"))
    if not window[0] <= at <= window[1]:
        raise InquiryError("ORDER_TIME_OUTSIDE_ATTEMPT_WINDOW")
    filled = count(row.get("cntr_qty"), "cntr_qty")
    pending = count(row.get("ord_remnq"), "ord_remnq")
    price = None
    if filled:
        avg = count(row.get("cntr_uv"), "cntr_uv")
        if avg <= 0:
            raise InquiryError("MALFORMED_CNTR_UV")
        price = str(avg)
    children = [r for r in rows if order_no(r.get("ori_ord")) == target]
    if filled > quantity or pending > quantity:
        raise InquiryError("QUANTITY_INCONSISTENT")
    if children:
        status, reason = "UNRESOLVED", "MODIFIED_OR_CANCELLED_OUTSIDE_PROGRAM"
    elif filled == quantity and pending == 0:
        status, reason = "FILLED", None
    elif filled + pending == quantity:
        status, reason = "OPEN", None
    else:
        status, reason = "UNRESOLVED", "REMAINDER_NOT_ESTABLISHED"  # rejected/cancelled/expired: not documented
    return KrOrder(target, status, quantity, filled, pending, price, reason)


def kr_unique_candidate(result, *, side, symbol, quantity, limit_price, window) -> str:
    """Order number of the ONLY row that matches the terms inside the window (used to verify a
    human-supplied number: it must be this one). Raises unless exactly one exists."""
    rows = _rows(result, KR_ORDER_LIST)
    hits = []
    for r in rows:
        if _matches_terms(r, side=side, symbol=symbol, quantity=quantity, limit_price=limit_price):
            try:
                at = _seconds(r.get("ord_tm"))
            except InquiryError:
                raise InquiryError("MALFORMED_ORDER_TIME") from None
            if window[0] <= at <= window[1]:
                hits.append(order_no(r.get("ord_no")))
    if len(hits) != 1 or not hits[0]:
        raise InquiryError("NO_UNIQUE_MATCHING_ORDER")
    return hits[0]


def kr_holding(result, symbol) -> tuple[int, int]:
    """(tradeable_qty, held_qty) for one KR symbol from kt00018 qry_tp=1; (0, 0) when absent."""
    rows = [r for r in _rows(result, KR_HOLDING_LIST) if _kr_code(r.get("stk_cd")) == symbol]
    if not rows:
        return 0, 0
    if len(rows) != 1:
        raise InquiryError("HOLDING_ROW_NOT_UNIQUE")
    tradeable = count(rows[0].get("trde_able_qty"), "trde_able_qty")
    held = count(rows[0].get("rmnd_qty"), "rmnd_qty")
    if tradeable > held:
        raise InquiryError("HOLDING_INCONSISTENT")
    return tradeable, held


# ---------------------------------------------------------------- US (ust21180 / ust21070)

def one_list(result, names):
    """Rows of the list field that every page names identically, from exactly one of `names`.

    A page carrying two of the names, or pages that disagree on the name, is ambiguous and refused.
    A page without any of them has no rows (the list is documented as optional).
    """
    used, rows = set(), []
    for page in pages(result):
        present = [n for n in names if n in page]
        if len(present) > 1:
            raise InquiryError("AMBIGUOUS_LIST_FIELD")
        if not present:
            continue
        used.add(present[0])
        items = page[present[0]]
        if not isinstance(items, list) or not all(isinstance(r, dict) for r in items):
            raise InquiryError("MALFORMED_LIST")
        rows += items
    if len(used) > 1:
        raise InquiryError("AMBIGUOUS_LIST_FIELD")
    return rows


def usd(value, field) -> Decimal:
    """Unsigned (optional '+') USD amount with at most 4 decimals."""
    if not isinstance(value, str) or not re.fullmatch(r"\+?[0-9]{1,12}(\.[0-9]{1,4})?", value.strip()):
        raise InquiryError(f"MALFORMED_{field.upper()}")
    try:
        return Decimal(value.strip().lstrip("+"))
    except InvalidOperation:
        raise InquiryError(f"MALFORMED_{field.upper()}") from None


def _text(row, field) -> str:
    value = row.get(field)
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class UsOrder:
    order_no: str
    order_date: str
    status: str            # FILLED | NOT_TERMINAL | UNRESOLVED
    ordered_qty: int
    filled_qty: int
    pending_qty: int       # ord_remnq as reported; not interpreted
    avg_fill_price: str | None
    reason: str | None


def _us_matches(row, *, side, symbol, quantity, limit_price) -> bool:
    try:
        return (_text(row, "stk_cd") == symbol and _text(row, "slby_tp_nm") == US_SIDE_TEXT[side]
                and _text(row, "crnc_code") == "USD"
                and count(row.get("ord_qty"), "ord_qty") == quantity
                and usd(row.get("ord_uv"), "ord_uv") == Decimal(limit_price)
                and _text(row, "rsrv_tp") == "일반" and _text(row, "oppo_trde_tp_nm") == "일반")
    except InquiryError:
        return False


def _us_fits(row, candidates) -> bool:
    """candidates: [(YYYYMMDD, (lo, hi) seconds-of-day)] for each time-zone reading of the attempt."""
    date = _text(row, "ord_dt")
    at = _seconds(row.get("ord_time"))
    return any(date == d and lo <= at <= hi for d, (lo, hi) in candidates)


def us_order(result, *, target_no, side, symbol, quantity, limit_price, candidates) -> UsOrder:
    """Locate and classify exactly one ust21180 row for `target_no` (see module docstring)."""
    target = order_no(target_no)
    if not target:
        raise InquiryError("NO_ORDER_IDENTITY")
    dates = {d for d, _ in candidates}
    rows = one_list(result, US_ORDER_LISTS)
    own = [r for r in rows if order_no(r.get("ord_no")) == target and _text(r, "ord_dt") in dates]
    if not own:
        raise InquiryError("ORDER_NOT_FOUND")
    if len(own) != 1:
        raise InquiryError("ORDER_NUMBER_NOT_UNIQUE")
    row = own[0]
    if not _us_matches(row, side=side, symbol=symbol, quantity=quantity, limit_price=limit_price):
        raise InquiryError("ORDER_TERMS_MISMATCH")
    if not _us_fits(row, candidates):
        raise InquiryError("ORDER_TIME_OUTSIDE_ATTEMPT_WINDOW")
    filled = count(row.get("cntr_qty"), "cntr_qty")
    pending = count(row.get("ord_remnq"), "ord_remnq")
    modified = count(row.get("mdfy_qty"), "mdfy_qty")
    cancelled = count(row.get("cncl_qty"), "cncl_qty")
    if filled > quantity or pending > quantity or filled + pending + cancelled > quantity:
        raise InquiryError("QUANTITY_INCONSISTENT")
    price = None
    if filled:
        avg = usd(row.get("cntr_uv"), "cntr_uv")
        amount = usd(row.get("cntr_amt"), "cntr_amt")
        # cntr_uv is rounded to 4 decimals, so cntr_qty * cntr_uv may differ from cntr_amt by rounding only.
        if avg <= 0 or abs(filled * avg - amount) > Decimal("0.01") + filled * Decimal("0.0001"):
            raise InquiryError("FILL_AMOUNT_INCONSISTENT")
        price = f"{avg:f}"
    if modified or cancelled:
        status, reason = "UNRESOLVED", "MODIFIED_OR_CANCELLED"
    elif filled == quantity and pending == 0:
        status, reason = "FILLED", None
    else:
        status, reason = "NOT_TERMINAL", "REMAINDER_NOT_ESTABLISHED"
    return UsOrder(target, _text(row, "ord_dt"), status, quantity, filled, pending, price, reason)


def us_unique_candidate(result, *, side, symbol, quantity, limit_price, candidates) -> str:
    """Order number of the ONLY ust21180 row matching the terms inside one of the candidate windows."""
    hits = []
    for r in one_list(result, US_ORDER_LISTS):
        if _us_matches(r, side=side, symbol=symbol, quantity=quantity, limit_price=limit_price) \
                and _us_fits(r, candidates):
            hits.append(order_no(r.get("ord_no")))
    if len(hits) != 1 or not hits[0]:
        raise InquiryError("NO_UNIQUE_MATCHING_ORDER")
    return hits[0]


def us_holding(result, symbol) -> tuple[int, int]:
    """(sell_alowq, poss_qty) for one US symbol from ust21070; (0, 0) when absent."""
    rows = [r for r in one_list(result, US_ORDER_LISTS) if _text(r, "stk_cd") == symbol]
    if not rows:
        return 0, 0
    if len(rows) != 1:
        raise InquiryError("HOLDING_ROW_NOT_UNIQUE")
    if _text(rows[0], "crnc_code") != "USD":
        raise InquiryError("HOLDING_CURRENCY_NOT_USD")
    tradeable = count(rows[0].get("sell_alowq"), "sell_alowq")
    held = count(rows[0].get("poss_qty"), "poss_qty")
    if tradeable > held:
        raise InquiryError("HOLDING_INCONSISTENT")
    return tradeable, held
