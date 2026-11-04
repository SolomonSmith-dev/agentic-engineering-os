"""Lock state that cannot be read fails closed; table and legacy lock exclude each other in both startup orders."""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from agent_team.environments.locks import (
    LEGACY_LOCK,
    TABLE_NAME,
    LockHeldError,
    acquire,
    live_run_ids,
    read_table,
    release,
)


def snapshot(state: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(state.iterdir()) if p.is_file() and p.name != "lock.guard"}


def live_entry(run_id: str, paths: list[str]) -> dict:
    return {"run_id": run_id, "pid": os.getpid(), "host": socket.gethostname(), "paths": paths, "acquired_at": ""}


BAD_TABLES = {
    "invalid-json": "{broken",
    "not-a-list": json.dumps({"run_id": "r"}),
    "empty-file": "",
}


@pytest.mark.parametrize("name", BAD_TABLES)
def test_corrupt_table_refuses_and_stays_unchanged(tmp_path: Path, name: str) -> None:
    (tmp_path / TABLE_NAME).write_text(BAD_TABLES[name])
    before = snapshot(tmp_path)
    with pytest.raises(LockHeldError):
        acquire(tmp_path, "r1", ["src"])
    with pytest.raises(LockHeldError):
        release(tmp_path, "r1")
    with pytest.raises(LockHeldError):
        live_run_ids(tmp_path)
    assert snapshot(tmp_path) == before


def test_malformed_entry_beside_a_live_holder_is_not_discarded(tmp_path: Path) -> None:
    (tmp_path / TABLE_NAME).write_text(json.dumps([live_entry("live", ["a"]), {"run_id": "broken"}, "junk"]))
    before = snapshot(tmp_path)
    with pytest.raises(LockHeldError):
        acquire(tmp_path, "r1", ["b"])
    with pytest.raises(LockHeldError):
        release(tmp_path, "live")
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("invalid", [{"paths": "src"}, {"paths": [None]}, {"paths": ["../src"]}, {"pid": 0.5}, {"host": None}])
def test_malformed_typed_entry_refuses_without_narrowing_scope(tmp_path: Path, invalid: dict) -> None:
    (tmp_path / TABLE_NAME).write_text(json.dumps([{**live_entry("live", ["src"]), **invalid}]))
    before = snapshot(tmp_path)
    with pytest.raises(LockHeldError):
        acquire(tmp_path, "new", ["src"])
    with pytest.raises(LockHeldError):
        release(tmp_path, "live")
    assert snapshot(tmp_path) == before


def test_live_holder_in_a_wellformed_table_still_refuses_unchanged(tmp_path: Path) -> None:
    (tmp_path / TABLE_NAME).write_text(json.dumps([live_entry("live", ["a"])]))
    before = snapshot(tmp_path)
    with pytest.raises(LockHeldError, match="run live holds a"):
        acquire(tmp_path, "r1", ["a"])
    assert snapshot(tmp_path) == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_unreadable_table_refuses_and_stays_unchanged(tmp_path: Path) -> None:
    table = tmp_path / TABLE_NAME
    table.write_text(json.dumps([live_entry("live", ["a"])]))
    table.chmod(0)
    try:
        with pytest.raises(LockHeldError):
            acquire(tmp_path, "r1", ["b"])
        with pytest.raises(LockHeldError):
            read_table(tmp_path)
    finally:
        table.chmod(0o600)
    assert json.loads(table.read_text())[0]["run_id"] == "live"
    assert not list(tmp_path.glob("*.tmp"))


def test_missing_table_means_unlocked(tmp_path: Path) -> None:
    assert read_table(tmp_path) == []
    assert acquire(tmp_path, "r1", ["a"]) == []


@pytest.mark.parametrize("content", ["not json", "", "null", json.dumps({"run_id": "x"}), "[]"])
def test_malformed_legacy_lock_refuses_and_stays(tmp_path: Path, content: str) -> None:
    (tmp_path / LEGACY_LOCK).write_text(content)
    before = snapshot(tmp_path)
    with pytest.raises(LockHeldError):
        acquire(tmp_path, "r1", ["src"])
    with pytest.raises(LockHeldError):
        live_run_ids(tmp_path)
    assert snapshot(tmp_path) == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_unreadable_legacy_lock_refuses_and_stays(tmp_path: Path) -> None:
    legacy = tmp_path / LEGACY_LOCK
    legacy.write_text(json.dumps({"run_id": "old", "pid": os.getpid(), "host": socket.gethostname(), "acquired_at": ""}))
    legacy.chmod(0)
    try:
        with pytest.raises(LockHeldError):
            acquire(tmp_path, "r1", ["src"])
    finally:
        legacy.chmod(0o600)
    assert json.loads(legacy.read_text())["run_id"] == "old"


# --- mixed versions: a legacy process reads only `lock`, creating it exclusively when absent or stale ---

LEGACY_ACQUIRE = r"""
import fcntl, json, os, socket, sys
path = sys.argv[1]
guard = os.open(os.path.join(os.path.dirname(path), "lock.guard"), os.O_WRONLY | os.O_CREAT, 0o600)
fcntl.flock(guard, fcntl.LOCK_EX)
try:
    held = json.loads(open(path).read())
    if held["host"] != socket.gethostname():
        sys.exit(3)  # legacy readers never take over a different host's lock
    os.kill(int(held["pid"]), 0)
    sys.exit(3)  # a live holder: refused
except FileNotFoundError:
    pass
except ProcessLookupError:
    os.unlink(path)
except PermissionError:
    sys.exit(3)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, json.dumps({"run_id": "legacy", "pid": os.getpid(), "host": socket.gethostname(), "acquired_at": ""}).encode())
"""


def legacy_acquire(state: Path) -> int:
    return subprocess.run([sys.executable, "-I", "-c", LEGACY_ACQUIRE, str(state / LEGACY_LOCK)], check=False).returncode


def test_new_first_then_legacy_is_refused(tmp_path: Path) -> None:
    acquire(tmp_path, "r1", ["src/a"])
    assert legacy_acquire(tmp_path) == 3
    acquire(tmp_path, "r2", ["src/b"])  # disjoint new-version concurrency still works
    assert legacy_acquire(tmp_path) == 3
    assert live_run_ids(tmp_path) == {"r1", "r2"}


def test_legacy_stays_excluded_while_any_table_holder_lives(tmp_path: Path) -> None:
    sleeper = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
    try:
        acquire(tmp_path, "r1", ["a"])
        acquire(tmp_path, "r2", ["b"], pid=sleeper.pid)
        release(tmp_path, "r1")  # the entry the marker named is gone; r2 still holds
        assert legacy_acquire(tmp_path) == 3
        release(tmp_path, "r2")
        assert not (tmp_path / LEGACY_LOCK).exists(), "an empty table leaves no marker behind"
        assert legacy_acquire(tmp_path) == 0
    finally:
        sleeper.kill()
        sleeper.wait()


def test_legacy_first_then_new_is_refused(tmp_path: Path) -> None:
    assert legacy_acquire(tmp_path) == 0
    legacy = tmp_path / LEGACY_LOCK  # the legacy process is still running: point the lock at this live process
    legacy.write_text(json.dumps({**json.loads(legacy.read_text()), "pid": os.getpid()}))
    with pytest.raises(LockHeldError, match="holds ."):
        acquire(tmp_path, "r1", ["src"])
    assert not (tmp_path / TABLE_NAME).exists()


def test_legacy_is_refused_after_marker_holder_dies(tmp_path: Path) -> None:
    sleeper = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
    try:
        acquire(tmp_path, "first", ["a"], pid=sleeper.pid)
        acquire(tmp_path, "survivor", ["b"])
        sleeper.kill()
        sleeper.wait()
        assert legacy_acquire(tmp_path) == 3
        assert live_run_ids(tmp_path) == {"survivor"}
        release(tmp_path, "survivor")
        assert legacy_acquire(tmp_path) == 0
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait()


def test_all_dead_holders_need_current_version_recovery_before_legacy(tmp_path: Path) -> None:
    sleeper = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
    try:
        acquire(tmp_path, "dead", ["a"], pid=sleeper.pid)
    finally:
        sleeper.kill()
        sleeper.wait()
    assert legacy_acquire(tmp_path) == 3
    assert [entry.run_id for entry in acquire(tmp_path, "recovery", ["a"])] == ["dead"]
    assert release(tmp_path, "recovery")
    assert not (tmp_path / LEGACY_LOCK).exists()
    assert legacy_acquire(tmp_path) == 0


def test_real_foreign_host_lock_with_table_flag_is_preserved(tmp_path: Path) -> None:
    acquire(tmp_path, "ours", ["a"])
    foreign = json.dumps({"run_id": "foreign", "pid": 1, "host": "other-host", "acquired_at": "", "table": True})
    (tmp_path / LEGACY_LOCK).write_text(foreign)
    with pytest.raises(LockHeldError, match="foreign"):
        acquire(tmp_path, "new", ["b"])
    assert release(tmp_path, "ours")
    assert (tmp_path / LEGACY_LOCK).read_text() == foreign


def test_malformed_table_does_not_clear_compatibility_marker(tmp_path: Path) -> None:
    acquire(tmp_path, "ours", ["a"])
    (tmp_path / TABLE_NAME).write_text('{"incomplete": true}')
    before = snapshot(tmp_path)
    with pytest.raises(LockHeldError):
        acquire(tmp_path, "recovery", ["a"])
    with pytest.raises(LockHeldError):
        release(tmp_path, "ours")
    assert snapshot(tmp_path) == before
    assert legacy_acquire(tmp_path) == 3


def test_stale_marker_does_not_block_a_new_acquire(tmp_path: Path) -> None:
    gone = subprocess.Popen(["true"])
    gone.wait()
    acquire(tmp_path, "r1", ["a"], pid=gone.pid)
    assert [e.run_id for e in read_table(tmp_path)] == ["r1"]
    assert acquire(tmp_path, "r2", ["a"])[0].run_id == "r1"
    assert live_run_ids(tmp_path) == {"r2"}
