import unittest
from datetime import date

from cal.ranges import days_in_range, nights


class HiddenRangesTest(unittest.TestCase):
    def test_reported_case(self):
        days = days_in_range(date(2024, 2, 27), date(2024, 3, 1))
        self.assertEqual(days, [date(2024, 2, 27), date(2024, 2, 28), date(2024, 2, 29), date(2024, 3, 1)])

    def test_single_day(self):
        self.assertEqual(days_in_range(date(2024, 1, 1), date(2024, 1, 1)), [date(2024, 1, 1)])

    def test_backwards_and_nights_unchanged(self):
        self.assertEqual(days_in_range(date(2024, 3, 2), date(2024, 3, 1)), [])
        self.assertEqual(nights(date(2024, 2, 27), date(2024, 3, 1)), 3)
