"""Money helpers: every amount is a Decimal, rounded to the cent."""

from decimal import ROUND_HALF_EVEN, Decimal

CENT = Decimal("0.01")


def to_money(value: str | int | Decimal) -> Decimal:
    return Decimal(str(value))


def round_cents(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, rounding=ROUND_HALF_EVEN)
