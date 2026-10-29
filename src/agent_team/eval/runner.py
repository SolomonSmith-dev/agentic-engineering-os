"""`agent-team eval run`: plan the matrix, price it, and run it under two caps (build-order step 13).

    tasks x arms x runs -> plan (hashed) -> dry run (printed, recorded) -> --execute (same plan only)
        -> refuse if any arm cannot run -> probe -> cells, task by task:
           total cap left for this cell's cap? -> fresh repo -> arm (per-task cap) -> snapshot -> verifier in srt

Cells run task by task, each task's arms back to back, so when the total cap
stops the run the tasks that finished have every arm. A cell starts only if
the spend so far plus its own cap fits under the total; the rest are recorded
`not_run` with the reason. A cell whose cost is unknown (mini-swe-agent died
before writing its trajectory) is charged its full cap.

While a cell runs, its repository lives in a fresh temporary directory, and
every arm's sandbox denies reading the task files, the fixtures, the verifier
files and `.agent-team/eval/` (earlier cells' trees, the results). After the
verifier the cell directory moves to `.agent-team/eval/work/<eval-id>/`, where
`agent-team trace -C <cell>/repo` reads its run record.
"""

import hashlib
import json
import math
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from agent_team.eval.arms import (
    ARM_DESCRIPTIONS,
    ARM_RUNNERS,
    EVAL_ENVIRONMENT,
    PROBE_BUDGET_USD,
    ArmContext,
    ArmOutcome,
    probe,
)
from agent_team.eval.store import EvalStore
from agent_team.eval.tasks import LoadedTask, TaskError, materialize
from agent_team.eval.verify import VerifyError, check_commands, diff_stats, snapshot
from agent_team.run_agent import RunAgentError, load_canonical
from agent_team.routing import REGISTRY_PATH
from agent_team.schemas.eval import (
    ArmName,
    ArmPlan,
    Caps,
    CellResult,
    CheckResult,
    DiffStats,
    EvalRecord,
    ExistingTestResult,
    Plan,
    ProbeRecord,
)

Echo = Callable[[str], None]
Clock = Callable[[], datetime]

EPSILON = 1e-9


class EvalError(RuntimeError):
    """The eval cannot start or must stop; the message says why and what to do."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


# --- the plan -------------------------------------------------------------------------------


def plan_hash(tasks: list[LoadedTask], arms: list[ArmPlan], runs: int, caps: Caps, timeout_s: float, probe_on: bool) -> str:
    """Identity of a plan: the task files' bytes, the arms and their models, runs, caps, timeout, probe."""
    payload = {
        "tasks": [(t.task.id, hashlib.sha256(t.path.read_bytes()).hexdigest()) for t in tasks],
        "arms": [(a.arm, a.model) for a in arms],
        "runs": runs,
        "caps": caps.model_dump(),
        "timeout_s": timeout_s,
        "probe": probe_on,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def build_plan(
    tasks: list[LoadedTask], arms: list[ArmPlan], *, runs: int, caps: Caps, timeout_s: float, probe_on: bool
) -> Plan:
    return Plan(
        plan_hash=plan_hash(tasks, arms, runs, caps, timeout_s, probe_on),
        tasks=[t.task.id for t in tasks],
        arms=arms,
        runs=runs,
        caps=caps,
        timeout_s=timeout_s,
        probe=probe_on,
    )


@dataclass(frozen=True)
class Cell:
    task: LoadedTask
    arm: ArmName
    run_index: int

    @property
    def name(self) -> str:
        return f"{self.task.task.id}--{self.arm}--{self.run_index}"


def cells(tasks: list[LoadedTask], arms: list[ArmName], runs: int) -> list[Cell]:
    """Task by task, run by run, every arm back to back."""
    return [Cell(t, a, r) for t in tasks for r in range(1, runs + 1) for a in arms]


@dataclass(frozen=True)
class Estimate:
    cells: int
    worst_case_usd: float
    probe_usd: float
    cells_within_total: int
    observed: dict[str, tuple[int, float]]  # arm -> (cells it ran, mean cost per cell) from earlier evals
    expected_usd: float | None  # every arm observed: cells per arm x its mean, plus the probe cap


def estimate(plan: Plan, history: list[CellResult]) -> Estimate:
    """Worst case from the caps; an expected figure only from costs this store actually recorded."""
    n = len(plan.tasks) * len(plan.arms) * plan.runs
    probe_usd = PROBE_BUDGET_USD if plan.probe else 0.0
    cap, total = plan.caps.per_task_usd, plan.caps.total_usd
    within = max(0, min(n, math.floor((total - probe_usd) / cap + EPSILON)))
    observed: dict[str, tuple[int, float]] = {}
    for arm in plan.arms:
        costs = [c.cost_usd for c in history if c.arm == arm.arm and c.cost_usd is not None and c.status != "not_run"]
        if costs:
            observed[arm.arm] = (len(costs), sum(costs) / len(costs))
    per_arm = len(plan.tasks) * plan.runs
    expected = None
    if plan.arms and all(a.arm in observed for a in plan.arms):
        expected = sum(per_arm * observed[a.arm][1] for a in plan.arms) + probe_usd
    return Estimate(n, n * cap + probe_usd, probe_usd, within, observed, expected)


def render_plan(plan: Plan, est: Estimate, *, probe_model: str | None) -> str:
    caps = plan.caps
    lines = [
        f"plan {plan.plan_hash}: {len(plan.tasks)} task(s) x {len(plan.arms)} arm(s) x {plan.runs} run(s) = "
        f"{est.cells} cell(s), task by task",
        "tasks  " + ", ".join(plan.tasks),
        "arms",
    ]
    for arm in plan.arms:
        state = f"refused: {arm.refusal}" if arm.refusal else "ready"
        lines.append(f"  {arm.arm:<11} {arm.description}; {arm.model}")
        lines.append(f"  {'':<11} {state}")
    lines.append(
        f"caps   ${caps.per_task_usd:.2f} per cell, ${caps.total_usd:.2f} in total; "
        f"agent calls time out after {plan.timeout_s:g} s"
    )
    if plan.probe:
        lines.append(f"probe  one sandboxed-Bash check on {probe_model or 'the cheap class'} first, at most ${est.probe_usd:.2f}")
    else:
        lines.append("probe  skipped (--skip-probe)")
    lines.append("estimate")
    probe_part = " plus the probe" if plan.probe else ""
    lines.append(
        f"  worst case  ${est.worst_case_usd:.2f}: {est.cells} cell(s) at the ${caps.per_task_usd:.2f} cap{probe_part}"
    )
    if est.worst_case_usd > caps.total_usd + EPSILON:
        lines.append(
            f"  total cap   ${caps.total_usd:.2f} holds: in the worst case {est.cells_within_total} cell(s) start and "
            f"{est.cells - est.cells_within_total} are recorded not_run"
        )
    else:
        lines.append(f"  total cap   ${caps.total_usd:.2f} is above the worst case; every cell can start")
    if est.observed:
        seen = ", ".join(f"{arm} ${mean:.2f}/cell over {n}" for arm, (n, mean) in est.observed.items())
        lines.append(f"  observed    {seen}")
        if est.expected_usd is not None:
            lines.append(f"  expected    ${est.expected_usd:.2f} at those means (not a cap)")
        else:
            missing = [a.arm for a in plan.arms if a.arm not in est.observed]
            lines.append(f"  expected    not estimated: no recorded cost for {', '.join(missing)}")
    else:
        lines.append("  expected    not estimated: no earlier cell in this store has a recorded cost")
    return "\n".join(lines)


# --- the eval's own canonical tree ------------------------------------------------------------


def prepare_canonical(canonical_dir: Path, registry_path: Path, dest: Path, deny_read: list[Path]) -> Path:
    """Copy canonical/ and the registry under `dest`, adding the `eval` environment: default plus deny_read.

    The copy also freezes what the eval ran with; it lives beside the eval's cells.
    """
    target = dest / "canonical"
    shutil.copytree(canonical_dir, target)
    (dest / REGISTRY_PATH).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(registry_path, dest / REGISTRY_PATH)
    default = yaml.safe_load((canonical_dir / "environments" / "default.yaml").read_text(encoding="utf-8"))
    env = {
        **default,
        "name": EVAL_ENVIRONMENT,
        "description": "The default environment, plus the eval's tasks, fixtures, verifier files and results unreadable.",
        "deny_read": sorted({str(p) for p in deny_read}),
    }
    (target / "environments" / f"{EVAL_ENVIRONMENT}.yaml").write_text(yaml.safe_dump(env, sort_keys=False), encoding="utf-8")
    try:
        load_canonical(target)
    except RunAgentError as exc:
        raise EvalError(f"the eval's canonical copy is invalid: {exc}") from exc
    return target


def hidden_paths(tasks: list[LoadedTask], store: EvalStore) -> list[Path]:
    """What no arm may read: every task file, fixture and verifier directory, and the results store."""
    paths = {store.dir.resolve()}
    for loaded in tasks:
        paths.update(loaded.hidden_paths())
        paths.add(loaded.path.parent.resolve())
        if loaded.fixture_dir is not None:
            paths.add(loaded.fixture_dir.parent.resolve())  # every fixture, not only the selected tasks'
    return sorted(paths)


# --- one cell ------------------------------------------------------------------------------------


def _base_checks(loaded: LoadedTask, repo: Path, base: str, work: Path, ctx: ArmContext) -> list[CheckResult]:
    task = loaded.task
    if not task.existing_tests:
        return []
    return check_commands(
        repo, base, work, task.existing_tests, files=loaded.verifier_dir, srt=ctx.srt, deny_read=ctx.deny_read,
        timeout_s=task.verifier.timeout_s, redaction=ctx.redaction,
    )  # fmt: skip


def run_cell(
    cell: Cell,
    *,
    eval_id: str,
    cap: float,
    ctx: ArmContext,
    store: EvalStore,
    base_cache: dict[str, list[CheckResult]],
    clock: Clock = _utc_now,
) -> CellResult:
    """Materialize, run the arm, snapshot, verify, and move the cell under `.agent-team/eval/work/`."""
    loaded, task = cell.task, cell.task.task
    started = clock()
    cell_dir = Path(tempfile.mkdtemp(prefix="agent-team-eval-")).resolve()
    repo = cell_dir / "repo"
    common = dict(eval_id=eval_id, task_id=task.id, task_kind=task.kind, arm=cell.arm, run_index=cell.run_index,
                  started_at=started, cost_cap_usd=cap)  # fmt: skip
    try:
        base = materialize(loaded, repo)
        if task.id not in base_cache:
            # Not in the cell directory: the checked tree carries the verifier's files, and the arm runs next.
            scratch = Path(tempfile.mkdtemp(prefix="agent-team-eval-base-")).resolve()
            try:
                base_cache[task.id] = _base_checks(loaded, repo, base, scratch, ctx)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
    except (TaskError, VerifyError, OSError) as exc:
        shutil.rmtree(cell_dir, ignore_errors=True)
        return CellResult(status="error", error=f"materialize: {exc}", cost_usd=0.0, **common)  # type: ignore[arg-type]

    t0 = time.monotonic()
    try:
        outcome = ARM_RUNNERS[cell.arm](loaded, repo, cell_dir, cap, ctx)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 - one broken cell must not end the eval; it is recorded as an error
        outcome = ArmOutcome(status="error", tree=repo, cost_usd=None, model="?", error=f"{type(exc).__name__}: {exc}")
    wall = round(time.monotonic() - t0, 3)

    diff: DiffStats | None = None
    final: str | None = None
    verifier: CheckResult | None = None
    existing: list[ExistingTestResult] = []
    notes = list(outcome.notes)
    try:
        snap = snapshot(outcome.tree, repo)
        final = snap.commit
        if snap.committed:
            notes.append(f"the harness committed {snap.committed} path(s) the arm left uncommitted")
        diff = diff_stats(repo, base, final, task.planned_files)
        [verifier] = check_commands(
            repo, final, cell_dir / "verify", [task.verifier.command], files=loaded.verifier_dir, srt=ctx.srt,
            deny_read=ctx.deny_read, timeout_s=task.verifier.timeout_s, redaction=ctx.redaction,
        )  # fmt: skip
        after = _base_checks(loaded, repo, final, cell_dir / "after", ctx)
        existing = [
            ExistingTestResult(command=b.command, base_passed=b.passed, after_passed=a.passed)
            for b, a in zip(base_cache[task.id], after, strict=True)
        ]
    except (TaskError, VerifyError, OSError) as exc:
        notes.append(f"verification failed: {exc}")
        if outcome.status != "error":
            outcome.status, outcome.error = "error", f"verification: {exc}"

    target = store.work_dir / eval_id / cell.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(cell_dir), target)
    notes.append(f"moved from {cell_dir}; git worktree paths inside point there")

    cost = outcome.cost_usd
    return CellResult(
        status=outcome.status,
        abort_reason=outcome.abort_reason,
        error=outcome.error,
        wall_time_s=wall,
        cost_usd=cost,
        over_cap=cost is not None and cost > cap + EPSILON,
        tokens=outcome.tokens,
        tokens_note=outcome.tokens_note if outcome.tokens is None else None,
        retries=outcome.retries,
        human_interventions=outcome.interventions,
        model=outcome.model,
        base_commit=base,
        final_commit=final,
        diff=diff,
        verifier=verifier,
        existing_tests=existing,
        run_ids=outcome.run_ids,
        work_dir=str(target),
        notes=notes,
        **common,  # type: ignore[arg-type]
    )


# --- the whole eval ---------------------------------------------------------------------------------


def new_eval_id(now: datetime) -> str:
    return f"{now.astimezone(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def charged(result: CellResult) -> float:
    """What a cell counts against the total cap: its reported cost, or its whole cap when that is unknown."""
    if result.status == "not_run":
        return 0.0
    return result.cost_usd if result.cost_usd is not None else result.cost_cap_usd


def run_eval(
    store: EvalStore,
    plan: Plan,
    tasks: list[LoadedTask],
    ctx: ArmContext,
    *,
    eval_id: str,
    clock: Clock = _utc_now,
    echo: Echo = print,
) -> str:
    """Run every cell of `plan` under its caps, recording each line as it lands. Returns the eval id."""
    refused = [f"{a.arm}: {a.refusal}" for a in plan.arms if a.refusal]
    if refused:
        raise EvalError("cannot run: " + "; ".join(refused))
    store.append(EvalRecord(eval_id=eval_id, started_at=clock(), plan=plan))
    echo(f"eval {eval_id}: plan {plan.plan_hash}, caps ${plan.caps.per_task_usd:.2f} per cell, ${plan.caps.total_usd:.2f} total")
    cap, total = plan.caps.per_task_usd, plan.caps.total_usd
    spent = 0.0

    if plan.probe:
        if PROBE_BUDGET_USD > total + EPSILON:
            raise EvalError(f"the probe's ${PROBE_BUDGET_USD:.2f} does not fit under --max-total-usd {total:.2f}")
        echo("probe: the reviewer must run Bash inside srt")
        probe_dir = Path(tempfile.mkdtemp(prefix="agent-team-eval-probe-")).resolve()
        result = probe(ctx, probe_dir)
        target = store.work_dir / eval_id / "probe"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(probe_dir), target)
        store.append(ProbeRecord(eval_id=eval_id, at=clock(), passed=result.passed, reason=result.reason,
                                 cost_usd=result.cost_usd, run_id=result.run_id, model=result.model))  # fmt: skip
        spent += result.cost_usd
        echo(f"probe {'passed' if result.passed else 'FAILED'}: {result.reason} (${result.cost_usd:.4f})")
        if not result.passed:
            raise EvalError(
                f"the probe failed, so no cell ran: {result.reason}. Workers would have no shell; "
                f"see the run record in {target / 'probe-repo'} and README 'Run one agent'"
            )

    base_cache: dict[str, list[CheckResult]] = {}
    planned = cells(tasks, [a.arm for a in plan.arms], plan.runs)
    for number, cell in enumerate(planned, 1):
        label = f"cell {number}/{len(planned)} {cell.task.task.id} {cell.arm} run {cell.run_index}"
        if spent + cap > total + EPSILON:
            reason = f"total budget: ${spent:.4f} of ${total:.2f} spent; this cell's ${cap:.2f} cap does not fit"
            store.append(CellResult(
                eval_id=eval_id, task_id=cell.task.task.id, task_kind=cell.task.task.kind, arm=cell.arm,
                run_index=cell.run_index, status="not_run", abort_reason=reason, started_at=clock(), cost_cap_usd=cap,
            ))  # fmt: skip
            echo(f"{label}: not run ({reason})")
            continue
        echo(f"{label}")
        result = run_cell(cell, eval_id=eval_id, cap=cap, ctx=ctx, store=store, base_cache=base_cache, clock=clock)
        store.append(result)
        spent += charged(result)
        verdict = "verifier passed" if result.verifier and result.verifier.passed else "verifier failed"
        cost = "cost unknown" if result.cost_usd is None else f"${result.cost_usd:.4f}"
        tail = f"; {result.abort_reason}" if result.abort_reason else (f"; {result.error}" if result.error else "")
        echo(f"  {result.status}: {verdict}, {cost}, {result.wall_time_s or 0:.0f} s{tail}")
    echo(f"eval {eval_id} done: ${spent:.4f} of ${total:.2f} spent; agent-team eval report --eval {eval_id}")
    return eval_id
