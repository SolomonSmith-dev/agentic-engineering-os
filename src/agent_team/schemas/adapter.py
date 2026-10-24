"""What a runtime adapter returns, and the structured outputs agents emit (build-order step 6).

`AdapterResult` is runtime-neutral: the Claude adapter (step 6) and the Codex
adapter (step 12) fill the same shape. `OUTPUT_SCHEMAS` maps an agent
output name to the model its structured output must validate against; the
adapter passes that model's JSON Schema to the runtime. Registered so far: the
reviewer's verdict (step 6) and the archaeologist's repository memory (step 9).
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AdapterStatus = Literal["succeeded", "failed", "budget_exceeded", "max_turns", "timeout", "error"]


class AdapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AdapterStatus
    runtime: str = Field(min_length=1)
    subtype: str | None = Field(default=None, description="The runtime's own end reason, kept verbatim.")
    result: str | None = None
    structured_output: dict[str, Any] | None = None
    structured_output_error: str | None = None
    reason: str | None = Field(
        default=None, description="Why the run did not succeed, in the adapter's words, when the subtype does not say."
    )
    cost_usd: float = Field(default=0.0, ge=0)
    num_turns: int | None = None
    duration_ms: int | None = None
    session_id: str | None = None
    tool_calls: int = Field(default=0, ge=0)
    permission_denials: list[str] = Field(default_factory=list, description="Tool names the runtime denied.")
    usage: dict[str, int] | None = Field(default=None, description="Token counts as the runtime reports them.")
    exit_code: int | None = None
    stderr_tail: str | None = Field(default=None, description="Redacted end of stderr when the run failed.")


Severity = Literal["blocker", "major", "minor"]


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=1)
    requirement_id: str = Field(min_length=1)
    severity: Severity
    fix: str = Field(min_length=1)


class ReviewVerdict(BaseModel):
    """The independent reviewer's output: blocker and major findings make the verdict `changes`."""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["approve", "changes", "escalate"]
    findings: list[ReviewFinding] = Field(default_factory=list)


class ClaimedChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class ImplementerReport(BaseModel):
    """The implementer's output (step 11). Claims only: the orchestrator re-runs the reproduction and reads the diff itself.

    In DISCOVERY `reproduction_fails` claims the command fails before any fix; in
    IMPLEMENTATION it claims the command still fails (false once fixed).
    """

    model_config = ConfigDict(extra="forbid")

    reproduction_command: str = Field(min_length=1, description="One shell command that exits non-zero while the bug exists.")
    reproduction_fails: bool
    changes: list[ClaimedChange] = Field(default_factory=list, description="Files the implementer says it changed, and why.")


class MemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModuleEntry(MemoryModel):
    path: str = Field(min_length=1, description="Path of the module or package, relative to the repository root.")
    purpose: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list, description="Paths of the other modules it imports or calls.")


class ArchitectureMap(MemoryModel):
    summary: str = Field(min_length=1, description="What the repository is and how it is laid out, in a few sentences.")
    modules: list[ModuleEntry] = Field(min_length=1)


class ModuleOwnership(MemoryModel):
    path: str = Field(min_length=1)
    owners: list[str] = Field(default_factory=list)
    source: Literal["codeowners", "git-history", "unknown"]


DeclaredCommandKind = Literal["build", "test", "lint", "typecheck", "format", "run", "other"]


class DeclaredCommand(MemoryModel):
    kind: DeclaredCommandKind
    command: str = Field(min_length=1)
    source: str = Field(min_length=1, description="The file that declares it, e.g. Makefile or package.json.")


class Convention(MemoryModel):
    topic: str = Field(min_length=1)
    rule: str = Field(min_length=1)
    evidence: str = Field(min_length=1, description="The file that shows the convention.")


class Pitfall(MemoryModel):
    title: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    evidence: str = Field(min_length=1, description="The file that shows the pitfall.")


class RepositoryMemory(MemoryModel):
    """The repository archaeologist's output; `agent-team inspect` writes it as repository memory."""

    architecture: ArchitectureMap
    ownership: list[ModuleOwnership] = Field(default_factory=list)
    commands: list[DeclaredCommand] = Field(default_factory=list)
    conventions: list[Convention] = Field(default_factory=list)
    pitfalls: list[Pitfall] = Field(default_factory=list)


OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "review_verdict": ReviewVerdict,
    "implementer_report": ImplementerReport,
    "repository_memory": RepositoryMemory,
}
