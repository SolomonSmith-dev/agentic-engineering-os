"""canonical/tools/*.yaml: capability name -> implementation and credential handle."""

from typing import Literal

from pydantic import Field, field_validator

from agent_team.schemas.common import CanonicalModel, Capability

ImplementationType = Literal["cli", "api", "mcp", "local_service"]


class Implementation(CanonicalModel):
    type: ImplementationType
    command: str | None = None  # cli
    url: str | None = None  # api, mcp (http), local_service
    server: str | None = None  # mcp server name


class ToolSpec(CanonicalModel):
    kind: Literal["tool"]
    capability: Capability
    description: str = Field(min_length=1)
    implementation: Implementation
    credential: str | None = Field(
        default=None,
        description="A credential handle (keychain item or env var name), never a value.",
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )

    @field_validator("capability")
    @classmethod
    def _no_wildcard(cls, value: str) -> str:
        if value.endswith(".*"):
            raise ValueError("a tool implements one capability; wildcards belong in policies")
        return value
