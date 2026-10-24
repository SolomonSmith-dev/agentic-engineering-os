"""Invoke a compiled agent with `claude -p` and normalize what comes back (build-order step 6).

Two auth modes, chosen from the environment:

- login (default): your Claude subscription login. `--bare` would ignore it, so
  the session runs with `--setting-sources ""` instead, which keeps the target
  repository's `.claude/settings.json` hooks and the user's settings out while
  our `--settings` still loads.
- bare: `ANTHROPIC_API_KEY` is set (CI). `--bare` skips every auto-discovered
  hook, skill, plugin, MCP server and CLAUDE.md.

The worker gets a scrubbed environment: a short allowlist plus the sandbox's
own variables, never the caller's tokens or Claude Code session variables.
Output is read as stream-json so every tool call reaches the run record.
"""

import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from agent_team.adapters.claude.compile import PERMISSION_MODE, STRUCTURED_OUTPUT_TOOL, CompiledAgent
from agent_team.environments.sandbox import Sandbox
from agent_team.runs.redact import redact_text
from agent_team.runs.store import RunStore
from agent_team.schemas.adapter import AdapterResult, AdapterStatus

RUNTIME = "claude"

# --agents <file> needs 2.1.281; Manual mode as the -p default needs 2.1.302.
MIN_VERSION = (2, 1, 302)

ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL")

STDERR_TAIL_CHARS = 2000

AuthMode = Literal["login", "bare"]


class ClaudeVersionError(RuntimeError):
    """The installed claude is missing or older than the flags the adapter uses."""


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(g) for g in match.groups()) if match else None


def check_version(claude: str) -> tuple[int, ...]:
    out = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=30, check=False)
    version = parse_version(out.stdout)
    if version is None:
        raise ClaudeVersionError(f"could not read the version of {claude}")
    if version < MIN_VERSION:
        wanted = ".".join(map(str, MIN_VERSION))
        raise ClaudeVersionError(f"claude {'.'.join(map(str, version))} is older than {wanted}; run 'claude update'")
    return version


def auth_mode(env: dict[str, str]) -> AuthMode:
    return "bare" if env.get("ANTHROPIC_API_KEY") else "login"


@dataclass(frozen=True)
class Invocation:
    argv: list[str]
    env: dict[str, str]
    mode: AuthMode


def build_invocation(
    compiled: CompiledAgent,
    *,
    claude: str,
    sandbox: Sandbox,
    base_env: dict[str, str],
    max_budget_usd: float,
    max_turns: int | None = None,
) -> Invocation:
    mode = auth_mode(base_env)
    argv = [claude, "-p"]
    argv += ["--bare"] if mode == "bare" else ["--setting-sources", ""]
    argv += [
        "--settings", str(compiled.settings_json),
        "--strict-mcp-config",
        "--agents", str(compiled.agents_json),
        "--agent", compiled.name,
        "--model", compiled.model,
        "--permission-mode", PERMISSION_MODE,
        "--permission-prompts", "none",
        "--output-format", "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--max-budget-usd", f"{max_budget_usd:.2f}",
    ]  # fmt: skip
    if max_turns is not None:
        argv += ["--max-turns", str(max_turns)]
    if compiled.schema_json is not None:
        argv += ["--json-schema", compiled.schema_json.read_text(encoding="utf-8")]
    env = {k: base_env[k] for k in ENV_ALLOWLIST if k in base_env}
    # Claude Code keeps its own Bash working files under $CLAUDE_CODE_TMPDIR/claude-<uid>/,
    # /tmp by default on macOS, which srt does not let the worker write. Use the run's temp dir.
    env["CLAUDE_CODE_TMPDIR"] = str(sandbox.tmp_dir)
    if mode == "bare":
        env["ANTHROPIC_API_KEY"] = base_env["ANTHROPIC_API_KEY"]
    return Invocation(argv=sandbox.wrap(argv), env=sandbox.env(env), mode=mode)


@dataclass
class ToolCall:
    tool_use_id: str
    name: str
    input: Any
    started: float
    started_at: datetime
    ended: float | None = None
    is_error: bool = False


@dataclass
class StreamParser:
    """Reads stream-json lines: tool_use / tool_result pairs and the final result event."""

    calls: dict[str, ToolCall] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    unparsed: int = 0

    def feed(self, line: str, now: float, wall: datetime) -> None:
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            self.unparsed += 1
            return
        # The stream carries event shapes this parser does not know (system notices whose
        # `message` is a string, for one); anything that is not the shape below is skipped.
        if not isinstance(event, dict):
            self.unparsed += 1
            return
        kind = event.get("type")
        if kind == "result":
            self.result = event
            return
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            # StructuredOutput is how the answer comes back, not a step the agent chose: not recorded.
            if block.get("name") == STRUCTURED_OUTPUT_TOOL:
                continue
            if kind == "assistant" and block.get("type") == "tool_use" and block.get("id"):
                self.calls[block["id"]] = ToolCall(
                    block["id"], str(block.get("name", "?")), block.get("input"), now, wall
                )
            elif kind == "user" and block.get("type") == "tool_result":
                call = self.calls.get(str(block.get("tool_use_id")))
                if call is not None:
                    call.ended = now
                    call.is_error = bool(block.get("is_error"))


def _status(result: dict[str, Any] | None, exit_code: int, timed_out: bool) -> AdapterStatus:
    if timed_out:
        return "timeout"
    if result is None:
        return "error"
    subtype = str(result.get("subtype", ""))
    if subtype == "success" and not result.get("is_error"):
        return "succeeded" if exit_code == 0 else "failed"
    if "max_turns" in subtype:
        return "max_turns"
    if "budget" in subtype:
        return "budget_exceeded"
    if "structured_output" in subtype:
        return "failed"
    return "failed" if subtype == "success" else "error"


def normalize(
    parser: StreamParser,
    *,
    exit_code: int,
    timed_out: bool,
    stderr: str,
    output_model: type[BaseModel] | None,
    redaction: tuple[str, ...] = (),
) -> AdapterResult:
    result = parser.result or {}
    status = _status(parser.result, exit_code, timed_out)
    denials = [str(d.get("tool_name", "?")) for d in result.get("permission_denials") or [] if isinstance(d, dict)]

    structured = result.get("structured_output")
    structured_error = None
    if output_model is not None and status == "succeeded":
        if structured is None:
            structured_error = "no structured_output in the result"
        else:
            try:
                structured = output_model.model_validate(structured).model_dump(mode="json")
            except ValidationError as exc:
                structured_error = f"structured_output failed validation: {exc.error_count()} error(s)"
        if structured_error is not None:
            status = "failed"
    tail = redact_text(stderr[-STDERR_TAIL_CHARS:], redaction).strip() if status != "succeeded" and stderr else None
    return AdapterResult(
        status=status,
        runtime=RUNTIME,
        subtype=result.get("subtype"),
        result=result.get("result") if isinstance(result.get("result"), str) else None,
        structured_output=structured if isinstance(structured, dict) else None,
        structured_output_error=structured_error,
        cost_usd=float(result.get("total_cost_usd") or 0.0),
        num_turns=result.get("num_turns"),
        duration_ms=result.get("duration_ms"),
        session_id=result.get("session_id"),
        tool_calls=len(parser.calls),
        permission_denials=denials,
        exit_code=exit_code,
        stderr_tail=tail or None,
    )


def record(store: RunStore, run_id: str, parser: StreamParser, result: AdapterResult) -> None:
    """Write every tool call and the run's model cost to the run record."""
    denied_ids = {
        str(d.get("tool_use_id"))
        for d in (parser.result or {}).get("permission_denials") or []
        if isinstance(d, dict)
    }
    for call in sorted(parser.calls.values(), key=lambda c: c.started):
        if call.tool_use_id in denied_ids:
            status = "denied"
        elif call.ended is None:
            status = "timeout" if result.status == "timeout" else "error"
        else:
            status = "error" if call.is_error else "ok"
        duration = int(((call.ended or call.started) - call.started) * 1000)
        store.record_tool_call(
            run_id, call.name, call.input, status, duration_ms=max(duration, 0), started_at=call.started_at
        )
    if result.cost_usd > 0:
        store.add_cost(run_id, result.cost_usd)


def execute(
    invocation: Invocation,
    *,
    prompt: str,
    cwd: Path,
    stderr_path: Path,
    timeout_s: float,
    output_model: type[BaseModel] | None = None,
    redaction: tuple[str, ...] = (),
) -> tuple[AdapterResult, StreamParser]:
    """Run the worker to completion, prompt on stdin, and parse its stream."""
    parser = StreamParser()
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    with stderr_path.open("w", encoding="utf-8") as err:
        proc = subprocess.Popen(
            invocation.argv,
            cwd=cwd,
            env=invocation.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err,
            text=True,
            start_new_session=True,  # its own process group, so a timeout reaches every child
        )
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        timer = threading.Timer(timeout_s, _kill)
        timer.start()
        try:
            assert proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except BrokenPipeError:
                pass  # the worker exited before reading; its stderr says why
            for line in proc.stdout:
                parser.feed(line, time.monotonic(), datetime.now(UTC))
            exit_code = proc.wait()
        finally:
            timer.cancel()
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    result = normalize(
        parser,
        exit_code=exit_code,
        timed_out=timed_out.is_set(),
        stderr=stderr,
        output_model=output_model,
        redaction=redaction,
    )
    return result, parser
