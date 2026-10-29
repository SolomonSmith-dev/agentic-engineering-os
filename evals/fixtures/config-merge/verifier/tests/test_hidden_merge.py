import copy
import unittest

from cfg.loader import DEFAULTS, load
from cfg.merge import merge


class HiddenMergeTest(unittest.TestCase):
    def test_reported_case(self):
        settings = load('{"db": {"port": 6543}}', {})
        self.assertEqual(settings["db"], {"host": "localhost", "port": 6543})

    def test_env_on_top_of_nested_user(self):
        settings = load('{"db": {"port": 6543}}', {"APP_DB__HOST": "db.internal"})
        self.assertEqual(settings["db"], {"host": "db.internal", "port": 6543})

    def test_inputs_untouched(self):
        before = copy.deepcopy(DEFAULTS)
        base = {"a": {"b": 1}}
        merge(base, {"a": {"c": 2}})
        load('{"db": {"port": 1}}', {"APP_DB__HOST": "x"})
        self.assertEqual(base, {"a": {"b": 1}})
        self.assertEqual(DEFAULTS, before)

    def test_scalar_replaces_mapping(self):
        self.assertEqual(merge({"a": {"b": 1}}, {"a": 3}), {"a": 3})
