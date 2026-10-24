"""Walk a DAG, checkpointing to the run record after every state (build-order step 10).

    start   start_run(<first state>, checkpoint) -> walk
    walk    run the node -> check its evidence -> pick an edge by gate -> transition(checkpoint) -> ...
    resume  last checkpoint -> RESUME -> <that state> -> walk

A checkpoint is written in the same transaction as the transition into the
state it re-enters, so a run interrupted anywhere resumes at the node that was
running, with the evidence every earlier node left. Gate outcomes:

    gate passes on the advance edge        next state
    retry gate passes, traversals left     back edge (one implementer retry)
    retry gate passes, none left           escalate edge, run ends blocked
    escalate gate passes                   escalate edge, run ends blocked
    no gate passes, evidence missing,
    or the node reports a blocker          BLOCKED, run ends blocked
    the executor raises                    BLOCKED, run ends failed, error re-raised
    KeyboardInterrupt                      nothing recorded; the run stays resumable

Every transition prints exactly one status line.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_team.orchestrator.dag import Dag, DagNode, Edge, OrchestratorError
from agent_team.runs.render import status_line
from agent_team.runs.store import RunStore
from agent_team.schemas.runs import RunOutcome, RunRecord

Echo = Callable[[str], None]


@dataclass(frozen=True)
class NodeInput:
    """What one node is given. `inputs` holds only the evidence keys its node lists."""

    run_id: str
    workflow: str
    state: str
    attempt: int
    task: str
    inputs: Mapping[str, Any]
    escalated: bool = False


@dataclass(frozen=True)
class NodeResult:
    """What a node hands back: its evidence, the cost it spent, and a blocker if it could not finish."""

    evidence: Mapping[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    note: str | None = None
    blocked: str | None = None


class Executor(Protocol):
    """Runs one node. The orchestrator owns the run record; an executor never transitions it."""

    def __call__(self, node: DagNode, given: NodeInput) -> NodeResult: ...


@dataclass
class Context:
    """The checkpointed part of a run: everything a resume needs to re-enter a state."""

    task: str
    evidence: dict[str, Any] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    traversals: dict[str, int] = field(default_factory=dict)
    escalated: bool = False

    def dump(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "evidence": self.evidence,
            "attempts": self.attempts,
            "traversals": self.traversals,
            "escalated": self.escalated,
        }

    @classmethod
    def load(cls, data: Mapping[str, Any]) -> "Context":
        try:
            return cls(
                task=str(data["task"]),
                evidence=dict(data.get("evidence", {})),
                attempts={str(k): int(v) for k, v in data.get("attempts", {}).items()},
                traversals={str(k): int(v) for k, v in data.get("traversals", {}).items()},
                escalated=bool(data.get("escalated", False)),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise OrchestratorError(f"checkpoint is unreadable ({type(exc).__name__}: {exc})") from exc

    def enter(self, state: str) -> None:
        self.attempts[state] = self.attempts.get(state, 0) + 1


def _join(*parts: str | None) -> str | None:
    kept = [p for p in parts if p]
    return " | ".join(kept) or None


def _block(store: RunStore, run_id: str, note: str | None, echo: Echo, *, outcome: RunOutcome = "blocked") -> RunRecord:
    echo(status_line(store.transition(run_id, "BLOCKED", note=note)))
    return store.finish_run(run_id, outcome)


def choose_edge(node: DagNode, ctx: Context) -> tuple[Edge | None, str]:
    """The edge to take after `node`, and why. None means no gate passed."""
    for edge in node.edges:
        if edge.gate is not None and not edge.gate.passes(ctx.evidence):
            continue
        if edge.kind == "retry":
            used, limit = ctx.traversals.get(edge.key, 0), edge.max_traversals or 0
            if used < limit:
                return edge, f"retry {used + 1}/{limit}: {edge.gate}"
            escalate = next(e for e in node.edges if e.kind == "escalate")  # build_dag guarantees one
            return escalate, f"escalated: {edge.gate} after {used}/{limit} retries"
        if edge.kind == "escalate":
            return edge, f"escalated: {edge.gate}"
        return edge, f"gate {edge.gate} passed" if edge.gate else ""
    seen = ", ".join(f"{e.gate} (got {ctx.evidence.get(e.gate.key)!r})" for e in node.edges if e.gate)
    return None, f"gate failed: {seen}"


def start(store: RunStore, dag: Dag, task: str, *, echo: Echo = print) -> RunRecord:
    """Record a new workflow run in its first state, with the first checkpoint."""
    ctx = Context(task=task)
    ctx.enter(dag.start.state)
    run = store.start_run(dag.workflow, initial_state=dag.start.state, checkpoint=ctx.dump())
    echo(f"run {run.run_id}  workflow {dag.workflow}")
    echo(status_line(store.transitions(run.run_id)[-1]))
    return run


def reenter(store: RunStore, dag: Dag, run_id: str, *, echo: Echo = print, note: str | None = None) -> RunRecord:
    """Move an interrupted run back into its last checkpointed state, through RESUME."""
    run = store.get_run(run_id)
    if run.ended_at is not None:
        raise OrchestratorError(f"run {run_id} already ended ({run.outcome}); start a new run")
    if run.workflow != dag.workflow:
        raise OrchestratorError(f"run {run_id} is a {run.workflow} run, not {dag.workflow}")
    checkpoint = store.last_checkpoint(run_id)
    if checkpoint is None:
        raise OrchestratorError(f"run {run_id} has no checkpoint to resume from")
    if checkpoint.state not in dag:
        raise OrchestratorError(f"run {run_id} stopped in {checkpoint.state}, which workflow {dag.workflow} no longer has")
    Context.load(checkpoint.data)  # refuse an unreadable checkpoint before recording anything
    echo(f"run {run_id}  workflow {dag.workflow}  resuming at {checkpoint.state}")
    echo(status_line(store.transition(run_id, "RESUME", note=_join(f"interrupted in {run.state}", note))))
    t = store.transition(run_id, checkpoint.state, note="re-entered from checkpoint", checkpoint=checkpoint.data)
    echo(status_line(t))
    return store.get_run(run_id)


def walk(
    store: RunStore,
    dag: Dag,
    executor: Executor,
    run_id: str,
    *,
    echo: Echo = print,
    first_note: str | None = None,
) -> RunRecord:
    """Run nodes from the run's current state until it ends. The run must sit on a checkpoint."""
    checkpoint = store.last_checkpoint(run_id)
    if checkpoint is None or checkpoint.state != store.get_run(run_id).state:
        raise OrchestratorError(f"run {run_id} is not at a checkpoint; use resume")
    ctx = Context.load(checkpoint.data)
    state = checkpoint.state
    pending = first_note
    while True:
        node = dag.node(state)
        given = NodeInput(
            run_id=run_id,
            workflow=dag.workflow,
            state=state,
            attempt=ctx.attempts.get(state, 1),
            task=ctx.task,
            inputs={k: ctx.evidence[k] for k in node.inputs if k in ctx.evidence},
            escalated=ctx.escalated,
        )
        try:
            result = executor(node, given)
        except KeyboardInterrupt:
            echo(f"interrupted in {state}; the checkpoint is kept. Resume with: agent-team resume {run_id}")
            raise
        except Exception as exc:
            error = f"{state} {node.worker}: {type(exc).__name__}: {exc}"
            _block(store, run_id, _join(pending, error), echo, outcome="failed")
            raise
        if result.cost_usd:
            store.add_cost(run_id, result.cost_usd)
        if result.blocked:
            return _block(store, run_id, _join(pending, result.note, f"blocked: {result.blocked}"), echo)
        missing = [key for key in node.evidence if result.evidence.get(key) is None]
        if missing:
            return _block(store, run_id, _join(pending, result.note, f"{state} left no evidence {missing}"), echo)
        ctx.evidence.update(result.evidence)
        if node.terminal:
            return store.finish_run(run_id, "blocked" if ctx.escalated else "succeeded")
        edge, why = choose_edge(node, ctx)
        if edge is None:
            return _block(store, run_id, _join(pending, result.note, why), echo)
        if edge.kind == "retry":
            ctx.traversals[edge.key] = ctx.traversals.get(edge.key, 0) + 1
        if edge.kind == "escalate":
            ctx.escalated = True
        ctx.enter(edge.to_state)
        t = store.transition(run_id, edge.to_state, note=_join(pending, result.note, why), checkpoint=ctx.dump())
        echo(status_line(t))
        pending = None
        state = edge.to_state
