import unittest

from units.temperature import celsius_to_fahrenheit, fahrenheit_to_celsius


class HiddenTemperatureTest(unittest.TestCase):
    def test_round_trip(self):
        for c in (-40, -10.5, 0, 37, 100):
            self.assertAlmostEqual(fahrenheit_to_celsius(celsius_to_fahrenheit(c)), c)

    def test_body_temperature(self):
        self.assertAlmostEqual(fahrenheit_to_celsius(98.6), 37)
