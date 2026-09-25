"""Offline cost/loss scenario for a small first live pilot.

Pure arithmetic on user-supplied values: no quotes, DB, network, keyring or broker
calls. It is a planning aid, not a recommendation or an order.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext

from .domain import CURRENCIES, ValidationError, decimal, integer, market

PILOT_CEILING_KRW = Decimal("50000")
PILOT_MAX_LOSS_KRW = Decimal("2500")
PILOT_TOTAL_KRW = Decimal("100000")
PILOT_TOTAL_MAX_LOSS_KRW = Decimal("5000")
MAX_COST_BPS = Decimal("1000")
FX_RATE_RANGE = (Decimal("100"), Decimal("10000"))
BPS = Decimal("10000")
HUNDRED = Decimal("100")
WON = Decimal("1")
FINE = Decimal("0.0001")

CONVENTION = [
    "모든 비율 입력은 bps(1 bps = 0.01%)입니다. 예: 수수료 0.015% = 1.5 bps.",
    "매수 체결가 = 입력 단가 × (1 + 슬리피지). 매수 수수료 = 매수 체결 금액 × 매수 수수료율.",
    "매도 체결가 = 청산 가격 × (1 - 슬리피지). 매도 수수료와 매도 세금은 각각 매도 체결 금액에 적용해 차감합니다.",
    "US: 매수에 필요한 원화 = 달러 매수 총액 × 환율 × (1 + 환전 비용), 매도 원화 = 달러 매도 순액 × 환율 × (1 - 환전 비용). 매수·매도에 같은 환율을 사용합니다.",
    "왕복 마찰 비용 = 예상 매수 총액 - 가격이 입력 단가 그대로일 때의 매도 원화 순액. 비율은 기준 금액(단가 × 수량 × 환율) 대비입니다.",
    "손익분기 가격 = 매도 원화 순액이 예상 매수 총액과 같아지는 청산 가격(같은 환율 가정).",
    "불리한 시나리오 손실 = 예상 매수 총액 - 입력 하락률로 청산한 매도 원화 순액. 손실 한도와 정확값으로 비교합니다.",
    "손실 한도 도달 하락률: 손실이 max_loss_krw와 같아지는 청산 가격까지의 하락률. 음수면 가격이 그대로여도 비용만으로 한도를 넘고, null이면 가격이 0이 되어도 한도 이내입니다.",
    "표시 반올림: 매수·왕복 총액과 손실은 원 단위 올림, 매수·매도별 비용은 소수 4자리 반올림, 남는 현금은 한도 - 올림한 매수 총액, 비율은 소수 4자리.",
]

EXCLUDED = [
    "입력한 매도 세율만 계산합니다. 다른 거래소/규제 수수료(미국 SEC·FINRA 수수료 등)와 양도소득세는 포함하지 않습니다.",
    "환전 스프레드·우대율 변화, 매수 이후 환율 변동은 반영하지 않습니다(US). 입력 환율은 조회값이 아닙니다.",
    "증권사 최소 수수료, 건당 정액 수수료, 수수료 원 미만 절사 규칙은 반영하지 않습니다.",
    "호가단위, 가격제한폭, 거래정지, 소수점 거래 여부, 체결 가능 수량은 검사하지 않습니다.",
    "시장 가격 변동은 지정한 하락 시나리오 1개뿐입니다. 실제 손실은 이보다 클 수 있고 확률이나 기대수익을 뜻하지 않습니다.",
    "주문 가능 금액·증거금·결제일(예: 국내 T+2) 현금 제약은 계좌에서 확인해야 합니다.",
]


def _number(name: str, value) -> Decimal:
    if value is None:
        raise ValidationError(f"{name} 값을 명시해야 합니다(기본값 없음)")
    try:
        return decimal(value)
    except ValidationError as exc:
        raise ValidationError(f"{name}: {exc}") from None


def _positive(name: str, value, upper: Decimal | None = None) -> Decimal:
    number = _number(name, value)
    if number <= 0 or (upper is not None and number > upper):
        rule = f"0보다 크고 {upper} 이하여야" if upper is not None else "0보다 커야"
        raise ValidationError(f"{name}은(는) {rule} 합니다")
    return number


def _rate(name: str, value) -> Decimal:
    number = _number(name, value)
    if number > MAX_COST_BPS:
        raise ValidationError(f"{name}은(는) 0~{MAX_COST_BPS} bps 범위여야 합니다")
    return number


def _plain(value: Decimal) -> str:
    return format(value, "f")


def _text(value: Decimal, step: Decimal, rounding) -> str:
    return str(value.quantize(step, rounding=rounding))


def scenario(*, market_code: str, price, quantity, buy_commission_bps, sell_commission_bps, sell_tax_bps, slippage_bps,
             max_loss_krw, adverse_move_pct, fx_rate=None, fx_cost_bps=None,
             budget_krw=PILOT_CEILING_KRW) -> dict:
    mkt = market(market_code)
    with localcontext() as ctx:
        ctx.prec = 50
        budget = _positive("budget_krw", budget_krw, PILOT_CEILING_KRW)
        if budget != budget.to_integral_value():
            raise ValidationError("budget_krw는 원 단위 정수여야 합니다")
        unit = _positive("price", price)
        if mkt == "KR" and unit != unit.to_integral_value():
            raise ValidationError("국내 주식 price는 원 단위 정수여야 합니다")
        try:
            qty = integer(quantity, minimum=1)
        except ValidationError:
            raise ValidationError("quantity는 1 이상의 정수여야 합니다") from None
        buy_fee_bps = _rate("buy_commission_bps", buy_commission_bps)
        sell_fee_bps = _rate("sell_commission_bps", sell_commission_bps)
        tax_bps = _rate("sell_tax_bps", sell_tax_bps)
        slip_bps = _rate("slippage_bps", slippage_bps)
        max_loss = _positive("max_loss_krw", max_loss_krw, min(budget, PILOT_MAX_LOSS_KRW))
        adverse = _positive("adverse_move_pct", adverse_move_pct, HUNDRED)
        if mkt == "US":
            if fx_rate is None or fx_cost_bps is None:
                raise ValidationError("US 시나리오는 fx_rate(원/달러)와 fx_cost_bps를 명시해야 합니다")
            fx = _positive("fx_rate", fx_rate)
            if not FX_RATE_RANGE[0] <= fx <= FX_RATE_RANGE[1]:
                raise ValidationError(f"fx_rate는 1달러당 원화 {FX_RATE_RANGE[0]}~{FX_RATE_RANGE[1]} 범위여야 합니다")
            fx_bps = _rate("fx_cost_bps", fx_cost_bps)
        else:
            if fx_rate is not None or fx_cost_bps is not None:
                raise ValidationError("KR 시나리오에는 fx_rate/fx_cost_bps를 입력하지 않습니다")
            fx, fx_bps = Decimal(1), Decimal(0)

        buy_fee, sell_fee, sell_tax, slip, fx_cost = (
            v / BPS for v in (buy_fee_bps, sell_fee_bps, tax_bps, slip_bps, fx_bps))
        reference = unit * qty * fx
        outlay = unit * (1 + slip) * qty * (1 + buy_fee) * fx * (1 + fx_cost)
        if outlay > budget:
            shown = outlay.to_integral_value(rounding=ROUND_CEILING)
            raise ValidationError(f"예상 매수 총액 {shown}원이 한도 {budget}원을 초과합니다. 수량이나 단가를 줄이세요")
        # KRW received per 1 unit of local exit price per share, after sell-side costs.
        sell_factor = qty * (1 - slip) * (1 - sell_fee - sell_tax) * fx * (1 - fx_cost)
        flat_proceeds = unit * sell_factor
        friction = outlay - flat_proceeds
        break_even = outlay / sell_factor
        adverse_price = unit * (1 - adverse / HUNDRED)
        adverse_loss = outlay - adverse_price * sell_factor
        budget_price = (outlay - max_loss) / sell_factor
        drop_to_budget = (1 - budget_price / unit) * HUNDRED if budget_price >= 0 else None

        outlay_won = outlay.to_integral_value(rounding=ROUND_CEILING)
        price_step = WON if mkt == "KR" else FINE
        return {
            "report": "pilot_scenario",
            "status": "planning_only",
            "notice": "입력값에 대한 산술 시나리오입니다. 종목 추천·매매 권유가 아니며 주문·시세 조회·계좌 조회를 하지 않습니다.",
            "market": mkt,
            "price_currency": CURRENCIES[mkt],
            "pilot_policy": {
                "market_budget_ceiling_krw": _plain(PILOT_CEILING_KRW),
                "market_planned_loss_ceiling_krw": _plain(PILOT_MAX_LOSS_KRW),
                "combined_budget_krw": _plain(PILOT_TOTAL_KRW),
                "combined_planned_loss_ceiling_krw": _plain(PILOT_TOTAL_MAX_LOSS_KRW),
                "combined_limits_enforced_by_this_report": False,
            },
            "inputs": {
                "budget_krw": _plain(budget),
                "price": _plain(unit),
                "quantity": qty,
                "buy_commission_bps": _plain(buy_fee_bps),
                "sell_commission_bps": _plain(sell_fee_bps),
                "sell_tax_bps": _plain(tax_bps),
                "slippage_bps_per_side": _plain(slip_bps),
                "fx_rate_krw_per_usd": _plain(fx) if mkt == "US" else None,
                "fx_cost_bps_per_conversion": _plain(fx_bps) if mkt == "US" else None,
                "max_loss_krw": _plain(max_loss),
                "adverse_move_pct": _plain(adverse),
            },
            "results": {
                "reference_notional_krw": _text(reference, FINE, ROUND_HALF_UP),
                "projected_purchase_outlay_krw": _plain(outlay_won),
                "remaining_cash_krw": _plain(budget - outlay_won),
                "buy_side_friction_krw": _text(outlay - reference, FINE, ROUND_HALF_UP),
                "sell_side_friction_krw": _text(reference - flat_proceeds, FINE, ROUND_HALF_UP),
                "round_trip_friction_krw": _text(friction, WON, ROUND_CEILING),
                "round_trip_friction_pct_of_notional": _text(friction / reference * HUNDRED, FINE, ROUND_CEILING),
                "break_even_exit_price": _text(break_even, price_step, ROUND_CEILING),
                "break_even_price_move_pct": _text((break_even / unit - 1) * HUNDRED, FINE, ROUND_CEILING),
                "adverse_scenario": {
                    "price_move_pct": _text(-adverse, FINE, ROUND_HALF_UP),
                    "exit_price": _text(adverse_price, FINE, ROUND_HALF_UP),
                    "loss_krw": _text(adverse_loss, WON, ROUND_CEILING),
                    "loss_pct_of_outlay": _text(adverse_loss / outlay * HUNDRED, FINE, ROUND_CEILING),
                    "max_loss_krw": _plain(max_loss),
                    "breaches_loss_budget": adverse_loss > max_loss,
                    "breaches_loss_budget_at_flat_price": friction > max_loss,
                    "price_drop_pct_reaching_loss_budget": (
                        _text(drop_to_budget, FINE, ROUND_FLOOR) if drop_to_budget is not None else None),
                },
            },
            "convention": list(CONVENTION),
            "excluded_and_uncertain": list(EXCLUDED),
        }
