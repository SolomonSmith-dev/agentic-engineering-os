import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent_team.adapters.claude import focus_hook
from agent_team.focus import analyze, brief, is_test_command
from agent_team.schemas.runs import ToolCallRecord


def call(seq: int, tool: str, args: dict) -> ToolCallRecord:
    return ToolCallRecord(
        run_id="20261028-120000-abcdef",
        seq=seq,
        state="IMPLEMENTATION",
        tool_name=tool,
        args_summary=json.dumps(args),
        result_status="ok",
        started_at=datetime(2026, 10, 28, tzinfo=UTC),
        duration_ms=1,
        cost_usd=0,
    )


def test_test_command_matches_leading_words():
    assert is_test_command("uv run pytest -q tests/test_x.py", ["uv run pytest"])
    assert not is_test_command("uv run python -c 1", ["uv run pytest"])
    assert not is_test_command("ls", [])


def test_analyze_counts_progress_stall_and_loops():
    calls = [call(i, "Read", {"file_path": f"f{i}.py"}) for i in range(1, 5)]
    calls += [call(5 + i, "Bash", {"command": "git status"}) for i in range(3)]
    calls += [call(9, "Edit", {"file_path": "app.py"}), call(10, "Bash", {"command": "uv run pytest -q"})]
    report = analyze(calls, ["uv run pytest"])
    assert report.calls == 9
    assert report.progress == 2
    assert report.longest_stall == 7
    assert report.loops and "git status" in report.loops[0]
    assert "2/9 calls made progress" in report.render(stall_after=5)
    assert "over the 5-call threshold" in report.render(stall_after=5)


def test_analyze_empty():
    assert analyze([]).render() == "focus: no tool calls recorded"


def test_brief_cuts_long_prompts():
    assert brief("short") == "short"
    long = "\n".join(f"line {i} " + "x" * 50 for i in range(100))
    out = brief(long, limit=500)
    assert len(out) < 600 and out.endswith("above in this session]")


FOCUS = {"brief": "Fix the bug.", "test_commands": ["pytest"], "refocus_every": 4, "stall_after": 3, "loop_after": 3}


def test_hook_step_refocus_stall_loop():
    state: dict = {}
    nudges = [focus_hook.step({"tool_name": "Bash", "tool_input": {"command": "ls"}}, FOCUS, state) for _ in range(4)]
    assert nudges[0] == [] and nudges[1] == []
    assert any("3 tool calls in a row" in n for n in nudges[2])
    assert any("`ls` 3 times" in n for n in nudges[2])
    assert any("focus check after 4 tool calls" in n and "Fix the bug." in n for n in nudges[3])
    focus_hook.step({"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}, FOCUS, state)
    assert state["stall"] == 0


def test_hook_flags_unplanned_writes_once(tmp_path: Path):
    focus = {**FOCUS, "planned_files": ["src/app.py"], "write_root": str(tmp_path)}
    state: dict = {}
    assert focus_hook.step({"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "src/app.py")}}, focus, state) == []
    out = focus_hook.step({"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "README.md")}}, focus, state)
    assert any("README.md, which the plan does not name" in n for n in out)
    assert focus_hook.step({"tool_name": "Write", "tool_input": {"file_path": "README.md"}}, focus, state) == []


def run_hook(focus_json: Path, event: dict | str) -> subprocess.CompletedProcess[str]:
    payload = event if isinstance(event, str) else json.dumps(event)
    return subprocess.run(
        [sys.executable, "-m", "agent_team.adapters.claude.focus_hook", str(focus_json)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )


def test_hook_process_emits_additional_context_and_logs(tmp_path: Path):
    focus_json = tmp_path / "focus.json"
    focus_json.write_text(json.dumps({**FOCUS, "refocus_every": 2}), encoding="utf-8")
    first = run_hook(focus_json, {"tool_name": "Read", "tool_input": {"file_path": "a.py"}})
    assert first.returncode == 0 and first.stdout == ""
    second = run_hook(focus_json, {"tool_name": "Read", "tool_input": {"file_path": "b.py"}})
    out = json.loads(second.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "Fix the bug." in out["hookSpecificOutput"]["additionalContext"]
    log = (tmp_path / "focus.log.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(log[-1])["call"] == 2


def test_hook_fails_open(tmp_path: Path):
    bad = run_hook(tmp_path / "missing.json", "not json")
    assert bad.returncode == 0 and bad.stdout == ""
