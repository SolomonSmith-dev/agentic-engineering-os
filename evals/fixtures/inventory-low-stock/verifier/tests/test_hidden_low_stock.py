import unittest

from inventory.store import Inventory


class HiddenLowStockTest(unittest.TestCase):
    def setUp(self):
        self.inv = Inventory()
        for name, qty in (("washer", 2), ("bolt", 10), ("nut", 0), ("gear", 5)):
            self.inv.add(name, qty)

    def test_below_threshold_most_urgent_first(self):
        self.assertEqual(self.inv.low_stock(5), [("nut", 0), ("washer", 2)])

    def test_ties_by_name(self):
        self.inv.add("axle", 2)
        self.assertEqual(self.inv.low_stock(3), [("nut", 0), ("axle", 2), ("washer", 2)])

    def test_threshold_must_be_positive(self):
        with self.assertRaises(ValueError):
            self.inv.low_stock(0)
