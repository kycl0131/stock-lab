"""Strict boundary types shared by research, risk and execution."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

CURRENCIES = {"KR": "KRW", "US": "USD"}
ACTIVE = ("OPEN", "PARTIAL")


class ValidationError(ValueError):
    pass


def timestamp(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError("timezone required")
        return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("Use an ISO timestamp with a timezone, e.g. 2026-09-01T06:30:00Z") from exc


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def decimal(value, *, nonnegative=True) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError("Boolean is not a numeric value")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("Invalid decimal") from exc
    if not number.is_finite() or (nonnegative and number < 0):
        raise ValidationError("Number must be finite and nonnegative")
    if abs(number) > Decimal("1e15"):
        raise ValidationError("Number outside supported range")
    return number


def integer(value, *, minimum=0) -> int:
    number = decimal(value)
    if number != number.to_integral_value() or number < minimum:
        raise ValidationError(f"Expected integer >= {minimum}")
    return int(number)


def market(value: str) -> str:
    if value not in CURRENCIES:
        raise ValidationError("Market must be KR or US")
    return value


def symbol(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9_.-]{1,32}", value):
        raise ValidationError("Invalid symbol")
    return value


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RiskPolicy:
    max_order_notional: str = "1000"
    max_symbol_weight: str = "0.25"
    max_gross_weight: str = "0.80"
    cash_buffer: str = "0.10"
    fee_bps: str = "5"
    slippage_bps: str = "10"
    participation: str = "0.01"
    max_quote_age_seconds: int = 86400
    order_ttl_seconds: int = 345600

    def __post_init__(self):
        for name, val in asdict(self).items():
            decimal(val)
        if decimal(self.max_order_notional) <= 0:
            raise ValidationError("max_order_notional must be positive")
        for name in ("max_symbol_weight", "max_gross_weight", "cash_buffer", "participation"):
            if decimal(getattr(self, name)) > 1:
                raise ValidationError(f"{name} must be <= 1")
        if decimal(self.participation) == 0:
            raise ValidationError("participation must be positive")
        if decimal(self.fee_bps) > 1000 or decimal(self.slippage_bps) > 1000:
            raise ValidationError("Fee/slippage assumption must be <= 1000 bps")
        for name in ("max_quote_age_seconds", "order_ttl_seconds"):
            object.__setattr__(self, name, integer(getattr(self, name), minimum=1))

    def payload(self) -> dict:
        return asdict(self)


def default_policy(mkt: str) -> RiskPolicy:
    return RiskPolicy(max_order_notional="1000000" if market(mkt) == "KR" else "1000")
