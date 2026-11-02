"""Ledger task file (M1): one unit of work handed to `agent-team run --task-file`.

agent-team reads the request, target, scope and budget, and writes back only the
top-level `result:` block (or `<id>.result.yaml` beside the file). `paths` stays
raw strings here; `agent_team.tasks.audit` rejects bad ones so the refusal reads
like the other audit findings.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from agent_team.schemas.common import CanonicalModel, KebabName

TaskOutcome = Literal["pr_opened", "failed", "blocked", "escalated", "halted_budget", "refused", "busy"]
BudgetLimit = Literal["tokens", "minutes", "usd"]

# Filename-safe: the id names <id>.result.yaml and run branches.
TaskId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]


class TaskTarget(CanonicalModel):
    repo: str = Field(min_length=1, description="Path to the target repository.")
    base: str = Field(min_length=1, description="Branch the draft PR is based on.")
    remote: str | None = Field(default=None, description="Git remote to push to; the repo's default when omitted.")


class TaskBudget(CanonicalModel):
    tokens: int = Field(gt=0, description="Input plus output tokens; cache reads and writes are priced, not counted.")
    minutes: float = Field(gt=0, description="Wall-clock minutes.")
    usd: float = Field(gt=0, description="List-price dollars.")


class TaskResult(CanonicalModel):
    run_id: str | None = None
    outcome: TaskOutcome
    halted: BudgetLimit | None = Field(default=None, description="Which limit stopped the run; set only for halted_budget.")
    tokens: int = Field(default=0, ge=0)
    usd_list: float = Field(default=0.0, ge=0)
    minutes: float = Field(default=0.0, ge=0)
    pr: int | None = None
    trace: str | None = None
    finished_at: datetime
    reason: str | None = None

    @model_validator(mode="after")
    def _halted_iff_halted_budget(self) -> "TaskResult":
        if (self.outcome == "halted_budget") != (self.halted is not None):
            raise ValueError("halted must be set if and only if outcome is halted_budget")
        return self


class TaskFile(CanonicalModel):
    kind: Literal["task"]
    id: TaskId
    target: TaskTarget
    request: str = Field(min_length=1)
    issue: int | None = Field(default=None, ge=1)
    paths: list[str] = Field(min_length=1, description="Write scope: repo-relative paths the run may change.")
    allow_dependency_changes: bool = False
    budget: TaskBudget
    workflow: KebabName
    models: dict[KebabName, KebabName] = Field(default_factory=dict, description="Model class overrides per role.")
    status: str | None = None
    result: TaskResult | None = None
