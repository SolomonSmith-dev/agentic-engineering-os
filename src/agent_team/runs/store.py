"""SQLite run record at `<repo>/.agent-team/runs.db` (build-order step 4).

The store is opened before any agent runs, so every run is recorded from its
first state. It refuses to create anything unless git already ignores
`.agent-team/`, and it never writes outside that directory.
"""

import secrets
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

from agent_team.runs.redact import redact_text, summarize_args
from agent_team.schemas.runs import (
    RunOutcome,
    RunRecord,
    RunState,
    StateTransition,
    ToolCallRecord,
    ToolResultStatus,
)

STATE_DIR = ".agent-team"
DB_NAME = "runs.db"

# Append-only. Each entry is one schema version; PRAGMA user_version records how
# many have been applied. Never edit a shipped entry: add a new one.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """
        CREATE TABLE runs (
            run_id        TEXT PRIMARY KEY,
            workflow      TEXT NOT NULL,
            model_class   TEXT,
            worktree_path TEXT,
            state         TEXT NOT NULL,
            started_at    TEXT NOT NULL,
            ended_at      TEXT,
            last_event_at TEXT NOT NULL,
            outcome       TEXT,
            cost_usd      REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0)
        )
        """,
        """
        CREATE TABLE transitions (
            run_id     TEXT NOT NULL REFERENCES runs(run_id),
            seq        INTEGER NOT NULL CHECK (seq >= 1),
            from_state TEXT,
            to_state   TEXT NOT NULL,
            at         TEXT NOT NULL,
            cost_usd   REAL NOT NULL CHECK (cost_usd >= 0),
            note       TEXT,
            PRIMARY KEY (run_id, seq)
        )
        """,
        """
        CREATE TABLE tool_calls (
            run_id        TEXT NOT NULL REFERENCES runs(run_id),
            seq           INTEGER NOT NULL CHECK (seq >= 1),
            state         TEXT NOT NULL,
            tool_name     TEXT NOT NULL,
            args_summary  TEXT NOT NULL,
            result_status TEXT NOT NULL,
            started_at    TEXT NOT NULL,
            duration_ms   INTEGER NOT NULL CHECK (duration_ms >= 0),
            cost_usd      REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
            PRIMARY KEY (run_id, seq)
        )
        """,
        "CREATE INDEX runs_started_at ON runs(started_at)",
    ),
)

SCHEMA_VERSION = len(MIGRATIONS)


class RunStoreError(RuntimeError):
    """The run record cannot be opened safely; the message says what to fix."""


class UnknownRunError(KeyError):
    def __str__(self) -> str:
        return f"no run {self.args[0]!r} in the run record"


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise RunStoreError("git is not on PATH; the run record lives inside a git repository")
    return subprocess.run([git, "-C", str(repo), *args], capture_output=True, text=True, check=False)


def repo_root(path: Path) -> Path:
    """The top level of the git repository containing `path`."""
    result = _git(path, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        raise RunStoreError(f"{path} is not inside a git repository")
    return Path(result.stdout.strip())


def check_state_dir_ignored(root: Path) -> None:
    """Fail unless git ignores `.agent-team/` in `root` and tracks nothing under it. Never edits git state."""
    tracked = _git(root, "ls-files", "--", STATE_DIR)
    if tracked.returncode != 0:
        raise RunStoreError(f"git ls-files failed in {root}: {tracked.stderr.strip()}")
    if tracked.stdout.strip():
        raise RunStoreError(
            f"{STATE_DIR}/ has files tracked by git in {root}; run 'git rm -r --cached {STATE_DIR}' "
            "so the run record is never committed"
        )
    result = _git(root, "check-ignore", "-q", "--no-index", f"{STATE_DIR}/{DB_NAME}")
    if result.returncode == 0:
        return
    if result.returncode == 1:
        raise RunStoreError(
            f"{STATE_DIR}/ is not gitignored in {root}; add '{STATE_DIR}/' to .gitignore "
            "so the run record is never committed"
        )
    raise RunStoreError(f"git check-ignore failed in {root}: {result.stderr.strip()}")


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migration newer than the database's user_version. Idempotent; returns the version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RunStoreError(
            f"run record schema version {current} is newer than this agent-team ({SCHEMA_VERSION}); upgrade agent-team"
        )
    for version in range(current, SCHEMA_VERSION):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version + 1}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    return SCHEMA_VERSION


def _ts(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("run record timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


class RunStore:
    """Read and append run records. Construct with `RunStore.open(path)`."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        path: Path,
        clock: Clock = _utc_now,
        redaction_patterns: Sequence[str] = (),
    ) -> None:
        self._conn = conn
        self.path = path
        self._clock = clock
        self._redaction = tuple(redaction_patterns)

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        create: bool = True,
        clock: Clock = _utc_now,
        redaction_patterns: Sequence[str] = (),
    ) -> "RunStore | None":
        """Open the run record for the repository containing `path`, migrating it on open.

        With `create=False` a missing database returns None and nothing is written.
        """
        root = repo_root(path)
        check_state_dir_ignored(root)
        db = root / STATE_DIR / DB_NAME
        if not db.exists():
            if not create:
                return None
            db.parent.mkdir(exist_ok=True)
        conn = sqlite3.connect(db, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        migrate(conn)
        return cls(conn, db, clock=clock, redaction_patterns=redaction_patterns)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "RunStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _clean(self, text: str | None) -> str | None:
        return None if text is None else redact_text(text, self._redaction)

    # Writes

    def start_run(
        self,
        workflow: str,
        *,
        model_class: str | None = None,
        worktree_path: str | None = None,
        run_id: str | None = None,
        initial_state: RunState = "INTAKE",
    ) -> RunRecord:
        """Record a new run and its first transition (into `initial_state`) in one transaction."""
        now = self._clock()
        run_id = run_id or f"{now.astimezone(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        record = RunRecord(
            run_id=run_id,
            workflow=workflow,
            model_class=model_class,
            worktree_path=worktree_path,
            state=initial_state,
            started_at=now,
            last_event_at=now,
            cost_usd=0.0,
        )
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, workflow, model_class, worktree_path, state, started_at, last_event_at, cost_usd)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (run_id, workflow, model_class, worktree_path, initial_state, _ts(now), _ts(now)),
            )
            conn.execute(
                "INSERT INTO transitions (run_id, seq, from_state, to_state, at, cost_usd) VALUES (?, 1, NULL, ?, ?, 0)",
                (run_id, initial_state, _ts(now)),
            )
        return record

    def transition(self, run_id: str, to_state: RunState, *, note: str | None = None) -> StateTransition:
        """Move a run to `to_state`, stamping the time and the cost so far."""
        now = self._clock()
        with self._tx() as conn:
            run = self._run_row(run_id)
            if run["ended_at"] is not None:
                raise RunStoreError(f"run {run_id} already ended ({run['outcome']}); start a new run or resume")
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM transitions WHERE run_id = ?", (run_id,)).fetchone()[0]
            transition = StateTransition(
                run_id=run_id,
                seq=seq,
                from_state=run["state"],
                to_state=to_state,
                at=now,
                elapsed_s=(now - _dt(run["started_at"])).total_seconds(),
                cost_usd=run["cost_usd"],
                note=self._clean(note),
            )
            conn.execute(
                "INSERT INTO transitions (run_id, seq, from_state, to_state, at, cost_usd, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, seq, run["state"], to_state, _ts(now), run["cost_usd"], transition.note),
            )
            conn.execute(
                "UPDATE runs SET state = ?, last_event_at = ? WHERE run_id = ?", (to_state, _ts(now), run_id)
            )
        return transition

    def record_tool_call(
        self,
        run_id: str,
        tool_name: str,
        args: Any,
        result_status: ToolResultStatus,
        *,
        duration_ms: int,
        cost_usd: float = 0.0,
        started_at: datetime | None = None,
    ) -> ToolCallRecord:
        """Record one tool call. `args` is redacted and truncated before it is stored."""
        now = self._clock()
        started = started_at or now
        summary = summarize_args(args, self._redaction)
        with self._tx() as conn:
            run = self._run_row(run_id)
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM tool_calls WHERE run_id = ?", (run_id,)).fetchone()[0]
            record = ToolCallRecord(
                run_id=run_id,
                seq=seq,
                state=run["state"],
                tool_name=tool_name,
                args_summary=summary,
                result_status=result_status,
                started_at=started,
                duration_ms=duration_ms,
                cost_usd=cost_usd,
            )
            conn.execute(
                "INSERT INTO tool_calls (run_id, seq, state, tool_name, args_summary, result_status, started_at, duration_ms, cost_usd)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, seq, run["state"], tool_name, summary, result_status, _ts(started), duration_ms, cost_usd),
            )
            conn.execute(
                "UPDATE runs SET cost_usd = cost_usd + ?, last_event_at = ? WHERE run_id = ?",
                (cost_usd, _ts(now), run_id),
            )
        return record

    def add_cost(self, run_id: str, cost_usd: float) -> None:
        """Add model cost that no single tool call carries (the adapter's normalized total)."""
        if cost_usd < 0:
            raise ValueError("cost_usd must be >= 0")
        with self._tx() as conn:
            self._run_row(run_id)
            conn.execute(
                "UPDATE runs SET cost_usd = cost_usd + ?, last_event_at = ? WHERE run_id = ?",
                (cost_usd, _ts(self._clock()), run_id),
            )

    def finish_run(self, run_id: str, outcome: RunOutcome) -> RunRecord:
        """Stamp the end time and outcome. The run's rows are kept whatever the outcome."""
        if outcome not in get_args(RunOutcome):
            raise ValueError(f"unknown outcome {outcome!r}")
        now = self._clock()
        with self._tx() as conn:
            run = self._run_row(run_id)
            if run["ended_at"] is not None:
                raise RunStoreError(f"run {run_id} already ended ({run['outcome']})")
            conn.execute(
                "UPDATE runs SET ended_at = ?, last_event_at = ?, outcome = ? WHERE run_id = ?",
                (_ts(now), _ts(now), outcome, run_id),
            )
        return self.get_run(run_id)

    # Reads

    def _run_row(self, run_id: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise UnknownRunError(run_id)
        return row

    @staticmethod
    def _to_run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            workflow=row["workflow"],
            model_class=row["model_class"],
            worktree_path=row["worktree_path"],
            state=row["state"],
            started_at=_dt(row["started_at"]),
            ended_at=_dt(row["ended_at"]),
            last_event_at=_dt(row["last_event_at"]),
            outcome=row["outcome"],
            cost_usd=row["cost_usd"],
        )

    def get_run(self, run_id: str) -> RunRecord:
        return self._to_run(self._run_row(run_id))

    def list_runs(self, limit: int = 20) -> list[RunRecord]:
        """Most recent first."""
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, run_id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._to_run(row) for row in rows]

    def transitions(self, run_id: str) -> list[StateTransition]:
        started = _dt(self._run_row(run_id)["started_at"])
        rows = self._conn.execute(
            "SELECT * FROM transitions WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [
            StateTransition(
                run_id=row["run_id"],
                seq=row["seq"],
                from_state=row["from_state"],
                to_state=row["to_state"],
                at=_dt(row["at"]),
                elapsed_s=(_dt(row["at"]) - started).total_seconds(),
                cost_usd=row["cost_usd"],
                note=row["note"],
            )
            for row in rows
        ]

    def tool_calls(self, run_id: str) -> list[ToolCallRecord]:
        self._run_row(run_id)
        rows = self._conn.execute("SELECT * FROM tool_calls WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
        return [
            ToolCallRecord(
                run_id=row["run_id"],
                seq=row["seq"],
                state=row["state"],
                tool_name=row["tool_name"],
                args_summary=row["args_summary"],
                result_status=row["result_status"],
                started_at=_dt(row["started_at"]),
                duration_ms=row["duration_ms"],
                cost_usd=row["cost_usd"],
            )
            for row in rows
        ]
