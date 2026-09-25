"""Decimal money helpers; amounts are converted to float only when writing output."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

CENT = Decimal("0.01")
TOLERANCE = Decimal("0.01")


def to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def total(values: Iterable[Any]) -> Decimal:
    return sum((to_decimal(value) or Decimal(0) for value in values), Decimal(0))


def same_amount(left: Decimal | None, right: Decimal | None) -> bool:
    return left is not None and right is not None and abs(left - right) <= TOLERANCE


def to_brl(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(max(value, Decimal(0)).quantize(CENT, rounding=ROUND_HALF_UP))
