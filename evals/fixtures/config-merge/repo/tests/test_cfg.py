import unittest

from cfg.env import from_env
from cfg.loader import DEFAULTS, load


class CfgTest(unittest.TestCase):
    def test_defaults_alone(self):
        self.assertEqual(load(None, {}), DEFAULTS)

    def test_env_parsing(self):
        self.assertEqual(from_env({"APP_DB__PORT": "1", "HOME": "/x"}), {"db": {"port": "1"}})

    def test_top_level_override(self):
        self.assertEqual(load('{"debug": true}', {})["debug"], True)
