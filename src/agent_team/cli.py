"""Command-line entry point for agent-team."""

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import typer
import yaml

from agent_team import __version__
from agent_team.adapters.claude.invoke import ClaudeVersionError
from agent_team.adapters.codex.invoke import API_KEY_ENV, CodexUnavailableError, has_auth
from agent_team.bootstrap import BootstrapError, apply_plan, plan_configure, render_summary, write_proposal
from agent_team.canonical import validate_tree
from agent_team.doctor import _which, build_report, render_table
from agent_team.environments import AllocatorError, LockHeldError, srt_path, sweep
from agent_team.focus import analyze as analyze_focus
from agent_team.eval import (
    ARM_DESCRIPTIONS,
    BASELINE_CLASS,
    ArmContext,
    EvalError,
    EvalStore,
    EvalStoreError,
    FieldError,
    TaskError,
    baseline_model,
    build_plan,
    estimate,
    hidden_paths,
    load_tasks,
    new_eval_id,
    prepare_canonical,
    record_last,
    refusal,
    render_field,
    render_plan,
    render_report,
    run_eval,
    treatment_models,
)
from agent_team.eval.store import RESULTS
from agent_team.memory import DEFAULT_COMMAND_TIMEOUT_S, MEMORY_DIR, InspectError, inspect_repo, render_memory_summary
from agent_team import lessons as lessons_mod
from agent_team.orchestrator import OrchestratorError, default_executor_factory, resume_workflow, run_workflow
from agent_team.routing import REGISTRY_PATH, RegistryError, default_registry_path, load_registry, resolve_model
from agent_team.run_agent import RunAgentError, load_canonical, run_agent
from agent_team.runs import RunStore, RunStoreError, UnknownRunError, render_run_list, render_trace
from agent_team.runs.store import STATE_DIR, repo_root
from agent_team.schemas import SCHEMAS, RunRecord, json_schema
from agent_team.bootstrap.configure import CONFIG_NAME
from agent_team.schemas.bootstrap import RepoConfig
from agent_team.schemas.eval import ARMS, ArmPlan, Caps, DryRunRecord

# Runtimes `agent-team run --runtime` accepts; codex runs the independent reviewer only (step 12).
RUNTIMES: tuple[Literal["claude", "codex"], ...] = ("claude", "codex")

app = typer.Typer(
    name="agent-team",
    help="Run Claude Code workers and an independent reviewer in sandboxed git worktrees.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"agent-team {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Print the installed version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """agent-team: subcommands arrive one build-order step at a time."""


@app.command()
def validate(
    path: Path = typer.Argument(
        Path("canonical"),
        help="Directory holding agents/, skills/, workflows/, tools/ and policies/; routing/ beside it is checked too.",
    ),
) -> None:
    """Validate every file under canonical/ against the canonical schemas."""
    report = validate_tree(path)
    for problem in report.problems:
        typer.echo(problem.render(), err=True)
    kinds = {}
    for doc in report.documents:
        kinds[doc.kind] = kinds.get(doc.kind, 0) + 1
    summary = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in sorted(kinds.items())) or "0 files"
    if report.ok:
        typer.echo(f"ok: {summary} valid under {path}")
        return
    typer.echo(f"failed: {len(report.problems)} problem(s) in {summary} under {path}", err=True)
    raise typer.Exit(code=1)


@app.command()
def doctor(
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON instead of a table."),
) -> None:
    """Report host, runtimes, auth state, egress and managed settings. Read-only."""
    report = build_report()
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
        return
    typer.echo(render_table(report))


@app.command("init")
def init_repo(
    repo: Path = typer.Argument(Path("."), help="Target repository; defaults to the current directory."),
    force: bool = typer.Option(False, "--force", help="Replace an existing .agent-team/proposal.yaml."),
) -> None:
    """Discover a repository read-only and write .agent-team/proposal.yaml. Runs nothing from the repo."""
    try:
        proposal, path = write_proposal(repo, force=force)
    except (BootstrapError, RunStoreError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(render_summary(proposal))
    typer.echo("✓ means the command's tool and target were found; nothing was run to check it.")
    typer.echo(f"wrote {path}")
    typer.echo("review it, set ask.approved: true, then run 'agent-team configure'")


@app.command()
def configure(
    repo: Path = typer.Argument(Path("."), help="Target repository; defaults to the current directory."),
    canonical: Path = typer.Option(Path("canonical"), "--canonical", help="Directory holding the canonical files."),
    registry: Path | None = typer.Option(
        None, "--registry", help="Model registry; defaults to routing/registry.yaml next to --canonical."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Write without asking for confirmation."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the diff and write nothing."),
) -> None:
    """Turn the approved proposal into .agent-team/config.yaml and runtime files, showing the diff first."""
    try:
        plan = plan_configure(repo, canonical, registry_path=registry, require_approved=not dry_run)
    except (BootstrapError, RunStoreError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    for note in plan.notes:
        typer.echo(f"note: {note}")
    if not plan.writes:
        typer.echo("nothing to change")
        return
    typer.echo(plan.diff(), nl=False)
    typer.echo("files to write: " + ", ".join(w.path for w in plan.writes))
    if dry_run:
        typer.echo("dry run: nothing written")
        return
    if not yes and not typer.confirm("Write these files?", default=False):
        typer.echo("nothing written")
        raise typer.Exit(code=1)
    try:
        written = apply_plan(plan)
    except (BootstrapError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    for path in written:
        typer.echo(f"wrote {path}")


@app.command()
def trace(
    run_id: str | None = typer.Argument(None, help="Run to show; omit to list recent runs."),
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="Target repository; defaults to the current directory."),
    limit: int = typer.Option(20, "--limit", "-n", min=1, help="How many recent runs to list."),
    calls: bool = typer.Option(False, "--calls", help="Also list every tool call (arguments are stored redacted)."),
    focus: bool = typer.Option(False, "--focus", help="Also print progress, the longest stall and command loops."),
) -> None:
    """Print a run's state transitions with elapsed time and cost, or list recent runs."""
    try:
        store = RunStore.open(repo, create=False)
    except RunStoreError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if store is None:
        if run_id is not None:
            typer.echo(f"error: no run {run_id!r}; no run record exists yet", err=True)
            raise typer.Exit(code=1)
        typer.echo("no runs recorded")
        return
    with store:
        if run_id is None:
            typer.echo(render_run_list(store.list_runs(limit)))
            return
        try:
            run = store.get_run(run_id)
        except UnknownRunError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        tool_calls = store.tool_calls(run_id)
        typer.echo(render_trace(run, store.transitions(run_id), tool_calls, show_calls=calls))
        if focus:
            typer.echo(analyze_focus(tool_calls, _test_commands(repo)).render())


def _test_commands(repo: Path) -> list[str]:
    """The test commands configure verified, so a test run counts as progress; none when unconfigured."""
    path = repo_root(repo) / STATE_DIR / CONFIG_NAME
    try:
        config = RepoConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 - --focus still reports without test commands
        return []
    return list(config.commands.get("test", []))


# Errors that refuse a run with exit 2: something to fix before anything useful ran.
REFUSALS = (
    RunAgentError, ClaudeVersionError, CodexUnavailableError, RunStoreError, AllocatorError, LockHeldError,
    OrchestratorError,
)


def _claude_or_exit() -> str:
    claude = _which("claude")
    if claude is None:
        typer.echo("error: claude is not on PATH; run 'agent-team doctor'", err=True)
        raise typer.Exit(code=2)
    return claude


def _workflow_summary(record: RunRecord) -> None:
    typer.echo(
        f"{record.outcome}: workflow {record.workflow} ended in {record.state}, ${record.cost_usd:.4f}; "
        f"details: agent-team trace {record.run_id}"
    )
    if record.outcome != "succeeded":
        raise typer.Exit(code=1)


@app.command()
def run(
    prompt: str | None = typer.Argument(None, help="The task; omit to read it from --prompt-file."),
    agent: str | None = typer.Option(None, "--agent", "-a", help="Canonical agent to run, e.g. independent-reviewer."),
    workflow: str | None = typer.Option(
        None, "--workflow", "-w", help="Canonical workflow to run through the orchestrator, e.g. bug-fix."
    ),
    model: str | None = typer.Option(
        None, "--model", "-m", help="Override the agent's model class with this registry model id (--agent only)."
    ),
    runtime: str = typer.Option(
        "claude", "--runtime", help="Runtime to run the agent on: claude, or codex (the independent reviewer only)."
    ),
    prompt_file: Path | None = typer.Option(None, "--prompt-file", help="Read the prompt from this file."),
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="Target repository; defaults to the current directory."),
    canonical: Path = typer.Option(Path("canonical"), "--canonical", help="Directory holding the canonical files."),
    registry: Path | None = typer.Option(
        None, "--registry", help="Model registry; defaults to routing/registry.yaml next to --canonical."
    ),
    budget: float = typer.Option(
        1.00, "--max-budget-usd", min=0.01, help="Stop each agent call at this estimated spend."
    ),
    timeout: float = typer.Option(1800, "--timeout", min=1, help="Kill each agent call after this many seconds."),
    test_command: list[str] = typer.Option([], "--test-command", help="Test command the agent may run (repeatable)."),
    lint_command: list[str] = typer.Option([], "--lint-command", help="Lint command the TEST step runs (repeatable; --workflow)."),
    typecheck_command: list[str] = typer.Option(
        [], "--typecheck-command", help="Type-check command the TEST step runs (repeatable; --workflow)."
    ),
) -> None:
    """Run one canonical agent (--agent) or a workflow (--workflow), and record everything it does."""
    if (agent is None) == (workflow is None):
        typer.echo("error: give exactly one of --agent or --workflow", err=True)
        raise typer.Exit(code=2)
    if workflow is not None and model is not None:
        typer.echo("error: --model applies to --agent runs; a workflow resolves each agent's own model class", err=True)
        raise typer.Exit(code=2)
    if (prompt is None) == (prompt_file is None):
        typer.echo("error: give the prompt as an argument or with --prompt-file, not both", err=True)
        raise typer.Exit(code=2)
    try:
        text = prompt if prompt is not None else prompt_file.read_text(encoding="utf-8")  # type: ignore[union-attr]
    except (OSError, UnicodeDecodeError) as exc:
        typer.echo(f"error: cannot read --prompt-file {prompt_file}: {exc.strerror or exc}", err=True)
        raise typer.Exit(code=2) from exc
    if runtime not in RUNTIMES:
        typer.echo(f"error: --runtime must be one of {', '.join(RUNTIMES)}, not {runtime!r}", err=True)
        raise typer.Exit(code=2)
    if workflow is not None:
        if runtime != "claude":
            typer.echo("error: --runtime applies to --agent runs; a workflow resolves each agent's own runtime", err=True)
            raise typer.Exit(code=2)
        claude = _claude_or_exit()
        factory = default_executor_factory(
            repo=repo,
            canonical_dir=canonical,
            claude=claude,
            max_budget_usd=budget,
            timeout_s=timeout,
            registry_path=registry,
            test_commands=tuple(test_command),
            lint_commands=tuple(lint_command),
            typecheck_commands=tuple(typecheck_command),
            gh=_which("gh"),
            echo=typer.echo,
        )
        try:
            record = run_workflow(repo, canonical, workflow, text, executor_factory=factory, echo=typer.echo)
        except REFUSALS as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        except KeyboardInterrupt as exc:
            raise typer.Exit(code=130) from exc
        _workflow_summary(record)
        return
    binary = _which(runtime)
    if binary is None:
        typer.echo(f"error: {runtime} is not on PATH; run 'agent-team doctor'", err=True)
        raise typer.Exit(code=2)
    if runtime == "codex" and not has_auth(binary, dict(os.environ)):
        typer.echo(f"error: codex has no auth: set {API_KEY_ENV} for this shell", err=True)
        raise typer.Exit(code=2)
    try:
        record, result = run_agent(
            repo,
            canonical,
            agent,  # type: ignore[arg-type]
            text,
            model=model,
            registry_path=registry,
            runtime=runtime,  # type: ignore[arg-type]
            claude=binary if runtime == "claude" else None,
            codex=binary if runtime == "codex" else None,
            max_budget_usd=budget,
            timeout_s=timeout,
            test_commands=tuple(test_command),
            echo=typer.echo,
        )
    except REFUSALS as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    summary = (
        f"{record.outcome}: {result.status} in {result.num_turns or 0} turn(s), "
        f"{result.tool_calls} tool call(s), ${result.cost_usd:.4f}"
    )
    if result.usage:
        summary += f", {result.usage.get('input_tokens', 0)} in / {result.usage.get('output_tokens', 0)} out tokens"
    typer.echo(summary)
    if result.structured_output is not None:
        typer.echo(json.dumps(result.structured_output, indent=2))
    elif result.result:
        typer.echo(result.result)
    if result.stderr_tail:
        typer.echo(result.stderr_tail, err=True)
    if record.outcome != "succeeded":
        raise typer.Exit(code=1)


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="The interrupted workflow run to re-enter."),
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="Target repository; defaults to the current directory."),
    canonical: Path = typer.Option(Path("canonical"), "--canonical", help="Directory holding the canonical files."),
    registry: Path | None = typer.Option(
        None, "--registry", help="Model registry; defaults to routing/registry.yaml next to --canonical."
    ),
    budget: float = typer.Option(
        1.00, "--max-budget-usd", min=0.01, help="Stop each agent call at this estimated spend."
    ),
    timeout: float = typer.Option(1800, "--timeout", min=1, help="Kill each agent call after this many seconds."),
    test_command: list[str] = typer.Option([], "--test-command", help="Test command the agent may run (repeatable)."),
    lint_command: list[str] = typer.Option([], "--lint-command", help="Lint command the TEST step runs (repeatable)."),
    typecheck_command: list[str] = typer.Option([], "--typecheck-command", help="Type-check command the TEST step runs (repeatable)."),
) -> None:
    """Re-enter an interrupted workflow run at its last checkpoint; its worktrees were kept."""
    claude = _claude_or_exit()
    factory = default_executor_factory(
        repo=repo,
        canonical_dir=canonical,
        claude=claude,
        max_budget_usd=budget,
        timeout_s=timeout,
        registry_path=registry,
        test_commands=tuple(test_command),
        lint_commands=tuple(lint_command),
        typecheck_commands=tuple(typecheck_command),
        gh=_which("gh"),
        echo=typer.echo,
    )
    try:
        record = resume_workflow(repo, canonical, run_id, executor_factory=factory, echo=typer.echo)
    except UnknownRunError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except REFUSALS as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except KeyboardInterrupt as exc:
        raise typer.Exit(code=130) from exc
    _workflow_summary(record)


@app.command("inspect")
def inspect_cmd(
    repo: Path = typer.Argument(Path("."), help="Target repository; defaults to the current directory."),
    summary: bool = typer.Option(
        False, "--summary", help="Print the stored map and every command's verification result; run nothing."
    ),
    canonical: Path = typer.Option(Path("canonical"), "--canonical", help="Directory holding the canonical files."),
    registry: Path | None = typer.Option(
        None, "--registry", help="Model registry; defaults to routing/registry.yaml next to --canonical."
    ),
    budget: float = typer.Option(1.00, "--max-budget-usd", min=0.01, help="Stop the archaeologist at this estimated spend."),
    timeout: float = typer.Option(1800, "--timeout", min=1, help="Kill the archaeologist after this many seconds."),
    command_timeout: float = typer.Option(
        DEFAULT_COMMAND_TIMEOUT_S, "--command-timeout", min=1, help="Kill each verified command after this many seconds."
    ),
) -> None:
    """Map the repository at A0 into .agent-team/memory/repo/ and verify init's commands inside srt."""
    if summary:
        try:
            typer.echo(render_memory_summary(repo))
        except (InspectError, RunStoreError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        return
    claude = _claude_or_exit()
    try:
        outcome = inspect_repo(
            repo,
            canonical,
            claude=claude,
            registry_path=registry,
            max_budget_usd=budget,
            timeout_s=timeout,
            command_timeout_s=command_timeout,
            echo=typer.echo,
        )
    except (*REFUSALS, BootstrapError, InspectError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    record, result = outcome.record, outcome.result
    typer.echo(
        f"{record.outcome}: {result.status} in {result.num_turns or 0} turn(s), "
        f"{result.tool_calls} tool call(s), ${result.cost_usd:.4f}; details: agent-team trace {record.run_id} --calls"
    )
    if record.outcome != "succeeded":
        if result.structured_output_error:
            typer.echo(result.structured_output_error, err=True)
        if result.stderr_tail:
            typer.echo(result.stderr_tail, err=True)
        raise typer.Exit(code=1)
    for path in outcome.written:
        typer.echo(f"wrote {MEMORY_DIR / path.name}")
    typer.echo("")
    typer.echo(render_memory_summary(repo))


eval_app = typer.Typer(
    help="The step 13 eval: Harbor-shaped tasks, two baselines and the team, a hidden verifier, the section 13 report.",
    invoke_without_command=True,
)
app.add_typer(eval_app, name="eval")


def _csv(values: list[str]) -> list[str]:
    return [v.strip() for value in values for v in value.split(",") if v.strip()]


@eval_app.callback()
def eval_main(
    ctx: typer.Context,
    last: bool = typer.Option(
        False, "--last", help="Record this repository's last workflow run against the baseline (no verifier, no model)."
    ),
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="Repository whose run record --last reads."),
) -> None:
    """Run the eval (eval run), print its report (eval report), or record the last real run (eval --last)."""
    if ctx.invoked_subcommand is not None:
        return
    if not last:
        typer.echo(ctx.get_help())
        return
    try:
        record, new, baseline = record_last(repo)
    except (FieldError, RunStoreError, EvalStoreError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(render_field(record, baseline))
    typer.echo(f"{'recorded in' if new else 'already in'} .agent-team/eval/{RESULTS}")


@eval_app.command("run")
def eval_run(
    tasks: list[str] = typer.Option([], "--tasks", help="Task ids to run (comma-separated or repeated); default all."),
    arms: list[str] = typer.Option([], "--arms", help=f"Arms to run, from {', '.join(ARMS)}; default all."),
    runs: int = typer.Option(1, "--runs", min=1, help="Runs per task per arm. Section 13 asks for 3; 1 fits the $15 cap."),
    per_task: float = typer.Option(
        1.00, "--max-budget-usd-per-task", min=0.05, help="Cap on one cell (one task, one arm, one run), all its calls together."
    ),
    total: float = typer.Option(15.00, "--max-total-usd", min=0.10, help="Cap on the whole eval, probe included."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the matrix and the estimate; call no model."),
    execute: bool = typer.Option(False, "--execute", help="Run it and spend; refused for a plan that was never dry-run."),
    skip_probe: bool = typer.Option(False, "--skip-probe", help="Skip the sandboxed-Bash probe before the first cell."),
    baseline: str | None = typer.Option(
        None, "--baseline-model", help=f"Registry model for both baselines; default the {BASELINE_CLASS} class's."
    ),
    timeout: float = typer.Option(1800, "--timeout", min=1, help="Kill each agent call (or mini-swe-agent) after this many seconds."),
    tasks_dir: Path = typer.Option(Path("evals/tasks"), "--tasks-dir", help="Directory of task files."),
    canonical: Path = typer.Option(Path("canonical"), "--canonical", help="Directory holding the canonical files."),
    registry: Path | None = typer.Option(
        None, "--registry", help="Model registry; defaults to routing/registry.yaml next to --canonical."
    ),
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="Repository whose .agent-team/eval/ holds the results."),
) -> None:
    """Plan the tasks x arms x runs matrix and print its cost; with --execute (after a dry run), run it under the caps."""
    if dry_run and execute:
        typer.echo("error: give --dry-run or --execute, not both", err=True)
        raise typer.Exit(code=2)
    selected = _csv(arms) or list(ARMS)
    unknown = [a for a in selected if a not in ARMS]
    if unknown:
        typer.echo(f"error: unknown arm(s) {', '.join(unknown)}; known: {', '.join(ARMS)}", err=True)
        raise typer.Exit(code=2)
    if per_task > total:
        typer.echo("error: --max-budget-usd-per-task is above --max-total-usd; no cell could start", err=True)
        raise typer.Exit(code=2)
    registry_path = registry or default_registry_path(canonical)
    try:
        store = EvalStore.open(repo)
        loaded = load_tasks(tasks_dir, _csv(tasks) or None)
        model = baseline_model(registry_path, baseline)
        team = treatment_models(registry_path)
        probe_model = resolve_model(load_registry(registry_path), "cheap", runtime="claude").model
        tree = load_canonical(canonical)
        policy = tree.one("policy", "default").spec
        default_env = tree.one("environment", "default").spec
    except (TaskError, RegistryError, RunAgentError, RunStoreError, EvalStoreError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    claude, mini = _which("claude"), shutil.which("mini")
    ordered = [a for a in ARMS if a in selected]
    arm_plans = [
        ArmPlan(arm=a, description=ARM_DESCRIPTIONS[a], model=team if a == "treatment" else model,
                refusal=refusal(a, claude=claude, mini=mini, environ=dict(os.environ)))  # fmt: skip
        for a in ordered
    ]
    probe_on = not skip_probe and any(a != "baseline-a" for a in ordered)
    plan = build_plan(loaded, arm_plans, runs=runs, caps=Caps(per_task_usd=per_task, total_usd=total),
                      timeout_s=timeout, probe_on=probe_on)  # fmt: skip
    typer.echo(render_plan(plan, estimate(plan, store.cells()), probe_model=probe_model))
    seen = plan.plan_hash in store.dry_run_hashes()
    if not execute or not seen:
        store.append(DryRunRecord(at=datetime.now(UTC), plan=plan))
        if execute:
            typer.echo("this plan has not been dry-run before, so this was its dry run: no model was called. "
                       "Review it, then run the same command with --execute again.")  # fmt: skip
            raise typer.Exit(code=1)
        typer.echo(f"dry run: no model was called. Run the same command with --execute to spend up to ${total:.2f}.")
        return
    refused = [f"{a.arm}: {a.refusal}" for a in arm_plans if a.refusal]
    if refused:
        typer.echo("error: cannot run: " + "; ".join(refused), err=True)
        raise typer.Exit(code=2)
    eval_id = new_eval_id(datetime.now(UTC))
    try:
        canonical_copy = prepare_canonical(canonical, registry_path, store.work_dir / eval_id,
                                           hidden_paths(loaded, store))  # fmt: skip
        srt = srt_path()
        assert srt is not None  # refusal() checked it for every arm
        ctx = ArmContext(
            canonical_dir=canonical_copy,
            registry_path=canonical_copy.parent / REGISTRY_PATH,
            srt=srt,
            baseline_model=model,
            deny_read=hidden_paths(loaded, store),
            allowed_domains=list(default_env.network.allowed_domains),
            timeout_s=timeout,
            claude=claude,
            mini=mini,
            redaction=tuple(r.pattern for r in policy.redaction),
            echo=typer.echo,
        )
        run_eval(store, plan, loaded, ctx, eval_id=eval_id, echo=typer.echo)
    except EvalError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt as exc:
        typer.echo(f"interrupted: eval {eval_id} keeps the cells it recorded; the rest did not run", err=True)
        raise typer.Exit(code=130) from exc
    typer.echo("")
    _print_report(store, eval_id)


def _print_report(store: EvalStore, eval_id: str | None) -> None:
    evals = store.evals()
    if not evals:
        typer.echo("no eval recorded yet; run 'agent-team eval run'")
        return
    record = next((e for e in evals if e.eval_id == eval_id), None) if eval_id else evals[-1]
    if record is None:
        typer.echo(f"error: no eval {eval_id!r}; known: {', '.join(e.eval_id for e in evals)}", err=True)
        raise typer.Exit(code=1)
    typer.echo(render_report(record, store.cells(record.eval_id), store.probes(record.eval_id)))


@eval_app.command("report")
def eval_report(
    eval_id: str | None = typer.Option(None, "--eval", help="The eval to report; default the latest."),
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="Repository whose .agent-team/eval/ holds the results."),
) -> None:
    """Print the section 13 metrics per arm and the decision rule's verdict, from the recorded cells only."""
    try:
        store = EvalStore.open(repo)
        _print_report(store, eval_id)
    except (RunStoreError, EvalStoreError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


env_app = typer.Typer(help="Manage the per-run worktree environments under .agent-team/.", no_args_is_help=True)
app.add_typer(env_app, name="env")


@env_app.command("sweep")
def env_sweep(
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="Target repository; defaults to the current directory."),
) -> None:
    """Dispose worktrees whose run ended or has no run record; keep in-progress runs for resume."""
    try:
        results = sweep(repo)
    except (RunStoreError, AllocatorError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if not results:
        typer.echo("no worktrees")
        return
    for result in results:
        line = f"{result.action:<8} {result.run_id}  {result.reason}"
        if result.archive:
            line += f"; work saved to {result.archive}"
        typer.echo(line)


lessons_app = typer.Typer(
    help="Lessons reviewer findings taught an agent: propose from a run, approve, list, quarantine.", no_args_is_help=True
)
app.add_typer(lessons_app, name="lessons")

REPO_OPTION = typer.Option(Path("."), "--repo", "-C", help="Target repository; defaults to the current directory.")


def _lessons_root(repo: Path) -> Path:
    try:
        return repo_root(repo)
    except RunStoreError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@lessons_app.command("list")
def lessons_list(repo: Path = REPO_OPTION) -> None:
    """Every lesson, proposed and approved, with the run it came from."""
    try:
        typer.echo(lessons_mod.render_list(_lessons_root(repo)))
    except lessons_mod.LessonError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@lessons_app.command("propose")
def lessons_propose(run_id: str = typer.Argument(..., help="A workflow run whose reviewer findings to learn from."), repo: Path = REPO_OPTION) -> None:
    """Turn a run's blocker and major reviewer findings into proposed implementer lessons."""
    try:
        added = lessons_mod.propose_from_run(_lessons_root(repo), run_id)
    except (lessons_mod.LessonError, RunStoreError, UnknownRunError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not added:
        typer.echo(f"no new lessons: run {run_id} has no blocker or major findings that are not already proposed")
        return
    for lesson in added:
        typer.echo(f"proposed {lesson.id}  {lesson.agent}  {lesson.text}")
    typer.echo("approve with: agent-team lessons approve <id> [--text 'the general rule']")


@lessons_app.command("approve")
def lessons_approve(
    lesson_id: str = typer.Argument(..., help="The lesson id from 'lessons list'."),
    text: str | None = typer.Option(None, "--text", help="Reword the lesson as a general rule before approving."),
    repo: Path = REPO_OPTION,
) -> None:
    """Approve a lesson so the agent's future prompts carry it."""
    try:
        lesson = lessons_mod.approve(_lessons_root(repo), lesson_id, text)
    except lessons_mod.LessonError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"approved {lesson.id}  {lesson.agent}  {lesson.text}")


@lessons_app.command("quarantine")
def lessons_quarantine(
    text: str = typer.Argument(..., help="Text the bad lesson contains (case-insensitive, at least 4 characters)."),
    repo: Path = REPO_OPTION,
) -> None:
    """Remove every lesson containing TEXT from every agent and log where each came from."""
    try:
        removed = lessons_mod.quarantine(_lessons_root(repo), text)
    except lessons_mod.LessonError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not removed:
        typer.echo(f"no lesson contains {text!r}")
        return
    for lesson in removed:
        typer.echo(f"removed {lesson.id}  {lesson.agent}  from run {lesson.source_run}  {lesson.text}")
    typer.echo(f"logged in {lessons_mod.LESSONS_DIR / lessons_mod.QUARANTINE_LOG}")


schema_app = typer.Typer(help="Inspect and export the canonical JSON Schemas.", no_args_is_help=True)
app.add_typer(schema_app, name="schema")


@schema_app.command("list")
def schema_list() -> None:
    """List the canonical kinds that have a schema."""
    for kind in SCHEMAS:
        typer.echo(kind)


@schema_app.command("export")
def schema_export(
    kind: str | None = typer.Argument(None, help="One kind; omit for all."),
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Directory to write <kind>.schema.json files into; omit for stdout."
    ),
) -> None:
    """Export the JSON Schema (draft 2020-12) for one or every canonical kind."""
    if kind is not None and kind not in SCHEMAS:
        typer.echo(f"unknown kind {kind!r}; known: {', '.join(SCHEMAS)}", err=True)
        raise typer.Exit(code=2)
    kinds = [kind] if kind else list(SCHEMAS)
    schemas = {k: json_schema(k) for k in kinds}
    if out is None:
        payload = schemas[kinds[0]] if kind else schemas
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    out.mkdir(parents=True, exist_ok=True)
    for k, schema in schemas.items():
        target = out / f"{k}.schema.json"
        target.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        typer.echo(f"wrote {target}")


if __name__ == "__main__":
    app()
