"""Path-lock table (engine plan M2 Task 6): overlap, limit, stale takeover, legacy lock, flock race."""

import json
import multiprocessing
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from agent_team.environments.locks import (
    DEFAULT_MAX_RUNS,
    GUARD_NAME,
    LEGACY_LOCK,
    MAX_RUNS_CEILING,
    TABLE_NAME,
    LockEntry,
    LockHeldError,
    acquire,
    is_live,
    live_entries,
    live_run_ids,
    pid_alive,
    read_table,
    release,
)


def dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def try_acquire(state_dir: str, run_id: str, path: str, start_at: float) -> str:
    time.sleep(max(0.0, start_at - time.time()))
    try:
        acquire(Path(state_dir), run_id, [path], pid=os.getppid())
    except LockHeldError:
        return "busy"
    return "ok"


def test_constants() -> None:
    assert (TABLE_NAME, GUARD_NAME, LEGACY_LOCK, DEFAULT_MAX_RUNS, MAX_RUNS_CEILING) == ("locks.json", "lock.guard", "lock", 2, 3)


def test_disjoint_paths_share_the_repo_up_to_the_limit(tmp_path: Path) -> None:
    acquire(tmp_path, "r1", ["src/a"])
    acquire(tmp_path, "r2", ["src/b"])
    with pytest.raises(LockHeldError, match=r"2 runs already hold locks in this repository \(limit 2\)"):
        acquire(tmp_path, "r3", ["docs"])
    assert acquire(tmp_path, "r3", ["docs"], max_runs=3) == []
    assert live_run_ids(tmp_path) == {"r1", "r2", "r3"}


def test_overlap_refuses_with_the_conflict(tmp_path: Path) -> None:
    acquire(tmp_path, "r1", ["src/ui/"])
    with pytest.raises(LockHeldError) as err:
        acquire(tmp_path, "r2", ["src/ui/app.py"])
    assert err.value.conflicts == ["run r1 holds src/ui, overlapping src/ui/app.py"]
    assert live_run_ids(tmp_path) == {"r1"}


def test_sibling_prefix_is_not_an_overlap(tmp_path: Path) -> None:
    acquire(tmp_path, "r1", ["src/ui"])
    acquire(tmp_path, "r2", ["src/uix"])


def test_whole_repo_lock_excludes_everything(tmp_path: Path) -> None:
    acquire(tmp_path, "r1")
    with pytest.raises(LockHeldError):
        acquire(tmp_path, "r2", ["anything"])
    release(tmp_path, "r1")
    acquire(tmp_path, "r2", ["anything"])
    with pytest.raises(LockHeldError):
        acquire(tmp_path, "r3")


def test_stale_entry_is_taken_over_and_returned(tmp_path: Path) -> None:
    acquire(tmp_path, "dead", ["src"], pid=dead_pid())
    assert live_run_ids(tmp_path) == set()
    [stale] = acquire(tmp_path, "r2", ["src"])
    assert stale.run_id == "dead" and live_run_ids(tmp_path) == {"r2"}
    assert [e.run_id for e in read_table(tmp_path)] == ["r2"]


def test_stale_entries_do_not_count_against_the_limit(tmp_path: Path) -> None:
    acquire(tmp_path, "d1", ["a"], pid=dead_pid())
    acquire(tmp_path, "d2", ["b"], pid=dead_pid())
    acquire(tmp_path, "r1", ["c"])
    acquire(tmp_path, "r2", ["d"])


def test_other_host_is_always_live() -> None:
    assert is_live(LockEntry("r", 1, "some-other-host", ("src",), ""))
    assert not is_live(LockEntry("r", dead_pid(), socket.gethostname(), ("src",), ""))
    assert pid_alive(os.getpid())


def test_live_legacy_lock_counts_as_whole_repo(tmp_path: Path) -> None:
    (tmp_path / "lock").write_text(
        json.dumps({"run_id": "old", "pid": os.getpid(), "host": socket.gethostname(), "acquired_at": ""})
    )
    with pytest.raises(LockHeldError, match="run old holds ."):
        acquire(tmp_path, "r2", ["src"])
    assert (tmp_path / "lock").exists()
    assert live_run_ids(tmp_path) == {"old"}


def test_stale_legacy_lock_is_deleted(tmp_path: Path) -> None:
    (tmp_path / "lock").write_text(
        json.dumps({"run_id": "old", "pid": dead_pid(), "host": socket.gethostname(), "acquired_at": ""})
    )
    [stale] = acquire(tmp_path, "r2", ["src"])
    marker = json.loads((tmp_path / "lock").read_text())  # the stale lock is replaced by the table marker
    assert stale.run_id == "old" and marker["table"] is True
    assert marker["host"] != socket.gethostname(), "legacy readers cannot reclaim the table marker"


def test_unreadable_state_fails_closed_not_stale(tmp_path: Path) -> None:
    # An unreadable legacy lock or table may name a live owner, so it refuses (details in test_locks_failclosed.py).
    (tmp_path / "lock").write_text("not json")
    with pytest.raises(LockHeldError):
        acquire(tmp_path, "r2", ["src"])
    assert (tmp_path / "lock").read_text() == "not json"
    (tmp_path / "lock").unlink()
    (tmp_path / TABLE_NAME).write_text("{broken")
    with pytest.raises(LockHeldError):
        acquire(tmp_path, "r1", ["src"])
    assert (tmp_path / TABLE_NAME).read_text() == "{broken"


def test_max_runs_must_be_one_to_three(tmp_path: Path) -> None:
    for bad in (0, 4):
        with pytest.raises(ValueError):
            acquire(tmp_path, "r1", max_runs=bad)
    assert not (tmp_path / TABLE_NAME).exists()


def test_release_removes_only_the_holders_entry(tmp_path: Path) -> None:
    acquire(tmp_path, "r1", ["a"])
    acquire(tmp_path, "r2", ["b"])
    assert release(tmp_path, "r3") is False
    assert release(tmp_path, "r1") is True
    assert release(tmp_path, "r1") is False
    assert live_run_ids(tmp_path) == {"r2"}
    assert [e.paths for e in live_entries(tmp_path)] == [("b",)]


def test_table_is_private_and_atomic(tmp_path: Path) -> None:
    acquire(tmp_path, "r1", ["a"])
    assert (tmp_path / TABLE_NAME).stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([GUARD_NAME, LEGACY_LOCK, TABLE_NAME])  # `lock` is the marker legacy processes honour


def test_a_live_run_cannot_lock_twice_but_a_dead_one_resumes(tmp_path: Path) -> None:
    acquire(tmp_path, "r1", ["a"])
    with pytest.raises(LockHeldError, match="run r1 holds a"):
        acquire(tmp_path, "r1", ["a"])
    acquire(tmp_path, "r2", ["b"], pid=dead_pid())
    [stale] = acquire(tmp_path, "r2", ["b"])  # the resume takes over its own killed entry
    assert stale.run_id == "r2" and len(read_table(tmp_path)) == 2


def test_concurrent_acquire_admits_exactly_one(tmp_path: Path) -> None:  # Review Focus 3
    ctx = multiprocessing.get_context("spawn")
    start_at = time.time() + 1.5
    with ctx.Pool(2) as pool:
        got = pool.starmap(
            try_acquire, [(str(tmp_path), "a", "src/x", start_at), (str(tmp_path), "b", "src", start_at)]
        )
    assert sorted(got) == ["busy", "ok"]
    assert len(live_run_ids(tmp_path)) == 1
