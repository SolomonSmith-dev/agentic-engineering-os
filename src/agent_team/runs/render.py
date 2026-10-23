"""Plain-text rendering for `agent-team trace` and the per-transition status line."""

from agent_team.schemas.runs import RunRecord, StateTransition, ToolCallRecord


def format_elapsed(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def format_cost(usd: float) -> str:
    return f"${usd:.4f}"


def status_line(t: StateTransition) -> str:
    """One line per state transition with elapsed time and cost so far."""
    move = t.to_state if t.from_state is None else f"{t.from_state} -> {t.to_state}"
    line = f"+{format_elapsed(t.elapsed_s)}  {format_cost(t.cost_usd)}  {move}"
    return f"{line}  ({t.note})" if t.note else line


def render_trace(
    run: RunRecord,
    transitions: list[StateTransition],
    calls: list[ToolCallRecord],
    *,
    show_calls: bool = False,
) -> str:
    outcome = run.outcome or "running"
    lines = [
        f"run {run.run_id}  workflow {run.workflow}  model {run.model_class or '-'}  outcome {outcome}",
        f"started {run.started_at.isoformat(timespec='seconds')}  worktree {run.worktree_path or '-'}",
        "",
    ]
    lines += [f"  {status_line(t)}" for t in transitions]
    if run.ended_at is not None:
        lines.append(f"  +{format_elapsed(run.elapsed_s)}  {format_cost(run.cost_usd)}  end: {outcome}")
    if show_calls and calls:
        lines += ["", "tool calls:"]
        for c in calls:
            lines.append(
                f"  {c.seq:>3}  {c.state:<15} {c.tool_name:<12} {c.result_status:<7} "
                f"{c.duration_ms:>7}ms  {format_cost(c.cost_usd)}  {c.args_summary}"
            )
    by_status: dict[str, int] = {}
    for c in calls:
        by_status[c.result_status] = by_status.get(c.result_status, 0) + 1
    breakdown = ", ".join(f"{n} {s}" for s, n in sorted(by_status.items()))
    lines += [
        "",
        f"{len(transitions)} transition(s), {len(calls)} tool call(s)"
        + (f" ({breakdown})" if breakdown else "")
        + f", elapsed {format_elapsed(run.elapsed_s)}, cost {format_cost(run.cost_usd)}",
    ]
    return "\n".join(lines)


def render_run_list(runs: list[RunRecord]) -> str:
    if not runs:
        return "no runs recorded"
    header = ("RUN", "WORKFLOW", "STATE", "OUTCOME", "ELAPSED", "COST", "STARTED")
    rows = [
        (
            r.run_id,
            r.workflow,
            r.state,
            r.outcome or "running",
            format_elapsed(r.elapsed_s),
            format_cost(r.cost_usd),
            r.started_at.isoformat(timespec="seconds"),
        )
        for r in runs
    ]
    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(len(header))]
    return "\n".join("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() for row in (header, *rows))
