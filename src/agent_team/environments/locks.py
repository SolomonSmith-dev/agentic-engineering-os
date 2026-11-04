"""Path locks: `.agent-team/locks.json` holds one entry per live run with the paths it may write.

Two runs may share a repository when their paths do not overlap (equal or nested
prefixes overlap; `.` is the whole repo) and fewer than `max_runs` runs are live.
An entry whose PID is dead (on this host) is stale: the next acquire takes it
over and returns it so the caller can log the takeover in the run record. An
entry from another host is always live, since its PID cannot be checked here.
A live legacy `.agent-team/lock` file (the old one-run-per-repo lock) counts as a
whole-repo entry; a stale one is deleted.

State that exists but cannot be read or parsed (the table, an entry in it, or the
legacy lock) fails closed: acquire, release and live_run_ids raise LockStateError
and write nothing, because the unreadable part may name a live owner. Only a
missing file means unlocked.

Mixed versions: a process that knows only the legacy lock reads `lock` alone. While
the table has a live entry, acquire and release keep a marker `lock` file (flagged
`"table": true`) with a synthetic foreign host. Legacy readers never take over a
foreign-host lock, even if a table holder dies. Only a table-aware process can
remove this marker after observing no live entries. This module ignores the marker
when judging conflicts (the table is authoritative). After a crash, use this version
to recover stale table entries before downgrading to a legacy version.

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
_ABSENT = object()
DEFAULT_MAX_RUNS = 2
MAX_RUNS_CEILING = 3


class LockHeldError(RuntimeError):
    """The paths are held by a live run, or the repository is at its concurrent-run limit."""

    def __init__(self, message: str, conflicts: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.conflicts = list(conflicts)


class LockStateError(LockHeldError):
    """Existing lock state cannot be read or parsed; it may name a live owner, so nothing proceeds."""


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
    if not isinstance(data, dict):
        raise ValueError("lock entry must be an object")
    if any(not isinstance(data[name], str) for name in ("run_id", "host", "acquired_at")):
        raise ValueError("lock identity fields must be strings")
    if not data["run_id"] or not data["host"] or type(data["pid"]) is not int or data["pid"] <= 0:
        raise ValueError("lock identity must name a run, host and positive pid")
    if not isinstance(data["paths"], list) or any(not isinstance(path, str) for path in data["paths"]):
        raise ValueError("lock paths must be a list of strings")
    paths = tuple(normalize(path) for path in data["paths"])
    return LockEntry(data["run_id"], data["pid"], data["host"], paths or (WHOLE_REPO,), data["acquired_at"])


def read_table(state_dir: Path) -> list[LockEntry]:
    """Every entry in the table, live or stale. A missing table is empty; anything else unreadable raises LockStateError."""
    path = state_dir / TABLE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise LockStateError(f"{path} cannot be read ({type(exc).__name__}); refusing to guess who holds locks") from exc
    if not isinstance(raw, list):
        raise LockStateError(f"{path} is not a list of lock entries; refusing to guess who holds locks")
    entries = []
    for index, item in enumerate(raw):
        try:
            entries.append(_entry(item))
        except (KeyError, TypeError, ValueError) as exc:
            raise LockStateError(f"{path} entry {index} is malformed; refusing to guess who holds locks") from exc
    return entries


def _legacy_data(state_dir: Path) -> Any:
    """The parsed `lock` file, _ABSENT when missing; unreadable raises LockStateError."""
    path = state_dir / LEGACY_LOCK
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _ABSENT
    except (OSError, ValueError) as exc:
        raise LockStateError(f"{path} cannot be read ({type(exc).__name__}); refusing to guess who holds the lock") from exc


def _is_marker(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and data.get("table") is True
        and data.get("run_id") == "<path-lock-table>"
        and type(data.get("pid")) is int
        and data["pid"] == 0
        and data.get("host") == f"{socket.gethostname()}:path-lock-table"
        and isinstance(data.get("acquired_at"), str)
    )


def _legacy(state_dir: Path) -> LockEntry | None:
    """A real legacy one-run lock as a whole-repo entry; None when absent or when it is our own table marker."""
    data = _legacy_data(state_dir)
    if data is _ABSENT or _is_marker(data):
        return None
    try:
        return _entry({**data, "paths": [WHOLE_REPO]})
    except (KeyError, TypeError, ValueError) as exc:
        raise LockStateError(f"{state_dir / LEGACY_LOCK} is malformed; refusing to guess who holds the lock") from exc


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


def _sync_marker(state_dir: Path, entries: Sequence[LockEntry]) -> None:
    """Keep the legacy-readable `lock` file in step with the table; never touches a real legacy lock."""
    path = state_dir / LEGACY_LOCK
    existing = _legacy_data(state_dir)
    if existing is not _ABSENT and not _is_marker(existing):
        return
    holders = [e for e in entries if is_live(e)]
    if not holders:
        path.unlink(missing_ok=True)
        return
    holder = holders[0]
    tmp = path.with_name(f"{LEGACY_LOCK}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "run_id": "<path-lock-table>",
                "pid": 0,
                "host": f"{socket.gethostname()}:path-lock-table",
                "acquired_at": holder.acquired_at,
                "table": True,
            },
            handle,
        )
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
        kept = [e for e in live if e is not legacy] + [entry]
        # Publish legacy exclusion first: a crash must never expose a live table
        # without its marker when the shared guard is released by process death.
        _sync_marker(state_dir, kept)
        _write(state_dir, kept)
    return stale


def release(state_dir: Path, run_id: str) -> bool:
    """Remove `run_id`'s entry. Returns whether one was removed."""
    if not (state_dir / TABLE_NAME).exists():
        return False
    with _guard(state_dir):
        table = read_table(state_dir)
        _legacy(state_dir)  # validate legacy structure before anything is written
        kept = [e for e in table if e.run_id != run_id]
        if len(kept) == len(table):
            return False
        _write(state_dir, kept)
        _sync_marker(state_dir, kept)
        return True
