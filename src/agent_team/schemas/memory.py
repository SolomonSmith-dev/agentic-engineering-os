"""Repository memory under `.agent-team/memory/repo/` (build-order step 9).

`agent-team inspect` writes one YAML file per concern (architecture, ownership,
commands, conventions, pitfalls) and an `index.yaml` naming them. The concerns
come from the archaeologist's structured output (`RepositoryMemory`); the
commands file adds what agent-team itself found by running each detected
command inside the run's srt sandbox. Nothing here holds a secret: the
archaeologist's output and every command's output are redacted through the
policy before they are written.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MEMORY_VERSION = 1

# passed / failed: the command ran in the sandbox and exited 0 / non-zero.
# timeout: it was killed at the command timeout. unverified: it was not run, and `reason` says why.
VerificationStatus = Literal["passed", "failed", "timeout", "unverified"]
VERIFICATION_STATUSES: tuple[VerificationStatus, ...] = ("passed", "failed", "timeout", "unverified")


class MemoryFileModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommandVerification(MemoryFileModel):
    kind: str = Field(min_length=1)
    command: str = Field(min_length=1)
    source: str = Field(description="Where the command was found.")
    detected_by: Literal["init", "archaeologist"] = Field(
        description="init's commands are run; commands only the archaeologist proposed are never run."
    )
    dry_run: bool | None = Field(
        default=None, description="init's non-executing check (✓ / ✗); None for archaeologist-only commands."
    )
    status: VerificationStatus
    exit_code: int | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, description="Why the command was not run, or how it ended.")
    output_tail: str | None = Field(default=None, description="Redacted end of stdout and stderr, interleaved.")


class MemoryIndex(MemoryFileModel):
    version: Literal[1] = MEMORY_VERSION
    run_id: str
    generated_at: datetime
    base_commit: str
    sandbox: Literal["srt", "none"] = Field(description="Whether the detected commands could run inside srt.")
    files: dict[str, str] = Field(description="Concern -> file name in this directory.")
    commands: dict[VerificationStatus, int] = Field(default_factory=dict, description="Command count by status.")
