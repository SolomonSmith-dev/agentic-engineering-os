"""Build the run DAG from a workflow spec (build-order step 10).

Minimal on purpose: the DAG is the workflow's `states` in order, a linear chain.
Each node carries its state, its worker (canonical agent or in-process step),
the evidence it must produce and the edges out of it. Three edge kinds exist:

    advance   to the next state in the chain, behind an optional gate
    retry     back to an earlier state, gated, at most `max_traversals` times
    escalate  forward past the chain, gated; also taken when a retry runs out

Anything else (fan-out, a second forward edge, an ungated retry) is refused
here, so the state machine never has to guess.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from agent_team.schemas.runs import RunState
from agent_team.schemas.workflow import GATE_PATTERN, WorkflowSpec

EdgeKind = Literal["advance", "retry", "escalate"]


class OrchestratorError(RuntimeError):
    """The orchestrator refused to start or resume a run. The message says what to fix."""


class DagError(OrchestratorError):
    """The workflow is not a shape this orchestrator can run."""


@dataclass(frozen=True)
class Gate:
    """`key` passes when that evidence is exactly true; `key == value` when it equals value."""

    key: str
    value: str | None = None

    @classmethod
    def parse(cls, text: str) -> "Gate":
        match = GATE_PATTERN.match(text)
        if match is None:
            raise DagError(f"gate {text!r} is not '<evidence>' or '<evidence> == <value>'")
        return cls(match["key"], match["value"])

    def passes(self, evidence: Mapping[str, Any]) -> bool:
        got = evidence.get(self.key)
        return got is True if self.value is None else got == self.value

    def __str__(self) -> str:
        return self.key if self.value is None else f"{self.key} == {self.value}"


@dataclass(frozen=True)
class Edge:
    from_state: RunState
    to_state: RunState
    kind: EdgeKind
    gate: Gate | None = None
    max_traversals: int | None = None

    @property
    def key(self) -> str:
        return f"{self.from_state}->{self.to_state}"


@dataclass(frozen=True)
class DagNode:
    state: RunState
    agent: str | None
    step: str | None
    evidence: tuple[str, ...]
    inputs: tuple[str, ...]
    edges: tuple[Edge, ...]

    @property
    def worker(self) -> str:
        return f"agent {self.agent}" if self.agent else f"step {self.step}"

    @property
    def terminal(self) -> bool:
        return not self.edges

    @property
    def gate(self) -> Gate | None:
        """The gate on the advance edge: what must pass to move down the chain."""
        return next((e.gate for e in self.edges if e.kind == "advance"), None)


@dataclass(frozen=True)
class Dag:
    workflow: str
    nodes: tuple[DagNode, ...]

    @property
    def start(self) -> DagNode:
        return self.nodes[0]

    def node(self, state: str) -> DagNode:
        for node in self.nodes:
            if node.state == state:
                return node
        raise DagError(f"state {state} is not in workflow {self.workflow}")

    def __contains__(self, state: object) -> bool:
        return any(node.state == state for node in self.nodes)

    @property
    def agents(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(n.agent for n in self.nodes if n.agent))

    @property
    def steps(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(n.step for n in self.nodes if n.step))


def build_dag(spec: WorkflowSpec, *, required_gates: Iterable[str] = ()) -> Dag:
    """The linear-chain DAG for `spec`. `required_gates` (from the policy) must each gate some edge."""
    order = {state: i for i, state in enumerate(spec.states)}
    edges: dict[str, list[Edge]] = {state: [] for state in spec.states}
    for t in spec.transitions:
        here, there = order[t.from_state], order[t.to_state]
        gate = Gate.parse(t.gate) if t.gate is not None else None
        label = f"{t.from_state} -> {t.to_state}"
        if t.escalate:
            if there <= here or t.max_traversals is not None or gate is None:
                raise DagError(f"{label}: an escalate edge goes forward, has a gate and no max_traversals")
            kind: EdgeKind = "escalate"
        elif t.max_traversals is not None:
            if there >= here or gate is None:
                raise DagError(f"{label}: a retry edge (max_traversals) goes back and has a gate")
            kind = "retry"
        elif there == here + 1:
            kind = "advance"
        else:
            raise DagError(f"{label}: only the next state is reachable without max_traversals or escalate")
        if gate is not None and gate.key not in spec.nodes[t.from_state].evidence:
            raise DagError(f"{label}: gate {gate} reads evidence {t.from_state} does not produce")
        edges[t.from_state].append(Edge(t.from_state, t.to_state, kind, gate, t.max_traversals))  # type: ignore[arg-type]

    for state in spec.states[:-1]:
        advances = [e for e in edges[state] if e.kind == "advance"]
        if len(advances) != 1:
            raise DagError(f"{state} needs exactly one edge to the next state, has {len(advances)}")
    if edges[spec.states[-1]]:
        raise DagError(f"{spec.states[-1]} is the last state and ends the run; it cannot have edges")
    for state, out in edges.items():
        if [e for e in out if e.kind == "retry"] and not [e for e in out if e.kind == "escalate"]:
            raise DagError(f"{state} has a retry edge but no escalate edge to take when it runs out")

    produced = {key for node in spec.nodes.values() for key in node.evidence}
    for state, node in spec.nodes.items():
        unknown = [key for key in node.inputs if key not in produced]
        if unknown:
            raise DagError(f"{state} reads evidence no node produces: {unknown}")
    gated = {e.gate.key for out in edges.values() for e in out if e.gate is not None}
    missing = [g for g in required_gates if g not in gated]
    if missing:
        raise DagError(f"workflow {spec.name} has no gate on {missing}, which the policy requires")

    nodes = tuple(
        DagNode(
            state=state,  # type: ignore[arg-type]
            agent=spec.nodes[state].agent,
            step=spec.nodes[state].step,
            evidence=tuple(spec.nodes[state].evidence),
            inputs=tuple(spec.nodes[state].inputs),
            edges=tuple(edges[state]),
        )
        for state in spec.states
    )
    return Dag(workflow=spec.name, nodes=nodes)
