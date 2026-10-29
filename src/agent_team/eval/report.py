"""`agent-team eval report`: the section 13 metrics per arm, from the recorded cells only.

Every number is computed from `results.jsonl`. A metric the records cannot
support prints "not measured" with a footnote saying why; nothing is filled in.

    completion rate        verifier passes / cells that ran
    test pass rate         existing tests passing after / existing tests run, over cells that ran
    regressions            existing tests that passed at the ref and fail after
    retries                reviewer-requested implementer retries (0 for a one-shot arm)
    wall time              the arm alone, not materializing or verifying
    tokens, dollars        as each runtime reported them
    human interventions    RESUME transitions (0 in an automated eval, recorded all the same)
    unnecessary diff       lines changed outside the task's planned_files
    headline               dollars / passes, beside interventions / passes

The decision rule compares the treatment with the best baseline on the
task-runs every compared arm ran, so a total-cap stop does not tilt it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from statistics import median

from agent_team.schemas.eval import ARMS, CellResult, EvalRecord, FieldRecord, ProbeRecord

NOT_MEASURED = "not measured"
COLUMN = 26
LABEL = 28

REVIEW_DEFECTS_NOTE = (
    "review defects: needs the findings a human reviewer adds after the agent reviewer; no record holds them yet"
)
CONTEXT_NOTE = "context consumed: no runtime reports context-window use into the run record or the trajectory"


@dataclass
class ArmMetrics:
    arm: str
    planned: int = 0
    ran: int = 0
    errors: int = 0
    passes: int = 0
    tests_run: int = 0
    tests_passed: int = 0
    regressions: int = 0
    regression_cells: int = 0
    cells_with_tests: int = 0
    retries: int | None = 0
    walls: list[float] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    token_cells: int = 0
    token_notes: list[str] = field(default_factory=list)
    dollars: float = 0.0
    unknown_cost: int = 0
    interventions: int | None = 0
    unplanned_lines: int = 0
    diff_cells: int = 0
    per_task_aborts: int = 0
    other_aborts: int = 0
    not_run: int = 0
    over_cap: int = 0
    not_run_reasons: list[str] = field(default_factory=list)

    @property
    def completion(self) -> float | None:
        return self.passes / self.ran if self.ran else None

    @property
    def regressions_per_cell(self) -> float | None:
        return self.regressions / self.cells_with_tests if self.cells_with_tests else None

    @property
    def cost_per_success(self) -> float | None:
        return self.dollars / self.passes if self.passes else None

    @property
    def interventions_per_success(self) -> float | None:
        return None if self.interventions is None or not self.passes else self.interventions / self.passes


def _tokens(tokens: dict[str, int]) -> tuple[int, int]:
    """(in, out) from either runtime's keys: Claude's input/cache/output, litellm's prompt/completion."""
    tin = sum(tokens.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "prompt_tokens"))
    tout = sum(tokens.get(k, 0) for k in ("output_tokens", "completion_tokens"))
    return tin, tout


def aggregate(arm: str, cells: list[CellResult]) -> ArmMetrics:
    m = ArmMetrics(arm=arm, planned=len(cells))
    for c in cells:
        if c.status == "not_run":
            m.not_run += 1
            if c.abort_reason:
                m.not_run_reasons.append(c.abort_reason)
            continue
        m.ran += 1
        m.errors += c.status == "error"
        m.passes += bool(c.verifier and c.verifier.passed)
        if c.existing_tests:
            m.cells_with_tests += 1
            m.tests_run += len(c.existing_tests)
            m.tests_passed += sum(t.after_passed for t in c.existing_tests)
            regressed = sum(t.regressed for t in c.existing_tests)
            m.regressions += regressed
            m.regression_cells += regressed > 0
        m.retries = None if m.retries is None or c.retries is None else m.retries + c.retries
        if c.wall_time_s is not None:
            m.walls.append(c.wall_time_s)
        if c.tokens:
            tin, tout = _tokens(c.tokens)
            m.tokens_in += tin
            m.tokens_out += tout
            m.token_cells += 1
        elif c.tokens_note:
            m.token_notes.append(c.tokens_note)
        if c.cost_usd is None:
            m.unknown_cost += 1
        else:
            m.dollars += c.cost_usd
        m.interventions = None if m.interventions is None or c.human_interventions is None else m.interventions + c.human_interventions
        if c.diff is not None:
            m.diff_cells += 1
            m.unplanned_lines += c.diff.unplanned_lines
        if c.abort_reason and c.abort_reason.startswith("per-task budget"):
            m.per_task_aborts += 1
        elif c.abort_reason:
            m.other_aborts += 1
        m.over_cap += c.over_cap
    return m


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)"


def _count(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1_000_000 else f"{n / 1e3:.1f}k" if n >= 1000 else str(n)


class Footnotes:
    def __init__(self) -> None:
        self.notes: list[str] = []

    def mark(self, note: str) -> str:
        if note not in self.notes:
            self.notes.append(note)
        return f"{NOT_MEASURED} ({self.notes.index(note) + 1})"


def _rows(metrics: list[ArmMetrics], refusals: dict[str, str], foot: Footnotes) -> list[tuple[str, list[str]]]:
    def cell(m: ArmMetrics, fn: Callable[[ArmMetrics], str | None], why: str) -> str:
        if m.ran == 0:
            reason = refusals.get(m.arm) or (m.not_run_reasons[0] if m.not_run_reasons else "no cell was planned")
            return foot.mark(f"{m.arm}: no cell ran ({reason})")
        value = fn(m)
        return value if value is not None else foot.mark(f"{m.arm}: {why}")

    rows: list[tuple[str, list[str]]] = []

    def row(label: str, fn: Callable[[ArmMetrics], str | None], why: str) -> None:
        rows.append((label, [cell(m, fn, why) for m in metrics]))

    row("cells run / planned", lambda m: f"{m.ran}/{m.planned}" + (f", {m.errors} error(s)" if m.errors else ""), "")
    row("completion rate", lambda m: _pct(m.passes, m.ran), "")
    row("test pass rate", lambda m: _pct(m.tests_passed, m.tests_run) if m.tests_run else None,
        "the tasks list no existing tests")  # fmt: skip
    row("regressions", lambda m: f"{m.regressions} in {m.regression_cells} cell(s), {m.regressions_per_cell:.2f}/cell"
        if m.cells_with_tests else None, "the tasks list no existing tests")  # fmt: skip
    row("retries", lambda m: (f"{m.retries}" + (" (one shot)" if m.arm != "treatment" else "")) if m.retries is not None else None,
        "a cell ended before its run record could say")  # fmt: skip
    row("wall time", lambda m: f"{sum(m.walls):.0f} s total, median {median(m.walls):.0f} s" if m.walls else None,
        "no cell recorded one")  # fmt: skip

    def tokens(m: ArmMetrics) -> str | None:
        if not m.token_cells:
            return None
        partial = f" ({m.token_cells}/{m.ran} cells)" if m.token_cells < m.ran else ""
        return f"in {_count(m.tokens_in)} / out {_count(m.tokens_out)}{partial}"

    rows.append(("tokens", [cell(m, tokens, m.token_notes[0] if m.token_notes else "no cell reported token counts")
                            for m in metrics]))  # fmt: skip
    row("dollars", lambda m: f"${m.dollars:.2f}" + (f" + {m.unknown_cost} cell(s) unknown" if m.unknown_cost else ""), "")
    row("human interventions", lambda m: str(m.interventions) if m.interventions is not None else None,
        "a cell ended before its run record could say")  # fmt: skip
    row("unnecessary diff lines", lambda m: f"{m.unplanned_lines} ({m.unplanned_lines / m.diff_cells:.1f}/cell)"
        if m.diff_cells else None, "no cell's tree could be diffed")  # fmt: skip
    rows.append(("review defects", [foot.mark(REVIEW_DEFECTS_NOTE) for _ in metrics]))
    rows.append(("context consumed", [foot.mark(CONTEXT_NOTE) for _ in metrics]))
    row("budget aborts", lambda m: f"{m.per_task_aborts} per-task, {m.not_run} total-cap" +
        (f", {m.other_aborts} other" if m.other_aborts else "") + (f", {m.over_cap} over cap" if m.over_cap else ""), "")  # fmt: skip

    def per_success(m: ArmMetrics) -> str | None:
        if not m.passes:
            return None
        cost = f"${m.cost_per_success:.2f}" + ("+" if m.unknown_cost else "")
        inter = "?" if m.interventions_per_success is None else f"{m.interventions_per_success:.2f}"
        return f"{cost} / {inter}"

    rows.append(("$ / interventions per pass", [cell(m, per_success, "no verifier pass to divide by") for m in metrics]))
    return rows


def _table(rows: list[tuple[str, list[str]]], arms: list[str]) -> list[str]:
    width = max([COLUMN, *(len(v) + 2 for _, values in rows for v in values)])
    lines = [f"{'metric':<{LABEL}}" + "".join(f"{a:<{width}}" for a in arms)]
    for label, values in rows:
        lines.append(f"{label:<{LABEL}}" + "".join(f"{v:<{width}}" for v in values))
    return [line.rstrip() for line in lines]


# --- the decision rule ---------------------------------------------------------------------------


BASELINES = ("baseline-a", "baseline-b")


def decision(cells: list[CellResult], *, planned_tasks: int, runs: int) -> list[str]:
    """Section 13's rule, on the task-runs that the treatment and every baseline that ran all have."""
    ran = [c for c in cells if c.status != "not_run"]
    present = [a for a in ARMS if any(c.arm == a for c in ran)]
    lines = [
        "decision rule (section 13): the treatment must beat the best baseline on at least two of completion,",
        "regressions and interventions, at no more than 2x the best baseline's cost per successful task.",
    ]
    if "treatment" not in present:
        return [*lines, "verdict: INCONCLUSIVE: the treatment ran no cell"]
    baselines = [a for a in BASELINES if a in present]
    if not baselines:
        return [*lines, "verdict: INCONCLUSIVE: no baseline ran a cell"]
    compared = ["treatment", *baselines]
    keys = set.intersection(*({(c.task_id, c.run_index) for c in ran if c.arm == a} for a in compared))
    if not keys:
        return [*lines, "verdict: INCONCLUSIVE: no task-run was run by every compared arm"]
    m = {a: aggregate(a, [c for c in ran if c.arm == a and (c.task_id, c.run_index) in keys]) for a in compared}
    missing = [a for a in BASELINES if a not in present]
    lines.append(f"compared on {len(keys)} task-run(s) that {', '.join(compared)} all ran"
                 + (f"; {', '.join(missing)} ran nothing and is left out" if missing else ""))  # fmt: skip
    t = m["treatment"]
    wins = 0
    undecided: list[str] = []

    def best(values: dict[str, float | None], lower: bool) -> tuple[str, float] | None:
        known = {a: v for a, v in values.items() if v is not None}
        if not known:
            return None
        arm = (min if lower else max)(known, key=lambda a: known[a])
        return arm, known[arm]

    def judge(name: str, tv: float | None, values: dict[str, float | None], lower: bool, fmt) -> None:  # type: ignore[no-untyped-def]
        nonlocal wins
        b = best(values, lower)
        if tv is None or b is None:
            undecided.append(name)
            lines.append(f"  {name:<14} not comparable: {NOT_MEASURED} for {'the treatment' if tv is None else 'every baseline'}")
            return
        beats = tv < b[1] if lower else tv > b[1]
        wins += beats
        word = "beats" if beats else ("tie, not a win" if tv == b[1] else "does not beat")
        lines.append(f"  {name:<14} treatment {fmt(tv)} vs best baseline {fmt(b[1])} ({b[0]}): {word}")

    judge("completion", t.completion, {a: m[a].completion for a in baselines}, False, lambda v: f"{100 * v:.0f}%")
    judge("regressions", t.regressions_per_cell, {a: m[a].regressions_per_cell for a in baselines}, True,
          lambda v: f"{v:.2f}/cell")  # fmt: skip
    judge("interventions", t.interventions_per_success, {a: m[a].interventions_per_success for a in baselines}, True,
          lambda v: f"{v:.2f}/pass")  # fmt: skip
    if all(m[a].interventions == 0 for a in compared):
        lines.append("  note           no arm can win interventions in an automated eval (every arm has 0), so the")
        lines.append("                 treatment must beat the best baseline on both completion and regressions")

    cost_ok: bool | None
    base_cost = best({a: m[a].cost_per_success for a in baselines}, True)
    if t.cost_per_success is None:
        cost_ok = False
        lines.append("  cost           the treatment passed no task, so it has no cost per successful task")
    elif base_cost is None:
        cost_ok = True
        lines.append(f"  cost           treatment ${t.cost_per_success:.2f}/pass; no baseline passed a task: within")
    else:
        cost_ok = t.cost_per_success <= 2 * base_cost[1] + 1e-9
        lines.append(f"  cost           treatment ${t.cost_per_success:.2f}/pass vs 2 x ${base_cost[1]:.2f} ({base_cost[0]}) = "
                     f"${2 * base_cost[1]:.2f}: {'within' if cost_ok else 'over'}")  # fmt: skip
    if any(m[a].unknown_cost for a in compared):
        lines.append("  note           some cells' cost is unknown; their arms' cost per pass is a lower bound")

    if wins >= 2 and cost_ok:
        verdict = f"PASS ({wins} of 3, cost within 2x): the gated work may proceed"
    elif wins + len(undecided) >= 2 and cost_ok:
        verdict = f"INCONCLUSIVE: {wins} win(s), and {', '.join(undecided)} could not be compared"
    else:
        why = f"{wins} of 3 metrics beaten" + ("" if cost_ok else ", cost over 2x or no pass")
        verdict = f"FAIL ({why}): cut the extra agent, per section 13"
    lines.append(f"verdict: {verdict}")
    lines.append(f"provisional: {planned_tasks} task(s) x {runs} run(s) here; section 13 asks for 30 private tasks x 3 runs")
    return lines


def render_report(
    record: EvalRecord, cells: list[CellResult], probes: list[ProbeRecord]
) -> str:
    plan = record.plan
    arms = [a.arm for a in plan.arms]
    refusals = {a.arm: a.refusal for a in plan.arms if a.refusal}
    spent = sum(c.cost_usd if c.cost_usd is not None else c.cost_cap_usd for c in cells if c.status != "not_run")
    probe_cost = sum(p.cost_usd for p in probes)
    lines = [
        f"eval {record.eval_id} (plan {plan.plan_hash}, started {record.started_at:%Y-%m-%d %H:%M} UTC): "
        f"{len(plan.tasks)} task(s) x {len(arms)} arm(s) x {plan.runs} run(s)",
        f"spent ${spent + probe_cost:.2f} of ${plan.caps.total_usd:.2f} (cells ${spent:.2f}, probe ${probe_cost:.2f}); "
        f"${plan.caps.per_task_usd:.2f} cap per cell",
    ]
    for p in probes:
        lines.append(f"probe {'passed' if p.passed else 'FAILED'} on {p.model}: {p.reason}")
    foot = Footnotes()
    metrics = [aggregate(a, [c for c in cells if c.arm == a]) for a in arms]
    lines += ["", *_table(_rows(metrics, refusals, foot), arms)]
    lines += ["", "headline: cost per successful task (dollars / verifier passes) beside interventions per successful task;"]
    lines.append("          the row '$ / interventions per pass' above. A '+' marks a lower bound (a cell's cost unknown).")
    if foot.notes:
        lines += ["", *(f"({i}) {note}" for i, note in enumerate(foot.notes, 1))]
    lines += ["", *decision(cells, planned_tasks=len(plan.tasks), runs=plan.runs)]
    return "\n".join(lines)


def render_field(record: FieldRecord, baseline: str | None) -> str:
    lines = [
        f"run {record.run_id} ({record.workflow}) in {record.repo}: {record.outcome or 'open'} in {record.end_state}",
        f"  wall time            {record.wall_time_s:.0f} s",
        f"  dollars              ${record.cost_usd:.4f}",
        f"  retries              {record.retries}",
        f"  human interventions  {record.human_interventions} (RESUME transitions)" + (", escalated" if record.escalated else ""),
        f"  completion           {NOT_MEASURED}: a real task has no hidden verifier; the human decision on the PR is the verdict",
        f"  tokens               {NOT_MEASURED}: the run record keeps dollars, not tokens",
    ]
    if baseline:
        lines += ["", baseline]
    return "\n".join(lines)
