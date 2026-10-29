import unittest

from cachekit.lru import LRUCache


class HiddenLRUTest(unittest.TestCase):
    def test_reported_case(self):
        c = LRUCache(2)
        c.put("a", 1)
        c.put("b", 2)
        self.assertEqual(c.get("a"), 1)
        c.put("c", 3)
        self.assertIn("a", c)
        self.assertNotIn("b", c)

    def test_missing_get_changes_nothing(self):
        c = LRUCache(2)
        c.put("a", 1)
        c.put("b", 2)
        self.assertIsNone(c.get("zzz"))
        c.put("c", 3)
        self.assertNotIn("a", c)
        self.assertNotIn("zzz", c)
