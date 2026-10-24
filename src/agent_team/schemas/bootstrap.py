"""`.agent-team/proposal.yaml` and `.agent-team/config.yaml` (build-order step 8).

`init` writes the proposal: what discovery found in a target repository plus the
ask (runtimes, privacy classes, autonomy ceiling, draft PRs) a manager approves
by editing it. `configure` turns an approved proposal into the config. Neither
holds a secret: secret handling is reported by path and kind, never by value,
and remote URLs are stored without their userinfo.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_team.schemas.common import AUTONOMY_ORDER, Autonomy
from agent_team.schemas.registry import PrivacyClass

PROPOSAL_VERSION = 1

CommandKind = Literal["build", "test", "lint", "typecheck"]
COMMAND_KINDS: tuple[CommandKind, ...] = ("build", "test", "lint", "typecheck")

Unknown = Literal["unknown"]

# The bootstrapper never proposes or accepts more than A3 (architecture section 9).
MAX_BOOTSTRAP_AUTONOMY: Autonomy = "A3"


class BootstrapModel(BaseModel):
    """A manager edits these files by hand, so unknown keys are errors, never ignored."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class VcsInfo(BootstrapModel):
    host: str = Field(description="github, gitlab, bitbucket, azure-devops, other, or none when there is no remote.")
    remote: str | None = Field(default=None, description="host/path of the remote, credentials stripped.")
    default_branch: str = Field(description="Branch name, or 'unknown'.")
    default_branch_source: str
    protected_branches: list[str] | Unknown = "unknown"
    protected_branches_source: str


class CiInfo(BootstrapModel):
    systems: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)


class DetectedCommand(BootstrapModel):
    kind: CommandKind
    command: str = Field(min_length=1)
    source: str = Field(description="Where the command was found, e.g. 'Makefile target test'.")
    verified: bool = Field(description="True when the dry-run check passed (shown as ✓); nothing was executed.")
    check: str = Field(description="What the dry-run check looked at, or why it failed.")


class McpConfig(BootstrapModel):
    path: str
    servers: list[str] = Field(default_factory=list, description="Server names only; never their env or args.")


class SecretHint(BootstrapModel):
    path: str
    kind: str = Field(description="e.g. env-file, env-template, key-file, vault, github-actions-secrets.")
    tracked: bool = Field(description="True when git tracks the file.")


class Ask(BootstrapModel):
    """What the proposal asks permission for (architecture section 9, item 4)."""

    runtimes: list[str] = Field(default_factory=lambda: ["claude"], min_length=1)
    privacy_classes: list[PrivacyClass] = Field(default_factory=lambda: ["vendor-cloud"], min_length=1)
    autonomy_ceiling: Autonomy = MAX_BOOTSTRAP_AUTONOMY
    draft_prs: bool = True
    approved: bool = Field(default=False, description="Set to true once the proposal is approved; configure needs it.")

    @field_validator("autonomy_ceiling")
    @classmethod
    def _at_most_a3(cls, value: Autonomy) -> Autonomy:
        if AUTONOMY_ORDER[value] > AUTONOMY_ORDER[MAX_BOOTSTRAP_AUTONOMY]:
            raise ValueError(f"the bootstrapper never sets autonomy above {MAX_BOOTSTRAP_AUTONOMY}")
        return value


class Proposal(BootstrapModel):
    version: Literal[1] = PROPOSAL_VERSION
    vcs: VcsInfo
    ci: CiInfo
    commands: list[DetectedCommand] = Field(default_factory=list)
    instruction_files: list[str] = Field(default_factory=list)
    mcp_configs: list[McpConfig] = Field(default_factory=list)
    secrets: list[SecretHint] = Field(default_factory=list)
    devcontainer: list[str] = Field(default_factory=list)
    codeowners: str | None = None
    ask: Ask = Field(default_factory=Ask)


class RepoConfig(BootstrapModel):
    """`.agent-team/config.yaml`: the approved proposal, reduced to what runs read."""

    version: Literal[1] = PROPOSAL_VERSION
    default_branch: str
    protected_branches: list[str] | Unknown
    runtimes: list[str]
    privacy_classes: list[PrivacyClass]
    autonomy_ceiling: Autonomy
    draft_prs: bool
    commands: dict[CommandKind, list[str]] = Field(
        default_factory=dict, description="Verified commands by kind; an empty kind means its gate is skipped."
    )
    unverified_commands: dict[CommandKind, list[str]] = Field(default_factory=dict)
    instruction_files: list[str] = Field(default_factory=list, description="Files carrying the marked block.")
    agents: list[str] = Field(default_factory=list, description="Compiled runtime agent files.")
