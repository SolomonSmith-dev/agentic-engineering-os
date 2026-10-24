import unittest
from decimal import Decimal

from invoice.invoice import Invoice
from invoice.money import round_cents


class HiddenRoundingTest(unittest.TestCase):
    def test_reported_order(self):
        inv = Invoice()
        inv.add("widget", 3, "0.375")
        self.assertEqual(inv.total(), Decimal("1.13"))

    def test_halves_round_up(self):
        for raw, cents in (("0.125", "0.13"), ("2.345", "2.35"), ("0.005", "0.01"), ("1.124", "1.12")):
            self.assertEqual(round_cents(Decimal(raw)), Decimal(cents), raw)

    def test_discount_still_rounds_half_up(self):
        inv = Invoice(discount_percent=50)
        inv.add("widget", 1, "0.25")
        self.assertEqual(inv.total(), Decimal("0.13"))
