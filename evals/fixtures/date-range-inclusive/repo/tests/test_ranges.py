import unittest
from datetime import date

from cal.ranges import days_in_range, nights


class RangesTest(unittest.TestCase):
    def test_backwards_is_empty(self):
        self.assertEqual(days_in_range(date(2024, 3, 2), date(2024, 3, 1)), [])

    def test_nights(self):
        self.assertEqual(nights(date(2024, 2, 27), date(2024, 3, 1)), 3)
        self.assertEqual(nights(date(2024, 3, 1), date(2024, 2, 27)), 0)
