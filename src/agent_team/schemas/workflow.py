"""canonical/workflows/*.yaml: state list, nodes, transitions, gates, definition of done.

A node says who works in a state (a canonical agent or an in-process step), the
evidence it must leave behind, and which earlier evidence it may see. A gate is
`<evidence>` (passes when that evidence is `true`) or `<evidence> == <value>`.
The orchestrator (build-order step 10) turns this into a DAG; the shapes it
accepts beyond these checks are in `agent_team.orchestrator.dag`.
"""

import re
from typing import Literal, get_args

from pydantic import Field, field_validator, model_validator

from agent_team.schemas.common import CanonicalModel, KebabName, SnakeName
from agent_team.schemas.runs import RunState

StateName = str  # SCREAMING_SNAKE, checked below so the message names the field

GATE_PATTERN = re.compile(r"^(?P<key>[a-z][a-z0-9_]*)(?: == (?P<value>[a-z][a-z0-9_]*))?$")


class Transition(CanonicalModel):
    from_state: StateName = Field(alias="from")
    to_state: StateName = Field(alias="to")
    gate: str | None = None
    # A back edge (a retry) may be taken at most this many times per run.
    max_traversals: int | None = Field(default=None, ge=1)
    # Taken on its gate, and also when a retry edge from the same state has run out.
    escalate: bool = False

    model_config = CanonicalModel.model_config | {"populate_by_name": True}

    @field_validator("gate")
    @classmethod
    def _gate_syntax(cls, value: str | None) -> str | None:
        if value is not None and not GATE_PATTERN.match(value):
            raise ValueError(f"gate {value!r} must be '<evidence>' or '<evidence> == <value>' in snake_case")
        return value


class Node(CanonicalModel):
    """Who works in one state, what it must produce, and what it may read."""

    agent: KebabName | None = None
    step: SnakeName | None = None
    evidence: list[SnakeName] = Field(default_factory=list)
    inputs: list[SnakeName] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_worker(self) -> "Node":
        if (self.agent is None) == (self.step is None):
            raise ValueError("a node names exactly one of agent or step")
        return self


class WorkflowSpec(CanonicalModel):
    kind: Literal["workflow"]
    name: KebabName
    purpose: str = Field(min_length=1)
    states: list[StateName] = Field(min_length=1)
    nodes: dict[StateName, Node] = Field(min_length=1)
    transitions: list[Transition] = Field(min_length=1)
    definition_of_done: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _states_consistent(self) -> "WorkflowSpec":
        bad = [s for s in self.states if not s.isupper() or not s.replace("_", "").isalnum()]
        if bad:
            raise ValueError(f"states must be SCREAMING_SNAKE_CASE: {bad}")
        # The run record stores states from the section 5 vocabulary only.
        unknown = [s for s in self.states if s not in get_args(RunState)]
        if unknown:
            raise ValueError(f"states not in the run state vocabulary: {unknown}")
        if len(set(self.states)) != len(self.states):
            raise ValueError("states contains duplicates")
        known = set(self.states)
        for t in self.transitions:
            for endpoint in (t.from_state, t.to_state):
                if endpoint not in known:
                    raise ValueError(f"transition references unknown state {endpoint!r}")
        missing = [s for s in self.states if s not in self.nodes]
        extra = [s for s in self.nodes if s not in known]
        if missing or extra:
            raise ValueError(f"nodes must cover every state exactly: missing {missing}, unknown {extra}")
        return self
