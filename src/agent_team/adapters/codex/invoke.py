"""Invoke a compiled agent with `codex exec --json` and normalize what comes back (build-order step 12).

Auth mode is API key only, read from `CODEX_API_KEY`:

- the key is passed to the `codex` process only, never persisted by value.
- shell commands run with Codex's shell-environment excludes enabled.
- `~/.codex/**` can therefore stay denied by srt for every command in the run.

The session never loads your `config.toml` (`--ignore-user-config`), your or
the repository's execpolicy rules (`--ignore-rules`), or writes a session file
(`--ephemeral`). A target repository's `.codex/config.toml` and hooks load only
for projects trusted in the user config, so with the user config ignored none
are trusted. Everything else comes in as `-c key=value` from the compiled config.

The worker gets a scrubbed environment, the prompt on stdin, and runs inside
srt with the compiled denyRead rules. Output is the JSONL event stream, so every
command and file change reaches the run record.

Flags, config keys, auth sources and the event shapes were read from the
openai/codex source (commit 970111f, 2026-10-23: `codex-rs/exec/src/cli.rs`,
`exec_events.rs`, `utils/cli/src/shared_options.rs`, `config/src/config_toml.rs`,
`login/src/auth/manager.rs`, `cli/src/login.rs`). No real Codex has run this code.
"""

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from agent_team.adapters.claude.invoke import ENV_ALLOWLIST as CLAUDE_ENV_ALLOWLIST
from agent_team.adapters.claude.invoke import STDERR_TAIL_CHARS
from agent_team.adapters.codex.compile import CompiledCodexAgent, toml_value
from agent_team.environments.sandbox import Sandbox
from agent_team.runs.redact import redact_text
from agent_team.runs.store import RunStore
from agent_team.schemas.adapter import AdapterResult, AdapterStatus
from agent_team.schemas.registry import ModelCost

RUNTIME = "codex"

API_KEY_ENV = "CODEX_API_KEY"

ENV_ALLOWLIST: tuple[str, ...] = CLAUDE_ENV_ALLOWLIST

# Hosts the srt allowlist needs for Codex API-key auth.
# unverified: that this is the full host list a run reaches.
API_DOMAINS: dict[str, tuple[str, ...]] = {
    "api_key": ("api.openai.com",),
}

# Flags this adapter passes; `codex exec --help` must list every one, or the run is refused.
REQUIRED_FLAGS: tuple[str, ...] = (
    "--json", "--output-schema", "--ignore-user-config", "--ignore-rules", "--ephemeral",
    "--sandbox", "--model", "--config", "--cd",
)  # fmt: skip

# Overrides that go in as flags rather than `-c`.
FLAG_KEYS: dict[str, str] = {"model": "--model", "sandbox_mode": "--sandbox"}

PROBE_TIMEOUT_S = 30

AuthMode = Literal["api_key"]


class CodexUnavailableError(RuntimeError):
    """The installed codex lacks a flag the adapter uses, or has no auth. The message says what to do."""


def _probe_env(base_env: dict[str, str]) -> dict[str, str]:
    return {k: base_env[k] for k in ENV_ALLOWLIST if k in base_env}


def check_flags(codex: str, base_env: dict[str, str]) -> None:
    try:
        out = subprocess.run(
            [codex, "exec", "--help"], capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
            check=False, stdin=subprocess.DEVNULL, env=_probe_env(base_env),
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodexUnavailableError(f"could not run {codex} exec --help: {exc}") from exc
    missing = [flag for flag in REQUIRED_FLAGS if flag not in out.stdout]
    if out.returncode != 0 or missing:
        raise CodexUnavailableError(
            f"{codex} exec does not support {', '.join(missing) or 'its --help'}; update Codex"
        )


def auth_mode(env: dict[str, str]) -> AuthMode:
    if env.get(API_KEY_ENV, "").strip():
        return "api_key"
    raise CodexUnavailableError(f"set {API_KEY_ENV} for this shell")


def has_auth(_codex: str, base_env: dict[str, str]) -> bool:
    """True when `CODEX_API_KEY` is set (the runtime does not allow login-file auth)."""
    return bool(base_env.get(API_KEY_ENV, "").strip())


@dataclass(frozen=True)
class Invocation:
    argv: list[str]
    env: dict[str, str]
    mode: AuthMode


def build_invocation(
    compiled: CompiledCodexAgent,
    *,
    codex: str,
    sandbox: Sandbox,
    base_env: dict[str, str],
    cwd: Path,
) -> Invocation:
    mode = auth_mode(base_env)
    argv = [codex, "exec", "--json", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--cd", str(cwd)]
    for key, value in compiled.overrides.items():
        if key in FLAG_KEYS:
            argv += [FLAG_KEYS[key], str(value)]
        else:
            argv += ["--config", f"{key}={toml_value(value)}"]
    if compiled.schema_json is not None:
        argv += ["--output-schema", str(compiled.schema_json)]
    argv.append("-")  # the prompt comes in on stdin
    # unverified: that Codex's shell_environment_policy default excludes still strip CODEX_API_KEY from
    # command_execution shells while keeping it available to codex exec itself.
    env = _probe_env(base_env)
    if mode == "api_key":
        env[API_KEY_ENV] = base_env[API_KEY_ENV]
    return Invocation(argv=sandbox.wrap(argv), env=sandbox.env(env), mode=mode)


# Thread item types that are steps the agent took; agent_message, reasoning, todo_list and error are not.
TOOL_ITEMS: tuple[str, ...] = ("command_execution", "file_change", "mcp_tool_call", "web_search", "collab_tool_call")


@dataclass
class ToolCall:
    item_id: str
    item: dict[str, Any]
    started: float
    started_at: datetime
    ended: float | None = None

    @property
    def kind(self) -> str:
        return str(self.item.get("type"))

    @property
    def name(self) -> str:
        if self.kind == "mcp_tool_call":
            return f"mcp:{self.item.get('server', '?')}/{self.item.get('tool', '?')}"
        if self.kind == "collab_tool_call":
            return f"collab:{self.item.get('tool', '?')}"
        return self.kind

    @property
    def args(self) -> Any:
        if self.kind == "command_execution":
            return {"command": self.item.get("command")}
        if self.kind == "file_change":
            return {"changes": self.item.get("changes")}
        if self.kind == "mcp_tool_call":
            return self.item.get("arguments")
        if self.kind == "web_search":
            return {"query": self.item.get("query")}
        return {"prompt": self.item.get("prompt")}

    @property
    def denied(self) -> bool:
        return self.item.get("status") == "declined"

    @property
    def failed(self) -> bool:
        if self.item.get("status") == "failed" or self.item.get("error"):
            return True
        code = self.item.get("exit_code")
        return self.kind == "command_execution" and code is not None and code != 0


@dataclass
class StreamParser:
    """Reads `codex exec --json` lines: item lifecycles, agent messages, usage and the turn's end."""

    calls: dict[str, ToolCall] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    thread_id: str | None = None
    turns: int = 0
    terminal: str | None = None  # turn.completed or turn.failed
    errors: list[str] = field(default_factory=list)
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
        if not isinstance(event, dict):
            self.unparsed += 1
            return
        kind = event.get("type")
        if kind == "thread.started":
            self.thread_id = event.get("thread_id") if isinstance(event.get("thread_id"), str) else None
        elif kind == "turn.completed":
            self.turns += 1
            self.terminal = kind
            usage = event.get("usage")
            if isinstance(usage, dict):
                for key, value in usage.items():
                    if isinstance(value, int):
                        self.usage[key] = self.usage.get(key, 0) + value
        elif kind == "turn.failed":
            self.turns += 1
            self.terminal = kind
            error = event.get("error")
            self.errors.append(str(error.get("message")) if isinstance(error, dict) else "turn failed")
        elif kind == "error":
            self.errors.append(str(event.get("message", "error")))
        elif kind in ("item.started", "item.updated", "item.completed"):
            self._item(kind, event.get("item"), now, wall)

    def _item(self, kind: str, item: Any, now: float, wall: datetime) -> None:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            self.unparsed += 1
            return
        if item.get("type") == "agent_message" and kind == "item.completed":
            self.messages.append(str(item.get("text", "")))
            return
        if item.get("type") not in TOOL_ITEMS:
            return
        call = self.calls.get(item["id"])
        if call is None:
            call = self.calls[item["id"]] = ToolCall(item["id"], item, now, wall)
        call.item = item
        if kind == "item.completed":
            call.ended = now


def _status(parser: StreamParser, exit_code: int, timed_out: bool) -> AdapterStatus:
    if timed_out:
        return "timeout"
    if parser.terminal == "turn.completed":
        return "succeeded" if exit_code == 0 else "failed"
    return "error"  # turn.failed, or the stream ended without a turn ending


def estimate_cost(usage: dict[str, int], price: ModelCost | None) -> float:
    """List-price estimate from token counts; cached input is charged at the full input price."""
    # unverified: whether output_tokens already counts reasoning_output_tokens (assumed: it does).
    if price is None:
        return 0.0
    return (
        usage.get("input_tokens", 0) * price.input_per_mtok_usd
        + usage.get("output_tokens", 0) * price.output_per_mtok_usd
    ) / 1_000_000


def normalize(
    parser: StreamParser,
    *,
    exit_code: int,
    timed_out: bool,
    stderr: str,
    output_model: type[BaseModel] | None,
    price: ModelCost | None = None,
    max_budget_usd: float | None = None,
    redaction: tuple[str, ...] = (),
) -> AdapterResult:
    status = _status(parser, exit_code, timed_out)
    final = parser.messages[-1] if parser.messages else None
    reason = redact_text(parser.errors[-1], redaction)[:500] if parser.errors and status != "succeeded" else None

    structured: Any = None
    structured_error = None
    if output_model is not None and status == "succeeded":
        if final is None:
            structured_error = "no final agent message to read the structured output from"
        else:
            try:
                structured = output_model.model_validate_json(final).model_dump(mode="json")
            except ValidationError as exc:
                structured_error = f"structured output failed validation: {exc.error_count()} error(s)"
        if structured_error is not None:
            status = "failed"

    cost = estimate_cost(parser.usage, price)
    if max_budget_usd is not None and cost > max_budget_usd:
        # Codex cannot stop a run at a budget, so the cap is checked once the run has ended.
        reason = f"estimated cost ${cost:.4f} is over the ${max_budget_usd:.2f} budget; checked after the run ended"
        status = "budget_exceeded"
    tail = redact_text(stderr[-STDERR_TAIL_CHARS:], redaction).strip() if status != "succeeded" and stderr else None
    return AdapterResult(
        status=status,
        runtime=RUNTIME,
        subtype=parser.terminal,
        result=None if structured is not None else final,
        structured_output=structured if isinstance(structured, dict) else None,
        structured_output_error=structured_error,
        reason=reason,
        cost_usd=cost,
        num_turns=parser.turns or None,
        session_id=parser.thread_id,
        tool_calls=len(parser.calls),
        permission_denials=[c.name for c in parser.calls.values() if c.denied],
        usage=dict(parser.usage) or None,
        exit_code=exit_code,
        stderr_tail=tail or None,
    )


def record(store: RunStore, run_id: str, parser: StreamParser, result: AdapterResult) -> None:
    """Write every command, file change and tool call, and the estimated model cost, to the run record."""
    for call in sorted(parser.calls.values(), key=lambda c: c.started):
        if call.denied:
            status = "denied"
        elif call.ended is None:
            status = "timeout" if result.status == "timeout" else "error"
        else:
            status = "error" if call.failed else "ok"
        duration = int(((call.ended or call.started) - call.started) * 1000)
        store.record_tool_call(
            run_id, call.name, call.args, status, duration_ms=max(duration, 0), started_at=call.started_at
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
    price: ModelCost | None = None,
    max_budget_usd: float | None = None,
    redaction: tuple[str, ...] = (),
) -> tuple[AdapterResult, StreamParser]:
    """Run the worker to completion, prompt on stdin, and parse its event stream."""
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
        price=price,
        max_budget_usd=max_budget_usd,
        redaction=redaction,
    )
    return result, parser
