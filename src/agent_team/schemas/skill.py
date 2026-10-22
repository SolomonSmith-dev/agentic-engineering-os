"""canonical/skills/*/SKILL.md frontmatter (agentskills.io shape)."""

from typing import Literal

from pydantic import Field

from agent_team.schemas.common import CanonicalModel, KebabName


class SkillSpec(CanonicalModel):
    """A repeatable procedure. The body is the procedure; frontmatter is the index card."""

    kind: Literal["skill"] = "skill"
    name: KebabName
    description: str = Field(min_length=1, max_length=1024)
    license: str | None = None
    allowed_tools: list[str] = Field(default_factory=list, alias="allowed-tools")
    metadata: dict[str, str] = Field(default_factory=dict)

    model_config = CanonicalModel.model_config | {"populate_by_name": True}
