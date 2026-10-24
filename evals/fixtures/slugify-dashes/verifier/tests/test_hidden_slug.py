import unittest

from textutil.slug import slugify


class HiddenSlugTest(unittest.TestCase):
    def test_reported_case(self):
        self.assertEqual(slugify("  Hello,  World! "), "hello-world")

    def test_more(self):
        self.assertEqual(slugify("Caf\u00e9 au lait"), "cafe-au-lait")
        self.assertEqual(slugify("--a--b--"), "a-b")
        self.assertEqual(slugify("Version 2.0"), "version-2-0")
        self.assertEqual(slugify("!!!"), "")
