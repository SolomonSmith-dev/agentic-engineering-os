"""canonical/policies/*.yaml: autonomy ceilings, protected paths, redaction, gates."""

from typing import Literal

from pydantic import Field

from agent_team.schemas.common import Autonomy, CanonicalModel, Capability, KebabName
from agent_team.schemas.registry import PrivacyClass


class RedactionRule(CanonicalModel):
    name: KebabName
    pattern: str = Field(min_length=1, description="Regular expression applied to logs and transcripts.")


class PolicySpec(CanonicalModel):
    kind: Literal["policy"]
    name: KebabName
    autonomy_ceiling: dict[KebabName, Autonomy] = Field(
        min_length=1, description="Agent name -> highest autonomy level it may run at."
    )
    protected_paths: list[str] = Field(default_factory=list)
    denied_capabilities: list[Capability] = Field(default_factory=list)
    redaction: list[RedactionRule] = Field(default_factory=list)
    required_gates: list[str] = Field(default_factory=list)
    default_autonomy: Autonomy = "A0"
    allowed_privacy_classes: list[PrivacyClass] | None = Field(
        default=None, description="Registry privacy classes a model may have; omit to allow every class."
    )
