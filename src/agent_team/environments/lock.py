"""One run per repository: `.agent-team/lock` holds the live run's PID.

A lock whose PID is dead (on this host) is taken over, and the caller gets the
previous holder back so it can log the takeover in the run record. A lock held
from another host is never taken over: its PID cannot be checked from here.
Acquire and release run under an flock on `.agent-team/lock.guard`, so two
processes can never both judge the same lock stale and both take it.
"""

import fcntl
import json
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

LOCK_NAME = "lock"
GUARD_NAME = "lock.guard"


class LockHeldError(RuntimeError):
    """Another live run holds the repository lock."""


@dataclass(frozen=True)
class LockInfo:
    run_id: str
    pid: int
    host: str
    acquired_at: str


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _read(path: Path) -> LockInfo | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LockInfo(run_id=str(data["run_id"]), pid=int(data["pid"]), host=str(data["host"]), acquired_at=str(data["acquired_at"]))
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        # Unreadable lock: treat as stale rather than wedging the repository forever.
        return LockInfo(run_id="<unreadable>", pid=0, host=socket.gethostname(), acquired_at="")


def is_live(info: LockInfo) -> bool:
    if info.host != socket.gethostname():
        return True
    return info.pid > 0 and pid_alive(info.pid)


def read_lock(state_dir: Path) -> LockInfo | None:
    """The current holder, live or stale, or None when unlocked."""
    return _read(state_dir / LOCK_NAME)


def live_run_id(state_dir: Path) -> str | None:
    info = read_lock(state_dir)
    return info.run_id if info is not None and is_live(info) else None


@contextmanager
def _guard(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(exist_ok=True)
    fd = os.open(state_dir / GUARD_NAME, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the flock


def acquire(state_dir: Path, run_id: str, *, pid: int | None = None) -> LockInfo | None:
    """Take the lock for `run_id`. Returns the stale holder it replaced, or None.

    Raises LockHeldError when a live run holds it.
    """
    path = state_dir / LOCK_NAME
    mine = LockInfo(
        run_id=run_id,
        pid=pid if pid is not None else os.getpid(),
        host=socket.gethostname(),
        acquired_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    with _guard(state_dir):
        replaced = _read(path)
        if replaced is not None:
            if is_live(replaced):
                raise LockHeldError(
                    f"run {replaced.run_id} (pid {replaced.pid} on {replaced.host}) holds {path}; "
                    "one run per repository"
                )
            path.unlink()
        tmp = path.with_name(f"{LOCK_NAME}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(asdict(mine)), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    return replaced


def release(state_dir: Path, run_id: str) -> bool:
    """Remove the lock if `run_id` holds it. Returns whether it was removed."""
    path = state_dir / LOCK_NAME
    if not path.exists():
        return False
    with _guard(state_dir):
        holder = _read(path)
        if holder is None or holder.run_id != run_id:
            return False
        path.unlink(missing_ok=True)
        return True
