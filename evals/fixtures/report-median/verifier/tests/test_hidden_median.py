import io
import unittest

from report.reader import read_columns
from report.render import render
from report.stats import median


class HiddenMedianTest(unittest.TestCase):
    def test_reported_case(self):
        out = render(read_columns(io.StringIO("v\n1\n2\n3\n4\n")))
        self.assertIn("v  4  2.5  2.5", out)

    def test_even_and_odd(self):
        self.assertEqual(median([4.0, 1.0, 3.0, 2.0]), 2.5)
        self.assertEqual(median([5.0, 1.0, 3.0]), 3.0)
        self.assertEqual(median([7.0]), 7.0)
        self.assertEqual(median([1.0, 2.0]), 1.5)
