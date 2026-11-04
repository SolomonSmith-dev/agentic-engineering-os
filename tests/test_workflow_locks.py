"""Workflows lock their paths before the run record exists (engine plan M2 Task 7).

Nodes run through the scripted fake executor from test_orchestrator.py; no model is called.
"""

import json
import os
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from test_orchestrator import (  # noqa: F401  (fixtures are used by name)
    CANONICAL,
    FakeExecutor,
    claude_on_path,
    factory_for,
    happy,
    repo,
    runner,
)

from agent_team import cli
from agent_team.cli import app
from agent_team.environments import LockHeldError, acquire, live_run_ids, read_table
from agent_team.orchestrator.machine import start
from agent_team.orchestrator.run import _max_runs, resume_workflow, run_workflow
from agent_team.orchestrator import load_dag
from agent_team.run_agent import load_canonical
from agent_team.runs import RunStore
from agent_team.runs.store import new_run_id
from agent_team.environments.worktree import RUN_ID_RE
from agent_team.schemas.bootstrap import RepoConfig

HOLDER = "20261101-000000-aaaaaa"


def noop_factory(canonical):  # noqa: ANN001, ANN201
    return FakeExecutor(happy())


def scoped(*paths: str) -> dict:
    return {"paths": list(paths), "allow_dependency_changes": False}


def dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_new_run_id_has_the_run_record_format() -> None:
    run_id = new_run_id(datetime(2026, 11, 1, 12, 30, 5, tzinfo=UTC))
    assert RUN_ID_RE.match(run_id) and run_id.startswith("20261101-123005-")
    assert new_run_id(datetime.now(UTC)) != new_run_id(datetime.now(UTC))


def test_busy_workflow_refuses_before_the_run_record_is_touched(repo: Path) -> None:
    acquire(repo / ".agent-team", HOLDER, ["."], pid=os.getpid())
    with pytest.raises(LockHeldError, match=r"holds \."):
        run_workflow(repo, CANONICAL, "bug-fix", "Fix it.", executor_factory=noop_factory, echo=lambda _: None)
    assert RunStore.open(repo, create=False) is None
    assert live_run_ids(repo / ".agent-team") == {HOLDER}


def test_cli_busy_workflow_exits_2_and_names_the_holder(repo: Path, claude_on_path) -> None:  # noqa: ANN001
    acquire(repo / ".agent-team", HOLDER, ["."], pid=os.getpid())
    result = runner.invoke(app, ["run", "--workflow", "bug-fix", "--repo", str(repo), "--canonical", str(CANONICAL), "Fix it."])
    assert result.exit_code == 2 and f"run {HOLDER} holds ." in result.output
    assert RunStore.open(repo, create=False) is None


def test_two_workflows_on_disjoint_paths_can_hold_locks_together(repo: Path) -> None:
    acquire(repo / ".agent-team", HOLDER, ["src/a"], pid=os.getpid())
    record = run_workflow(
        repo, CANONICAL, "bug-fix", "Fix it.", executor_factory=noop_factory, scope=scoped("src/b"), echo=lambda _: None
    )
    assert record.outcome == "succeeded"
    assert live_run_ids(repo / ".agent-team") == {HOLDER}, "the run released only its own entry"


def test_overlapping_paths_are_refused(repo: Path) -> None:
    acquire(repo / ".agent-team", HOLDER, ["src/a"], pid=os.getpid())
    with pytest.raises(LockHeldError) as err:
        run_workflow(repo, CANONICAL, "bug-fix", "x", executor_factory=noop_factory, scope=scoped("src/a/b.py"), echo=lambda _: None)
    assert err.value.conflicts == [f"run {HOLDER} holds src/a, overlapping src/a/b.py"]


def test_the_run_holds_its_own_id_in_the_table_while_it_walks(repo: Path) -> None:
    seen: list[set[str]] = []

    class Spy(FakeExecutor):
        def __call__(self, node, given):  # noqa: ANN001, ANN204
            seen.append({e.run_id for e in read_table(repo / ".agent-team")})
            assert [e.paths for e in read_table(repo / ".agent-team")] == [("src/b",)]
            return super().__call__(node, given)

    record = run_workflow(
        repo, CANONICAL, "bug-fix", "x", executor_factory=lambda c: Spy(happy()), scope=scoped("src/b"), echo=lambda _: None
    )
    assert all(ids == {record.run_id} for ids in seen) and seen
    assert read_table(repo / ".agent-team") == []


def test_a_stale_takeover_becomes_the_first_note_per_entry(repo: Path) -> None:
    state_dir = repo / ".agent-team"
    acquire(state_dir, "20261101-000000-dead01", ["src"], pid=dead_pid())
    (state_dir / "lock").write_text(  # a legacy one-run lock, also stale
        json.dumps({"run_id": "20261101-000000-dead02", "pid": dead_pid(), "host": socket.gethostname(), "acquired_at": ""})
    )
    record = run_workflow(
        repo, CANONICAL, "bug-fix", "x", executor_factory=noop_factory, scope=scoped("src"), echo=lambda _: None
    )
    with RunStore.open(repo, create=False) as store:
        notes = [t.note or "" for t in store.transitions(record.run_id)]
    first = next(n for n in notes if "took over" in n)
    assert "took over a stale lock from run 20261101-000000-dead01" in first
    assert "took over a stale lock from run 20261101-000000-dead02" in first
    assert "pid" in first


def test_max_runs_comes_from_the_repo_config(repo: Path) -> None:
    assert _max_runs(repo) == 2
    state_dir = repo / ".agent-team"
    state_dir.mkdir(exist_ok=True)
    config = RepoConfig(
        default_branch="main", protected_branches=["main"], runtimes=["claude"],
        privacy_classes=["vendor-cloud"], autonomy_ceiling="A3", draft_prs=True, max_concurrent_runs=3,
    )  # fmt: skip
    (state_dir / "config.yaml").write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    assert _max_runs(repo) == 3
    (state_dir / "config.yaml").write_text("not: [valid", encoding="utf-8")
    assert _max_runs(repo) == 2, "an unreadable config falls back to the default"


def test_the_configured_limit_is_enforced(repo: Path) -> None:
    state_dir = repo / ".agent-team"
    acquire(state_dir, HOLDER, ["a"], pid=os.getpid())
    acquire(state_dir, "20261101-000000-bbbbbb", ["b"], pid=os.getpid())
    with pytest.raises(LockHeldError, match=r"limit 2"):
        run_workflow(repo, CANONICAL, "bug-fix", "x", executor_factory=noop_factory, scope=scoped("c"), echo=lambda _: None)
    assert RunStore.open(repo, create=False) is None


def test_max_concurrent_runs_is_bounded() -> None:
    from pydantic import ValidationError

    base = dict(default_branch="main", protected_branches=["main"], runtimes=["claude"],
                privacy_classes=["vendor-cloud"], autonomy_ceiling="A3", draft_prs=True)  # fmt: skip
    assert RepoConfig(**base).max_concurrent_runs == 2
    for bad in (0, 4):
        with pytest.raises(ValidationError):
            RepoConfig(**base, max_concurrent_runs=bad)


def test_resume_locks_the_whole_repo_without_a_scope(repo: Path) -> None:
    script = happy()
    script["CODE_REVIEW"] = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        run_workflow(repo, CANONICAL, "bug-fix", "x", executor_factory=factory_for(FakeExecutor(script)), echo=lambda _: None)
    with RunStore.open(repo, create=False) as store:
        [run] = store.list_runs()
    acquire(repo / ".agent-team", HOLDER, ["src"], pid=os.getpid())
    with pytest.raises(LockHeldError, match=r"holds src, overlapping \."):
        resume_workflow(repo, CANONICAL, run.run_id, executor_factory=factory_for(FakeExecutor(happy())), echo=lambda _: None)


def test_resume_locks_the_checkpoints_scope(repo: Path) -> None:
    script = happy()
    script["CODE_REVIEW"] = [KeyboardInterrupt(), happy()["CODE_REVIEW"][0]]
    scope = {"paths": ["src/b"], "allow_dependency_changes": False}
    with pytest.raises(KeyboardInterrupt):
        run_workflow(
            repo, CANONICAL, "bug-fix", "x", executor_factory=factory_for(FakeExecutor(script)),
            scope=scope, echo=lambda _: None,
        )  # fmt: skip
    with RunStore.open(repo, create=False) as store:
        [run] = store.list_runs()
    acquire(repo / ".agent-team", HOLDER, ["src/a"], pid=os.getpid())  # disjoint: the resume may proceed
    record = resume_workflow(repo, CANONICAL, run.run_id, executor_factory=factory_for(FakeExecutor(script)), echo=lambda _: None)
    assert record.outcome == "succeeded" and live_run_ids(repo / ".agent-team") == {HOLDER}


def test_start_seeds_evidence_and_takes_a_given_run_id(repo: Path) -> None:
    dag = load_dag(load_canonical(CANONICAL), "bug-fix", "default")
    with RunStore.open(repo) as store:
        run = start(store, dag, "x", run_id="20261101-000000-cccccc", seed={"scope": {"paths": ["a"]}}, echo=lambda _: None)
        assert run.run_id == "20261101-000000-cccccc"
        assert store.last_checkpoint(run.run_id).data["evidence"] == {"scope": {"paths": ["a"]}}
        other = start(store, dag, "x", echo=lambda _: None)
        assert store.last_checkpoint(other.run_id).data["evidence"] == {}


def test_unignored_state_rejection_leaves_no_runtime_files(repo: Path) -> None:
    from agent_team.runs import RunStoreError

    (repo / ".gitignore").write_text("")
    with pytest.raises(RunStoreError, match="not gitignored"):
        run_workflow(repo, CANONICAL, "bug-fix", "x", executor_factory=noop_factory, echo=lambda _: None)
    assert not (repo / ".agent-team").exists()
