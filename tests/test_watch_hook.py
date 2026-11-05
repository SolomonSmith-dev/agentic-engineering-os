"""Task 14 watch hook acceptance tests: worker halting mid-call, per-event usage, stream parsing."""

import importlib
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_team.budget import Usage, message_usage, turn_usage


@pytest.mark.parametrize(
    ("runtime", "event", "usage_fn"),
    [
        (
            "claude",
            {"type": "assistant", "message": {"id": "m1", "model": "x", "usage": {"input_tokens": 1, "output_tokens": 2}, "content": []}},
            message_usage,
        ),
        (
            "codex",
            {"type": "turn.completed", "usage": {"input_tokens": 5, "cached_input_tokens": 2, "output_tokens": 3}},
            turn_usage,
        ),
    ],
)
def test_watch_hook_halts_worker_mid_call(tmp_path: Path, runtime: str, event: dict[str, Any], usage_fn: Any) -> None:
    invoke = importlib.import_module(f"agent_team.adapters.{runtime}.invoke")
    pidfile = tmp_path / "child.pid"
    event_json = json.dumps(event)
    script_file = tmp_path / f"worker_{runtime}.py"
    script_file.write_text(
        f"import subprocess, sys, time\n"
        f"child = subprocess.Popen(['sleep', '60'])\n"
        f"with open({str(pidfile)!r}, 'w') as f:\n"
        f"    f.write(str(child.pid))\n"
        f"sys.stdout.write({event_json!r} + '\\n')\n"
        f"sys.stdout.flush()\n"
        f"time.sleep(60)\n"
    )

    mode = "login" if runtime == "claude" else "api_key"
    inv = invoke.Invocation(argv=[sys.executable, str(script_file)], env=dict(os.environ), mode=mode)

    start = time.monotonic()
    try:
        result, _ = invoke.execute(
            inv,
            prompt="",
            cwd=tmp_path,
            stderr_path=tmp_path / "err",
            timeout_s=30,
            watch=lambda e: "budget halt: usd 6 > 5" if usage_fn(e) else None,
        )
        duration = time.monotonic() - start
        assert duration < 5
        assert result.status == "budget_exceeded"
        assert result.reason == "budget halt: usd 6 > 5"

        assert pidfile.exists(), "child pidfile was not written"
        pid = int(pidfile.read_text().strip())

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"child process {pid} was not killed within 5 s")
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


@pytest.mark.parametrize(
    ("runtime", "events"),
    [
        (
            "claude",
            [
                {"type": "assistant", "message": {"id": "m1", "model": "x", "usage": {"input_tokens": 1, "output_tokens": 2}, "content": []}},
                {"type": "result", "subtype": "success"},
            ],
        ),
        (
            "codex",
            [
                {"type": "turn.completed", "usage": {"input_tokens": 5, "cached_input_tokens": 2, "output_tokens": 3}},
            ],
        ),
    ],
)
def test_watch_returning_none_lets_worker_finish_normally(
    tmp_path: Path, runtime: str, events: list[dict[str, Any]]
) -> None:
    invoke = importlib.import_module(f"agent_team.adapters.{runtime}.invoke")
    script_file = tmp_path / f"worker_normal_{runtime}.py"

    lines = "\n".join(
        f"sys.stdout.write({json.dumps(ev)!r} + '\\n')\nsys.stdout.flush()"
        for ev in events
    )
    script_file.write_text(f"import sys\n{lines}\n")

    mode = "login" if runtime == "claude" else "api_key"
    inv = invoke.Invocation(argv=[sys.executable, str(script_file)], env=dict(os.environ), mode=mode)

    seen: list[dict[str, Any]] = []

    def watch(e: dict[str, Any]) -> None:
        seen.append(e)
        return None

    result, _ = invoke.execute(
        inv,
        prompt="",
        cwd=tmp_path,
        stderr_path=tmp_path / "err",
        timeout_s=30,
        watch=watch,
    )

    assert len(seen) == len(events)
    assert result.status != "budget_exceeded"
    assert result.status == "succeeded"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_stream_parser_feed(runtime: str) -> None:
    invoke = importlib.import_module(f"agent_team.adapters.{runtime}.invoke")
    parser = invoke.StreamParser()
    now = time.monotonic()
    wall = datetime.now(UTC)

    # Valid JSON object line returns parsed dict
    parsed = parser.feed('{"type": "turn.completed", "key": "val"}', now, wall)
    assert parsed == {"type": "turn.completed", "key": "val"}

    # None for empty line
    assert parser.feed("", now, wall) is None

    # None for "not json"
    assert parser.feed("not json", now, wall) is None

    # None for "[1]"
    assert parser.feed("[1]", now, wall) is None


def test_message_usage() -> None:
    event = {
        "type": "assistant",
        "message": {
            "id": "m1",
            "model": "model-test",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 40,
            },
        },
    }
    got = message_usage(event)
    assert got is not None
    mid, model, usage = got
    assert mid == "m1"
    assert model == "model-test"
    assert usage == Usage(
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=30,
        cache_write_tokens=40,
    )
    assert usage.input_tokens == 10
    assert usage.output_tokens == 20
    assert usage.cache_read_tokens == 30
    assert usage.cache_write_tokens == 40

    assert message_usage({"type": "user"}) is None
    assert message_usage({"type": "assistant", "message": {"id": "m1"}}) is None


def test_turn_usage() -> None:
    event = {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 50,
            "cached_input_tokens": 20,
            "output_tokens": 15,
        },
    }
    usage = turn_usage(event)
    assert usage is not None
    assert usage == Usage(
        input_tokens=30,
        output_tokens=15,
        cache_read_tokens=20,
        cache_write_tokens=0,
    )
    assert usage.input_tokens == 50 - 20
    assert usage.cache_read_tokens == 20
    assert usage.output_tokens == 15

    assert turn_usage({"type": "turn.started"}) is None
    assert turn_usage({"type": "turn.completed"}) is None
