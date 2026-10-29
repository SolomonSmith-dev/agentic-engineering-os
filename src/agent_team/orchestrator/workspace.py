"""The workflow's worktree, the commands the orchestrator runs in it, and the artifacts it keeps (build-order step 11).

A workflow run owns one worktree, `.agent-team/worktrees/<run-id>/` on branch
`agent-team/<run-id>`, allocated the first time a node whose agent works in a
`worktree` (the implementer) runs. DISCOVERY, IMPLEMENTATION and the retry all
work there, so attempt two builds on attempt one's commits. After each, the
orchestrator settles the worktree to one commit:

    implementer exits -> still on the run branch? base still an ancestor? -> commit what it left uncommitted
                      -> HEAD: the commit the diff, the tests, the reviewer and the PR all use

The reviewer never works here: it gets a fresh worktree of its own, cut at
that commit. Commands the orchestrator runs itself (the reproduction, test,
lint and type-check commands) run in the run's srt sandbox with the worker's
scrubbed environment; without srt nothing runs, because the reproduction
command comes from the agent and every command executes code the agent wrote.

Git commands the orchestrator runs on agent-written trees ignore repository
hooks, fsmonitor and external diff drivers, and in the worktree they run with
GIT_DIR, GIT_COMMON_DIR and GIT_WORK_TREE pinned to paths resolved from the
repository root, never read from files the agent can write (the worktree's
`.git`, its admin dir's `commondir`), so nothing the agent wrote runs outside the
sandbox. Artifacts live in `.agent-team/artifacts/<run-id>/`; evidence holds a
reference (path relative to the repository root, size, sha256), never the
content, because checkpoint redaction truncates strings at 8 KiB.
"""

import hashlib
import os
import signal
import subprocess
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_team.adapters.claude.invoke import ENV_ALLOWLIST
from agent_team.environments import AllocatorError, Environment, attach, create
from agent_team.environments.worktree import BRANCH_PREFIX, check_pointers, read_meta
from agent_team.runs.redact import redact_text
from agent_team.runs.store import STATE_DIR
from agent_team.schemas.common import Autonomy
from agent_team.schemas.environment import EnvironmentSpec

ARTIFACTS_DIR = "artifacts"

# For git run by the orchestrator on a tree the agent wrote: no hooks, no fsmonitor, no signing prompt.
SAFE_GIT: tuple[str, ...] = (
    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false",
)  # fmt: skip

# Inherited git variables that would point git at another repository, index or config.
_GIT_ENV_DROP = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG", "GIT_CONFIG_GLOBAL",
                 "GIT_CONFIG_SYSTEM", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")  # fmt: skip

LEFTOVER_MESSAGE = "agent-team: changes the implementer left uncommitted"

# An artifact keeps at most this much of one command's output: the tail, where failures are.
MAX_OUTPUT_BYTES = 1_000_000


class WorkspaceError(RuntimeError):
    """The worktree is not in a state the orchestrator can vouch for; the message says why."""


def git(
    cwd: Path, *args: str, check: bool = True, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), *SAFE_GIT, *args], capture_output=True, text=True, check=False,
        env=None if env is None else dict(env),
    )  # fmt: skip
    if check and result.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {result.stderr.strip()[-500:]}")
    return result


def pinned_env(env: Environment) -> dict[str, str]:
    """Git's repository paths for the worktree, from the root's common .git; nothing read from the worktree."""
    if env.git_dir is None:
        raise WorkspaceError(f"the repository's git directory for {env.path} is unknown; refusing to run git there")
    try:
        admin = check_pointers(env.git_dir, env.path)
    except AllocatorError as exc:
        raise WorkspaceError(str(exc)) from exc
    pinned = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    pinned.update(GIT_DIR=str(admin), GIT_COMMON_DIR=str(env.git_dir), GIT_WORK_TREE=str(env.path))
    return pinned


def worktree_git(env: Environment, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """git in the agent's worktree, with its repository paths pinned (see `pinned_env`)."""
    return git(env.path, *args, check=check, env=pinned_env(env))


def short(sha: str | None) -> str:
    return (sha or "?")[:12]


# --- artifacts -------------------------------------------------------------------------


def write_artifact(root: Path, run_id: str, name: str, content: str | bytes) -> dict[str, Any]:
    """Write one artifact and return its reference: path (relative to `root`), bytes, sha256."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    path = root / STATE_DIR / ARTIFACTS_DIR / run_id / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": path.relative_to(root).as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def is_artifact(value: object) -> bool:
    return isinstance(value, Mapping) and isinstance(value.get("path"), str) and "sha256" in value


def read_artifact(root: Path, ref: Mapping[str, Any], limit: int) -> tuple[str, bool]:
    """The artifact's text, cut at `limit` characters, and whether it was cut."""
    path = (root / str(ref["path"])).resolve()
    if not path.is_relative_to((root / STATE_DIR / ARTIFACTS_DIR).resolve()):
        raise WorkspaceError(f"artifact {ref['path']!r} is not under {STATE_DIR}/{ARTIFACTS_DIR}/")
    text = path.read_text(encoding="utf-8", errors="replace")
    return (text[:limit], True) if len(text) > limit else (text, False)


def redact_lines(text: str, patterns: Iterable[str] = ()) -> str:
    """Redact `text` line by line; `redact_text` scans at most 8 KiB, which a log easily exceeds."""
    extra = tuple(patterns)
    return "".join(redact_text(line, extra) for line in text.splitlines(keepends=True))


# --- the workflow worktree ---------------------------------------------------------------


def workflow_environment(
    root: Path, run_id: str, spec: EnvironmentSpec, *, requested_autonomy: Autonomy
) -> tuple[Environment, bool]:
    """The workflow run's worktree: attached when it exists, created at HEAD otherwise. True when created."""
    if read_meta(root, run_id) is not None:
        return attach(root, run_id, spec, requested_autonomy=requested_autonomy), False
    return create(root, run_id, spec, requested_autonomy=requested_autonomy), True


@dataclass(frozen=True)
class Settled:
    head: str
    committed: int  # paths the orchestrator committed on the implementer's behalf


def settle(env: Environment) -> Settled:
    """Check the implementer stayed on the run branch above its base, and commit what it left uncommitted."""
    branch = worktree_git(env, "symbolic-ref", "--quiet", "HEAD", check=False).stdout.strip()
    if branch != f"refs/heads/{env.meta.branch}":
        raise WorkspaceError(f"the worktree is not on {env.meta.branch} any more (HEAD is {branch or 'detached'})")
    if worktree_git(env, "merge-base", "--is-ancestor", env.meta.base_commit, "HEAD", check=False).returncode != 0:
        raise WorkspaceError(f"the run branch no longer contains its base {short(env.meta.base_commit)}")
    dirty = [line for line in worktree_git(env, "status", "--porcelain").stdout.splitlines() if line.strip()]
    if dirty:
        worktree_git(env, "add", "--all")
        worktree_git(env, "commit", "--quiet", "--no-verify", "-m", LEFTOVER_MESSAGE)
    return Settled(head=head_of(env), committed=len(dirty))


def head_of(env: Environment) -> str:
    return worktree_git(env, "rev-parse", "HEAD").stdout.strip()


def is_clean(env: Environment) -> bool:
    return not worktree_git(env, "status", "--porcelain").stdout.strip()


def restore(env: Environment, head: str) -> None:
    """Put the worktree back at `head` after commands that were run on a clean tree changed it."""
    worktree_git(env, "reset", "--quiet", "--hard", head)
    worktree_git(env, "clean", "--quiet", "-fd")


def changed_files(root: Path, base: str, head: str) -> list[str]:
    out = git(root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", base, head).stdout
    return [name for name in out.split("\0") if name]


def diff_evidence(root: Path, run_id: str, base: str, head: str, attempt: int) -> dict[str, Any]:
    """The diff `base..head` as an artifact, with the files and line counts the PR body and reviewer read."""
    flags = ("--no-ext-diff", "--no-textconv")
    patch = subprocess.run(
        ["git", "-C", str(root), *SAFE_GIT, "diff", *flags, "--binary", base, head], capture_output=True, check=False
    )
    if patch.returncode != 0:
        raise WorkspaceError(f"git diff {short(base)} {short(head)} failed: {patch.stderr.decode(errors='replace')[-500:]}")
    files: list[str] = []
    added = removed = 0
    for line in git(root, "diff", *flags, "--numstat", "-z", base, head).stdout.split("\0"):
        parts = line.split("\t")
        if len(parts) == 3 and parts[2]:
            files.append(parts[2])
            added += int(parts[0]) if parts[0].isdigit() else 0
            removed += int(parts[1]) if parts[1].isdigit() else 0
    ref = write_artifact(root, run_id, f"diff-{attempt}.patch", patch.stdout)
    return {**ref, "base": base, "head": head, "files": files, "insertions": added, "deletions": removed}


# --- commands the orchestrator runs in the sandbox -----------------------------------------


@dataclass(frozen=True)
class CommandResult:
    kind: str  # reproduction, test, lint, typecheck
    command: str
    exit_code: int | None  # None: killed at the timeout
    duration_ms: int
    output: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    def summary(self) -> dict[str, Any]:
        return {"kind": self.kind, "command": self.command, "exit_code": self.exit_code, "duration_ms": self.duration_ms}


def run_command(env: Environment, kind: str, command: str, *, timeout_s: float, base_env: Mapping[str, str]) -> CommandResult:
    """Run one shell command in the run's sandbox, from the worktree, with the worker's scrubbed environment."""
    if not env.sandbox.sandboxed:
        raise WorkspaceError("srt is absent; the orchestrator runs no command outside the sandbox")
    child_env = env.sandbox.env({k: base_env[k] for k in ENV_ALLOWLIST if k in base_env})
    # Evidence runs never leave bytecode behind: Python validates a .pyc by mtime (whole seconds)
    # and size, so a fix written in the same second as a check that compiled the old file, at the
    # same size, would be judged against the stale .pyc. Seen in the eval fixture (calc-add).
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Tool caches land where srt allows writes: the sandbox mounts home read-only, so uv's default
    # `~/.cache/uv` fails to open and `uv run <anything>` exits 2 before the command runs (seen on
    # the first real run; the implementer's `--no-cache` reproduction command had masked it).
    cache = env.sandbox.tmp_dir / "cache"
    child_env["XDG_CACHE_HOME"] = str(cache)
    child_env["UV_CACHE_DIR"] = str(cache / "uv")
    child_env["CI"] = "1"  # no watch modes, prompts or interactive reporters
    start = time.monotonic()
    proc = subprocess.Popen(
        env.sandbox.wrap(["/bin/sh", "-c", command]),
        cwd=env.path,
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # its own process group, so the timeout reaches every child
    )
    try:
        out, _ = proc.communicate(timeout=timeout_s)
        code: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = proc.communicate()
        code = None
    duration = int((time.monotonic() - start) * 1000)
    return CommandResult(kind, command, code, duration, out[-MAX_OUTPUT_BYTES:].decode("utf-8", errors="replace"))


def render_results(results: Iterable[CommandResult], header: str) -> str:
    parts = [header]
    for r in results:
        status = "timed out" if r.exit_code is None else f"exit {r.exit_code}"
        parts += ["", f"== {r.kind}: {r.command}  ({status}, {r.duration_ms} ms)", r.output.rstrip()]
    return "\n".join(parts) + "\n"


def ensure_run_branch(env: Environment, run_id: str) -> str:
    """The run's branch name, refusing anything outside the `agent-team/` namespace."""
    branch = env.meta.branch
    if branch != f"{BRANCH_PREFIX}{run_id}":
        raise AllocatorError(f"worktree branch {branch!r} is not the run branch {BRANCH_PREFIX}{run_id}")
    return branch
