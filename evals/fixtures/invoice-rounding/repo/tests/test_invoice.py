import unittest
from decimal import Decimal

from invoice.invoice import Invoice


class InvoiceTest(unittest.TestCase):
    def test_total_of_whole_cents(self):
        inv = Invoice()
        inv.add("pen", 2, "1.50")
        inv.add("pad", 1, "3.25")
        self.assertEqual(inv.total(), Decimal("6.25"))

    def test_discount(self):
        inv = Invoice(discount_percent=10)
        inv.add("pen", 4, "2.50")
        self.assertEqual(inv.total(), Decimal("9.00"))

    def test_negative_quantity_is_refused(self):
        with self.assertRaises(ValueError):
            Invoice().add("pen", -1, "1.00")
