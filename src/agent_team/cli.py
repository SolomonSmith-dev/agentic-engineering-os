"""Command-line entry point for agent-team."""

import typer

from agent_team import __version__

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


if __name__ == "__main__":
    app()
