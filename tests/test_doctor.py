"""Snapshot tests for `agent-team doctor` with every host probe replaced.

No test here runs a real tool, reads a real file or opens a socket, so CI never
depends on what the runner has installed. Regenerate the fixtures after an
intended output change with `UPDATE_SNAPSHOTS=1 uv run pytest tests/test_doctor.py`.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_team import doctor
from agent_team.cli import app
from agent_team.schemas import DoctorReport

FIXTURES = Path(__file__).parent / "fixtures" / "doctor"
runner = CliRunner()

SECRET_API_KEY = "sk-ant-api03-DOCTOR-LEAK-CANARY-0000"
SECRET_OAUTH = "sk-ant-oat01-DOCTOR-LEAK-CANARY-1111"
SECRET_GH = "ghp_DOCTORLEAKCANARY2222"
SECRET_EMAIL = "canary@example.invalid"


@dataclass
class FakeHost:
    facts: dict[str, str]
    tools: dict[str, str] = field(default_factory=dict)
    commands: dict[tuple[str, ...], tuple[int, str]] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    files: set[str] = field(default_factory=set)
    reachable: bool = True
    run_calls: list[tuple[str, ...]] = field(default_factory=list)
    run_argv: list[list[str]] = field(default_factory=list)
    run_envs: list[dict[str, str] | None] = field(default_factory=list)
    exists_calls: list[str] = field(default_factory=list)
    head_calls: list[tuple[str, float]] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor, "_host_facts", lambda: dict(self.facts))
        monkeypatch.setattr(doctor, "_which", self.tools.get)
        monkeypatch.setattr(doctor, "_run", self._run)
        monkeypatch.setattr(doctor, "_head", self._head)
        monkeypatch.setattr(doctor, "_exists", self._exists)
        monkeypatch.setattr(doctor, "_env", lambda: dict(self.env))

    def _run(self, argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
        # Fixtures are keyed by tool name; doctor runs the resolved absolute path.
        key = (Path(argv[0]).name, *argv[1:])
        self.run_calls.append(key)
        self.run_argv.append(list(argv))
        self.run_envs.append(None if env is None else dict(env))
        return self.commands.get(key, (127, ""))

    def _head(self, url: str, timeout: float) -> bool:
        self.head_calls.append((url, timeout))
        return self.reachable

    def _exists(self, path: str) -> bool:
        self.exists_calls.append(path)
        return path in self.files


def full_mac() -> FakeHost:
    """Everything installed and logged in, managed settings deployed."""
    return FakeHost(
        facts={"os": "Darwin", "os_version": "15.4", "arch": "arm64", "python": "3.12.9", "platform": "darwin"},
        tools={name: f"/usr/local/bin/{name}" for name in ("uv", "claude", "srt", "gh", "git")},
        commands={
            ("uv", "--version"): (0, "uv 0.11.2 (02036a8ba 2026-03-26 aarch64-apple-darwin)\n"),
            ("claude", "--version"): (0, "2.1.305 (Claude Code)\n"),
            ("srt", "--version"): (0, "0.0.42\n"),
            ("gh", "--version"): (0, "gh version 2.102.0 (2026-09-30)\nhttps://github.com/cli/cli/releases/tag/v2.102.0\n"),
            ("git", "--version"): (0, "git version 2.56.0\n"),
            ("claude", "auth", "status", "--json"): (0, json.dumps({"loggedIn": True, "email": SECRET_EMAIL})),
            ("gh", "auth", "status", "--hostname", "github.com"): (0, ""),
        },
        files={"/Library/Application Support/ClaudeCode/managed-settings.json"},
        reachable=True,
    )


def bare_linux() -> FakeHost:
    """Fresh Linux box: only git and uv, no auth, no egress, no managed settings."""
    return FakeHost(
        facts={"os": "Linux", "os_version": "6.8.0-1021-azure", "arch": "x86_64", "python": "3.12.3", "platform": "linux"},
        tools={"uv": "/usr/bin/uv", "git": "/usr/bin/git"},
        commands={
            ("uv", "--version"): (0, "uv 0.11.2\n"),
            ("git", "--version"): (0, "git version 2.43.0\n"),
        },
        reachable=False,
    )


def leaky_host() -> FakeHost:
    """Every channel a secret could ride in on carries one."""
    host = full_mac()
    host.env = {
        "ANTHROPIC_API_KEY": SECRET_API_KEY,
        "CLAUDE_CODE_OAUTH_TOKEN": SECRET_OAUTH,
        "GH_TOKEN": SECRET_GH,
        "GITHUB_TOKEN": SECRET_GH,
    }
    host.commands[("claude", "auth", "status", "--json")] = (
        0,
        json.dumps({"loggedIn": True, "email": SECRET_EMAIL, "token": SECRET_OAUTH}),
    )
    host.commands[("gh", "auth", "status", "--hostname", "github.com")] = (0, f"Token: {SECRET_GH}\n")
    host.commands[("git", "--version")] = (0, f"git version 2.56.0 token={SECRET_API_KEY}\n")
    return host


def check_snapshot(name: str, actual: str) -> None:
    path = FIXTURES / name
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        path.write_text(actual, encoding="utf-8")
    assert actual == path.read_text(encoding="utf-8")


SCENARIOS = {"full_mac": full_mac, "bare_linux": bare_linux}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_table_matches_snapshot(scenario: str, monkeypatch: pytest.MonkeyPatch) -> None:
    SCENARIOS[scenario]().install(monkeypatch)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    check_snapshot(f"{scenario}.txt", result.output)


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_json_matches_snapshot_and_schema(scenario: str, monkeypatch: pytest.MonkeyPatch) -> None:
    SCENARIOS[scenario]().install(monkeypatch)
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    check_snapshot(f"{scenario}.json", result.output)
    DoctorReport.model_validate_json(result.output)


def test_no_secret_reaches_any_output(monkeypatch: pytest.MonkeyPatch) -> None:
    leaky_host().install(monkeypatch)
    table = runner.invoke(app, ["doctor"])
    as_json = runner.invoke(app, ["doctor", "--json"])
    assert table.exit_code == 0 and as_json.exit_code == 0
    for output in (table.output, as_json.output):
        for secret in (SECRET_API_KEY, SECRET_OAUTH, SECRET_GH, SECRET_EMAIL, "DOCTOR-LEAK-CANARY", "DOCTORLEAKCANARY"):
            assert secret not in output
    report = DoctorReport.model_validate_json(as_json.output)
    assert {a.name: a.state for a in report.auth} == {"claude": "present", "gh": "present"}


def test_env_credentials_short_circuit_cli_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    host = leaky_host()
    host.install(monkeypatch)
    doctor.probe_auth()
    assert ("claude", "auth", "status", "--json") not in host.run_calls
    assert ("gh", "auth", "status", "--hostname", "github.com") not in host.run_calls


def test_logged_out_claude_reports_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    host = full_mac()
    host.commands[("claude", "auth", "status", "--json")] = (1, json.dumps({"loggedIn": False}))
    host.commands[("gh", "auth", "status", "--hostname", "github.com")] = (1, "")
    host.install(monkeypatch)
    assert {a.name: a.state for a in doctor.probe_auth()} == {"claude": "absent", "gh": "absent"}


def test_probes_stay_off_user_claude_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    host = full_mac()
    host.install(monkeypatch)
    doctor.build_report()
    assert host.exists_calls == ["/Library/Application Support/ClaudeCode/managed-settings.json"]
    assert all(".claude" not in part for call in host.run_calls for part in call)
    assert host.head_calls == [(doctor.EGRESS_URL, doctor.EGRESS_TIMEOUT_S)]


def test_probes_run_resolved_absolute_paths_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    host = leaky_host()
    host.env["PATH"] = "/usr/bin"
    host.install(monkeypatch)
    doctor.probe_host()
    doctor.probe_runtimes()
    assert host.run_argv, "no probe ran"
    for argv in host.run_argv:
        assert argv[0] == host.tools[Path(argv[0]).name]
    version_envs = [env for argv, env in zip(host.run_argv, host.run_envs) if argv[1:] == ["--version"]]
    assert version_envs and all(env == {"PATH": "/usr/bin"} for env in version_envs)


def test_relative_path_hit_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"./{name}")
    assert doctor._which("claude") is None
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/opt/bin/{name}")
    assert doctor._which("claude") == "/opt/bin/claude"


@pytest.mark.parametrize(
    ("platform_name", "expected"),
    [
        ("darwin", "/Library/Application Support/ClaudeCode/managed-settings.json"),
        ("linux", "/etc/claude-code/managed-settings.json"),
        ("win32", r"C:\Program Files\ClaudeCode\managed-settings.json"),
        ("freebsd14", None),
    ],
)
def test_managed_settings_path_by_platform(platform_name: str, expected: str | None) -> None:
    assert doctor.managed_settings_path(platform_name) == expected


def test_doctor_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    full_mac().install(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["doctor"]).exit_code == 0
    assert runner.invoke(app, ["doctor", "--json"]).exit_code == 0
    assert list(tmp_path.iterdir()) == []
