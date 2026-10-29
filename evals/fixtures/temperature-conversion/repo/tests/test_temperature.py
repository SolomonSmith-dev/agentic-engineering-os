import unittest

from units.temperature import celsius_to_fahrenheit, fahrenheit_to_celsius


class TemperatureTest(unittest.TestCase):
    def test_to_fahrenheit(self):
        self.assertEqual(celsius_to_fahrenheit(100), 212)

    def test_freezing_and_boiling(self):
        self.assertAlmostEqual(fahrenheit_to_celsius(32), 0)
        self.assertAlmostEqual(fahrenheit_to_celsius(212), 100)
