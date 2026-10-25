"""`agent-team inspect` (build-order step 9): archaeologist at A0, repository memory, command verification.

No test calls a model. A fake `claude` on PATH records its inputs and replays
a stream-json transcript whose structured output is an archaeologist's
repository memory. Tests that run repository commands need the real srt: they
skip when srt is absent, unless AGENT_TEAM_REQUIRE_SRT=1 (set in CI), which
turns a missing srt into a failure. The no-srt path is tested by hiding srt.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agent_team.adapters.claude import resolve
from agent_team.bootstrap import write_proposal
from agent_team.canonical import parse_file
from agent_team.cli import app
from agent_team.environments import sandbox as sandbox_module
from agent_team.environments import srt_path
from agent_team.environments.sandbox import Sandbox
from agent_team.memory import verify as verify_module
from agent_team.memory.verify import Candidate, run_command
from agent_team.runs import RunStore
from agent_team.schemas import AgentSpec
from agent_team.schemas.adapter import OUTPUT_SCHEMAS, RepositoryMemory
from agent_team.schemas.memory import CommandVerification, MemoryIndex

REPO = Path(__file__).resolve().parents[1]
CANONICAL = REPO / "canonical"
MEMORY = Path(".agent-team") / "memory" / "repo"
MEMORY_FILES = {"index.yaml", "architecture.yaml", "ownership.yaml", "commands.yaml", "conventions.yaml", "pitfalls.yaml"}
ENV_CANARY = "INSPECT-ENV-CANARY"
OUTPUT_TOKEN = "ghp_INSPECTOUTPUTCANARY000000"
MEMORY_TOKEN = "ghp_INSPECTMEMORYCANARY000000"
runner = CliRunner()

MAKEFILE = f"""\
test:
\t@echo tests ran with {OUTPUT_TOKEN}
lint:
\t@echo lint failed; exit 3
typecheck:
\t@cat ../../../.env; cat ../../runs.db > /dev/null && echo runs-db-readable; touch ran-marker; exit 1
"""

MEMORY_OUTPUT = {
    "architecture": {
        "summary": f"A tiny app; the README pastes the deploy key {MEMORY_TOKEN}.",
        "modules": [{"path": "app.py", "purpose": "prints a greeting", "depends_on": []}],
    },
    "ownership": [{"path": "app.py", "owners": ["test"], "source": "git-history"}],
    "commands": [
        {"kind": "test", "command": "make test", "source": "Makefile"},
        {"kind": "run", "command": "python app.py", "source": "README.md"},
    ],
    "conventions": [{"topic": "layout", "rule": "one module at the root", "evidence": "app.py"}],
    "pitfalls": [{"title": "no tests", "detail": "the test target only echoes", "evidence": "Makefile"}],
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: Path, makefile: str = MAKEFILE, gitignore: str = ".agent-team/\n.env\n") -> Path:
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text(gitignore, encoding="utf-8")
    (root / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (root / "Makefile").write_text(makefile, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    (root / ".env").write_text(f"GREETING={ENV_CANARY}\n", encoding="utf-8")  # not credential-shaped: only srt hides it
    return root.resolve()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = make_repo(tmp_path / "target")
    write_proposal(root, which=lambda tool: f"/usr/bin/{tool}")
    return root


@pytest.fixture
def srt() -> str:
    path = srt_path()
    if path is None:
        if os.environ.get("AGENT_TEAM_REQUIRE_SRT") == "1":
            pytest.fail("AGENT_TEAM_REQUIRE_SRT=1 but srt is not on PATH")
        pytest.skip("srt not installed; inspect runs repository commands only inside srt")
    return path


@pytest.fixture
def no_srt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_module, "srt_path", lambda: None)
    monkeypatch.setattr(verify_module, "srt_path", lambda: None)


FAKE_CLAUDE = r'''#!{python}
"""Fake claude: records its inputs and replays an archaeologist transcript."""
import json, os, sys

if sys.argv[1:] == ["--version"]:
    print("2.1.310 (Claude Code)")
    sys.exit(0)
argv = sys.argv[1:]
seen = {{"argv": argv, "env": dict(os.environ), "stdin": sys.stdin.read(), "cwd": os.getcwd()}}
with open(os.path.join(os.environ["TMPDIR"], "fake-claude.json"), "w") as out:
    json.dump(seen, out)
events = [
    {{"type": "system", "subtype": "init"}},
    {{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "t1", "name": "Bash", "input": {{"command": "git log --format=%an -20"}}}},
                                                  {{"type": "tool_use", "id": "t2", "name": "Read", "input": {{"file_path": "Makefile"}}}}]}}}},
    {{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "t1", "is_error": False, "content": "test"}},
                                             {{"type": "tool_result", "tool_use_id": "t2", "is_error": False, "content": "test:"}}]}}}},
    {{"type": "result", "subtype": "success", "is_error": False, "result": "mapped", "total_cost_usd": 0.12,
      "num_turns": 2, "session_id": "fake", "structured_output": {output}}},
]
for event in events:
    print(json.dumps(event), flush=True)
'''


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def _install(output: dict | None = None) -> Path:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        fake = bin_dir / "claude"
        payload = MEMORY_OUTPUT if output is None else output
        fake.write_text(FAKE_CLAUDE.format(python=sys.executable, output=repr(payload)), encoding="utf-8")
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("GH_TOKEN", "ghp_E2ECANARY000000000000")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        return fake

    return _install


def inspect(repo: Path, *extra: str):
    return runner.invoke(app, ["inspect", str(repo), "--canonical", str(CANONICAL), *extra])


def only_run(repo: Path):
    store = RunStore.open(repo, create=False)
    assert store is not None
    with store:
        [run] = store.list_runs()
        return run, store.transitions(run.run_id), store.tool_calls(run.run_id)


def commands_file(repo: Path) -> list[CommandVerification]:
    data = yaml.safe_load((repo / MEMORY / "commands.yaml").read_text(encoding="utf-8"))
    return [CommandVerification.model_validate(c) for c in data["commands"]]


def untracked(repo: Path) -> list[str]:
    return git(repo, "status", "--porcelain", "--untracked-files=all", "--ignored=no").splitlines()


# --- the agent ------------------------------------------------------------------


def archaeologist() -> AgentSpec:
    data, _ = parse_file(CANONICAL / "agents" / "repository-archaeologist.md", "agent")
    return AgentSpec.model_validate(data)


def test_archaeologist_is_read_only_at_a0() -> None:
    agent = archaeologist()
    assert agent.max_autonomy == "A0" and agent.model_class == "strong-coding"
    assert OUTPUT_SCHEMAS[agent.outputs[0]] is RepositoryMemory
    perms = resolve(agent.capabilities.allow, agent.capabilities.deny, "A0")
    assert perms.granted == ["fs.read", "git.read", "shell.read"] and perms.dropped == []
    assert perms.tools == ["Bash", "Glob", "Grep", "Read"]
    assert {"Edit", "Write", "NotebookEdit"} <= set(perms.deny)
    assert all(r.startswith(("Bash(git ", "Bash(ls", "Bash(pwd", "Bash(wc", "Bash(head", "Bash(tail", "Read", "Glob", "Grep"))
               for r in perms.allow)  # fmt: skip


def test_run_command_refuses_outside_srt(tmp_path: Path) -> None:
    sandbox = Sandbox(srt=None, settings_path=None, tmp_dir=tmp_path, port_offset=0)
    with pytest.raises(ValueError, match="outside srt"):
        run_command(Candidate("test", "touch ran", "Makefile", True), sandbox=sandbox, cwd=tmp_path, timeout_s=5,
                    base_env={}, redaction=())  # fmt: skip
    assert not (tmp_path / "ran").exists()


# --- end to end -------------------------------------------------------------------


def test_inspect_writes_memory_and_verifies_commands_in_srt(repo: Path, fake_claude, srt: str) -> None:
    fake_claude()
    result = inspect(repo)
    assert result.exit_code == 0, result.output
    assert "succeeded: succeeded in 2 turn(s), 2 tool call(s), $0.1200" in result.output

    # Memory: exactly the six files, and nothing outside .agent-team/.
    assert {p.name for p in (repo / MEMORY).iterdir()} == MEMORY_FILES
    assert untracked(repo) == [], "nothing is written outside the gitignored .agent-team/"
    assert not (repo / ".claude").exists() and not (repo / "ran-marker").exists()
    for name in MEMORY_FILES:
        text = (repo / MEMORY / name).read_text(encoding="utf-8")
        assert text.startswith("# agent-team repository memory")
        for canary in (OUTPUT_TOKEN, MEMORY_TOKEN, ENV_CANARY, "ghp_E2ECANARY"):
            assert canary not in text, f"{canary} leaked into {name}"
    architecture = yaml.safe_load((repo / MEMORY / "architecture.yaml").read_text(encoding="utf-8"))
    assert "[REDACTED]" in architecture["summary"], "the archaeologist's output is redacted too"
    assert architecture["modules"] == [{"path": "app.py", "purpose": "prints a greeting", "depends_on": []}]
    assert yaml.safe_load((repo / MEMORY / "pitfalls.yaml").read_text())["pitfalls"][0]["evidence"] == "Makefile"

    run, transitions, calls = only_run(repo)
    index = MemoryIndex.model_validate(yaml.safe_load((repo / MEMORY / "index.yaml").read_text(encoding="utf-8")))
    assert index.run_id == run.run_id and index.sandbox == "srt"
    assert index.base_commit == git(repo, "rev-parse", "HEAD")
    assert index.commands == {"passed": 1, "failed": 2, "unverified": 1}

    # Commands: init's three ran inside srt; the archaeologist's extra one never ran.
    by_command = {c.command: c for c in commands_file(repo)}
    assert list(by_command) == ["make test", "make lint", "make typecheck", "python app.py"]
    test, lint, typecheck, extra = by_command.values()
    assert (test.status, test.exit_code, test.detected_by) == ("passed", 0, "init")
    # macOS's /usr/bin/make is an xcrun shim that may first complain it cannot write its cache outside srt.
    assert (test.output_tail or "").endswith("tests ran with [REDACTED]"), "command output is redacted"
    assert OUTPUT_TOKEN not in (test.output_tail or "")
    assert (lint.status, lint.exit_code, lint.reason) == ("failed", 2, "exit 2"), "make's own exit code"
    assert "lint failed" in (lint.output_tail or "")
    assert typecheck.status == "failed" and typecheck.exit_code == 2
    assert ENV_CANARY not in (typecheck.output_tail or ""), "srt denies the target repo's .env"
    assert "runs-db-readable" not in (typecheck.output_tail or ""), "and the run record"
    assert (extra.status, extra.detected_by, extra.exit_code) == ("unverified", "archaeologist", None)
    assert all(c.duration_ms is not None for c in (test, lint, typecheck))

    # Run record: the archaeologist's run, its tool calls, then one inspect.verify per command.
    assert run.outcome == "succeeded" and run.workflow == "agent:repository-archaeologist"
    assert run.model_class == "strong-coding"
    assert [t.to_state for t in transitions] == ["INTAKE", "DISCOVERY"]
    assert "autonomy ceiling A0" in transitions[1].note and "srt sandbox" in transitions[1].note
    assert "model claude-sonnet-5-5 (class strong-coding" in transitions[1].note
    verify_calls = [c for c in calls if c.tool_name == "inspect.verify"]
    assert [c.tool_name for c in calls[:2]] == ["Bash", "Read"]
    assert [(json.loads(c.args_summary)["command"], json.loads(c.args_summary)["exit_code"], c.result_status)
            for c in verify_calls] == [("make test", 0, "ok"), ("make lint", 2, "error"),
                                       ("make typecheck", 2, "error"), ("python app.py", None, "denied")]  # fmt: skip
    assert all(c.state == "DISCOVERY" for c in verify_calls)

    # The worker: read-only agent, prompt on stdin naming the commands agent-team runs.
    seen = json.loads((repo / ".agent-team" / "tmp" / run.run_id / "fake-claude.json").read_text())
    argv = seen["argv"]
    assert argv[argv.index("--agent") + 1] == "repository-archaeologist"
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == RepositoryMemory.model_json_schema()
    assert "- test: `make test` (Makefile target test)" in seen["stdin"]
    assert "GH_TOKEN" not in seen["env"]
    tools = json.loads((repo / ".agent-team" / "compiled" / run.run_id / "agents.json").read_text())
    assert "Edit" not in tools["repository-archaeologist"]["tools"]

    # The summary, printed after the run and on demand.
    for out in (result.output, inspect(repo, "--summary").output):
        assert "app.py  prints a greeting" in out
        assert "✓ passed     test      make test  (exit 0," in out
        assert "✗ failed     lint      make lint  (exit 2," in out
        assert "- unverified run       python app.py  (proposed by the archaeologist" in out
        assert "(run inside srt)" in out


def test_inspect_prefers_the_approved_config(repo: Path, fake_claude, srt: str) -> None:
    config = {"version": 1, "default_branch": "main", "protected_branches": "unknown", "runtimes": ["claude"],
              "privacy_classes": ["vendor-cloud"], "autonomy_ceiling": "A3", "draft_prs": True,
              "commands": {"test": ["make test"]}, "unverified_commands": {}}  # fmt: skip
    (repo / ".agent-team" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    fake_claude()
    result = inspect(repo)
    assert result.exit_code == 0, result.output
    commands = commands_file(repo)
    assert [(c.command, c.status) for c in commands] == [("make test", "passed"), ("python app.py", "unverified")]
    assert commands[0].source == "config.yaml (verified by init)"


def test_command_timeout_kills_the_command(tmp_path: Path, fake_claude, srt: str) -> None:
    root = make_repo(tmp_path / "slow", makefile="build:\n\tsleep 60\n")
    write_proposal(root, which=lambda tool: f"/usr/bin/{tool}")
    fake_claude()
    start = time.monotonic()
    result = inspect(root, "--command-timeout", "1")
    assert result.exit_code == 0, result.output
    assert time.monotonic() - start < 30
    [build, *_] = commands_file(root)
    assert (build.command, build.status, build.exit_code, build.reason) == ("make build", "timeout", None, "killed after 1s")
    _, _, calls = only_run(root)
    assert [c.result_status for c in calls if c.tool_name == "inspect.verify"][0] == "timeout"


def test_without_srt_commands_are_never_run(repo: Path, fake_claude, no_srt) -> None:
    fake_claude()
    result = inspect(repo)
    assert result.exit_code == 0, result.output
    assert "commands unverified: srt is not on PATH" in result.output
    assert not list(repo.rglob("ran-marker")), "no repository command ran"
    commands = commands_file(repo)
    assert [c.status for c in commands] == ["unverified"] * 4
    assert all(c.exit_code is None and c.duration_ms is None for c in commands)
    assert commands[0].reason == "srt is not on PATH; agent-team runs repository commands only inside srt"
    index = yaml.safe_load((repo / MEMORY / "index.yaml").read_text(encoding="utf-8"))
    assert index["sandbox"] == "none" and index["commands"] == {"unverified": 4}

    run, transitions, calls = only_run(repo)
    assert run.outcome == "succeeded" and "worktree only" in transitions[1].note
    verify_calls = [c for c in calls if c.tool_name == "inspect.verify"]
    assert [c.result_status for c in verify_calls] == ["denied"] * 4
    assert "srt is not on PATH" in verify_calls[0].args_summary

    summary = inspect(repo, "--summary")
    assert summary.exit_code == 0
    assert "(not run: no srt)" in summary.output
    assert "- unverified test      make test  (srt is not on PATH" in summary.output


def test_inspect_refuses_unless_agent_team_is_gitignored(tmp_path: Path, fake_claude) -> None:
    root = make_repo(tmp_path / "open", gitignore=".env\n")
    fake_claude()
    result = inspect(root)
    assert result.exit_code == 2
    assert ".agent-team/ is not gitignored" in result.output
    assert not (root / ".agent-team").exists(), "nothing is created before the refusal"


def test_inspect_needs_init_first(tmp_path: Path, fake_claude) -> None:
    root = make_repo(tmp_path / "fresh")
    fake_claude()
    result = inspect(root)
    assert result.exit_code == 2 and "run 'agent-team init' first" in result.output
    assert RunStore.open(root, create=False) is None, "refused before recording a run"


def test_invalid_archaeologist_output_writes_no_memory(repo: Path, fake_claude, no_srt) -> None:
    fake_claude({"architecture": {"summary": "x", "modules": []}})
    result = inspect(repo)
    assert result.exit_code == 1
    assert "structured_output failed validation" in result.output
    assert not (repo / MEMORY).exists()
    run, transitions, calls = only_run(repo)
    assert run.outcome == "failed" and transitions[-1].to_state == "BLOCKED"
    assert not [c for c in calls if c.tool_name == "inspect.verify"], "no command runs after a failed agent"


def test_summary_without_memory(repo: Path) -> None:
    result = inspect(repo, "--summary")
    assert result.exit_code == 1 and "no repository memory" in result.output


# Credentials outside the default denies that a repository's own commands must not read
# (review of steps 8-9, finding 1). Each file holds a canary the command prints if it can.
HOME_CREDENTIALS = {
    ".docker/config.json": "DOCKER-CANARY",
    ".kube/config": "KUBE-CANARY",
    ".npmrc": "NPMRC-CANARY",
    ".pypirc": "PYPIRC-CANARY",
    ".gnupg/private-keys-v1.d/key": "GNUPG-CANARY",
    ".azure/accessTokens.json": "AZURE-CANARY",
    ".config/op/config": "OP-CANARY",
    ".cargo/credentials": "CARGO-CANARY",
    ".cargo/credentials.toml": "CARGO-TOML-CANARY",
    ".m2/settings.xml": "MAVEN-CANARY",
    ".gradle/gradle.properties": "GRADLE-CANARY",
    ".codex/auth.json": "CODEX-CANARY",
    ".vault-token": "VAULT-CANARY",
    ".terraform.d/credentials.tfrc.json": "TERRAFORM-CANARY",
    ".config/containers/auth.json": "CONTAINERS-CANARY",
    ".gem/credentials": "GEM-CANARY",
    ".config/hub": "HUB-CANARY",
    ".cache/huggingface/token": "HF-CANARY",
}
# Committed, so they are in the run's worktree as well as the target repository.
REPO_KEYS = {"deploy.key": "KEY-CANARY", "certs/signing.p12": "P12-CANARY", "cert.pfx": "PFX-CANARY",
             "id_ed25519": "ED25519-CANARY", "keys/id_rsa": "RSA-CANARY", "id_ecdsa.pub": "ECDSA-CANARY"}  # fmt: skip


def test_inspect_commands_cannot_read_host_or_repo_credentials(
    tmp_path: Path, fake_claude, srt: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    for name, canary in HOME_CREDENTIALS.items():
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text(f"{canary}\n", encoding="utf-8")
    (home / ".gitconfig").write_text("READABLE-CANARY\n", encoding="utf-8")  # control: not a credential
    monkeypatch.setenv("HOME", str(home))
    reads = " ".join(f"$$HOME/{name}" for name in [*HOME_CREDENTIALS, ".gitconfig"])
    keys = " ".join(f"../../../{name}" for name in REPO_KEYS)  # the target repo, from the run's worktree
    makefile = f"test:\n\t@for f in {reads} {keys} {' '.join(REPO_KEYS)}; do cat $$f 2>/dev/null; done; true\n"
    root = make_repo(tmp_path / "target", makefile=makefile)
    for name, canary in REPO_KEYS.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(f"{canary}\n", encoding="utf-8")
    git(root, "add", *REPO_KEYS)
    git(root, "commit", "-q", "-m", "commit key files")
    write_proposal(root, which=lambda tool: f"/usr/bin/{tool}")
    fake_claude()
    result = inspect(root)
    assert result.exit_code == 0, result.output
    [test] = [c for c in commands_file(root) if c.command == "make test"]
    assert test.status == "passed", test
    assert "READABLE-CANARY" in (test.output_tail or ""), "the command ran and could read an ordinary file"
    leaked = [c for c in [*HOME_CREDENTIALS.values(), *REPO_KEYS.values()] if c in (test.output_tail or "")]
    assert leaked == [], f"inspect commands read: {leaked}"
