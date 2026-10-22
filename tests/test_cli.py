import subprocess
import sys

from typer.testing import CliRunner

from agent_team.cli import app

runner = CliRunner()


def test_version_flag_prints_pinned_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == "agent-team 0.0.1"


def test_installed_console_script_runs() -> None:
    """The console script, not just the Python app object, must answer --version."""
    proc = subprocess.run(
        [sys.executable, "-m", "agent_team.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "agent-team 0.0.1"


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output
