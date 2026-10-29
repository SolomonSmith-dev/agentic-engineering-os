"""After an arm: snapshot its tree, cut the diff, and run the hidden verifier and the existing tests in srt.

    the arm's tree -> snapshot commit (what it left uncommitted, committed by the harness)
                   -> diff base..snapshot: files, lines, lines outside planned_files
                   -> git archive into a fresh directory, verifier files copied over it
                   -> each command inside srt: no network, writes only there and its temp directory

The verifier files reach the disk only here, after the agent has exited, and
only in a directory no agent ran in. Every command in this module runs code
an agent wrote, so none runs outside srt: without srt the eval refuses.
The exported tree is extracted with tarfile's `data` filter, so a symlink an
agent committed cannot point the overlay at a file outside the directory.
"""

import fnmatch
import io
import json
import os
import shutil
import signal
import subprocess
import tarfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_team.adapters.claude.invoke import ENV_ALLOWLIST
from agent_team.environments.sandbox import DENY_READ_HOME, Sandbox
from agent_team.eval.tasks import IDENTITY, TaskError, git, git_env
from agent_team.runs.redact import redact_text
from agent_team.schemas.eval import CheckResult, DiffStats

OUTPUT_TAIL_CHARS = 2000
SNAPSHOT_MESSAGE = "agent-team eval: the tree the arm left"


class VerifyError(RuntimeError):
    """The tree cannot be snapshotted, exported or checked; the message says why."""


def write_srt_settings(
    path: Path,
    *,
    allow_write: Sequence[Path],
    deny_write: Sequence[Path] = (),
    deny_read: Sequence[Path] = (),
    allowed_domains: Sequence[str] = (),
    home: Path | None = None,
) -> None:
    """srt settings for a process the eval runs itself (mini-swe-agent, the verifier, the existing tests)."""
    home = home or Path.home()
    settings = {
        "network": {"allowedDomains": list(allowed_domains), "deniedDomains": []},
        "filesystem": {
            "denyRead": [*(str(home / name) for name in DENY_READ_HOME), *(str(p) for p in deny_read)],
            "allowWrite": [str(p) for p in allow_write],
            "denyWrite": [str(p) for p in deny_write],
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def eval_sandbox(
    srt: str, settings_path: Path, tmp_dir: Path, *, allow_write: Sequence[Path], deny_write: Sequence[Path] = (),
    deny_read: Sequence[Path] = (), allowed_domains: Sequence[str] = (),
) -> Sandbox:  # fmt: skip
    tmp_dir.mkdir(parents=True, exist_ok=True)
    write_srt_settings(
        settings_path,
        allow_write=[*allow_write, tmp_dir],
        deny_write=deny_write,
        deny_read=deny_read,
        allowed_domains=allowed_domains,
    )
    return Sandbox(srt=srt, settings_path=settings_path, tmp_dir=tmp_dir, port_offset=0)


# --- the arm's tree ----------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    commit: str
    committed: int  # paths the harness committed on the arm's behalf


def trusted_git(tree: Path, repo: Path) -> dict[str, str]:
    """GIT_DIR, GIT_COMMON_DIR and GIT_WORK_TREE from the harness's own paths.

    A worktree finds its repository through `.git/worktrees/<id>/commondir`, which the
    run's srt settings let the agent rewrite; pointed at a copy of `.git` with
    `core.fsmonitor` or a clean filter in its config, plain `git status` or `git add`
    run here would execute it outside the sandbox. These variables override that file.
    """
    common = repo / ".git"
    if tree.resolve() == repo.resolve():
        return git_env({"GIT_DIR": str(common), "GIT_WORK_TREE": str(tree)})
    admin = common / "worktrees" / tree.name
    if not admin.is_dir():
        raise VerifyError(f"{tree} is not a worktree of {repo}")
    return git_env({"GIT_DIR": str(admin), "GIT_COMMON_DIR": str(common), "GIT_WORK_TREE": str(tree)})


def snapshot(tree: Path, repo: Path) -> Snapshot:
    """Commit whatever the arm left in `tree` (the checkout `repo`, or a worktree of it); the commit it ends at."""
    env = trusted_git(tree, repo)
    dirty = [line for line in git(tree, "status", "--porcelain", env=env).stdout.splitlines() if line.strip()]
    if dirty:
        env = {**env, **IDENTITY}
        git(tree, "add", "--all", env=env)
        git(tree, "commit", "--quiet", "--no-verify", "-m", SNAPSHOT_MESSAGE, env=env)
    return Snapshot(git(tree, "rev-parse", "HEAD", env=env).stdout.strip(), len(dirty))


def planned(path: str, patterns: Iterable[str]) -> bool:
    """Whether `path` falls under one of the task's planned-file globs (`**` spans directories)."""
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(path, pattern.replace("/**", "/*")):
            return True
        if pattern.endswith("/**") and path.startswith(pattern[:-2]):
            return True
    return False


def diff_stats(repo: Path, base: str, head: str, planned_files: Sequence[str]) -> DiffStats:
    """Files and lines changed from `base` to `head`, and the lines changed outside the planned files.

    Run in the main checkout `repo`, never in an arm's worktree: only the objects are read.
    """
    out = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z", base, head,
              env=trusted_git(repo, repo)).stdout  # fmt: skip
    files: list[str] = []
    added = removed = unplanned_lines = 0
    unplanned_files: list[str] = []
    for entry in out.split("\0"):
        parts = entry.split("\t")
        if len(parts) != 3 or not parts[2]:
            continue
        ins = int(parts[0]) if parts[0].isdigit() else 0  # "-" for a binary file: no line count
        dels = int(parts[1]) if parts[1].isdigit() else 0
        files.append(parts[2])
        added += ins
        removed += dels
        if not planned(parts[2], planned_files):
            unplanned_files.append(parts[2])
            unplanned_lines += ins + dels
    return DiffStats(
        files=files, insertions=added, deletions=removed, unplanned_files=unplanned_files, unplanned_lines=unplanned_lines
    )


def export(repo: Path, commit: str, dest: Path) -> None:
    """The files of `commit`, without `.git`, into a new directory `dest`; read from the main checkout `repo`."""
    archive = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "archive", "--format=tar", commit],
        capture_output=True, check=False, env=trusted_git(repo, repo),
    )  # fmt: skip
    if archive.returncode != 0:
        raise VerifyError(f"git archive {commit[:12]} failed: {archive.stderr.decode(errors='replace')[-500:]}")
    dest.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise VerifyError(f"the tree at {commit[:12]} cannot be extracted safely: {exc}") from exc


def overlay(files: Path, dest: Path) -> list[str]:
    """Copy the verifier's files over `dest`, replacing what is there; never through a symlink."""
    copied = []
    real_dest = dest.resolve()
    for src in sorted(files.rglob("*")):
        if src.is_dir() or src.is_symlink() or "__pycache__" in src.parts:
            continue
        rel = src.relative_to(files)
        target = dest / rel
        for parent in [*reversed(rel.parents)][1:]:
            node = dest / parent
            if node.is_symlink() or (node.exists() and not node.is_dir()):
                node.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve().is_relative_to(real_dest):
            raise VerifyError(f"verifier file {rel} would land outside the checked tree")
        if target.is_symlink() or target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.copyfile(src, target)
        copied.append(rel.as_posix())
    return copied


def run_check(
    sandbox: Sandbox, cwd: Path, command: str, *, timeout_s: float, redaction: tuple[str, ...] = ()
) -> CheckResult:
    """One shell command in srt from `cwd`, with the worker's scrubbed environment and no stdin."""
    if not sandbox.sandboxed:
        raise VerifyError("srt is absent; the eval runs no task command outside the sandbox")
    env = sandbox.env({k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ})
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            sandbox.wrap(["/bin/sh", "-c", command]),
            cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )  # fmt: skip
    except OSError as exc:
        return CheckResult(command=command, exit_code=None, duration_ms=0, passed=False, error=f"could not start: {exc}")
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
    passed = code == 0
    tail = None
    if not passed:
        tail = redact_text(out[-OUTPUT_TAIL_CHARS:].decode("utf-8", errors="replace"), redaction).strip() or None
    return CheckResult(
        command=command, exit_code=code, duration_ms=duration, passed=passed,
        error="timed out" if code is None else None, output_tail=tail,
    )  # fmt: skip


def checked_tree(repo: Path, commit: str, dest: Path, files: Path | None) -> None:
    """A fresh directory holding `commit`'s files with the verifier's files on top."""
    export(repo, commit, dest)
    if files is not None:
        overlay(files, dest)


def check_commands(
    repo: Path,
    commit: str,
    dest: Path,
    commands: Sequence[str],
    *,
    files: Path | None,
    srt: str,
    deny_read: Sequence[Path],
    timeout_s: float,
    redaction: tuple[str, ...] = (),
) -> list[CheckResult]:
    """Run `commands` in order on one fresh copy of `commit` (plus the verifier's files) inside srt.

    `repo` is the cell's main checkout; an arm's worktree shares its objects, so its commits are there.
    """
    try:
        checked_tree(repo, commit, dest / "tree", files)
    except (VerifyError, TaskError, OSError) as exc:
        return [CheckResult(command=c, exit_code=None, duration_ms=0, passed=False, error=str(exc)) for c in commands]
    sandbox = eval_sandbox(srt, dest / "srt.json", dest / "tmp", allow_write=[dest / "tree"], deny_read=deny_read)
    return [run_check(sandbox, dest / "tree", c, timeout_s=timeout_s, redaction=redaction) for c in commands]
