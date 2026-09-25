"""WRITE-CAPABLE Kiwoom client: real-money limit orders only. Kept apart from KiwoomReadOnly.

This module can transmit a REAL order to https://api.kiwoom.com. It is only reachable through
the ticket workflow in live_orders.py (typed human confirmation, or an armed autonomous intent);
nothing here schedules, retries or resends.

- Four fixed order templates: kt10000/kt10001 (/api/dostk/ordr),
  ust20000/ust20001 (/api/us/ordr). submit() transmits only a ticket that the live ledger
  shows as freshly ATTEMPTED with gate evidence (live_orders.verify_attempt_for_submit),
  a SELL within confirmed pilot inventory, and for autonomous tickets a still-valid arming.
  Callers cannot supply an API ID, path, header or body; requests are built from validated
  OrderTerms fields only.
- Ordinary limit orders only (spec trde_tp: KR "0" 보통, US "00" 지정가). No market, stop,
  conditional, modify, cancel, credit/margin, short or FX order is constructed.
- Real host pinned; demo host and PRD endpoint overrides are refused.
- Success requires return_code == 0 and a non-empty numeric ord_no. Every other outcome,
  including any exception or timeout, is reported as UNKNOWN; acceptance is not a fill.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re

from .domain import ValidationError, decimal, integer

REAL_HOST = "https://api.kiwoom.com"
MODE = "real"
ORDER_APIS = {("KR", "BUY"): ("kt10000", "/api/dostk/ordr"),
              ("KR", "SELL"): ("kt10001", "/api/dostk/ordr"),
              ("US", "BUY"): ("ust20000", "/api/us/ordr"),
              ("US", "SELL"): ("ust20001", "/api/us/ordr")}
LIMIT_CODES = {"KR": "0", "US": "00"}  # kiwoom_api_spec.json: KR "0:보통", US "00:지정가"
EXCHANGES = {"KR": ("KRX",), "US": ("ND", "NY", "NA")}
MAX_QUANTITY = 10000
MAX_PRICE = {"KR": Decimal("5000000"), "US": Decimal("100000")}
# KRX 주권 호가단위 (2023-01-25 개정). ETF/ETN 등은 규칙이 다를 수 있어 맞지 않으면 로컬에서 거부됩니다.
KRX_TICKS = ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500), (None, 1000))


def krx_tick(price: int) -> int:
    for bound, tick in KRX_TICKS:
        if bound is None or price < bound:
            return tick


@dataclass(frozen=True)
class OrderTerms:
    """Validated, immutable order terms. The only input from which a broker request is built."""
    market: str
    side: str
    symbol: str
    exchange: str
    quantity: int
    limit_price: str

    def __post_init__(self):
        if self.market not in EXCHANGES:
            raise ValidationError("시장은 KR 또는 US만 허용됩니다.")
        if self.side not in ("BUY", "SELL"):
            raise ValidationError("side는 BUY 또는 SELL만 허용됩니다.")
        if self.exchange not in EXCHANGES[self.market]:
            raise ValidationError("거래소: KR은 KRX, US는 ND/NY/NA만 허용됩니다.")
        if not isinstance(self.symbol, str) or not re.fullmatch(
                r"[0-9]{6}" if self.market == "KR" else r"[A-Z]{1,5}", self.symbol):
            raise ValidationError("종목코드: KR은 6자리 숫자, US는 대문자 1~5자 티커만 허용됩니다.")
        quantity = integer(self.quantity, minimum=1)
        if quantity > MAX_QUANTITY:
            raise ValidationError(f"수량은 1~{MAX_QUANTITY} 정수만 허용됩니다.")
        object.__setattr__(self, "quantity", quantity)
        if not isinstance(self.limit_price, str) or not re.fullmatch(r"[0-9]+(\.[0-9]+)?", self.limit_price.strip()):
            raise ValidationError("지정가는 양의 숫자로 입력하세요.")
        price = decimal(self.limit_price.strip())
        if price <= 0 or price > MAX_PRICE[self.market]:
            raise ValidationError("지정가가 허용 범위를 벗어났습니다.")
        if self.market == "KR":
            if price != price.to_integral_value():
                raise ValidationError("국내 지정가는 원 단위 정수여야 합니다.")
            if int(price) % krx_tick(int(price)):
                raise ValidationError(f"국내 지정가가 KRX 주권 호가단위({krx_tick(int(price))}원)에 맞지 않습니다.")
            canonical = str(int(price))
        else:
            if price != price.quantize(Decimal("0.01")):
                raise ValidationError("미국 지정가는 센트(소수 둘째 자리) 단위여야 합니다.")
            if price < 1:
                raise ValidationError("미국 지정가는 1.00달러 이상만 허용됩니다 (1달러 미만 호가단위 미지원).")
            canonical = f"{price.quantize(Decimal('0.01')):f}"
        object.__setattr__(self, "limit_price", canonical)

    def gross(self) -> Decimal:
        """Quantity × limit price in the market currency (KRW or USD), before costs."""
        return Decimal(self.quantity) * Decimal(self.limit_price)

    def payload(self) -> dict:
        return {"market": self.market, "side": self.side, "symbol": self.symbol, "exchange": self.exchange,
                "quantity": self.quantity, "limit_price": self.limit_price}


def build_request(terms: OrderTerms) -> dict:
    """Allowlisted request: fixed API ID/path and a body built field by field from OrderTerms."""
    if not isinstance(terms, OrderTerms):
        raise ValidationError("Order requests are built only from validated OrderTerms")
    api_id, path = ORDER_APIS[(terms.market, terms.side)]
    qty, price, code = str(terms.quantity), terms.limit_price, LIMIT_CODES[terms.market]
    if terms.market == "KR":
        body = {"dmst_stex_tp": "KRX", "stk_cd": terms.symbol, "ord_qty": qty, "ord_uv": price,
                "trde_tp": code, "cond_uv": ""}
    elif terms.side == "BUY":
        body = {"stex_tp": terms.exchange, "stk_cd": terms.symbol, "ord_qty": qty, "ord_uv": price, "trde_tp": code}
    else:
        body = {"stk_cd": terms.symbol, "stex_tp": terms.exchange, "ord_qty": qty, "ord_uv": price,
                "stop_pric": "", "trde_tp": code}
    return {"api_id": api_id, "path": path, "body": body}


class OrderOutcomeUnknown(Exception):
    """The order MAY have reached the broker. Never retry; treat as UNKNOWN until reconciled."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def _category(exc) -> str:
    return "EXCEPTION_" + re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:60]


def accepted_order_no(body) -> str:
    """ord_no when the response is an unambiguous acceptance; otherwise raise OrderOutcomeUnknown."""
    if not isinstance(body, dict):
        raise OrderOutcomeUnknown("RESPONSE_NOT_OBJECT")
    code = body.get("return_code")
    if isinstance(code, bool) or code not in (0, "0"):
        raise OrderOutcomeUnknown("RETURN_CODE_NOT_ZERO")
    order_no = body.get("ord_no")
    if not isinstance(order_no, str) or not re.fullmatch(r"[0-9]{1,12}", order_no.strip()) \
            or not order_no.strip().strip("0"):
        raise OrderOutcomeUnknown("ORDER_NUMBER_MISSING")
    return order_no.strip()


def mask_order_no(order_no) -> str:
    if not isinstance(order_no, str) or len(order_no) < 3:
        return "***"
    return "*" * (len(order_no) - 3) + order_no[-3:]


class KiwoomRealOrderClient:
    """Real-money order transmission. One instance per send; closed afterwards."""

    def __init__(self):
        try:
            from kiwoom import KiwoomAuth, KiwoomClient
            from kiwoom.core.auth import get_base_url
            from kiwoom.core.secrets import ProfileKeyringSecretProvider
            from kiwoom.core.token_store import MemoryTokenStore
        except ImportError:
            raise ValidationError("키움 공식 클라이언트가 없습니다. README의 전용 환경 설치 절차를 실행하세요.") from None
        self._base_url = get_base_url
        self._check_host()
        self._auth = KiwoomAuth(MODE, ProfileKeyringSecretProvider("stocklab-" + MODE), MemoryTokenStore(),
                                profile="stocklab-" + MODE, refresh_buffer_seconds=0, timeout_seconds=15)
        self._client = KiwoomClient(self._auth, timeout_seconds=15)

    def _check_host(self):
        if self._base_url(MODE) != REAL_HOST:
            raise ValidationError("실전 주문은 공식 실전 호스트만 허용합니다 (PRD 등 엔드포인트 재정의 거부).")

    def prepare_connection(self):
        """Issue the access token BEFORE a ticket is marked ATTEMPTED, so auth failure sends nothing."""
        from .kiwoom_bridge import safe_error
        self._check_host()
        try:
            self._auth.get_access_token()
        except Exception as exc:
            raise ValidationError(safe_error(exc)) from None

    def submit(self, terms: OrderTerms, *, ledger, ticket_id: str) -> str:
        """Transmit exactly once. Returns ord_no on unambiguous acceptance, else OrderOutcomeUnknown.

        Refuses unless the live ledger holds this exact ticket as freshly ATTEMPTED with its gate
        evidence (and, for SELL, pilot entitlement re-checked from the ledger at this moment).
        There is no other path to the order endpoint.
        """
        from .live_orders import claim_attempt_for_submit
        claim_attempt_for_submit(ledger, ticket_id, terms)
        request = build_request(terms)
        self._check_host()
        if (request["api_id"], request["path"]) not in ORDER_APIS.values():
            raise ValidationError("Order target outside the allowlist")
        try:
            response = self._client.request(api_id=request["api_id"], path=request["path"], body=request["body"],
                                            retry_on_auth_failure=False)
        except BaseException as exc:
            # Includes timeouts, HTTP errors, broker error codes and Ctrl+C: the order may have been received.
            raise OrderOutcomeUnknown(_category(exc)) from None
        return accepted_order_no(getattr(response, "body", None))

    def close(self):
        try:
            self._client.session.close()
        finally:
            self._auth.clear_token()
