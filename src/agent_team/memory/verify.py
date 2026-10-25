"""Verify detected commands by running them inside the run's srt sandbox (build-order step 9).

`init` checks commands without running anything (step 8). `inspect` goes one
step further: each command `init` detected runs in the run's worktree, through
the run's srt settings, so it gets no network beyond the environment's
allowlist and writes only the worktree and the run's temp directory. Before
the first command the settings gain read denies for the policy's protected
paths (as the Codex adapter compiles them), because no hook stands between a
repository command and the files it opens.

Without srt nothing is run: every command is marked `unverified` with the
reason. Repository code never runs outside the sandbox.

Each command gets a timeout (its whole process group is killed), a scrubbed
environment with `CI=1` and no stdin, and its output is cut to a redacted
tail. Every command, its exit code and its duration go to the run record as an
`inspect.verify` tool call.
"""

import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from agent_team.adapters.claude.compile import DEFAULT_PROTECTED, PROTECTED_FILES
from agent_team.adapters.claude.invoke import ENV_ALLOWLIST
from agent_team.adapters.codex.compile import srt_deny_read
from agent_team.environments.sandbox import Sandbox, extend_settings, srt_path
from agent_team.runs.redact import redact_text
from agent_team.runs.store import RunStore
from agent_team.schemas.memory import CommandVerification, VerificationStatus
from agent_team.schemas.policy import PolicySpec
from agent_team.schemas.runs import ToolResultStatus

VERIFY_TOOL = "inspect.verify"
DEFAULT_COMMAND_TIMEOUT_S = 600.0

# Output kept per command. A wider window is redacted first, so a token cut by the tail is never kept in part.
OUTPUT_TAIL_CHARS = 1000
REDACT_WINDOW_CHARS = 8000

STATUS_TO_RESULT: dict[VerificationStatus, ToolResultStatus] = {
    "passed": "ok",
    "failed": "error",
    "timeout": "timeout",
    "unverified": "denied",
}


@dataclass(frozen=True)
class Candidate:
    """A command `init` detected: what to run, where it came from, and init's own non-executing check."""

    kind: str
    command: str
    source: str
    dry_run: bool


def sandbox_refusal(sandbox: Sandbox) -> str | None:
    """Why commands cannot run in this run's environment, or None when it is wrapped in srt."""
    if sandbox.sandboxed:
        return None
    if srt_path() is None:
        return "srt is not on PATH; agent-team runs repository commands only inside srt"
    return "the run's environment is not wrapped in srt; agent-team runs repository commands only inside srt"


# Inspect never runs a Codex worker, so the Codex login is denied here too.
INSPECT_PROTECTED: tuple[str, ...] = ("~/.codex/**",)


def protect(sandbox: Sandbox, policy: PolicySpec, *, root: Path, worktree: Path, home: Path) -> None:
    """Add srt read denies for the policy's protected paths, keeping the run's own checkout and temp dir."""
    protected = list(dict.fromkeys([*policy.protected_paths, *DEFAULT_PROTECTED, *PROTECTED_FILES, *INSPECT_PROTECTED]))
    keep = [worktree, sandbox.tmp_dir]
    extend_settings(sandbox, deny_read=srt_deny_read(protected, [worktree, root], home, keep))


def _tail(handle: BinaryIO, redaction: Sequence[str]) -> str | None:
    """The redacted end of a command's output, read back from the file it wrote; None when it printed nothing."""
    size = handle.seek(0, os.SEEK_END)
    if size == 0:
        return None
    handle.seek(max(0, size - REDACT_WINDOW_CHARS * 4))  # up to 4 bytes per character
    text = handle.read().decode("utf-8", errors="replace")[-REDACT_WINDOW_CHARS:]
    return redact_text(text, redaction)[-OUTPUT_TAIL_CHARS:].strip() or None


def run_command(
    candidate: Candidate,
    *,
    sandbox: Sandbox,
    cwd: Path,
    timeout_s: float,
    base_env: dict[str, str],
    redaction: Sequence[str],
) -> CommandVerification:
    """Run one command through srt in `cwd`. Callers check `sandbox_refusal` first.

    Output goes to an unnamed temp file outside the sandbox and is read back through the
    same descriptor, so the command cannot point agent-team at another file to read.
    """
    if not sandbox.sandboxed:
        raise ValueError("refusing to run a repository command outside srt")
    env = {k: base_env[k] for k in ENV_ALLOWLIST if k in base_env}
    env["CI"] = "1"  # no watch modes, prompts or interactive reporters
    env["XDG_CACHE_HOME"] = str(sandbox.tmp_dir / "cache")  # tool caches land where srt allows writes
    argv = sandbox.wrap(["/bin/sh", "-c", candidate.command])
    base = {"kind": candidate.kind, "command": candidate.command, "source": candidate.source,
            "detected_by": "init", "dry_run": candidate.dry_run}  # fmt: skip
    start = time.monotonic()
    timed_out = False
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=sandbox.env(env),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # its own process group, so a timeout reaches every child
            )
        except OSError as exc:
            return CommandVerification(**base, status="failed", reason=f"could not start: {exc.strerror or exc}")
        try:
            exit_code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            exit_code = proc.wait()
        duration_ms = int((time.monotonic() - start) * 1000)
        tail = _tail(out, redaction)  # only the redacted tail is kept
    if timed_out:
        return CommandVerification(
            **base, status="timeout", duration_ms=duration_ms, reason=f"killed after {timeout_s:g}s", output_tail=tail
        )
    return CommandVerification(
        **base,
        status="passed" if exit_code == 0 else "failed",
        exit_code=exit_code,
        duration_ms=duration_ms,
        reason=None if exit_code == 0 else f"exit {exit_code}",
        output_tail=tail,
    )


def verify_commands(
    candidates: Sequence[Candidate],
    *,
    sandbox: Sandbox,
    cwd: Path,
    timeout_s: float,
    redaction: Sequence[str],
    base_env: dict[str, str],
    echo: Callable[[str], None] = print,
) -> list[CommandVerification]:
    """Run every candidate in order inside srt, or mark each unverified when srt is not there."""
    refusal = sandbox_refusal(sandbox)
    results: list[CommandVerification] = []
    for candidate in candidates:
        if refusal is not None:
            results.append(
                CommandVerification(
                    kind=candidate.kind, command=candidate.command, source=candidate.source,
                    detected_by="init", dry_run=candidate.dry_run, status="unverified", reason=refusal,
                )  # fmt: skip
            )
        else:
            echo(f"verify {candidate.kind}: {candidate.command}")
            results.append(
                run_command(
                    candidate,
                    sandbox=sandbox,
                    cwd=cwd,
                    timeout_s=timeout_s,
                    base_env=base_env,
                    redaction=redaction,
                )
            )
    return results


def record_verifications(store: RunStore, run_id: str, results: Sequence[CommandVerification]) -> None:
    """One `inspect.verify` tool call per command: what ran, how it ended, and how long it took."""
    for result in results:
        args = {"kind": result.kind, "command": result.command, "status": result.status, "exit_code": result.exit_code}
        if result.status == "unverified":
            args["reason"] = result.reason
        store.record_tool_call(
            run_id,
            VERIFY_TOOL,
            args,
            STATUS_TO_RESULT[result.status],
            duration_ms=result.duration_ms or 0,
        )
