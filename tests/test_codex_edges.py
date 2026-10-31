"""Edges of the Codex adapter (build-order step 12): probes that fail, every item type, and recording.

Parsing and recording are fed hand-written `codex exec --json` events; the
probes and `execute` run small stand-in processes. No real Codex runs.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_codex_adapter import repo  # noqa: F401  (fixture is used by name)

from agent_team.adapters.codex import StreamParser, normalize
from agent_team.adapters.codex.compile import toml_value
from agent_team.adapters.codex.invoke import (
    REQUIRED_FLAGS,
    CodexUnavailableError,
    Invocation,
    check_flags,
    execute,
    has_auth,
    record,
)
from agent_team.runs import RunStore
from agent_team.schemas.adapter import ReviewVerdict
from agent_team.schemas.registry import ModelCost

WALL = datetime(2026, 10, 24, tzinfo=UTC)


def feed(parser: StreamParser, *events: dict, start: float = 0.0) -> StreamParser:
    for i, event in enumerate(events):
        parser.feed(json.dumps(event), start + i, WALL)
    return parser


def completed(item: dict) -> dict:
    return {"type": "item.completed", "item": item}


# --- probes ------------------------------------------------------------------------


def test_a_codex_that_cannot_run_is_unavailable(tmp_path: Path) -> None:
    missing = str(tmp_path / "no-codex")
    with pytest.raises(CodexUnavailableError, match="could not run .* exec --help"):
        check_flags(missing, {"PATH": "/usr/bin"})
    assert has_auth(missing, {"PATH": "/usr/bin"}, tmp_path) is False


def test_a_help_that_fails_is_unavailable_even_with_every_flag(tmp_path: Path) -> None:
    fake = tmp_path / "codex"
    flags = " ".join(REQUIRED_FLAGS)
    fake.write_text(f"#!{sys.executable}\nimport sys\nprint({flags!r})\nsys.exit(1)\n", encoding="utf-8")
    fake.chmod(0o755)
    with pytest.raises(CodexUnavailableError, match="does not support its --help; update Codex"):
        check_flags(str(fake), {})


def test_the_flag_probe_gets_only_the_allowlisted_environment(tmp_path: Path) -> None:
    fake = tmp_path / "codex"
    seen = tmp_path / "seen.json"
    flags = " ".join(REQUIRED_FLAGS)
    fake.write_text(f"#!{sys.executable}\nimport json, os\njson.dump(dict(os.environ), open({str(seen)!r}, 'w'))\n"
                    f"print({flags!r})\n", encoding="utf-8")  # fmt: skip
    fake.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "CODEX_HOME": str(tmp_path / ".codex"), "GH_TOKEN": "ghp_PROBECANARY000000000000",
           "OPENAI_API_KEY": "sk-proj-PROBECANARY0000000000", "CODEX_API_KEY": "sk-proj-CODEXPROBECANARY00000"}  # fmt: skip
    check_flags(str(fake), env)
    got = json.loads(seen.read_text())
    assert not {"GH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_HOME"} & set(got), "the probe needs no credential"


def test_auth_is_the_api_key_or_a_chatgpt_login(tmp_path: Path) -> None:
    codex = str(tmp_path / "missing")
    assert has_auth(codex, {"CODEX_API_KEY": "sk-proj-KEY"}, tmp_path) is True
    assert has_auth(codex, {"CODEX_API_KEY": "  "}, tmp_path) is False, "a blank key is not an api key"
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text('{"access_token": "test"}')
    assert has_auth(codex, {"CODEX_HOME": str(tmp_path / ".codex")}, tmp_path) is False, "not a ChatGPT login"


# --- parsing ------------------------------------------------------------------------


def test_every_tool_item_type_has_a_name_and_args() -> None:
    parser = feed(
        StreamParser(),
        completed({"id": "a", "type": "file_change", "changes": [{"path": "x.py", "kind": "update"}], "status": "completed"}),
        completed({"id": "b", "type": "mcp_tool_call", "server": "gh", "tool": "search", "arguments": {"q": "x"}, "status": "completed"}),
        completed({"id": "c", "type": "mcp_tool_call", "arguments": None, "status": "failed", "error": {"message": "no"}}),
        completed({"id": "d", "type": "web_search", "query": "codex flags"}),
        completed({"id": "e", "type": "collab_tool_call", "tool": "spawn", "prompt": "help", "status": "completed"}),
        completed({"id": "f", "type": "command_execution", "command": "false", "exit_code": 1, "status": "completed"}),
        completed({"id": "g", "type": "something_new"}),
    )
    calls = {c.item_id: c for c in parser.calls.values()}
    assert set(calls) == {"a", "b", "c", "d", "e", "f"}, "an unknown item type is not a step"
    assert [calls[k].name for k in "abcdef"] == ["file_change", "mcp:gh/search", "mcp:?/?", "web_search", "collab:spawn",
                                                 "command_execution"]  # fmt: skip
    assert calls["a"].args == {"changes": [{"path": "x.py", "kind": "update"}]}
    assert calls["b"].args == {"q": "x"} and calls["d"].args == {"query": "codex flags"} and calls["e"].args == {"prompt": "help"}
    assert [calls[k].failed for k in "abcdef"] == [False, False, True, False, False, True]


def test_item_updates_replace_the_item_and_only_completion_ends_it() -> None:
    started = {"id": "x", "type": "command_execution", "command": "sleep 1", "status": "in_progress", "exit_code": None}
    parser = feed(StreamParser(), {"type": "item.started", "item": started},
                  {"type": "item.updated", "item": {**started, "aggregated_output": "partial"}})  # fmt: skip
    call = parser.calls["x"]
    assert call.ended is None and call.item["aggregated_output"] == "partial"
    feed(parser, completed({**started, "status": "completed", "exit_code": 0}), start=5.0)
    assert call.ended == 5.0 and call.started == 0.0 and not call.failed


def test_odd_events_are_counted_not_fatal() -> None:
    parser = feed(
        StreamParser(),
        {"type": "thread.started", "thread_id": 7},
        {"type": "item.completed", "item": {"type": "command_execution"}},  # no id
        {"type": "turn.failed", "error": "plain string"},
        {"type": "error"},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": "many"}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
    )
    assert parser.thread_id is None and parser.unparsed == 1
    assert parser.errors == ["turn failed", "error"]
    assert parser.turns == 3 and parser.terminal == "turn.completed"
    assert parser.usage == {"input_tokens": 15, "output_tokens": 2}, "usage adds up across turns; non-integers are skipped"


def test_a_succeeded_run_carries_no_reason_or_stderr_and_the_last_message_wins() -> None:
    parser = feed(
        StreamParser(),
        {"type": "error", "message": "transient; retried"},
        completed({"id": "m1", "type": "agent_message", "text": "draft"}),
        {"type": "item.started", "item": {"id": "m2", "type": "agent_message", "text": "ignored until completed"}},
        completed({"id": "m3", "type": "agent_message", "text": json.dumps({"verdict": "approve", "findings": []})}),
        {"type": "turn.completed", "usage": {}},
    )
    result = normalize(parser, exit_code=0, timed_out=False, stderr="noise on stderr", output_model=ReviewVerdict)
    assert result.status == "succeeded" and result.reason is None and result.stderr_tail is None
    assert result.structured_output == {"verdict": "approve", "findings": []}
    assert result.usage is None and result.num_turns == 1
    plain = normalize(parser, exit_code=0, timed_out=False, stderr="", output_model=None)
    assert plain.result == json.dumps({"verdict": "approve", "findings": []}) and plain.structured_output is None


def test_a_timeout_wins_over_a_completed_turn() -> None:
    parser = feed(StreamParser(), {"type": "turn.completed", "usage": {}})
    result = normalize(parser, exit_code=-9, timed_out=True, stderr="killed ghp_TIMEOUTCANARY00000000000", output_model=None)
    assert result.status == "timeout" and "TIMEOUTCANARY" not in (result.stderr_tail or "")


def test_cost_with_a_price_and_no_budget() -> None:
    parser = feed(StreamParser(), {"type": "turn.completed", "usage": {"input_tokens": 1_000_000, "cached_input_tokens": 500_000,
                                                                       "output_tokens": 100_000}})  # fmt: skip
    result = normalize(parser, exit_code=0, timed_out=False, stderr="", output_model=None,
                       price=ModelCost(input_per_mtok_usd=2.0, output_per_mtok_usd=8.0))  # fmt: skip
    assert result.status == "succeeded" and result.cost_usd == pytest.approx(2.8), "cached input is charged in full"


# --- recording -------------------------------------------------------------------------


def test_record_writes_each_call_with_its_status_and_the_cost(repo: Path) -> None:
    parser = feed(
        StreamParser(),
        completed({"id": "ok", "type": "command_execution", "command": "git diff", "exit_code": 0, "status": "completed"}),
        completed({"id": "no", "type": "command_execution", "command": "rm -rf /", "status": "declined"}),
        completed({"id": "bad", "type": "command_execution", "command": "false", "exit_code": 1, "status": "completed"}),
        {"type": "item.started", "item": {"id": "open", "type": "command_execution", "command": "sleep 99", "status": "in_progress"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1_000_000, "output_tokens": 0}},
    )
    price = ModelCost(input_per_mtok_usd=3.0, output_per_mtok_usd=15.0)
    timed_out = normalize(parser, exit_code=-9, timed_out=True, stderr="", output_model=None, price=price)
    finished = normalize(parser, exit_code=0, timed_out=False, stderr="", output_model=None, price=price)
    with RunStore.open(repo) as store:
        first = store.start_run("agent:independent-reviewer").run_id
        record(store, first, parser, timed_out)
        second = store.start_run("agent:independent-reviewer").run_id
        record(store, second, parser, finished)
        statuses = [(json.loads(c.args_summary)["command"], c.result_status) for c in store.tool_calls(first)]
        assert statuses == [("git diff", "ok"), ("rm -rf /", "denied"), ("false", "error"), ("sleep 99", "timeout")]
        assert [c.result_status for c in store.tool_calls(second)][-1] == "error", "an item never completed is an error"
        assert store.get_run(first).cost_usd == pytest.approx(3.0)


# --- execute ---------------------------------------------------------------------------


def test_a_worker_that_exits_before_reading_its_prompt(tmp_path: Path) -> None:
    inv = Invocation(argv=["sh", "-c", "echo gone >&2; exit 3"], env={"PATH": "/usr/bin:/bin"}, mode="login")
    result, parser = execute(inv, prompt="x" * 2_000_000, cwd=tmp_path, stderr_path=tmp_path / "err", timeout_s=30)
    assert result.status == "error" and result.exit_code == 3 and result.stderr_tail == "gone"
    assert parser.turns == 0


def test_toml_values() -> None:
    assert toml_value(True) == "true" and toml_value(0.5) == "0.5" and toml_value(3) == "3"
    assert toml_value(["a", 1, False]) == '["a", 1, false]' and toml_value(("q\"uote",)) == '["q\\"uote"]'
    with pytest.raises(TypeError, match="no TOML form for dict"):
        toml_value({"a": 1})
