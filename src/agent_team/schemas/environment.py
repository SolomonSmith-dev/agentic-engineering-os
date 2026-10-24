"""canonical/environments/*.yaml: isolation tier, network allowlist, port block per worktree."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from agent_team.schemas.common import CanonicalModel, KebabName

# Hostnames srt accepts in allowedDomains: an optional leading "*." wildcard and an optional :port.
Domain = Annotated[
    str,
    StringConstraints(pattern=r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+(:\d{1,5})?$"),
]

# worktree: git worktree only, autonomy capped at A2. srt: worktree wrapped in srt, falling back
# to worktree-only (and the A2 cap) when srt is not installed.
Tier = Literal["worktree", "srt"]


class NetworkPolicy(CanonicalModel):
    allowed_domains: list[Domain] = Field(
        default_factory=list, description="Hosts the sandbox may reach; everything else is denied."
    )


class PortPolicy(CanonicalModel):
    block_size: int = Field(
        default=100, ge=1, le=1000, description="Ports per worktree; worktree slot N gets offset N * block_size."
    )


# An absolute path, or one under the home directory (~/...).
DenyReadPath = Annotated[str, StringConstraints(pattern=r"^(/|~/)\S")]


class EnvironmentSpec(CanonicalModel):
    kind: Literal["environment"]
    name: KebabName
    description: str = Field(min_length=1)
    tier: Tier
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    ports: PortPolicy = Field(default_factory=PortPolicy)
    deny_read: list[DenyReadPath] = Field(
        default_factory=list,
        description="Paths srt keeps unreadable, on top of the credentials it always denies. The eval (step 13) "
        "adds its tasks, fixtures and results here so no arm can read a hidden verifier.",
    )
