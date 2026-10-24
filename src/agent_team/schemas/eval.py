"""Eval tasks and eval results (build-order step 13).

`EvalTask` is one Harbor-shaped task under `evals/tasks/*.yaml` (architecture
section 6): a repository and ref, the instruction every arm gets, and a hidden
verifier the agent never sees. The rest of the file is what `agent-team eval`
writes to `.agent-team/eval/results.jsonl`, one record per line:

    eval      one `eval run --execute`: the plan, the caps, the arms
    probe     the sandboxed-Bash check that ran before the first paid cell
    cell      one task x arm x run: cost, tokens, retries, the diff, the verifier
    dry_run   a plan that was printed and not run (an --execute needs one first)
    field     a real `agent-team run` recorded by `eval --last` (no verifier)

Like `DoctorReport` and `Proposal`, these are exported from `agent_team.schemas`
but are not canonical kinds: they live in `evals/` and `.agent-team/`, not `canonical/`.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agent_team.schemas.common import CanonicalModel, KebabName

TaskKind = Literal["locate-bug", "fix-bug", "small-feature", "repair-failing-test"]

# Baseline A: mini-swe-agent; baseline B: one `claude -p` session; treatment: the bug-fix workflow.
ArmName = Literal["baseline-a", "baseline-b", "treatment"]
ARMS: tuple[ArmName, ...] = ("baseline-a", "baseline-b", "treatment")

# `fixture:<name>` is a tiny project under evals/fixtures/<name>/repo, committed at a fixed ref;
# anything else is a git URL or path, cloned and checked out at `ref`.
FIXTURE_PREFIX = "fixture:"

Command = Annotated[str, StringConstraints(min_length=1)]


class Verifier(CanonicalModel):
    command: Command = Field(description="Run from the root of the agent's final tree, inside srt; exit 0 means the task is done.")
    files: str | None = Field(
        default=None,
        description="Directory, relative to the task file, copied over the final tree before the command runs. "
        "Never in the repository an agent works in, and unreadable from every agent's sandbox.",
    )
    timeout_s: float = Field(default=300, gt=0, le=3600)


class EvalTask(CanonicalModel):
    id: KebabName = Field(description="Must match the task file's stem.")
    kind: TaskKind
    repo: str = Field(min_length=1, description="fixture:<name>, or a git URL or path to clone.")
    ref: str = Field(min_length=1, description="The commit every arm starts from; for a fixture, the commit it materializes to.")
    instruction: str = Field(min_length=1, description="What every arm is told, verbatim.")
    test_command: Command | None = Field(
        default=None, description="The repository's own test command: agents may run it, the treatment's TEST step does."
    )
    existing_tests: list[Command] = Field(
        default_factory=list,
        description="One command per existing test; one that passes at the ref and fails afterwards is a regression.",
    )
    planned_files: list[str] = Field(
        min_length=1, description="Globs of the files a fix should touch; lines changed elsewhere are unnecessary diff."
    )
    verifier: Verifier


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Caps(EvalModel):
    per_task_usd: float = Field(gt=0)
    total_usd: float = Field(gt=0)


class CheckResult(EvalModel):
    """One command run by the harness inside srt (the verifier, or one existing test)."""

    command: str
    exit_code: int | None = Field(description="None: killed at the timeout, or never started (see `error`).")
    duration_ms: int = Field(ge=0)
    passed: bool
    error: str | None = None
    output_tail: str | None = Field(default=None, description="Redacted end of the output, kept when the check failed.")


class ExistingTestResult(EvalModel):
    command: str
    base_passed: bool
    after_passed: bool

    @property
    def regressed(self) -> bool:
        return self.base_passed and not self.after_passed


class DiffStats(EvalModel):
    files: list[str] = Field(default_factory=list)
    insertions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    unplanned_files: list[str] = Field(default_factory=list)
    unplanned_lines: int = Field(default=0, ge=0, description="Inserted plus deleted lines in files outside planned_files.")


CellStatus = Literal["completed", "aborted", "error", "not_run"]


class CellResult(EvalModel):
    """One task x arm x run. `not_run` cells were stopped by the total cap before they started."""

    record: Literal["cell"] = "cell"
    eval_id: str
    task_id: str
    task_kind: TaskKind
    arm: ArmName
    run_index: int = Field(ge=1)
    status: CellStatus
    abort_reason: str | None = Field(default=None, description="Set whenever a budget cap stopped the cell, or kept it from starting.")
    error: str | None = None
    started_at: datetime
    wall_time_s: float | None = Field(default=None, ge=0, description="The arm only: materializing and verifying are not counted.")
    cost_usd: float | None = Field(default=None, ge=0, description="As the runtime reports it; None when the cell never ran.")
    cost_cap_usd: float = Field(gt=0)
    over_cap: bool = False
    tokens: dict[str, int] | None = None
    tokens_note: str | None = Field(default=None, description="Why `tokens` is None.")
    retries: int | None = Field(default=None, ge=0)
    human_interventions: int | None = Field(default=None, ge=0, description="RESUME transitions: a human restarting a stopped run.")
    model: str | None = None
    base_commit: str | None = None
    final_commit: str | None = None
    diff: DiffStats | None = None
    verifier: CheckResult | None = None
    existing_tests: list[ExistingTestResult] = Field(default_factory=list)
    run_ids: list[str] = Field(default_factory=list, description="Run-record ids in the cell's task repository.")
    work_dir: str | None = None
    notes: list[str] = Field(default_factory=list)


class ArmPlan(EvalModel):
    arm: ArmName
    description: str
    model: str
    refusal: str | None = Field(default=None, description="Why the arm cannot run here; an --execute refuses while any arm has one.")


class Plan(EvalModel):
    plan_hash: str
    tasks: list[str]
    arms: list[ArmPlan]
    runs: int = Field(ge=1)
    caps: Caps
    timeout_s: float = Field(gt=0)
    probe: bool


class EvalRecord(EvalModel):
    record: Literal["eval"] = "eval"
    eval_id: str
    started_at: datetime
    plan: Plan


class DryRunRecord(EvalModel):
    record: Literal["dry_run"] = "dry_run"
    at: datetime
    plan: Plan


class ProbeRecord(EvalModel):
    record: Literal["probe"] = "probe"
    eval_id: str
    at: datetime
    passed: bool
    reason: str
    cost_usd: float = Field(ge=0)
    run_id: str | None = None
    model: str | None = None


class FieldRecord(EvalModel):
    """A real workflow run recorded by `agent-team eval --last`: what its run record supports, nothing more."""

    record: Literal["field"] = "field"
    at: datetime
    repo: str
    run_id: str
    workflow: str
    outcome: str | None
    end_state: str
    wall_time_s: float = Field(ge=0)
    cost_usd: float = Field(ge=0)
    retries: int = Field(ge=0)
    human_interventions: int = Field(ge=0)
    escalated: bool


EvalLine = Annotated[
    CellResult | EvalRecord | DryRunRecord | ProbeRecord | FieldRecord, Field(discriminator="record")
]
