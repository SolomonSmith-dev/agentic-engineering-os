"""Run record models (build-order step 4).

One `RunRecord` per run, one `StateTransition` per state change and one
`ToolCallRecord` per tool call. These are what `agent_team.runs` reads back out
of `.agent-team/runs.db`; nothing in them is a credential, because tool-call
arguments are redacted and truncated before they are stored.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# The full state vocabulary from section 5 of the architecture doc: 13 workflow
# states plus the four failure and recovery edges. Most runs visit six to nine.
RunState = Literal[
    "INTAKE",
    "DISCOVERY",
    "REQUIREMENTS",
    "PLAN",
    "PLAN_REVIEW",
    "IMPLEMENTATION",
    "TEST",
    "SECURITY_REVIEW",
    "CODE_REVIEW",
    "QA",
    "PR",
    "HUMAN_DECISION",
    "RETROSPECTIVE",
    "BLOCKED",
    "RETRY",
    "ABANDON",
    "RESUME",
]

# Set when a run ends; a run with no outcome is still running or was interrupted.
RunOutcome = Literal["succeeded", "failed", "blocked", "abandoned"]

ToolResultStatus = Literal["ok", "error", "denied", "timeout"]

# <UTC date>-<UTC time>-<6 hex>, e.g. 20261023-161700-a1b2c3: sortable and short.
RunId = Annotated[str, StringConstraints(pattern=r"^\d{8}-\d{6}-[0-9a-f]{6}$")]


class RunModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunRecord(RunModel):
    run_id: RunId
    workflow: str = Field(min_length=1)
    model_class: str | None = None
    worktree_path: str | None = None
    state: RunState
    started_at: datetime
    ended_at: datetime | None = None
    last_event_at: datetime
    outcome: RunOutcome | None = None
    cost_usd: float = Field(ge=0)

    @property
    def elapsed_s(self) -> float:
        """Seconds from start to the end, or to the last recorded event while running."""
        return ((self.ended_at or self.last_event_at) - self.started_at).total_seconds()


class StateTransition(RunModel):
    run_id: RunId
    seq: int = Field(ge=1)
    from_state: RunState | None
    to_state: RunState
    at: datetime
    elapsed_s: float = Field(ge=0, description="Seconds since the run started.")
    cost_usd: float = Field(ge=0, description="Run cost so far at this transition.")
    note: str | None = None


class ToolCallRecord(RunModel):
    run_id: RunId
    seq: int = Field(ge=1)
    state: RunState
    tool_name: str = Field(min_length=1)
    args_summary: str = Field(description="Redacted, truncated JSON of the call arguments.")
    result_status: ToolResultStatus
    started_at: datetime
    duration_ms: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
