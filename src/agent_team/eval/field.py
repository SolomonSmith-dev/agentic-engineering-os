"""`agent-team eval --last`: record the repository's last workflow run against the baseline (section 9, item 8).

A real run has no hidden verifier, so only what its run record supports is
kept: outcome, wall time, cost, retries, RESUME transitions, escalation. The
record goes to that repository's `.agent-team/eval/results.jsonl` once per run;
the latest eval in the same store, if any, is printed beside it.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from agent_team.eval.report import aggregate
from agent_team.eval.store import EvalStore
from agent_team.runs.store import RunStore
from agent_team.schemas.eval import FieldRecord


class FieldError(RuntimeError):
    """There is no finished workflow run to record; the message says why."""


def last_workflow_run(repo: Path, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> FieldRecord:
    store = RunStore.open(repo, create=False)
    if store is None:
        raise FieldError(f"no run record in {repo}; run 'agent-team run --workflow bug-fix' first")
    with store:
        runs = [r for r in store.list_runs(1000) if not r.workflow.startswith("agent:")]
        if not runs:
            raise FieldError(f"no workflow run in {repo}'s run record; 'eval --last' records 'run --workflow' runs")
        run = runs[0]
        if run.ended_at is None:
            raise FieldError(f"run {run.run_id} has not ended; resume it ('agent-team resume {run.run_id}') first")
        transitions = store.transitions(run.run_id)
        checkpoint = store.last_checkpoint(run.run_id)
    return FieldRecord(
        at=clock(),
        repo=str(store.path.parent.parent),
        run_id=run.run_id,
        workflow=run.workflow,
        outcome=run.outcome,
        end_state=run.state,
        wall_time_s=run.elapsed_s,
        cost_usd=run.cost_usd,
        retries=sum(1 for t in transitions if t.from_state == "CODE_REVIEW" and t.to_state == "IMPLEMENTATION"),
        human_interventions=sum(1 for t in transitions if t.to_state == "RESUME"),
        escalated=bool(checkpoint and checkpoint.data.get("escalated")),
    )


def record_last(repo: Path) -> tuple[FieldRecord, bool, str | None]:
    """The field record, whether it was new, and the latest eval's headline from the same store."""
    record = last_workflow_run(repo)
    store = EvalStore.open(repo)
    known = {f.run_id for f in store.fields()}
    new = record.run_id not in known
    if new:
        store.append(record)
    evals = store.evals()
    baseline = None
    if evals:
        latest = evals[-1]
        cells = store.cells(latest.eval_id)
        parts = []
        for arm in (a.arm for a in latest.plan.arms):
            m = aggregate(arm, [c for c in cells if c.arm == arm])
            parts.append(f"{arm} ${m.cost_per_success:.2f}/pass" if m.cost_per_success is not None else f"{arm} no pass")
        baseline = f"latest eval {latest.eval_id}: " + ", ".join(parts)
    return record, new, baseline
