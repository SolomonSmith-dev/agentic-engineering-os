"""Releasing a table entry refuses malformed legacy state without changing files."""
import json
import tempfile
import unittest
from pathlib import Path

from agent_team.environments.locks import LEGACY_LOCK, LockHeldError, acquire, release


class ReleaseFailClosedTests(unittest.TestCase):
    def test_malformed_legacy_lock_preserves_table_on_release(self):
        cases = (
            {"run_id": "broken"},
            [],
            {"run_id": "x", "pid": "bad", "host": "host", "acquired_at": ""},
        )
        for legacy in cases:
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                acquire(state, "live", ["src/a"])
                (state / LEGACY_LOCK).write_text(json.dumps(legacy))
                before = {p.name: p.read_bytes() for p in state.iterdir() if p.name != "lock.guard"}
                with self.assertRaises(LockHeldError):
                    release(state, "live")
                after = {p.name: p.read_bytes() for p in state.iterdir() if p.name != "lock.guard"}
                self.assertEqual(before, after)
