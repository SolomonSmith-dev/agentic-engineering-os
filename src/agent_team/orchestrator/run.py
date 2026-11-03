"""`agent-team run --workflow` and `agent-team resume`: load, check, lock, walk (build-order step 10).

    load canonical -> build DAG (policy's required gates) -> executor can run every node?
        -> start_run / RESUME -> lock -> walk -> release

Everything that can refuse does so before the run record is touched, the path lock included:
the lock is taken under the run id the record is then started with.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from agent_team.bootstrap.configure import CONFIG_NAME
from agent_team.environments import LockEntry, acquire, release
from agent_team.environments.locks import DEFAULT_MAX_RUNS
from agent_team.orchestrator.dag import Dag, OrchestratorError, build_dag
from agent_team.orchestrator.executors import DefaultExecutor
from agent_team.orchestrator.machine import Echo, Executor, reenter, start, walk
from agent_team.run_agent import Canonical, RunAgentError, load_canonical
from agent_team.runs.store import STATE_DIR, RunStore, new_run_id, repo_root
from agent_team.schemas.bootstrap import RepoConfig
from agent_team.schemas.runs import RunRecord
from agent_team.tasks.paths import WHOLE_REPO

# Builds the executor once the canonical tree is loaded; tests pass their own.
ExecutorFactory = Callable[[Canonical], Executor]


def load_dag(canonical: Canonical, workflow: str, policy_name: str) -> Dag:
    try:
        spec = canonical.one("workflow", workflow).spec
        policy = canonical.one("policy", policy_name).spec
    except RunAgentError as exc:
        raise OrchestratorError(str(exc)) from exc
    return build_dag(spec, required_gates=policy.required_gates)


def _prepare(canonical_dir: Path, workflow: str, policy_name: str, factory: ExecutorFactory) -> tuple[Dag, Executor, tuple[str, ...]]:
    try:
        canonical = load_canonical(canonical_dir)
    except RunAgentError as exc:
        raise OrchestratorError(str(exc)) from exc
    dag = load_dag(canonical, workflow, policy_name)
    executor = factory(canonical)
    check = getattr(executor, "unavailable", None)
    problems = check(dag) if check is not None else []
    if problems:
        raise OrchestratorError(f"workflow {workflow} cannot run yet: " + "; ".join(problems))
    redaction = tuple(r.pattern for r in canonical.one("policy", policy_name).spec.redaction)
    return dag, executor, redaction


def _max_runs(root: Path) -> int:
    """`max_concurrent_runs` from the repo's approved config, else the default."""
    try:
        config = RepoConfig.model_validate(yaml.safe_load((root / STATE_DIR / CONFIG_NAME).read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 - a missing or unreadable config means the default limit
        return DEFAULT_MAX_RUNS
    return config.max_concurrent_runs


def _scope_paths(scope: Mapping[str, Any] | None) -> list[str]:
    paths = scope.get("paths") if scope else None
    return [str(p) for p in paths] if paths else [WHOLE_REPO]


def _lock_note(replaced: Sequence[LockEntry]) -> str | None:
    notes = [f"took over a stale lock from run {e.run_id} (pid {e.pid})" for e in replaced]
    return "; ".join(notes) or None


def run_workflow(
    repo: Path,
    canonical_dir: Path,
    workflow: str,
    task: str,
    *,
    executor_factory: ExecutorFactory,
    policy_name: str = "default",
    paths: Sequence[str] = (WHOLE_REPO,),
    scope: Mapping[str, Any] | None = None,
    echo: Echo = print,
) -> RunRecord:
    """Start a workflow run on `repo` and walk it until it ends or is interrupted.

    The path lock is taken before the run record exists, so a busy repository refuses
    (LockHeldError) without touching it. A `scope` also fixes the lock paths.
    """
    dag, executor, redaction = _prepare(canonical_dir, workflow, policy_name, executor_factory)
    root = repo_root(repo)
    state_dir = root / STATE_DIR
    if scope is not None and scope.get("paths") is not None:
        paths = _scope_paths(scope)
    run_id = new_run_id(datetime.now(UTC))
    note = _lock_note(acquire(state_dir, run_id, paths, max_runs=_max_runs(root)))
    try:
        store = RunStore.open(root, redaction_patterns=redaction)
        assert store is not None
        with store:
            start(store, dag, task, run_id=run_id, seed={"scope": dict(scope)} if scope is not None else None, echo=echo)
            return walk(store, dag, executor, run_id, echo=echo, first_note=note)
    finally:
        release(state_dir, run_id)


def resume_workflow(
    repo: Path,
    canonical_dir: Path,
    run_id: str,
    *,
    executor_factory: ExecutorFactory,
    policy_name: str = "default",
    echo: Echo = print,
) -> RunRecord:
    """Re-enter an interrupted workflow run at its last checkpoint and walk it on.

    The lock covers the paths in the checkpoint's scope (the whole repo when it has none).
    """
    root = repo_root(repo)
    state_dir = root / STATE_DIR
    store = RunStore.open(root, create=False)
    if store is None:
        raise OrchestratorError(f"no run {run_id!r}; no run record exists yet")
    with store:
        workflow = store.get_run(run_id).workflow
        checkpoint = store.last_checkpoint(run_id)
    if workflow.startswith("agent:"):
        raise OrchestratorError(f"run {run_id} is a single-agent run; only workflow runs resume")
    dag, executor, redaction = _prepare(canonical_dir, workflow, policy_name, executor_factory)
    evidence = checkpoint.data.get("evidence") if checkpoint is not None and isinstance(checkpoint.data, Mapping) else None
    scope = evidence.get("scope") if isinstance(evidence, Mapping) else None
    # A live holder refuses before anything is recorded.
    note = _lock_note(acquire(state_dir, run_id, _scope_paths(scope if isinstance(scope, Mapping) else None), max_runs=_max_runs(root)))
    try:
        store = RunStore.open(root, create=False, redaction_patterns=redaction)
        assert store is not None
        with store:
            reenter(store, dag, run_id, echo=echo, note=note)
            return walk(store, dag, executor, run_id, echo=echo)
    finally:
        release(state_dir, run_id)


def default_executor_factory(
    *,
    repo: Path,
    canonical_dir: Path,
    claude: str,
    max_budget_usd: float,
    timeout_s: float,
    registry_path: Path | None = None,
    test_commands: tuple[str, ...] = (),
    lint_commands: tuple[str, ...] = (),
    typecheck_commands: tuple[str, ...] = (),
    gh: str | None = None,
    policy_name: str = "default",
    echo: Echo = print,
) -> ExecutorFactory:
    def factory(canonical: Canonical) -> Executor:
        return DefaultExecutor(
            repo=repo,
            canonical_dir=canonical_dir,
            canonical=canonical,
            claude=claude,
            max_budget_usd=max_budget_usd,
            timeout_s=timeout_s,
            registry_path=registry_path,
            test_commands=test_commands,
            lint_commands=lint_commands,
            typecheck_commands=typecheck_commands,
            gh=gh,
            policy_name=policy_name,
            echo=echo,
        )

    return factory
