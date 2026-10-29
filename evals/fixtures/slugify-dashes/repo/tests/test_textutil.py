import unittest

from textutil.slug import slugify
from textutil.wrap import wrap


class TextutilTest(unittest.TestCase):
    def test_simple_slug(self):
        self.assertEqual(slugify("hello"), "hello")

    def test_accents_drop(self):
        self.assertEqual(slugify("cafe"), "cafe")

    def test_wrap(self):
        self.assertEqual(wrap("a b c", 3), ["a b", "c"])
