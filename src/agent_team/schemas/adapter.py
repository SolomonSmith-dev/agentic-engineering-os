"""What a runtime adapter returns, and the structured outputs agents emit (build-order step 6).

`AdapterResult` is runtime-neutral: the Claude adapter (step 6) and the Codex
adapter (step 12) fill the same shape. `OUTPUT_SCHEMAS` maps an agent
output name to the model its structured output must validate against; the
adapter passes that model's JSON Schema to the runtime.
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


OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "review_verdict": ReviewVerdict,
}
