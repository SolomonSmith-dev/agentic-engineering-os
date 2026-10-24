import contextlib
import io
import json
import os
import tempfile
import unittest

from wc.cli import main


def run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(argv)
    return code, out.getvalue()


class HiddenJsonTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "f.txt")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("one two\nthree\n")

    def tearDown(self):
        self.dir.cleanup()

    def test_json(self):
        code, out = run(["--json", self.path])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"file": self.path, "lines": 2, "words": 3, "chars": 14})

    def test_json_after_file(self):
        code, out = run([self.path, "--json"])
        self.assertEqual(json.loads(out)["words"], 3)

    def test_plain_unchanged(self):
        self.assertEqual(run([self.path])[1], f"2 3 14 {self.path}\n")
