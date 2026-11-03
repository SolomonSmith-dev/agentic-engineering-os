"""`agent-team run --workflow` and `agent-team resume`: load, check, lock, walk (build-order step 10).

    load canonical -> build DAG (policy's required gates) -> executor can run every node?
        -> start_run / RESUME -> lock -> walk -> release

Everything that can refuse does so before the run record is touched.

Every workflow run appends to `.agent-team/runs/<run-id>/trace.jsonl`. A task-file run
passes a `meter` (its budget) and `meta` (the file, budget and base, checkpointed so a
resume can rebuild the meter and write the result back).
"""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from agent_team.budget import BudgetMeter

from agent_team.environments import acquire, release
from agent_team.orchestrator.dag import Dag, OrchestratorError, build_dag
from agent_team.orchestrator.executors import DefaultExecutor
from agent_team.orchestrator.machine import Echo, Executor, reenter, start, walk
from agent_team.run_agent import Canonical, RunAgentError, load_canonical
from agent_team.runs.render import status_line
from agent_team.runs.store import STATE_DIR, RunStore, repo_root
from agent_team.runs.trace import Trace, open_trace, read_trace, trace_elapsed_s, trace_tokens, trace_unknown, trace_usd
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
    meter: BudgetMeter | None = None,
    meta: Mapping[str, Any] | None = None,
    on_start: Callable[[str], None] | None = None,
) -> RunRecord:
    """Start a workflow run on `repo` and walk it until it ends or is interrupted.

    `on_start` is called with the run id as soon as the run is recorded (a task-file run
    keeps it, so an interrupted run's result can still name its run).
    """
    dag, executor, redaction = _prepare(canonical_dir, workflow, policy_name, executor_factory)
    root = repo_root(repo)
    state_dir = root / STATE_DIR
    store = RunStore.open(root, redaction_patterns=redaction)
    assert store is not None
    with store:
        run = start(store, dag, task, echo=echo, meta=meta)
        if on_start is not None:
            on_start(run.run_id)
        trace = open_trace(root, run.run_id, redaction)
        trace.emit("run_started", run=run.run_id, workflow=workflow, budget=meter.limits.as_dict() if meter else None)
        if meter is not None:
            meter.trace = trace
        try:
            note = _lock_note(state_dir, run.run_id)
        except Exception as exc:
            echo(status_line(store.transition(run.run_id, "BLOCKED", note=str(exc))))
            store.finish_run(run.run_id, "blocked")
            raise
        try:
            return _ended(walk(store, dag, executor, run.run_id, echo=echo, first_note=note, trace=trace), trace)
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
    meter: BudgetMeter | None = None,
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
            trace = open_trace(root, run_id, redaction)
            if meter is not None:
                # The trace, not the store: it holds usage metered by a node that died before its result was stored.
                events = read_trace(trace.path)
                meter.seed(tokens=trace_tokens(events), usd=trace_usd(events), elapsed_s=trace_elapsed_s(events),
                           unknown=trace_unknown(events))  # fmt: skip
                meter.trace = trace
            trace.emit("run_resumed", run=run_id, budget=meter.limits.as_dict() if meter else None,
                       spent_tokens=meter.tokens if meter else None, spent_usd=meter.usd if meter else None,
                       spent_minutes=meter.elapsed_s() / 60 if meter else None,
                       spend_unknown=bool(meter.unknown) if meter else None)  # fmt: skip
            reenter(store, dag, run_id, echo=echo, note=note, trace=trace)
            return _ended(walk(store, dag, executor, run_id, echo=echo, trace=trace), trace)
        finally:
            release(state_dir, run_id)


def _ended(record: RunRecord, trace: Trace) -> RunRecord:
    trace.emit("run_ended", run=record.run_id, outcome=record.outcome, halted=record.halted,
               cost_usd=record.cost_usd, tokens=record.tokens_total, state=record.state)  # fmt: skip
    return record


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
    meter: BudgetMeter | None = None,
    base: str | None = None,
    model_classes: Mapping[str, str] | None = None,
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
            meter=meter,
            base=base,
            model_classes=dict(model_classes or {}),
        )

    return factory
