"""Edges of `inspect` (build-order step 9): refusals, unreadable state, and `run_command` details.

`run_command` tests use a pass-through fake `srt` that drops its own flags and
runs the command, so the scrubbed environment, the output tail and a failed
start are checked without the real sandbox. Nothing here calls a model.
"""

import shutil
from pathlib import Path

import pytest
import yaml
from test_inspect import (  # noqa: F401  (fixtures are used by name)
    MEMORY,
    MEMORY_OUTPUT,
    fake_claude,
    inspect,
    no_srt,
    only_run,
    repo,
)

from agent_team.environments.sandbox import Sandbox
from agent_team.memory import inspect as inspect_mod
from agent_team.memory import verify as verify_module
from agent_team.memory.inspect import (
    InspectError,
    build_prompt,
    load_candidates,
    render_memory_summary,
    write_memory,
)
from agent_team.memory.verify import (
    Candidate,
    run_command,
    sandbox_refusal,
    verify_commands,
)
from agent_team.schemas.adapter import RepositoryMemory

TOKEN = "ghp_MEMORYEDGECANARY0000000000"

FAKE_SRT = """#!/bin/sh
# Pass-through srt: skip everything up to "--", then run the rest.
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
shift
exec "$@"
"""


@pytest.fixture
def passthrough(tmp_path: Path) -> Sandbox:
    fake = tmp_path / "fake-bin" / "srt"
    fake.parent.mkdir()
    fake.write_text(FAKE_SRT, encoding="utf-8")
    fake.chmod(0o755)
    tmp = tmp_path / "run-tmp"
    tmp.mkdir()
    return Sandbox(srt=str(fake), settings_path=tmp_path / "srt.json", tmp_dir=tmp, port_offset=7)


def candidate(command: str) -> Candidate:
    return Candidate("test", command, "Makefile target test", True)


# --- run_command ------------------------------------------------------------------


def test_run_command_passes_only_allowlisted_env_and_sets_ci(passthrough: Sandbox, tmp_path: Path) -> None:
    base = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GH_TOKEN": TOKEN, "ANTHROPIC_API_KEY": "sk-ant-x",
            "AWS_SECRET_ACCESS_KEY": "aws-x"}  # fmt: skip
    result = run_command(candidate("env"), sandbox=passthrough, cwd=tmp_path, timeout_s=30, base_env=base, redaction=())
    assert result.status == "passed" and result.exit_code == 0
    env = dict(line.split("=", 1) for line in (result.output_tail or "").splitlines() if "=" in line)
    assert env["CI"] == "1" and env["XDG_CACHE_HOME"] == str(passthrough.tmp_dir / "cache")
    assert env["TMPDIR"] == str(passthrough.tmp_dir) and env["AGENT_TEAM_PORT_OFFSET"] == "7"
    assert not {"GH_TOKEN", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY"} & set(env)
    assert TOKEN not in (result.output_tail or "")


def test_run_command_with_no_output_keeps_no_tail(passthrough: Sandbox, tmp_path: Path) -> None:
    result = run_command(candidate("exit 4"), sandbox=passthrough, cwd=tmp_path, timeout_s=30,
                         base_env={"PATH": "/usr/bin:/bin"}, redaction=())  # fmt: skip
    assert (result.status, result.exit_code, result.reason, result.output_tail) == ("failed", 4, "exit 4", None)


def test_run_command_redacts_the_tail_with_policy_patterns(passthrough: Sandbox, tmp_path: Path) -> None:
    result = run_command(candidate(f"echo {TOKEN}; echo internal-id-1234"), sandbox=passthrough, cwd=tmp_path,
                         timeout_s=30, base_env={"PATH": "/usr/bin:/bin"}, redaction=(r"internal-id-\d+",))  # fmt: skip
    assert TOKEN not in (result.output_tail or "") and "internal-id-1234" not in (result.output_tail or "")
    assert (result.output_tail or "").count("[REDACTED]") == 2


def test_run_command_that_cannot_start_fails_without_raising(tmp_path: Path) -> None:
    sandbox = Sandbox(srt=str(tmp_path / "missing-srt"), settings_path=tmp_path / "s.json", tmp_dir=tmp_path, port_offset=0)
    result = run_command(candidate("true"), sandbox=sandbox, cwd=tmp_path, timeout_s=5, base_env={}, redaction=())
    assert result.status == "failed" and (result.reason or "").startswith("could not start:")
    assert result.exit_code is None


def test_an_unwrapped_environment_is_refused_even_with_srt_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_module, "srt_path", lambda: "/usr/local/bin/srt")
    bare = Sandbox(srt=None, settings_path=None, tmp_dir=tmp_path, port_offset=0)
    assert sandbox_refusal(bare) == "the run's environment is not wrapped in srt; agent-team runs repository commands only inside srt"
    lines: list[str] = []
    results = verify_commands([candidate(f"touch {tmp_path / 'ran'}")], sandbox=bare, cwd=tmp_path, timeout_s=5,
                              redaction=(), base_env={}, echo=lines.append)  # fmt: skip
    assert [r.status for r in results] == ["unverified"] and lines == []
    assert not (tmp_path / "ran").exists()


# --- inspect refusals and the state it reads --------------------------------------------


def test_a_symlinked_memory_dir_is_refused_before_any_run(repo: Path, fake_claude, tmp_path: Path) -> None:
    outside = tmp_path / "outside-memory"
    outside.mkdir()
    (repo / ".agent-team" / "memory").symlink_to(outside, target_is_directory=True)
    fake_claude()
    result = inspect(repo)
    assert result.exit_code == 2 and "is a symlink or sits under one" in result.output
    assert list(outside.iterdir()) == []
    assert not (repo / ".agent-team" / "runs.db").exists(), "refused before recording a run"
    summary = inspect(repo, "--summary")
    assert summary.exit_code == 1 and "is a symlink or sits under one" in summary.output


def test_an_invalid_config_yaml_is_refused(repo: Path, fake_claude) -> None:
    (repo / ".agent-team" / "config.yaml").write_text("version: 1\ndefault_branch: [\n", encoding="utf-8")
    fake_claude()
    result = inspect(repo)
    assert result.exit_code == 2 and "config.yaml is unreadable or invalid; re-run 'agent-team configure'" in result.output
    (repo / ".agent-team" / "config.yaml").write_text("version: 1\nsurprise: true\n", encoding="utf-8")
    with pytest.raises(InspectError, match="unreadable or invalid"):
        load_candidates(repo)


def test_config_yaml_alone_is_enough_and_lists_unverified_commands(repo: Path) -> None:
    (repo / ".agent-team" / "proposal.yaml").unlink()
    config = {"version": 1, "default_branch": "main", "protected_branches": "unknown", "runtimes": ["claude"],
              "privacy_classes": ["vendor-cloud"], "autonomy_ceiling": "A2", "draft_prs": True,
              "commands": {"test": ["make test"]}, "unverified_commands": {"test": ["make check"], "lint": ["make lint"]}}  # fmt: skip
    (repo / ".agent-team" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    candidates, proposal = load_candidates(repo)
    assert proposal is None
    assert [(c.kind, c.command, c.dry_run) for c in candidates] == [
        ("test", "make test", True), ("test", "make check", False), ("lint", "make lint", False),
    ]  # fmt: skip
    assert candidates[1].source == "config.yaml (unverified by init)"


def test_prompt_without_detected_commands_or_proposal() -> None:
    prompt = build_prompt([], None)
    assert "init detected none" in prompt and "## Found by agent-team init" not in prompt


# --- writing and reading memory -------------------------------------------------------------


def memory() -> RepositoryMemory:
    return RepositoryMemory.model_validate(MEMORY_OUTPUT)


def test_a_failed_memory_write_leaves_no_temp_file(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(src: str, dst: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(inspect_mod.os, "replace", fail)
    with pytest.raises(OSError, match="No space left"):
        write_memory(repo, run_id="r1", base_commit="a" * 40, memory=memory(), commands=[], sandboxed=False, redaction=())
    assert list((repo / MEMORY).iterdir()) == []


def test_summary_with_no_commands(repo: Path) -> None:
    write_memory(repo, run_id="r1", base_commit="b" * 40, memory=memory(), commands=[], sandboxed=False, redaction=())
    summary = render_memory_summary(repo)
    assert "(run r1," in summary and "base bbbbbbbbbbbb)" in summary
    assert "(not run: no srt)\n  none detected" in summary


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda d: (d / "commands.yaml").unlink(), "commands.yaml is missing"),
        (lambda d: (d / "architecture.yaml").write_text("summary: [\n", encoding="utf-8"), "cannot read"),
        (lambda d: (d / "index.yaml").write_text("run_id: r1\n", encoding="utf-8"), "is invalid; re-run 'agent-team inspect'"),
        (lambda d: (d / "commands.yaml").write_text("commands: 3\n", encoding="utf-8"), "is invalid"),
    ],
)
def test_damaged_memory_is_named_not_crashed_on(repo: Path, damage, message: str) -> None:
    write_memory(repo, run_id="r1", base_commit="c" * 40, memory=memory(), commands=[], sandboxed=False, redaction=())
    damage(repo / MEMORY)
    with pytest.raises(InspectError, match=message):
        render_memory_summary(repo)


def test_memory_files_are_redacted_with_the_policy_patterns(repo: Path) -> None:
    data = {**MEMORY_OUTPUT, "conventions": [{"topic": "ids", "rule": "use internal-id-9876", "evidence": "x"}]}
    written = write_memory(repo, run_id="r1", base_commit="d" * 40, memory=RepositoryMemory.model_validate(data),
                           commands=[], sandboxed=True, redaction=(r"internal-id-\d+",))  # fmt: skip
    assert {p.name for p in written} == {"index.yaml", "architecture.yaml", "ownership.yaml", "commands.yaml",
                                         "conventions.yaml", "pitfalls.yaml"}  # fmt: skip
    assert "internal-id-9876" not in (repo / MEMORY / "conventions.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load((repo / MEMORY / "index.yaml").read_text(encoding="utf-8"))["sandbox"] == "srt"


def test_a_symlinked_state_dir_is_refused(tmp_path: Path, repo: Path, fake_claude) -> None:
    elsewhere = tmp_path / "elsewhere-state"
    shutil.move(repo / ".agent-team", elsewhere)
    (repo / ".agent-team").symlink_to(elsewhere, target_is_directory=True)
    fake_claude()
    result = inspect(repo)
    assert result.exit_code == 2 and ("symlink" in result.output or "symbolic link" in result.output), result.output
    assert not (elsewhere / "runs.db").exists() and not (elsewhere / "memory").exists()


def test_an_after_step_that_raises_fails_the_run(repo: Path, fake_claude, no_srt, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("disk went away")

    monkeypatch.setattr(inspect_mod, "write_memory", broken)
    fake_claude()
    result = inspect(repo)
    assert result.exit_code != 0
    run, transitions, _ = only_run(repo)
    assert run.outcome == "failed" and transitions[-1].to_state == "BLOCKED"
    assert "RuntimeError: disk went away" in transitions[-1].note
    assert not (repo / MEMORY).exists()

