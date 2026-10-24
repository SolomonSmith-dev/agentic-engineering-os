"""Codex adapter (build-order step 12): capabilities, compile, invocation, JSONL parsing, run.

No test calls a model, and none calls a real Codex: the adapter has not been
run against one yet. End-to-end tests put a fake `codex` on PATH that answers
`exec --help` and `login status`, records its argv, environment and stdin,
probes the protected paths srt should hide, and replays a `codex exec --json`
transcript in the event shape of openai/codex `codex-rs/exec/src/exec_events.rs`.
The Codex runtime refuses to run without srt, so those tests skip when srt is
absent (and fail when AGENT_TEAM_REQUIRE_SRT=1, as in CI). Regenerate the
compile snapshots after an intended change with
`UPDATE_SNAPSHOTS=1 uv run pytest tests/test_codex_adapter.py`.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_team.adapters.codex import (
    StreamParser,
    build_invocation,
    compile_agent,
    normalize,
    resolve,
    srt_deny_read,
    strict_schema,
)
from agent_team.adapters.codex.invoke import Invocation, check_flags, execute, has_auth
from agent_team.canonical import parse_file
from agent_team.cli import app
from agent_team.environments import srt_path
from agent_team.environments.sandbox import Sandbox
from agent_team.runs import RunStore
from agent_team.schemas import AgentSpec, PolicySpec
from agent_team.schemas.adapter import ReviewVerdict
from agent_team.schemas.registry import ModelCost
from agent_team.schemas.skill import SkillSpec

REPO = Path(__file__).resolve().parents[1]
CANONICAL = REPO / "canonical"
FIXTURES = Path(__file__).parent / "fixtures" / "codex"
MODEL = "gpt-6.1-sol"
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
    (root / ".env").write_text("TOKEN=SRT-CANARY\n", encoding="utf-8")
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


def _compiled(tmp_path: Path, **kwargs):
    agent, body = reviewer()
    defaults = dict(
        ceiling="A2", model=MODEL, out_dir=tmp_path / "compiled", roots=[tmp_path / "worktree", tmp_path / "repo"],
        home=tmp_path / "home", keep=[tmp_path / "worktree"], test_commands=("uv run pytest",),
    )  # fmt: skip
    return compile_agent(agent, body, skills(agent), policy(), **{**defaults, **kwargs})


# --- capabilities ---------------------------------------------------------------


def test_reviewer_runs_read_only() -> None:
    agent, _ = reviewer()
    perms = resolve(agent.capabilities.allow, agent.capabilities.deny, "A2", test_commands=("uv run pytest",))
    assert perms.sandbox_mode == "read-only"
    assert perms.granted == ["fs.read", "git.read", "shell.read", "test.run"]


def test_a_write_capability_selects_workspace_write_and_the_ceiling_still_drops() -> None:
    perms = resolve(["fs.read", "fs.write.workspace", "git.commit", "github.pr.create", "db.migrate.dev"], [], "A2")
    assert perms.sandbox_mode == "workspace-write"
    assert perms.dropped == [
        "git.commit: needs A3, ceiling is A2",
        "github.pr.create: exercised by the orchestrator, not the agent",
        "db.migrate.dev: no Codex mapping",
    ]
    assert resolve(["fs.write.workspace"], ["fs.write.*"], "A3").sandbox_mode == "read-only", "deny beats allow"
    assert resolve(["test.run"], [], "A2").dropped == ["test.run: no test command detected"]


# --- compile --------------------------------------------------------------------


def _normalized(path: Path, tmp_path: Path) -> str:
    return path.read_text(encoding="utf-8").replace(str(tmp_path), "<TMP>")


@pytest.mark.parametrize("name", ["independent-reviewer.toml", "config.toml", "schema.json"])
def test_compiled_reviewer_matches_snapshot(name: str, tmp_path: Path) -> None:
    compiled = _compiled(tmp_path)
    produced = {
        "independent-reviewer.toml": compiled.agent_toml,
        "config.toml": compiled.config_toml,
        "schema.json": compiled.schema_json,
    }[name]
    assert produced is not None
    actual = _normalized(produced, tmp_path)
    fixture = FIXTURES / name
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(actual, encoding="utf-8")
    assert actual == fixture.read_text(encoding="utf-8")


def test_compiled_files_are_valid_toml_with_the_codex_agent_shape(tmp_path: Path) -> None:
    compiled = _compiled(tmp_path)
    agent = tomllib.loads(compiled.agent_toml.read_text(encoding="utf-8"))
    assert set(agent) == {"name", "description", "model", "developer_instructions"}
    assert agent["name"] == "independent-reviewer" and agent["model"] == MODEL
    assert "## Skill: review-checklist" in agent["developer_instructions"]
    assert "- `uv run pytest`" in agent["developer_instructions"]
    assert "StructuredOutput" not in agent["developer_instructions"], "that is a Claude Code tool"
    config = tomllib.loads(compiled.config_toml.read_text(encoding="utf-8"))
    assert config["sandbox_mode"] == "read-only" and config["approval_policy"] == "never"
    assert config["shell_environment_policy"] == {"inherit": "core", "ignore_default_excludes": False}
    assert config["developer_instructions"] == agent["developer_instructions"]
    assert not (tmp_path / ".codex").exists()


def test_strict_schema_requires_every_property_and_still_validates_verdicts() -> None:
    schema = strict_schema(ReviewVerdict.model_json_schema())
    finding = schema["$defs"]["ReviewFinding"]
    assert schema["required"] == ["verdict", "findings"] and schema["additionalProperties"] is False
    assert finding["required"] == ["file", "line", "requirement_id", "severity", "fix"]
    assert "default" not in json.dumps(schema)
    jsonschema = pytest.importorskip("jsonschema")
    verdict = {"verdict": "changes", "findings": [
        {"file": "a.py", "line": None, "requirement_id": "AC-1", "severity": "major", "fix": "add a test"}]}  # fmt: skip
    jsonschema.validate(verdict, schema)
    ReviewVerdict.model_validate(verdict)


def test_protected_paths_compile_to_srt_deny_read_around_the_run(tmp_path: Path) -> None:
    repo, home = tmp_path / "repo", tmp_path / "home"
    state = repo / ".agent-team"
    worktree, tmp, compiled = state / "worktrees" / "r1", state / "tmp" / "r1", state / "compiled" / "r1"
    for d in (worktree, state / "worktrees" / "r0", tmp, state / "tmp" / "r0", compiled, state / "sandbox"):
        d.mkdir(parents=True)
    (state / "runs.db").write_text("", encoding="utf-8")
    entries = srt_deny_read([".env*", "~/.ssh/**", ".agent-team/**"], [worktree, repo], home, [worktree, tmp, compiled])
    literal = sorted(e["path"] for e in entries if isinstance(e, dict))
    assert f"{worktree}/**/.env*" in entries and f"{repo}/**/.env*" in entries
    assert str(home / ".ssh/**") in entries
    assert f"{worktree}/.agent-team/**" in entries, "a worktree's own .agent-team/ stays covered"
    assert literal == sorted(str(p) for p in (state / "runs.db", state / "sandbox", state / "tmp" / "r0",
                                               state / "worktrees" / "r0"))  # fmt: skip
    assert all(not str(worktree).startswith(p) for p in literal), "the run's own checkout stays readable"


# --- invocation -----------------------------------------------------------------

BASE_ENV = {
    "PATH": "/usr/bin",
    "HOME": "/home/u",
    "LANG": "C.UTF-8",
    "CODEX_HOME": "/home/u/.codex",
    "GH_TOKEN": "ghp_INVOKECANARY0000000000",
    "OPENAI_API_KEY": "sk-proj-NOTPASSEDCANARY000000000",
    "CLAUDE_CODE_SESSION_ID": "parent-session",
}


def test_login_mode_argv_and_scrubbed_env(tmp_path: Path) -> None:
    sandbox = Sandbox(srt=None, settings_path=None, tmp_dir=tmp_path / "t", port_offset=100)
    compiled = _compiled(tmp_path)
    inv = build_invocation(compiled, codex="/bin/codex", sandbox=sandbox, base_env=BASE_ENV, cwd=tmp_path / "wt")
    argv = inv.argv
    assert inv.mode == "login"
    assert argv[:2] == ["/bin/codex", "exec"] and argv[-1] == "-", "the prompt comes in on stdin"
    for flag in ("--json", "--ignore-user-config", "--ignore-rules", "--ephemeral"):
        assert flag in argv
    for flag, value in [("--cd", str(tmp_path / "wt")), ("--model", MODEL), ("--sandbox", "read-only"),
                        ("--output-schema", str(compiled.schema_json))]:  # fmt: skip
        assert argv[argv.index(flag) + 1] == value
    configs = [argv[i + 1] for i, a in enumerate(argv) if a == "--config"]
    assert 'approval_policy="never"' in configs and 'web_search="disabled"' in configs
    assert "shell_environment_policy.ignore_default_excludes=false" in configs
    [instructions] = [c for c in configs if c.startswith("developer_instructions=")]
    assert tomllib.loads(instructions)["developer_instructions"] == compiled.overrides["developer_instructions"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv and "danger-full-access" not in argv
    assert inv.env == {"PATH": "/usr/bin", "HOME": "/home/u", "LANG": "C.UTF-8", "CODEX_HOME": "/home/u/.codex",
                       "TMPDIR": str(tmp_path / "t"), "AGENT_TEAM_PORT_OFFSET": "100"}  # fmt: skip


def test_api_key_mode_passes_only_codex_api_key(tmp_path: Path) -> None:
    sandbox = Sandbox(srt="/usr/bin/srt", settings_path=tmp_path / "srt.json", tmp_dir=tmp_path / "t", port_offset=0)
    env = {**BASE_ENV, "CODEX_API_KEY": "sk-proj-CODEXKEYCANARY00000000"}
    inv = build_invocation(_compiled(tmp_path), codex="codex", sandbox=sandbox, base_env=env, cwd=tmp_path)
    assert inv.mode == "api_key" and inv.env["CODEX_API_KEY"] == env["CODEX_API_KEY"]
    assert "OPENAI_API_KEY" not in inv.env and "GH_TOKEN" not in inv.env
    assert inv.argv[:6] == ["/usr/bin/srt", "--settings", str(tmp_path / "srt.json"), "--", "env", f"TMPDIR={tmp_path / 't'}"]
    assert inv.argv[6:8] == ["codex", "exec"]


# --- stream parsing -------------------------------------------------------------

VERDICT = {"verdict": "changes", "findings": [
    {"file": "app.py", "line": 1, "requirement_id": "AC-1", "severity": "major", "fix": "add a test"}]}  # fmt: skip


def events(verdict: object = VERDICT, *, end: str = "completed", usage: dict | None = None) -> list[dict]:
    out = [
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "reading the diff"}},
        {"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "git diff HEAD",
                                          "aggregated_output": "", "exit_code": None, "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "git diff HEAD",
                                            "aggregated_output": "diff", "exit_code": 0, "status": "completed"}},
        {"type": "item.started", "item": {"id": "item_2", "type": "command_execution", "command": "cat .env",
                                          "aggregated_output": "", "exit_code": None, "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "item_2", "type": "command_execution", "command": "cat .env",
                                            "aggregated_output": "Permission denied", "exit_code": 1, "status": "failed"}},
        {"type": "item.completed", "item": {"id": "item_3", "type": "command_execution", "command": "rm -rf build",
                                            "aggregated_output": "", "exit_code": None, "status": "declined"}},
        {"type": "item.completed", "item": {"id": "item_4", "type": "todo_list", "items": []}},
    ]  # fmt: skip
    if end == "completed":
        text = verdict if isinstance(verdict, str) else json.dumps(verdict)
        out.append({"type": "item.completed", "item": {"id": "item_5", "type": "agent_message", "text": text}})
        out.append({"type": "turn.completed", "usage": usage or {"input_tokens": 1200, "cached_input_tokens": 200,
                                                                 "output_tokens": 300, "reasoning_output_tokens": 50}})  # fmt: skip
    elif end == "failed":
        out.append({"type": "error", "message": "stream disconnected; retrying"})
        out.append({"type": "turn.failed", "error": {"message": "401 Unauthorized sk-proj-FAILCANARY0000000000000"}})
    return out


def _parse(evts: list[dict]) -> StreamParser:
    parser = StreamParser()
    odd = ['{"type": "item.completed", "item": "not an object"}\n', "[1, 2]\n", "not json\n", "\n"]
    for i, line in enumerate([*odd, *(json.dumps(e) + "\n" for e in evts)]):
        parser.feed(line, float(i), datetime(2026, 10, 24, tzinfo=UTC))
    return parser


def test_parser_reads_items_usage_and_the_structured_verdict() -> None:
    parser = _parse(events())
    assert parser.unparsed == 3, "bad JSON and odd shapes are skipped, never fatal"
    assert [c.name for c in parser.calls.values()] == ["command_execution"] * 3, "reasoning and todo are not steps"
    result = normalize(parser, exit_code=0, timed_out=False, stderr="", output_model=ReviewVerdict)
    assert result.status == "succeeded" and result.runtime == "codex" and result.subtype == "turn.completed"
    assert result.structured_output == VERDICT and result.result is None
    assert result.session_id == "th-1" and result.num_turns == 1 and result.tool_calls == 3
    assert result.permission_denials == ["command_execution"]
    assert result.usage == {"input_tokens": 1200, "cached_input_tokens": 200, "output_tokens": 300,
                            "reasoning_output_tokens": 50}  # fmt: skip
    assert result.cost_usd == 0.0, "no registry price, no invented dollars"


@pytest.mark.parametrize("final", ['{"verdict": "maybe", "findings": []}', "Looks good to me.",
                                   '```json\n{"verdict": "approve", "findings": []}\n```'])  # fmt: skip
def test_an_invalid_or_unstructured_final_message_fails_the_run(final: str) -> None:
    result = normalize(_parse(events(final)), exit_code=0, timed_out=False, stderr="", output_model=ReviewVerdict)
    assert result.status == "failed" and "validation" in (result.structured_output_error or "")


def test_no_final_message_fails_a_structured_run() -> None:
    evts = [e for e in events() if e.get("item", {}).get("type") != "agent_message"]
    result = normalize(_parse(evts), exit_code=0, timed_out=False, stderr="", output_model=ReviewVerdict)
    assert result.status == "failed" and "no final agent message" in (result.structured_output_error or "")


def test_turn_failed_is_an_error_with_a_redacted_reason() -> None:
    result = normalize(_parse(events(end="failed")), exit_code=1, timed_out=False, stderr="boom", output_model=None)
    assert result.status == "error" and result.subtype == "turn.failed"
    assert result.reason is not None and "401 Unauthorized" in result.reason and "FAILCANARY" not in result.reason
    assert result.stderr_tail == "boom"


def test_a_stream_without_a_turn_end_is_an_error_and_a_nonzero_exit_fails() -> None:
    assert normalize(_parse(events(end="none")), exit_code=0, timed_out=False, stderr="", output_model=None).status == "error"
    assert normalize(_parse(events()), exit_code=1, timed_out=False, stderr="", output_model=None).status == "failed"


def test_budget_is_checked_after_the_run_from_a_registry_price() -> None:
    price = ModelCost(input_per_mtok_usd=10.0, output_per_mtok_usd=40.0)
    usage = {"input_tokens": 100_000, "output_tokens": 10_000}
    parser = _parse(events(usage=usage))
    result = normalize(parser, exit_code=0, timed_out=False, stderr="", output_model=ReviewVerdict,
                       price=price, max_budget_usd=1.00)  # fmt: skip
    assert result.cost_usd == pytest.approx(1.4)
    assert result.status == "budget_exceeded" and "checked after the run ended" in (result.reason or "")
    ok = normalize(parser, exit_code=0, timed_out=False, stderr="", output_model=ReviewVerdict,
                   price=price, max_budget_usd=2.00)  # fmt: skip
    assert ok.status == "succeeded" and ok.cost_usd == pytest.approx(1.4)


def test_timeout_kills_the_whole_worker_process_group(tmp_path: Path) -> None:
    inv = Invocation(argv=["sh", "-c", "sleep 60 & sleep 60"], env=dict(os.environ), mode="login")
    start = time.monotonic()
    result, _ = execute(inv, prompt="", cwd=tmp_path, stderr_path=tmp_path / "err", timeout_s=1)
    assert result.status == "timeout"
    assert time.monotonic() - start < 15


# --- end to end with a fake codex ----------------------------------------------

FAKE_CODEX = r'''#!{python}
"""Fake codex: answers --help and login status, records its inputs, probes srt, replays a transcript."""
import json, os, sys, time

FLAGS = {flags!r}
argv = sys.argv[1:]
if argv == ["exec", "--help"]:
    print("Usage: codex exec [OPTIONS] [PROMPT]\n" + "\n".join("  " + f for f in FLAGS))
    sys.exit(0)
if argv == ["login", "status"]:
    print("Logged in using ChatGPT", file=sys.stderr)
    sys.exit(0 if {logged_in!r} else 1)
assert argv[0] == "exec", argv


def readable(path):
    try:
        open(path).read()
        return True
    except OSError:
        return False


cwd = os.getcwd()
repo = os.path.dirname(os.path.dirname(os.path.dirname(cwd)))  # <repo>/.agent-team/worktrees/<run>
schema = argv[argv.index("--output-schema") + 1]
seen = {{"argv": argv, "env": dict(os.environ), "stdin": sys.stdin.read(), "cwd": cwd,
         "env_file_readable": readable(os.path.join(repo, ".env")),
         "runs_db_readable": readable(os.path.join(repo, ".agent-team", "runs.db")),
         "schema_readable": readable(schema), "app_readable": readable(os.path.join(cwd, "app.py"))}}
with open(os.path.join(os.environ["TMPDIR"], "fake-codex.json"), "w") as out:
    json.dump(seen, out)

mode = {mode!r}
if mode == "hang":
    time.sleep(60)
for event in {events!r}:
    print(json.dumps(event), flush=True)
sys.exit(1 if mode == "failed" else 0)
'''

ALL_FLAGS = ("--json", "--output-schema", "--ignore-user-config", "--ignore-rules", "--ephemeral",
             "--sandbox", "--model", "--config", "--cd")  # fmt: skip


@pytest.fixture
def srt() -> str:
    path = srt_path()
    if path is None:
        if os.environ.get("AGENT_TEAM_REQUIRE_SRT") == "1":
            pytest.fail("AGENT_TEAM_REQUIRE_SRT=1 but srt is not on PATH")
        pytest.skip("srt not installed; the codex runtime refuses to run without it")
    return path


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def _install(mode: str = "ok", *, logged_in: bool = True, flags: tuple[str, ...] = ALL_FLAGS) -> Path:
        evts = {"ok": events({"verdict": "approve", "findings": []}), "changes": events(),
                "failed": events(end="failed"), "invalid": events("not json"), "hang": []}[mode]  # fmt: skip
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        fake = bin_dir / "codex"
        fake.write_text(FAKE_CODEX.format(python=sys.executable, flags=flags, logged_in=logged_in,
                                          mode=mode, events=evts), encoding="utf-8")  # fmt: skip
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("GH_TOKEN", "ghp_E2ECANARY000000000000")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-E2ECANARY0000000000000")
        monkeypatch.delenv("CODEX_API_KEY", raising=False)
        return fake

    return _install


def invoke_run(repo: Path, *extra: str):
    return runner.invoke(
        app,
        ["run", "--agent", "independent-reviewer", "--runtime", "codex", "--repo", str(repo),
         "--canonical", str(CANONICAL), "--test-command", "uv run pytest", *extra, "Review the diff against AC-1."],
    )  # fmt: skip


def only_run(repo: Path):
    store = RunStore.open(repo, create=False)
    assert store is not None
    with store:
        [run] = store.list_runs()
        return run, store.transitions(run.run_id), store.tool_calls(run.run_id)


def test_codex_reviewer_end_to_end(repo: Path, fake_codex, srt: str) -> None:
    fake_codex("changes")
    result = invoke_run(repo)
    assert result.exit_code == 0, result.output
    assert '"verdict": "changes"' in result.output
    assert "succeeded: succeeded in 1 turn(s), 3 tool call(s), $0.0000, 1200 in / 300 out tokens" in result.output

    run, transitions, calls = only_run(repo)
    assert run.outcome == "succeeded" and run.model_class == "independent"
    assert [t.to_state for t in transitions] == ["INTAKE", "PLAN_REVIEW"]
    note = transitions[1].note or ""
    assert "srt sandbox" in note and "model gpt-6.1-sol (class independent, vendor-cloud), no fallback" in note
    assert "runtime codex, sandbox_mode read-only" in note
    assert "unavailable: hooks: not used" in note and "budget cap: no Codex flag" in note
    assert [(c.tool_name, c.result_status) for c in calls] == [
        ("command_execution", "ok"), ("command_execution", "error"), ("command_execution", "denied")]  # fmt: skip
    assert json.loads(calls[0].args_summary) == {"command": "git diff HEAD"}

    seen = json.loads((repo / ".agent-team" / "tmp" / run.run_id / "fake-codex.json").read_text())
    assert seen["stdin"] == "Review the diff against AC-1." and seen["argv"][-1] == "-"
    assert seen["cwd"] == run.worktree_path
    assert seen["argv"][seen["argv"].index("--model") + 1] == MODEL, "the registry resolves independent on codex"
    for leaked in ("GH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY"):
        assert leaked not in seen["env"]
    assert not seen["env_file_readable"], "srt denyRead hides the target repo's .env"
    assert not seen["runs_db_readable"], "and the run record"
    assert seen["schema_readable"] and seen["app_readable"], "but not the run's own checkout or compiled files"

    settings = json.loads((repo / ".agent-team" / "sandbox" / f"{run.run_id}.json").read_text())
    assert {"chatgpt.com", "auth.openai.com"} <= set(settings["network"]["allowedDomains"])
    assert "api.openai.com" not in settings["network"]["allowedDomains"], "login mode needs no API host"
    assert f"{repo}/**/.env*" in settings["filesystem"]["denyRead"]
    compiled = repo / ".agent-team" / "compiled" / run.run_id
    assert (compiled / "agents" / "independent-reviewer.toml").is_file() and (compiled / "config.toml").is_file()
    assert not (repo / ".codex").exists() and not (repo / ".claude").exists()
    assert not (repo / ".agent-team" / "lock").exists()


def test_codex_turn_failure_is_recorded_blocked_with_the_reason(repo: Path, fake_codex, srt: str) -> None:
    fake_codex("failed")
    result = invoke_run(repo)
    assert result.exit_code == 1, result.output
    run, transitions, _ = only_run(repo)
    assert run.outcome == "failed" and transitions[-1].to_state == "BLOCKED"
    assert transitions[-1].note.startswith("error: 401 Unauthorized")
    assert "FAILCANARY" not in transitions[-1].note


def test_codex_unstructured_answer_fails_the_run(repo: Path, fake_codex, srt: str) -> None:
    fake_codex("invalid")
    result = invoke_run(repo)
    assert result.exit_code == 1
    _, transitions, _ = only_run(repo)
    assert transitions[-1].note.startswith("failed: structured output failed validation")


def test_codex_timeout_is_recorded(repo: Path, fake_codex, srt: str) -> None:
    fake_codex("hang")
    result = invoke_run(repo, "--timeout", "2")
    assert result.exit_code == 1, result.output
    run, transitions, _ = only_run(repo)
    assert run.outcome == "failed" and transitions[-1].note == "timeout: exit -9"


def test_codex_refuses_without_auth_before_recording(repo: Path, fake_codex) -> None:
    fake_codex(logged_in=False)
    result = invoke_run(repo)
    assert result.exit_code == 2 and "codex has no auth" in result.output
    assert RunStore.open(repo, create=False) is None


def test_codex_api_key_counts_as_auth_without_asking_codex(fake_codex, tmp_path: Path) -> None:
    fake = fake_codex(logged_in=False)
    assert not has_auth(str(fake), {"PATH": os.environ["PATH"]})
    assert has_auth(str(fake), {"CODEX_API_KEY": "sk-proj-X"})


def test_codex_missing_from_path_is_refused(repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result = invoke_run(repo)
    assert result.exit_code == 2 and "codex is not on PATH" in result.output


def test_codex_without_a_needed_flag_is_refused_before_recording(repo: Path, fake_codex) -> None:
    fake = fake_codex(flags=tuple(f for f in ALL_FLAGS if f != "--ignore-user-config"))
    with pytest.raises(Exception, match="does not support --ignore-user-config"):
        check_flags(str(fake), dict(os.environ))
    result = invoke_run(repo)
    assert result.exit_code == 2 and "does not support --ignore-user-config" in result.output
    assert RunStore.open(repo, create=False) is None


def test_codex_without_srt_is_refused_before_recording(repo: Path, fake_codex, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_codex()
    monkeypatch.setattr("agent_team.run_agent.srt_path", lambda: None)
    result = invoke_run(repo)
    assert result.exit_code == 2 and "--runtime codex needs srt" in result.output
    assert RunStore.open(repo, create=False) is None


def test_codex_runs_only_the_reviewer(repo: Path, fake_codex, tmp_path: Path) -> None:
    fake_codex()
    canonical = tmp_path / "tree" / "canonical"
    shutil.copytree(CANONICAL, canonical)
    shutil.copytree(REPO / "routing", tmp_path / "tree" / "routing")
    text = (canonical / "agents" / "independent-reviewer.md").read_text(encoding="utf-8")
    (canonical / "agents" / "second-reviewer.md").write_text(
        text.replace("name: independent-reviewer", "name: second-reviewer"), encoding="utf-8"
    )
    result = runner.invoke(app, ["run", "--agent", "second-reviewer", "--runtime", "codex", "--repo", str(repo),
                                 "--canonical", str(canonical), "Review."])  # fmt: skip
    assert result.exit_code == 2 and "runs only independent-reviewer, not second-reviewer" in result.output
    assert RunStore.open(repo, create=False) is None


def test_model_override_must_be_a_codex_registry_entry(repo: Path, fake_codex) -> None:
    fake_codex()
    result = invoke_run(repo, "--model", "claude-opus-5-5")
    assert result.exit_code == 2 and "runs on claude, not codex" in result.output


def test_unknown_runtime_is_refused(repo: Path) -> None:
    result = runner.invoke(app, ["run", "--agent", "independent-reviewer", "--runtime", "gemini", "--repo", str(repo), "x"])
    assert result.exit_code == 2 and "--runtime must be one of claude, codex" in result.output
