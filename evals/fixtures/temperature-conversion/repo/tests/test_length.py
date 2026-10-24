import unittest

from units.length import inches_to_mm


class LengthTest(unittest.TestCase):
    def test_inch(self):
        self.assertAlmostEqual(inches_to_mm(1), 25.4)
