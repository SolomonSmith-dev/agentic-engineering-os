"""Command-line entry point for agent-team."""

import json
import os
from pathlib import Path
from typing import Literal

import typer

from agent_team import __version__
from agent_team.adapters.claude.invoke import ClaudeVersionError
from agent_team.adapters.codex.invoke import API_KEY_ENV, CodexUnavailableError, has_auth
from agent_team.canonical import validate_tree
from agent_team.doctor import _which, build_report, render_table
from agent_team.environments import AllocatorError, LockHeldError, sweep
from agent_team.run_agent import RunAgentError, run_agent
from agent_team.runs import RunStore, RunStoreError, UnknownRunError, render_run_list, render_trace
from agent_team.schemas import SCHEMAS, json_schema

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


@app.command()
def trace(
    run_id: str | None = typer.Argument(None, help="Run to show; omit to list recent runs."),
    repo: Path = typer.Option(Path("."), "--repo", "-C", help="Target repository; defaults to the current directory."),
    limit: int = typer.Option(20, "--limit", "-n", min=1, help="How many recent runs to list."),
    calls: bool = typer.Option(False, "--calls", help="Also list every tool call (arguments are stored redacted)."),
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
        typer.echo(render_trace(run, store.transitions(run_id), store.tool_calls(run_id), show_calls=calls))


@app.command()
def run(
    prompt: str | None = typer.Argument(None, help="The task for the agent; omit to read it from --prompt-file."),
    agent: str = typer.Option(..., "--agent", "-a", help="Canonical agent to run, e.g. independent-reviewer."),
    model: str | None = typer.Option(
        None, "--model", "-m", help="Override the agent's model class with this registry model id."
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
    budget: float = typer.Option(1.00, "--max-budget-usd", min=0.01, help="Stop the worker at this estimated spend."),
    timeout: float = typer.Option(1800, "--timeout", min=1, help="Kill the worker after this many seconds."),
    test_command: list[str] = typer.Option([], "--test-command", help="Test command the agent may run (repeatable)."),
) -> None:
    """Run one canonical agent in its own worktree and sandbox, and record everything it does."""
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
    binary = _which(runtime)
    if binary is None:
        typer.echo(f"error: {runtime} is not on PATH; run 'agent-team doctor'", err=True)
        raise typer.Exit(code=2)
    if runtime == "codex" and not has_auth(binary, dict(os.environ)):
        typer.echo(f"error: codex has no auth: run 'codex login', or set {API_KEY_ENV} for this shell", err=True)
        raise typer.Exit(code=2)
    try:
        record, result = run_agent(
            repo,
            canonical,
            agent,
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
    except (
        RunAgentError, ClaudeVersionError, CodexUnavailableError, RunStoreError, AllocatorError, LockHeldError
    ) as exc:
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
