import unittest

from cachekit.lru import LRUCache


class LRUTest(unittest.TestCase):
    def test_evicts_oldest_put(self):
        c = LRUCache(2)
        c.put("a", 1)
        c.put("b", 2)
        c.put("c", 3)
        self.assertNotIn("a", c)
        self.assertEqual(len(c), 2)

    def test_default(self):
        self.assertIsNone(LRUCache(1).get("x"))
        self.assertEqual(LRUCache(1).get("x", 5), 5)

    def test_bad_capacity(self):
        with self.assertRaises(ValueError):
            LRUCache(0)
