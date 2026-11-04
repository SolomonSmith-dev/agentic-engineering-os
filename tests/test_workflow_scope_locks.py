"""The lock paths of a workflow run are its checkpointed scope, nothing else (engine plan M2 Task 9)."""

import inspect
import os
from pathlib import Path

import pytest
from test_orchestrator import (  # noqa: F401  (fixtures are used by name)
    CANONICAL,
    FakeExecutor,
    claude_on_path,
    factory_for,
    happy,
    repo,
    runner,
)
from test_workflow_locks import HOLDER, scoped

from agent_team.environments import LockHeldError, acquire, read_table, release
from agent_team.orchestrator.run import resume_workflow, run_workflow
from agent_team.runs import RunStore


def test_run_workflow_has_no_separate_paths_keyword() -> None:
    assert "paths" not in inspect.signature(run_workflow).parameters


def test_lock_paths_are_exactly_the_scope_the_executor_receives(repo: Path) -> None:
    state = repo / ".agent-team"
    seen: list[tuple] = []

    class Spy(FakeExecutor):
        def __call__(self, node, given):  # noqa: ANN001, ANN204
            [held] = read_table(state)
            seen.append((held.paths, given.inputs.get("scope")))
            with pytest.raises(LockHeldError):  # an overlapping run is excluded while this one walks
                acquire(state, HOLDER, ["src/b/deep"], pid=os.getpid())
            acquire(state, HOLDER, ["src/other"], pid=os.getpid())  # a disjoint one is not
            release(state, HOLDER)
            return super().__call__(node, given)

    run_workflow(repo, CANONICAL, "bug-fix", "x", executor_factory=lambda c: Spy(happy()), scope=scoped("src/b"), echo=lambda _: None)
    assert seen and all(paths == ("src/b",) for paths, _ in seen)
    assert any(scope and scope["paths"] == ["src/b"] for _, scope in seen)


def test_a_scopeless_run_locks_the_whole_repo_and_runs_unscoped(repo: Path) -> None:
    state = repo / ".agent-team"
    seen: list[tuple] = []

    class Spy(FakeExecutor):
        def __call__(self, node, given):  # noqa: ANN001, ANN204
            [held] = read_table(state)
            seen.append((held.paths, (given.inputs.get("scope") or {}).get("paths")))
            with pytest.raises(LockHeldError):
                acquire(state, HOLDER, ["src/a"], pid=os.getpid())
            return super().__call__(node, given)

    run_workflow(repo, CANONICAL, "bug-fix", "x", executor_factory=lambda c: Spy(happy()), echo=lambda _: None)
    assert seen and all(paths == (".",) and scope_paths is None for paths, scope_paths in seen)


def test_resume_holds_the_same_paths_the_scope_declares(repo: Path) -> None:
    state = repo / ".agent-team"
    script = happy()
    script["CODE_REVIEW"] = [KeyboardInterrupt(), happy()["CODE_REVIEW"][0]]
    with pytest.raises(KeyboardInterrupt):
        run_workflow(repo, CANONICAL, "bug-fix", "x", executor_factory=factory_for(FakeExecutor(script)), scope=scoped("src/b"), echo=lambda _: None)
    with RunStore.open(repo, create=False) as store:
        [run] = store.list_runs()
    held: list[tuple] = []

    class Spy(FakeExecutor):
        def __call__(self, node, given):  # noqa: ANN001, ANN204
            held.append(tuple(e.paths for e in read_table(state)))
            return super().__call__(node, given)

    resume_workflow(repo, CANONICAL, run.run_id, executor_factory=lambda c: Spy(script), echo=lambda _: None)
    assert held and all(h == (("src/b",),) for h in held)
