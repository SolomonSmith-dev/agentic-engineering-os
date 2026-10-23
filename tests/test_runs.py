"""Run record (build-order step 4): round-trip, migrations, gitignore guard, redaction, trace.

Every test works in a throwaway git repository under tmp_path with an injected
clock, so output is deterministic. Regenerate the trace fixtures after an
intended output change with `UPDATE_SNAPSHOTS=1 uv run pytest tests/test_runs.py`.
"""

import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_team.cli import app
from agent_team.runs import REDACTED, SCHEMA_VERSION, RunStore, RunStoreError, UnknownRunError, migrate
from agent_team.runs.redact import MAX_SUMMARY_CHARS, summarize_args
from agent_team.schemas import RunRecord, StateTransition, ToolCallRecord

FIXTURES = Path(__file__).parent / "fixtures" / "trace"
runner = CliRunner()

T0 = datetime(2026, 10, 23, 16, 0, 0, tzinfo=UTC)
RUN_A = "20261023-160000-a1b2c3"
RUN_B = "20261023-170000-d4e5f6"

SECRET_ANTHROPIC = "sk-ant-api03-RUNRECORD-LEAK-CANARY-0000"
SECRET_GH = "ghp_RUNRECORDLEAKCANARY1111"
SECRET_PASSWORD = "hunter2-RUNRECORD-CANARY-2222"
SECRET_ENV = "RUNRECORD-ENV-CANARY-3333"
SECRET_POLICY = "INTERNAL-PROJECT-CODENAME-4444"
CANARIES = (SECRET_ANTHROPIC, SECRET_GH, SECRET_PASSWORD, SECRET_ENV, SECRET_POLICY)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text(".agent-team/\n", encoding="utf-8")
    return root


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def open_store(repo: Path, clock: FakeClock, **kwargs) -> RunStore:
    store = RunStore.open(repo, clock=clock, **kwargs)
    assert store is not None
    return store


def populate(store: RunStore, clock: FakeClock) -> None:
    """Two runs: a finished bug-fix with tool calls, and one still running."""
    store.start_run("bug-fix", model_class="strong-coding", worktree_path=".agent-team/worktrees/a1b2c3", run_id=RUN_A)
    clock.tick(12)
    store.transition(RUN_A, "DISCOVERY")
    clock.tick(3)
    store.record_tool_call(RUN_A, "Bash", {"command": "uv run pytest -q tests/test_x.py"}, "error", duration_ms=2140, cost_usd=0.0123)
    clock.tick(40)
    store.transition(RUN_A, "PLAN", note="reproduction recorded")
    clock.tick(5)
    store.record_tool_call(RUN_A, "Read", {"file_path": "src/x.py"}, "ok", duration_ms=12, cost_usd=0.0021)
    store.add_cost(RUN_A, 0.05)
    clock.tick(65)
    store.transition(RUN_A, "IMPLEMENTATION")
    clock.tick(600)
    store.record_tool_call(RUN_A, "Edit", {"file_path": "src/x.py", "old_string": "a", "new_string": "b"}, "ok", duration_ms=30, cost_usd=0.21)
    clock.tick(15)
    store.transition(RUN_A, "TEST")
    clock.tick(90)
    store.transition(RUN_A, "CODE_REVIEW")
    clock.tick(240)
    store.transition(RUN_A, "PR")
    clock.tick(10)
    store.finish_run(RUN_A, "succeeded")

    clock.now = T0 + timedelta(hours=1)
    store.start_run("bug-fix", model_class="strong-coding", run_id=RUN_B)
    clock.tick(30)
    store.transition(RUN_B, "DISCOVERY")


def files_outside_state_dir(root: Path) -> dict[str, float]:
    return {
        str(p.relative_to(root)): p.stat().st_mtime_ns
        for p in root.rglob("*")
        if p.is_file() and ".agent-team" not in p.relative_to(root).parts
    }


# Round-trip


def test_records_round_trip(repo: Path, clock: FakeClock) -> None:
    store = open_store(repo, clock)
    started = store.start_run("bug-fix", model_class="strong-coding", worktree_path="wt/1", run_id=RUN_A)
    clock.tick(12.5)
    moved = store.transition(RUN_A, "DISCOVERY", note="looking")
    clock.tick(1)
    call = store.record_tool_call(RUN_A, "Bash", {"command": "ls"}, "ok", duration_ms=40, cost_usd=0.25)
    clock.tick(2)
    finished = store.finish_run(RUN_A, "failed")
    store.close()

    reopened = open_store(repo, clock)
    run = reopened.get_run(RUN_A)
    assert isinstance(run, RunRecord)
    assert run == finished
    assert run.started_at == started.started_at == T0
    assert run.state == "DISCOVERY"
    assert run.outcome == "failed"
    assert run.cost_usd == pytest.approx(0.25)
    assert run.elapsed_s == pytest.approx(15.5)
    assert run.model_class == "strong-coding" and run.worktree_path == "wt/1"

    transitions = reopened.transitions(RUN_A)
    assert all(isinstance(t, StateTransition) for t in transitions)
    assert [(t.seq, t.from_state, t.to_state) for t in transitions] == [(1, None, "INTAKE"), (2, "INTAKE", "DISCOVERY")]
    assert transitions[1] == moved
    assert transitions[1].elapsed_s == pytest.approx(12.5)

    calls = reopened.tool_calls(RUN_A)
    assert calls == [call]
    assert isinstance(calls[0], ToolCallRecord)
    assert calls[0].state == "DISCOVERY" and calls[0].duration_ms == 40


def test_cost_so_far_is_stamped_on_each_transition(repo: Path, clock: FakeClock) -> None:
    store = open_store(repo, clock)
    populate(store, clock)
    costs = {t.to_state: t.cost_usd for t in store.transitions(RUN_A)}
    assert costs["INTAKE"] == 0
    assert costs["PLAN"] == pytest.approx(0.0123)
    assert costs["IMPLEMENTATION"] == pytest.approx(0.0123 + 0.0021 + 0.05)
    assert costs["TEST"] == pytest.approx(0.0123 + 0.0021 + 0.05 + 0.21)


def test_list_runs_most_recent_first(repo: Path, clock: FakeClock) -> None:
    store = open_store(repo, clock)
    populate(store, clock)
    assert [r.run_id for r in store.list_runs()] == [RUN_B, RUN_A]
    assert [r.run_id for r in store.list_runs(limit=1)] == [RUN_B]


def test_generated_run_ids_are_valid_and_unique(repo: Path, clock: FakeClock) -> None:
    store = open_store(repo, clock)
    ids = {store.start_run("bug-fix").run_id for _ in range(5)}
    assert len(ids) == 5
    assert all(i.startswith("20261023-160000-") for i in ids)


def test_ended_run_rejects_more_transitions(repo: Path, clock: FakeClock) -> None:
    store = open_store(repo, clock)
    store.start_run("bug-fix", run_id=RUN_A)
    store.finish_run(RUN_A, "abandoned")
    with pytest.raises(RunStoreError, match="already ended"):
        store.transition(RUN_A, "PLAN")
    with pytest.raises(RunStoreError, match="already ended"):
        store.finish_run(RUN_A, "succeeded")


def test_invalid_state_and_unknown_run_write_nothing(repo: Path, clock: FakeClock) -> None:
    store = open_store(repo, clock)
    store.start_run("bug-fix", run_id=RUN_A)
    with pytest.raises(ValueError):
        store.transition(RUN_A, "NOT_A_STATE")  # type: ignore[arg-type]
    with pytest.raises(UnknownRunError):
        store.transition(RUN_B, "PLAN")
    with pytest.raises(ValueError):
        store.finish_run(RUN_A, "exploded")  # type: ignore[arg-type]
    assert len(store.transitions(RUN_A)) == 1
    assert store.get_run(RUN_A).state == "INTAKE"


# Migrations


def schema_snapshot(db: Path) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    finally:
        conn.close()


def test_migrations_are_idempotent(repo: Path, clock: FakeClock) -> None:
    store = open_store(repo, clock)
    populate(store, clock)
    db = store.path
    store.close()
    before = schema_snapshot(db)

    for _ in range(3):
        open_store(repo, clock).close()
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert migrate(conn) == SCHEMA_VERSION
    conn.close()

    assert schema_snapshot(db) == before
    with open_store(repo, clock) as again:
        assert {r.run_id for r in again.list_runs()} == {RUN_A, RUN_B}
        assert len(again.transitions(RUN_A)) == 7


def test_fresh_database_gets_every_table(repo: Path, clock: FakeClock) -> None:
    store = open_store(repo, clock)
    names = {row[1] for row in schema_snapshot(store.path) if row[0] == "table"}
    assert names == {"runs", "transitions", "tool_calls"}


def test_newer_schema_version_is_refused(repo: Path, clock: FakeClock) -> None:
    open_store(repo, clock).close()
    db = repo / ".agent-team" / "runs.db"
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RunStoreError, match="newer than this agent-team"):
        RunStore.open(repo, clock=clock)


# Where the store may write


def test_refuses_when_state_dir_not_gitignored(tmp_path: Path, clock: FakeClock) -> None:
    root = tmp_path / "unignored"
    root.mkdir()
    git(root, "init", "-q")
    with pytest.raises(RunStoreError, match="not gitignored"):
        RunStore.open(root, clock=clock)
    assert not (root / ".agent-team").exists()


def test_refuses_outside_a_git_repository(tmp_path: Path, clock: FakeClock) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    env = {"GIT_CEILING_DIRECTORIES": str(tmp_path)}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        with pytest.raises(RunStoreError, match="not inside a git repository"):
            RunStore.open(plain, clock=clock)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert not (plain / ".agent-team").exists()


def test_opens_at_repo_root_from_a_subdirectory(repo: Path, clock: FakeClock) -> None:
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    store = open_store(sub, clock)
    assert store.path.resolve() == (repo / ".agent-team" / "runs.db").resolve()
    assert not (sub / ".agent-team").exists()


def test_nothing_written_outside_state_dir(repo: Path, clock: FakeClock) -> None:
    before = files_outside_state_dir(repo)
    store = open_store(repo, clock)
    populate(store, clock)
    store.close()
    runner.invoke(app, ["trace", "--repo", str(repo)])
    runner.invoke(app, ["trace", RUN_A, "--calls", "--repo", str(repo)])
    assert files_outside_state_dir(repo) == before
    assert {p.name for p in (repo / ".agent-team").iterdir()} <= {"runs.db", "runs.db-journal"}


def test_open_without_create_writes_nothing(repo: Path, clock: FakeClock) -> None:
    assert RunStore.open(repo, create=False, clock=clock) is None
    assert not (repo / ".agent-team").exists()


# Redaction


def test_secret_looking_arguments_are_stored_redacted(repo: Path, clock: FakeClock) -> None:
    store = open_store(repo, clock, redaction_patterns=[r"INTERNAL-PROJECT-CODENAME-\d+"])
    store.start_run("bug-fix", run_id=RUN_A)
    args = {
        "command": f"curl -H 'Authorization: Bearer {SECRET_ANTHROPIC}' https://api.example.invalid",
        "env": {"GH_TOKEN": SECRET_GH, "PATH": "/usr/bin"},
        "password": SECRET_PASSWORD,
        "script": f"export DEPLOY_API_KEY={SECRET_ENV} && make deploy",
        "notes": [f"codename {SECRET_POLICY}", "keep me"],
    }
    call = store.record_tool_call(RUN_A, "Bash", args, "ok", duration_ms=5)
    store.transition(RUN_A, "PLAN", note=f"used token {SECRET_GH}")
    store.close()

    for canary in CANARIES:
        assert canary not in call.args_summary
    assert REDACTED in call.args_summary
    assert "/usr/bin" in call.args_summary and "keep me" in call.args_summary and "make deploy" in call.args_summary

    with open_store(repo, clock) as again:
        stored = again.tool_calls(RUN_A)[0].args_summary
        assert stored == call.args_summary
        assert SECRET_GH not in (again.transitions(RUN_A)[-1].note or "")

    raw = b"".join(p.read_bytes() for p in (repo / ".agent-team").iterdir())
    for canary in CANARIES:
        assert canary.encode() not in raw, f"{canary} reached the database file"


@pytest.mark.parametrize(
    "args",
    [
        {"api_key": "plain-value-without-shape"},
        {"headers": {"Cookie": "session=abc123def"}},
        "--password=letmein-canary",
        "postgres://admin:s3cr3t-canary@db.internal:5432/app",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\n-----END OPENSSH PRIVATE KEY-----",
        {"aws": "AKIAABCDEFGHIJKLMNOP"},
    ],
)
def test_redaction_shapes(args: object) -> None:
    summary = summarize_args(args)
    assert REDACTED in summary
    for fragment in ("plain-value-without-shape", "abc123def", "letmein-canary", "s3cr3t-canary", "b3BlbnNzaC1rZXktdjEAAAA", "AKIAABCDEFGHIJKLMNOP"):
        assert fragment not in summary


def test_summary_is_truncated_after_redaction() -> None:
    args = {"command": "x" * 1000 + f" GH_TOKEN={SECRET_GH}"}
    summary = summarize_args(args)
    assert len(summary) <= MAX_SUMMARY_CHARS
    assert summary.endswith("...")
    long_secret_first = summarize_args({"command": f"GH_TOKEN={SECRET_GH} " + "y" * 1000})
    assert SECRET_GH not in long_secret_first and "ghp_" not in long_secret_first


# trace


def check_snapshot(name: str, output: str) -> None:
    fixture = FIXTURES / name
    if os.environ.get("UPDATE_SNAPSHOTS"):
        fixture.write_text(output, encoding="utf-8")
    assert output == fixture.read_text(encoding="utf-8")


@pytest.fixture
def populated(repo: Path, clock: FakeClock) -> Path:
    store = open_store(repo, clock)
    populate(store, clock)
    store.close()
    return repo


def test_trace_run_matches_fixture(populated: Path) -> None:
    result = runner.invoke(app, ["trace", RUN_A, "--repo", str(populated)])
    assert result.exit_code == 0, result.output
    check_snapshot("run.txt", result.stdout)


def test_trace_run_with_calls_matches_fixture(populated: Path) -> None:
    result = runner.invoke(app, ["trace", RUN_A, "--calls", "--repo", str(populated)])
    assert result.exit_code == 0, result.output
    check_snapshot("run_calls.txt", result.stdout)


def test_trace_running_run_matches_fixture(populated: Path) -> None:
    result = runner.invoke(app, ["trace", RUN_B, "--repo", str(populated)])
    assert result.exit_code == 0, result.output
    check_snapshot("running.txt", result.stdout)


def test_trace_list_matches_fixture(populated: Path) -> None:
    result = runner.invoke(app, ["trace", "--repo", str(populated)])
    assert result.exit_code == 0, result.output
    check_snapshot("list.txt", result.stdout)


def test_trace_with_no_record_writes_nothing(repo: Path) -> None:
    result = runner.invoke(app, ["trace", "--repo", str(repo)])
    assert result.exit_code == 0
    assert result.stdout.strip() == "no runs recorded"
    assert not (repo / ".agent-team").exists()


def test_trace_unknown_run_exits_1(populated: Path) -> None:
    result = runner.invoke(app, ["trace", "20991231-000000-ffffff", "--repo", str(populated)])
    assert result.exit_code == 1
    assert "no run '20991231-000000-ffffff'" in result.stderr


def test_trace_refuses_unignored_repo(tmp_path: Path) -> None:
    root = tmp_path / "unignored"
    root.mkdir()
    git(root, "init", "-q")
    result = runner.invoke(app, ["trace", "--repo", str(root)])
    assert result.exit_code == 2
    assert "not gitignored" in result.stderr


# Security review follow-ups


@pytest.mark.parametrize(
    "text",
    [
        '{"password": "two words-canary"}',
        '{\\"api_key\\":\\"escaped-canary\\"}',
        "secret = 'quoted spaced canary'",
        "--token \"flag-canary\"",
    ],
)
def test_redaction_inside_serialized_strings(text: str) -> None:
    summary = summarize_args({"payload": text})
    assert REDACTED in summary
    for fragment in ("two words-canary", "escaped-canary", "quoted spaced canary", "flag-canary"):
        assert fragment not in summary


def test_refuses_when_state_dir_is_tracked(repo: Path, clock: FakeClock) -> None:
    state = repo / ".agent-team"
    state.mkdir()
    (state / "runs.db").write_bytes(b"")
    git(repo, "add", "-f", ".agent-team/runs.db")
    with pytest.raises(RunStoreError, match="tracked by git"):
        RunStore.open(repo, clock=clock)


def test_trace_escapes_control_characters(repo: Path, clock: FakeClock) -> None:
    store = open_store(repo, clock)
    store.start_run("bug-fix\x1b[2J", worktree_path="wt/\x1b]0;pwned\x07", run_id=RUN_A)
    store.record_tool_call(RUN_A, "Bash\x1b[31m", {"command": "echo \u009b31m"}, "ok", duration_ms=1)
    store.transition(RUN_A, "PLAN", note="clear\x1b[2Jscreen\rover")
    store.close()
    for argv in (["trace", "--repo", str(repo)], ["trace", RUN_A, "--calls", "--repo", str(repo)]):
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, result.output
        assert not any(ch in result.stdout for ch in ("\x1b", "\x07", "\r", "\u009b"))
    assert "\\x1b[2J" in result.stdout
