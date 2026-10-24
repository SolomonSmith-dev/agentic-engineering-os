import contextlib
import io
import os
import tempfile
import unittest

from wc.cli import main
from wc.count import count


class WcTest(unittest.TestCase):
    def test_count(self):
        c = count("a b\nc\n")
        self.assertEqual((c.lines, c.words, c.chars), (2, 3, 6))

    def test_cli_plain(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("one two\n")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main([path]), 0)
            self.assertEqual(out.getvalue(), f"1 2 8 {path}\n")
