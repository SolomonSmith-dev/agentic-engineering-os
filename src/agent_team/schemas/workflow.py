"""canonical/workflows/*.yaml: state list, transitions, gates, definition of done."""

from typing import Literal

from pydantic import Field, model_validator

from agent_team.schemas.common import CanonicalModel, KebabName

StateName = str  # SCREAMING_SNAKE, checked below so the message names the field


class Transition(CanonicalModel):
    from_state: StateName = Field(alias="from")
    to_state: StateName = Field(alias="to")
    gate: str | None = None

    model_config = CanonicalModel.model_config | {"populate_by_name": True}


class WorkflowSpec(CanonicalModel):
    kind: Literal["workflow"]
    name: KebabName
    purpose: str = Field(min_length=1)
    states: list[StateName] = Field(min_length=1)
    transitions: list[Transition] = Field(min_length=1)
    definition_of_done: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _states_consistent(self) -> "WorkflowSpec":
        bad = [s for s in self.states if not s.isupper() or not s.replace("_", "").isalnum()]
        if bad:
            raise ValueError(f"states must be SCREAMING_SNAKE_CASE: {bad}")
        if len(set(self.states)) != len(self.states):
            raise ValueError("states contains duplicates")
        known = set(self.states)
        for t in self.transitions:
            for endpoint in (t.from_state, t.to_state):
                if endpoint not in known:
                    raise ValueError(f"transition references unknown state {endpoint!r}")
        return self
