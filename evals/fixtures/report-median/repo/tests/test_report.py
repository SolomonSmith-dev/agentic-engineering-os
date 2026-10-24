import io
import unittest

from report.reader import read_columns
from report.render import render
from report.stats import mean


class ReportTest(unittest.TestCase):
    def test_reader_skips_blanks(self):
        cols = read_columns(io.StringIO("a,b\n1,\n3,4\n"))
        self.assertEqual(cols, {"a": [1.0, 3.0], "b": [4.0]})

    def test_mean(self):
        self.assertEqual(mean([1, 2, 3]), 2)

    def test_render_odd_column(self):
        out = render({"a": [3.0, 1.0, 2.0]})
        self.assertIn("a  3  2  2", out)
