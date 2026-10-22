"""Command-line entry point for agent-team."""

import json
from pathlib import Path

import typer

from agent_team import __version__
from agent_team.canonical import validate_tree
from agent_team.schemas import SCHEMAS, json_schema

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
        help="Directory holding agents/, skills/, workflows/, tools/ and policies/.",
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
