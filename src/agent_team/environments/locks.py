"""Path locks: `.agent-team/locks.json` holds one entry per live run with the paths it may write.

Two runs may share a repository when their paths do not overlap (equal or nested
prefixes overlap; `.` is the whole repo) and fewer than `max_runs` runs are live.
An entry whose PID is dead (on this host) is stale: the next acquire takes it
over and returns it so the caller can log the takeover in the run record. An
entry from another host is always live, since its PID cannot be checked here.
A live legacy `.agent-team/lock` file (the old one-run-per-repo lock) counts as a
whole-repo entry; a stale one is deleted.

Acquire and release run under an flock on `.agent-team/lock.guard`, so two
processes can never both judge the table and both be admitted. The table is
replaced atomically (temp file, `os.replace`, mode 0600).
"""

import fcntl
import json
import os
import socket
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_team.tasks.paths import WHOLE_REPO, normalize, overlaps

TABLE_NAME = "locks.json"
GUARD_NAME = "lock.guard"
LEGACY_LOCK = "lock"
DEFAULT_MAX_RUNS = 2
MAX_RUNS_CEILING = 3


class LockHeldError(RuntimeError):
    """The paths are held by a live run, or the repository is at its concurrent-run limit."""

    def __init__(self, message: str, conflicts: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.conflicts = list(conflicts)


@dataclass(frozen=True)
class LockEntry:
    run_id: str
    pid: int
    host: str
    paths: tuple[str, ...]
    acquired_at: str


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def is_live(entry: LockEntry) -> bool:
    if entry.host != socket.gethostname():
        return True
    return entry.pid > 0 and pid_alive(entry.pid)


def _entry(data: Any) -> LockEntry:
    paths = tuple(str(p) for p in data["paths"])
    return LockEntry(str(data["run_id"]), int(data["pid"]), str(data["host"]), paths or (WHOLE_REPO,), str(data["acquired_at"]))


def read_table(state_dir: Path) -> list[LockEntry]:
    """Every entry in the table, live or stale. An unreadable table is empty; an unreadable entry is stale."""
    try:
        raw = json.loads((state_dir / TABLE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    entries = []
    for item in raw:
        try:
            entries.append(_entry(item))
        except (KeyError, TypeError, ValueError):
            entries.append(LockEntry("<unreadable>", 0, socket.gethostname(), (WHOLE_REPO,), ""))
    return entries


def _legacy(state_dir: Path) -> LockEntry | None:
    path = state_dir / LEGACY_LOCK
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LockEntry(str(data["run_id"]), int(data["pid"]), str(data["host"]), (WHOLE_REPO,), str(data["acquired_at"]))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return LockEntry("<unreadable>", 0, socket.gethostname(), (WHOLE_REPO,), "")


def live_entries(state_dir: Path) -> list[LockEntry]:
    legacy = _legacy(state_dir)
    found = ([legacy] if legacy is not None else []) + read_table(state_dir)
    return [e for e in found if is_live(e)]


def live_run_ids(state_dir: Path) -> set[str]:
    return {e.run_id for e in live_entries(state_dir)}


@contextmanager
def _guard(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(exist_ok=True)
    fd = os.open(state_dir / GUARD_NAME, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the flock


def _write(state_dir: Path, entries: Sequence[LockEntry]) -> None:
    path = state_dir / TABLE_NAME
    tmp = path.with_name(f"{TABLE_NAME}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump([{**asdict(e), "paths": list(e.paths)} for e in entries], handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def acquire(
    state_dir: Path,
    run_id: str,
    paths: Sequence[str] = (WHOLE_REPO,),
    *,
    max_runs: int = DEFAULT_MAX_RUNS,
    pid: int | None = None,
) -> list[LockEntry]:
    """Take the lock on `paths` for `run_id`. Returns the stale entries it dropped.

    Raises LockHeldError when a live run's paths overlap ours or the run limit is reached,
    ValueError when `max_runs` is outside 1..MAX_RUNS_CEILING. Nothing is written on a refusal.
    """
    if not 1 <= max_runs <= MAX_RUNS_CEILING:
        raise ValueError(f"max_runs must be between 1 and {MAX_RUNS_CEILING}, got {max_runs}")
    mine = tuple(dict.fromkeys(normalize(p) for p in paths)) or (WHOLE_REPO,)
    entry = LockEntry(
        run_id=run_id,
        pid=pid if pid is not None else os.getpid(),
        host=socket.gethostname(),
        paths=mine,
        acquired_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    with _guard(state_dir):
        legacy = _legacy(state_dir)
        table = read_table(state_dir)
        stale = [e for e in table if not is_live(e)]
        live = [e for e in table if is_live(e)]
        if legacy is not None:
            if is_live(legacy):
                live.insert(0, legacy)
            else:
                stale.insert(0, legacy)
        conflicts = [
            f"run {other.run_id} holds {theirs}, overlapping {ours}"
            for other in live
            for theirs in other.paths
            for ours in mine
            if overlaps(theirs, ours)
        ]
        if conflicts:
            raise LockHeldError("; ".join(dict.fromkeys(conflicts)), list(dict.fromkeys(conflicts)))
        if len(live) >= max_runs:
            raise LockHeldError(f"{len(live)} runs already hold locks in this repository (limit {max_runs})")
        if legacy is not None and not is_live(legacy):
            (state_dir / LEGACY_LOCK).unlink(missing_ok=True)
        _write(state_dir, [e for e in live if e is not legacy] + [entry])
    return stale


def release(state_dir: Path, run_id: str) -> bool:
    """Remove `run_id`'s entry. Returns whether one was removed."""
    if not (state_dir / TABLE_NAME).exists():
        return False
    with _guard(state_dir):
        table = read_table(state_dir)
        kept = [e for e in table if e.run_id != run_id]
        if len(kept) == len(table):
            return False
        _write(state_dir, kept)
        return True
