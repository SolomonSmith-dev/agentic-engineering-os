"""Order lines."""

from dataclasses import dataclass
from decimal import Decimal

from invoice.money import to_money


@dataclass(frozen=True)
class Line:
    sku: str
    quantity: int
    unit_price: Decimal

    @classmethod
    def parse(cls, sku: str, quantity: int, unit_price: str) -> "Line":
        if quantity < 0:
            raise ValueError("quantity must be >= 0")
        return cls(sku, quantity, to_money(unit_price))

    def subtotal(self) -> Decimal:
        return self.unit_price * self.quantity
