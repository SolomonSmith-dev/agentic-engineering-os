"""`agent-team run --workflow` and `agent-team resume`: load, check, lock, walk (build-order step 10).

    load canonical -> build DAG (policy's required gates) -> executor can run every node?
        -> start_run / RESUME -> lock -> walk -> release

Everything that can refuse does so before the run record is touched.
"""

from collections.abc import Callable
from pathlib import Path

from agent_team.environments import acquire, release
from agent_team.orchestrator.dag import Dag, OrchestratorError, build_dag
from agent_team.orchestrator.executors import DefaultExecutor
from agent_team.orchestrator.machine import Echo, Executor, reenter, start, walk
from agent_team.run_agent import Canonical, RunAgentError, load_canonical
from agent_team.runs.render import status_line
from agent_team.runs.store import STATE_DIR, RunStore, repo_root
from agent_team.schemas.runs import RunRecord

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


def _lock_note(state_dir: Path, run_id: str) -> str | None:
    replaced = acquire(state_dir, run_id)
    if replaced is None:
        return None
    return f"took over a stale lock from run {replaced.run_id} (pid {replaced.pid})"


def run_workflow(
    repo: Path,
    canonical_dir: Path,
    workflow: str,
    task: str,
    *,
    executor_factory: ExecutorFactory,
    policy_name: str = "default",
    echo: Echo = print,
) -> RunRecord:
    """Start a workflow run on `repo` and walk it until it ends or is interrupted."""
    dag, executor, redaction = _prepare(canonical_dir, workflow, policy_name, executor_factory)
    root = repo_root(repo)
    state_dir = root / STATE_DIR
    store = RunStore.open(root, redaction_patterns=redaction)
    assert store is not None
    with store:
        run = start(store, dag, task, echo=echo)
        try:
            note = _lock_note(state_dir, run.run_id)
        except Exception as exc:
            echo(status_line(store.transition(run.run_id, "BLOCKED", note=str(exc))))
            store.finish_run(run.run_id, "blocked")
            raise
        try:
            return walk(store, dag, executor, run.run_id, echo=echo, first_note=note)
        finally:
            release(state_dir, run.run_id)


def resume_workflow(
    repo: Path,
    canonical_dir: Path,
    run_id: str,
    *,
    executor_factory: ExecutorFactory,
    policy_name: str = "default",
    echo: Echo = print,
) -> RunRecord:
    """Re-enter an interrupted workflow run at its last checkpoint and walk it on."""
    root = repo_root(repo)
    state_dir = root / STATE_DIR
    store = RunStore.open(root, create=False)
    if store is None:
        raise OrchestratorError(f"no run {run_id!r}; no run record exists yet")
    with store:
        workflow = store.get_run(run_id).workflow
    if workflow.startswith("agent:"):
        raise OrchestratorError(f"run {run_id} is a single-agent run; only workflow runs resume")
    dag, executor, redaction = _prepare(canonical_dir, workflow, policy_name, executor_factory)
    store = RunStore.open(root, create=False, redaction_patterns=redaction)
    assert store is not None
    with store:
        note = _lock_note(state_dir, run_id)  # a live holder refuses before anything is recorded
        try:
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
            policy_name=policy_name,
            echo=echo,
        )

    return factory
