"""Claude adapter (build-order step 6): capabilities, compile, hook, invocation, stream parsing, run.

No test calls a model. End-to-end tests put a fake `claude` on PATH that
records its argv, environment and stdin, runs the compiled PreToolUse hook the
way Claude Code would, and replays a stream-json transcript. When srt is
installed the fake runs inside the real sandbox. Regenerate the compile
snapshots after an intended change with
`UPDATE_SNAPSHOTS=1 uv run pytest tests/test_claude_adapter.py`.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_team.adapters.claude import StreamParser, build_invocation, compile_agent, normalize, resolve
from agent_team.adapters.claude.hook import check
from agent_team.canonical import parse_file
from agent_team.cli import app
from agent_team.environments import acquire
from agent_team.environments.sandbox import Sandbox
from agent_team.runs import RunStore
from agent_team.schemas import AgentSpec, PolicySpec
from agent_team.schemas.adapter import ReviewVerdict
from agent_team.schemas.skill import SkillSpec

REPO = Path(__file__).resolve().parents[1]
CANONICAL = REPO / "canonical"
FIXTURES = Path(__file__).parent / "fixtures" / "claude"
MODEL = "claude-opus-5-5"
runner = CliRunner()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text(".agent-team/\n.env\n", encoding="utf-8")
    (root / "app.py").write_text("print('hello')\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    (root / ".env").write_text("TOKEN=HOOK-CANARY\n", encoding="utf-8")
    return root.resolve()


def reviewer() -> tuple[AgentSpec, str]:
    data, body = parse_file(CANONICAL / "agents" / "independent-reviewer.md", "agent")
    return AgentSpec.model_validate(data), body


def skills(agent: AgentSpec) -> list[tuple[SkillSpec, str]]:
    out = []
    for name in agent.skills:
        data, body = parse_file(CANONICAL / "skills" / name / "SKILL.md", "skill")
        out.append((SkillSpec.model_validate(data), body))
    return out


def policy() -> PolicySpec:
    data, _ = parse_file(CANONICAL / "policies" / "default.yaml", "policy")
    return PolicySpec.model_validate(data)


# --- capabilities ---------------------------------------------------------------


def test_reviewer_capabilities_at_a2() -> None:
    agent, _ = reviewer()
    perms = resolve(agent.capabilities.allow, agent.capabilities.deny, "A2", test_commands=("uv run pytest",))
    assert perms.granted == ["fs.read", "git.read", "shell.read", "test.run"]
    assert "Bash(uv run pytest)" in perms.allow and "Bash(uv run pytest *)" in perms.allow
    assert perms.tools == ["Bash", "Glob", "Grep", "Read"]
    assert "Edit" in perms.deny and "Write" in perms.deny, "fs.write.* is denied for the reviewer"
    assert "Bash(git push *)" in perms.deny
    assert {"Bash(git push)", "Bash(git reset --hard)", "Bash(git config)"} <= set(perms.deny), "bare forms too"


def test_capabilities_above_the_ceiling_are_dropped_and_reported() -> None:
    perms = resolve(["fs.read", "fs.write.workspace", "git.commit", "test.run", "github.pr.create", "db.migrate.dev"], [], "A0")
    assert perms.granted == ["fs.read"]
    assert perms.dropped == [
        "fs.write.workspace: needs A2, ceiling is A0",
        "git.commit: needs A3, ceiling is A0",
        "test.run: needs A2, ceiling is A0",
        "github.pr.create: exercised by the orchestrator, not the agent",
        "db.migrate.dev: no Claude Code mapping",
    ]


def test_test_run_without_a_detected_command_grants_nothing() -> None:
    perms = resolve(["test.run"], [], "A2")
    assert perms.granted == [] and perms.dropped == ["test.run: no test command detected"]


def test_wildcard_deny_beats_allow() -> None:
    perms = resolve(["fs.write.workspace", "git.commit"], ["fs.write.*"], "A3")
    assert perms.granted == ["git.commit"]
    assert "fs.write.workspace: denied by policy" in perms.dropped


# --- compile --------------------------------------------------------------------


def _normalized(path: Path, tmp_path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return text.replace(str(tmp_path), "<TMP>").replace(sys.executable, "<PYTHON>")


@pytest.mark.parametrize("name", ["agents.json", "settings.json", "policy.json", "independent-reviewer.md"])
def test_compiled_reviewer_matches_snapshot(name: str, tmp_path: Path) -> None:
    agent, body = reviewer()
    compiled = compile_agent(
        agent,
        body,
        skills(agent),
        policy(),
        ceiling="A2",
        model=MODEL,
        out_dir=tmp_path / "compiled",
        roots=[tmp_path / "worktree", tmp_path / "repo"],
        home=tmp_path / "home",
        test_commands=("uv run pytest",),
    )
    produced = {
        "agents.json": compiled.agents_json,
        "settings.json": compiled.settings_json,
        "policy.json": compiled.policy_json,
        "independent-reviewer.md": compiled.native_md,
    }[name]
    actual = _normalized(produced, tmp_path)
    fixture = FIXTURES / name
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(actual, encoding="utf-8")
    assert actual == fixture.read_text(encoding="utf-8")


def test_compile_inlines_skills_and_schema_for_the_reviewer(tmp_path: Path) -> None:
    agent, body = reviewer()
    compiled = compile_agent(
        agent, body, skills(agent), policy(), ceiling="A2", model=MODEL,
        out_dir=tmp_path, roots=[tmp_path], home=tmp_path,
    )  # fmt: skip
    definition = json.loads(compiled.agents_json.read_text())["independent-reviewer"]
    assert "## Skill: review-checklist" in definition["prompt"]
    assert "## Skill: requirement-tracing" in definition["prompt"]
    assert definition["permissionMode"] == "dontAsk" and definition["model"] == MODEL
    assert all("(" not in t for t in definition["disallowedTools"]), "scoped rules would remove whole tools"
    assert compiled.schema_json is not None
    assert json.loads(compiled.schema_json.read_text()) == ReviewVerdict.model_json_schema()
    settings = json.loads(compiled.settings_json.read_text())
    assert settings["attribution"] == {"commit": "", "pr": ""}
    assert "Read(**/.env*)" in settings["permissions"]["deny"]
    assert "Read(~/.ssh/**)" in settings["permissions"]["deny"]


# --- hook -----------------------------------------------------------------------


@pytest.fixture
def hook_policy(tmp_path: Path) -> dict:
    repo = tmp_path / "repo"
    worktree = repo / ".agent-team" / "worktrees" / "run-1"
    return {
        "protected_paths": [".env*", "*.pem", "~/.ssh/**", ".agent-team/**"],
        "roots": [str(worktree), str(repo)],
        "home": str(tmp_path / "home"),
    }


@pytest.mark.parametrize(
    ("tool", "tool_input", "blocked"),
    [
        ("Read", {"file_path": "app.py"}, False),
        ("Read", {"file_path": ".env"}, True),
        ("Read", {"file_path": "config/.env.local"}, True),
        ("Read", {"file_path": ".envrc"}, True),
        ("Edit", {"file_path": "certs/server.pem"}, True),
        ("Bash", {"command": "uv run pytest -q"}, False),
        ("Bash", {"command": "cat .env"}, True),
        ("Bash", {"command": "grep TOKEN < ./.env"}, True),
        ("Bash", {"command": "cat ~/.ssh/id_ed25519"}, True),
        ("Bash", {"command": 'cat "$HOME/.ssh/id_ed25519"'}, True),
        ("Bash", {"command": "cat ${HOME}/.ssh/config"}, True),
        ("Bash", {"command": "echo $HOMEBREW_PREFIX"}, False),
        ("Bash", {"command": "sqlite3 ../../runs.db .dump"}, True),
        ("Bash", {"command": "git diff HEAD~1"}, False),
        ("Grep", {"pattern": "TOKEN", "path": ".env"}, True),
        ("Glob", {"pattern": "**/*.py"}, False),
    ],
)
def test_hook_blocks_protected_paths(tool: str, tool_input: dict, blocked: bool, hook_policy: dict) -> None:
    reason = check({"tool_name": tool, "tool_input": tool_input}, hook_policy)
    assert (reason is not None) == blocked, reason


def test_worktree_files_are_not_caught_by_the_state_dir_rule(hook_policy: dict) -> None:
    inside = Path(hook_policy["roots"][0]) / "src" / "main.py"
    assert check({"tool_name": "Edit", "tool_input": {"file_path": str(inside)}}, hook_policy) is None


def _hook(policy_file: Path, stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agent_team.adapters.claude.hook", str(policy_file)],
        input=stdin, capture_output=True, text=True, check=False,
    )  # fmt: skip


def test_hook_entry_point_exit_codes(tmp_path: Path, hook_policy: dict) -> None:
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(hook_policy))
    ok = _hook(policy_file, json.dumps({"tool_name": "Read", "tool_input": {"file_path": "app.py"}}))
    assert ok.returncode == 0
    blocked = _hook(policy_file, json.dumps({"tool_name": "Bash", "tool_input": {"command": "cat .env"}}))
    assert blocked.returncode == 2
    assert _hook(policy_file, "not json").returncode == 2, "fails closed on bad input"
    assert "protected by '.env*'" in blocked.stderr
    assert _hook(tmp_path / "missing.json", "{}").returncode == 2, "fails closed without a policy"


# --- invocation -----------------------------------------------------------------


def _compiled(tmp_path: Path):
    agent, body = reviewer()
    return compile_agent(
        agent, body, skills(agent), policy(), ceiling="A2", model=MODEL,
        out_dir=tmp_path / "c", roots=[tmp_path], home=tmp_path,
    )  # fmt: skip


BASE_ENV = {
    "PATH": "/usr/bin",
    "HOME": "/home/u",
    "LANG": "C.UTF-8",
    "GH_TOKEN": "ghp_INVOKECANARY0000000000",
    "CLAUDE_CODE_SESSION_ID": "parent-session",
    "TMPDIR": "/tmp/caller",
}


def test_login_mode_keeps_repo_settings_out_without_bare(tmp_path: Path) -> None:
    sandbox = Sandbox(srt=None, settings_path=None, tmp_dir=tmp_path / "t", port_offset=100)
    inv = build_invocation(_compiled(tmp_path), claude="/bin/claude", sandbox=sandbox, base_env=BASE_ENV, max_budget_usd=1.0)
    assert inv.mode == "login"
    argv = inv.argv
    assert argv[:4] == ["/bin/claude", "-p", "--setting-sources", ""]
    assert "--bare" not in argv
    for flag, value in [("--agent", "independent-reviewer"), ("--model", MODEL), ("--permission-mode", "dontAsk"),
                        ("--output-format", "stream-json"), ("--max-budget-usd", "1.00"), ("--permission-prompts", "none")]:
        assert argv[argv.index(flag) + 1] == value
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == ReviewVerdict.model_json_schema()
    assert inv.env == {"PATH": "/usr/bin", "HOME": "/home/u", "LANG": "C.UTF-8",
                       "TMPDIR": str(tmp_path / "t"), "AGENT_TEAM_PORT_OFFSET": "100"}  # fmt: skip


def test_bare_mode_when_an_api_key_is_set(tmp_path: Path) -> None:
    sandbox = Sandbox(srt=None, settings_path=None, tmp_dir=tmp_path, port_offset=0)
    env = {**BASE_ENV, "ANTHROPIC_API_KEY": "sk-ant-api03-BARECANARY"}
    inv = build_invocation(_compiled(tmp_path), claude="claude", sandbox=sandbox, base_env=env, max_budget_usd=0.5)
    assert inv.mode == "bare" and inv.argv[:3] == ["claude", "-p", "--bare"]
    assert "--setting-sources" not in inv.argv
    assert inv.env["ANTHROPIC_API_KEY"] == "sk-ant-api03-BARECANARY"
    assert "GH_TOKEN" not in inv.env


def test_sandboxed_invocation_is_wrapped_in_srt(tmp_path: Path) -> None:
    settings = tmp_path / "srt.json"
    sandbox = Sandbox(srt="/usr/bin/srt", settings_path=settings, tmp_dir=tmp_path / "t", port_offset=200)
    inv = build_invocation(_compiled(tmp_path), claude="/bin/claude", sandbox=sandbox, base_env=BASE_ENV, max_budget_usd=1.0)
    assert inv.argv[:6] == ["/usr/bin/srt", "--settings", str(settings), "--", "env", f"TMPDIR={tmp_path / 't'}"]
    assert inv.argv[6:8] == ["/bin/claude", "-p"]
    assert "TMPDIR" not in inv.env


# --- stream parsing -------------------------------------------------------------


def _lines(*events: dict) -> list[str]:
    return [json.dumps(e) + "\n" for e in events]


TOOL_USE = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git diff"}},
    {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": ".env"}},
]}}  # fmt: skip
TOOL_RESULT = {"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "t1", "is_error": False, "content": "diff"},
    {"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": "denied"},
]}}  # fmt: skip
VERDICT = {"verdict": "changes", "findings": [
    {"file": "app.py", "line": 1, "requirement_id": "AC-1", "severity": "major", "fix": "add a test"}
]}  # fmt: skip


def _parse(*events: dict) -> StreamParser:
    from datetime import UTC, datetime

    parser = StreamParser()
    odd = ['{"type": "system", "subtype": "notice", "message": "a string, not an object"}\n', "[1, 2]\n", '"text"\n']
    for i, line in enumerate([*odd, *_lines(*events), "not json\n", "\n"]):
        parser.feed(line, float(i), datetime(2026, 10, 24, tzinfo=UTC))
    return parser


def test_parser_pairs_tool_calls_and_reads_the_result() -> None:
    result_event = {"type": "result", "subtype": "success", "is_error": False, "result": "done",
                    "total_cost_usd": 0.42, "num_turns": 3, "session_id": "s",
                    "permission_denials": [{"tool_name": "Read", "tool_use_id": "t2"}],
                    "structured_output": VERDICT}  # fmt: skip
    parser = _parse({"type": "system", "subtype": "init"}, TOOL_USE, TOOL_RESULT, result_event)
    assert parser.unparsed == 3, "non-object events and bad JSON are skipped, never fatal"
    assert [c.name for c in parser.calls.values()] == ["Bash", "Read"]
    result = normalize(parser, exit_code=0, timed_out=False, stderr="", output_model=ReviewVerdict)
    assert result.status == "succeeded" and result.cost_usd == 0.42 and result.num_turns == 3
    assert result.tool_calls == 2 and result.permission_denials == ["Read"]
    assert result.structured_output["verdict"] == "changes"


@pytest.mark.parametrize(
    ("subtype", "is_error", "status"),
    [("error_max_budget_usd", True, "budget_exceeded"), ("error_max_turns", True, "max_turns"),
     ("error_during_execution", True, "error"), ("success", True, "failed")],
)  # fmt: skip
def test_end_reasons_map_to_statuses(subtype: str, is_error: bool, status: str) -> None:
    parser = _parse({"type": "result", "subtype": subtype, "is_error": is_error, "total_cost_usd": 1.01})
    result = normalize(parser, exit_code=1, timed_out=False, stderr="boom", output_model=None)
    assert result.status == status and result.subtype == subtype and result.stderr_tail == "boom"


def test_missing_or_invalid_structured_output_fails_the_run() -> None:
    ok = {"type": "result", "subtype": "success", "is_error": False}
    assert normalize(_parse(ok), exit_code=0, timed_out=False, stderr="", output_model=ReviewVerdict).status == "failed"
    bad = {**ok, "structured_output": {"verdict": "maybe"}}
    result = normalize(_parse(bad), exit_code=0, timed_out=False, stderr="", output_model=ReviewVerdict)
    assert result.status == "failed" and "validation" in result.structured_output_error


def test_success_event_with_a_nonzero_exit_is_a_failed_run() -> None:
    parser = _parse({"type": "result", "subtype": "success", "is_error": False})
    assert normalize(parser, exit_code=1, timed_out=False, stderr="", output_model=None).status == "failed"


def test_timeout_kills_the_whole_worker_process_group(tmp_path: Path) -> None:
    import time

    from agent_team.adapters.claude.invoke import Invocation, execute

    # The child keeps stdout open after the parent dies; only a group kill ends the read loop.
    inv = Invocation(argv=["sh", "-c", "sleep 60 & sleep 60"], env=dict(os.environ), mode="login")
    start = time.monotonic()
    result, _ = execute(inv, prompt="", cwd=tmp_path, stderr_path=tmp_path / "err", timeout_s=1)
    assert result.status == "timeout"
    assert time.monotonic() - start < 15


def test_no_result_event_is_an_error_and_stderr_is_redacted() -> None:
    result = normalize(StreamParser(), exit_code=1, timed_out=False, stderr="auth failed sk-ant-api03-STDERRCANARY0000000", output_model=None)
    assert result.status == "error"
    assert "STDERRCANARY" not in (result.stderr_tail or "")


# --- end to end with a fake claude ---------------------------------------------

FAKE_CLAUDE = r'''#!{python}
"""Fake claude: records its inputs, runs the compiled PreToolUse hook, replays a transcript."""
import json, os, shlex, subprocess, sys

if sys.argv[1:] == ["--version"]:
    print(os.environ.get("FAKE_CLAUDE_VERSION", "{version} (Claude Code)"))
    sys.exit(0)

argv = sys.argv[1:]
settings = json.load(open(argv[argv.index("--settings") + 1]))
hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
probe = {{"tool_name": "Bash", "tool_input": {{"command": "cat .env"}}}}
hook_run = subprocess.run(shlex.split(hook), input=json.dumps(probe), capture_output=True, text=True)
prompt = sys.stdin.read()
seen = {{"argv": argv, "env": dict(os.environ), "stdin": prompt, "cwd": os.getcwd(),
         "hook_exit": hook_run.returncode, "hook_stderr": hook_run.stderr}}
with open(os.path.join(os.environ["TMPDIR"], "fake-claude.json"), "w") as out:
    json.dump(seen, out)

mode = {mode!r}
events = [
    {{"type": "system", "subtype": "init"}},
    {{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "t1", "name": "Bash", "input": {{"command": "git diff HEAD"}}}},
                                                  {{"type": "tool_use", "id": "t2", "name": "Bash", "input": {{"command": "cat .env"}}}}]}}}},
    {{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "t1", "is_error": False, "content": ""}},
                                             {{"type": "tool_result", "tool_use_id": "t2", "is_error": hook_run.returncode == 2, "content": hook_run.stderr}}]}}}},
]
if mode == "budget":
    events.append({{"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "total_cost_usd": 1.02, "num_turns": 4}})
    code = 1
else:
    events.append({{"type": "result", "subtype": "success", "is_error": False, "result": "reviewed",
                   "total_cost_usd": 0.37, "num_turns": 2, "session_id": "fake",
                   "structured_output": {{"verdict": "approve", "findings": []}}}})
    code = 0
for event in events:
    print(json.dumps(event), flush=True)
sys.exit(code)
'''


def install_fake_claude(tmp_path: Path, mode: str = "ok", version: str = "2.1.310") -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "claude"
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable, mode=mode, version=version), encoding="utf-8")
    fake.chmod(0o755)
    return fake


@pytest.fixture
def fake_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def _install(mode: str = "ok", version: str = "2.1.310") -> Path:
        fake = install_fake_claude(tmp_path, mode, version)
        monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("GH_TOKEN", "ghp_E2ECANARY000000000000")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-session")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        return fake

    return _install


def invoke_run(repo: Path, *extra: str):
    return runner.invoke(
        app,
        ["run", "--agent", "independent-reviewer", "--model", MODEL, "--repo", str(repo),
         "--canonical", str(CANONICAL), "--test-command", "uv run pytest", *extra, "Review the diff against AC-1."],
    )  # fmt: skip


def only_run(repo: Path):
    store = RunStore.open(repo, create=False)
    assert store is not None
    with store:
        [run] = store.list_runs()
        return run, store.transitions(run.run_id), store.tool_calls(run.run_id)


def test_run_reviewer_end_to_end(repo: Path, fake_path) -> None:
    fake_path()
    result = invoke_run(repo)
    assert result.exit_code == 0, result.output
    assert '"verdict": "approve"' in result.output
    assert "succeeded: succeeded in 2 turn(s), 2 tool call(s), $0.3700" in result.output

    run, transitions, calls = only_run(repo)
    assert run.outcome == "succeeded" and run.cost_usd == pytest.approx(0.37)
    assert run.workflow == "agent:independent-reviewer" and run.model_class == "independent"
    assert run.worktree_path == str(repo / ".agent-team" / "worktrees" / run.run_id)
    assert [t.to_state for t in transitions] == ["INTAKE", "PLAN_REVIEW"], "the agent's first state trigger"
    assert "autonomy ceiling A2" in transitions[1].note
    assert [(c.tool_name, c.result_status) for c in calls] == [("Bash", "ok"), ("Bash", "error")]

    seen = json.loads((repo / ".agent-team" / "tmp" / run.run_id / "fake-claude.json").read_text())
    assert seen["stdin"] == "Review the diff against AC-1.", "the prompt goes in on stdin"
    assert "Review the diff" not in " ".join(seen["argv"])
    assert seen["cwd"] == run.worktree_path
    assert "GH_TOKEN" not in seen["env"] and "CLAUDE_CODE_SESSION_ID" not in seen["env"]
    assert seen["hook_exit"] == 2 and "protected by '.env*'" in seen["hook_stderr"], "the hook runs and blocks"
    assert not (repo / ".agent-team" / "lock").exists(), "the lock is released"
    assert (repo / ".agent-team" / "compiled" / run.run_id / "agents" / "independent-reviewer.md").is_file()
    assert not (repo / ".claude").exists(), "nothing is written into the target repo's .claude/"


def test_run_budget_abort_is_recorded_with_reason(repo: Path, fake_path) -> None:
    fake_path("budget")
    result = invoke_run(repo)
    assert result.exit_code == 1, result.output
    run, transitions, _ = only_run(repo)
    assert run.outcome == "failed" and run.cost_usd == pytest.approx(1.02)
    assert transitions[-1].to_state == "BLOCKED"
    assert transitions[-1].note == "budget_exceeded: error_max_budget_usd"


def test_run_refuses_an_old_claude_before_recording_anything(repo: Path, fake_path) -> None:
    fake_path(version="2.1.200")
    result = invoke_run(repo)
    assert result.exit_code == 2
    assert "older than 2.1.302" in result.output
    assert RunStore.open(repo, create=False) is None


def test_run_while_locked_records_a_blocked_run(repo: Path, fake_path) -> None:
    fake_path()
    acquire(repo / ".agent-team", "other-run")
    result = invoke_run(repo)
    assert result.exit_code == 2 and "holds" in result.output
    run, transitions, _ = only_run(repo)
    assert run.outcome == "blocked" and transitions[-1].to_state == "BLOCKED"


def test_run_reports_an_unreadable_prompt_file(repo: Path, fake_path, tmp_path: Path) -> None:
    fake_path()
    result = runner.invoke(app, ["run", "--agent", "independent-reviewer", "--model", MODEL, "--repo", str(repo),
                                 "--prompt-file", str(tmp_path / "missing.md")])  # fmt: skip
    assert result.exit_code == 2 and "cannot read --prompt-file" in result.output
    assert RunStore.open(repo, create=False) is None


def test_run_needs_exactly_one_prompt_source(repo: Path, fake_path) -> None:
    fake_path()
    result = runner.invoke(app, ["run", "--agent", "independent-reviewer", "--model", MODEL, "--repo", str(repo)])
    assert result.exit_code == 2 and "--prompt-file" in result.output
