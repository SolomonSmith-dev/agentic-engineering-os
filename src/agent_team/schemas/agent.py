"""canonical/agents/*.md frontmatter (section 6 of docs/ARCHITECTURE.md)."""

from typing import Literal

from pydantic import Field, field_validator, model_validator

from agent_team.schemas.common import (
    Autonomy,
    CanonicalModel,
    Capability,
    KebabName,
    SnakeName,
    Trigger,
    reject_vendor_names,
)

Workspace = Literal["read_only_mirror", "worktree", "in_place"]


class Capabilities(CanonicalModel):
    allow: list[Capability] = Field(default_factory=list)
    deny: list[Capability] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_overlap(self) -> "Capabilities":
        overlap = sorted(set(self.allow) & set(self.deny))
        if overlap:
            raise ValueError(f"capabilities listed in both allow and deny: {overlap}")
        return self


class AgentSpec(CanonicalModel):
    """Role, capabilities, model class, autonomy ceiling and definition of done."""

    kind: Literal["agent"]
    name: KebabName
    purpose: str = Field(min_length=1)
    triggers: list[Trigger] = Field(min_length=1)
    inputs: list[SnakeName] = Field(min_length=1)
    outputs: list[SnakeName] = Field(min_length=1)
    capabilities: Capabilities
    skills: list[KebabName] = Field(default_factory=list)
    model_class: KebabName
    max_autonomy: Autonomy
    workspace: Workspace
    definition_of_done: list[str] = Field(min_length=1)
    escalate_when: list[SnakeName] = Field(default_factory=list)

    @field_validator("model_class")
    @classmethod
    def _model_class_is_a_class(cls, value: str) -> str:
        return reject_vendor_names(value)

    @field_validator("definition_of_done")
    @classmethod
    def _dod_items_non_empty(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("definition_of_done items must not be blank")
        return value
