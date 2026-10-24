"""An invoice: lines, an optional discount, a total."""

from decimal import Decimal

from invoice.lines import Line
from invoice.money import round_cents, to_money


class Invoice:
    def __init__(self, discount_percent: int = 0) -> None:
        self.lines: list[Line] = []
        self.discount_percent = discount_percent

    def add(self, sku: str, quantity: int, unit_price: str) -> None:
        self.lines.append(Line.parse(sku, quantity, unit_price))

    def gross(self) -> Decimal:
        return sum((line.subtotal() for line in self.lines), to_money(0))

    def total(self) -> Decimal:
        discount = self.gross() * Decimal(self.discount_percent) / 100
        return round_cents(self.gross() - discount)
