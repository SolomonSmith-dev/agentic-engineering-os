import unittest

from inventory.store import Inventory


class StoreTest(unittest.TestCase):
    def test_add_remove(self):
        inv = Inventory()
        inv.add("bolt", 5)
        inv.remove("bolt", 2)
        self.assertEqual(inv.quantity("bolt"), 3)

    def test_overdraw(self):
        inv = Inventory()
        with self.assertRaises(ValueError):
            inv.remove("nut", 1)

    def test_items_sorted(self):
        inv = Inventory()
        inv.add("b", 1)
        inv.add("a", 2)
        self.assertEqual(inv.items(), [("a", 2), ("b", 1)])
