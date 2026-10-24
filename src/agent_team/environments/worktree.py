"""Environment allocator (build-order step 5): one git worktree per run under `.agent-team/`.

Layout inside the target repository, all gitignored:

    .agent-team/worktrees/<run_id>/     the worktree, on branch agent-team/<run_id>
    .agent-team/meta/<run_id>.json      branch, base commit, port slot
    .agent-team/sandbox/<run_id>.json   srt settings (outside the worktree)
    .agent-team/tmp/<run_id>/           TMPDIR for the run
    .agent-team/archive/<run_id>.patch  work saved by dispose before removal

Nothing is written outside `.agent-team/` except git's own worktree bookkeeping
under `.git/`. Dispose never discards work silently: uncommitted changes and
commits past the base are saved as a patch first.
"""

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from agent_team.environments import ports
from agent_team.environments.include import copy_includes
from agent_team.environments.lock import live_run_id
from agent_team.environments.sandbox import Sandbox, effective_ceiling, prepare, settings_path
from agent_team.runs.store import STATE_DIR, RunStore, UnknownRunError, check_state_dir_ignored, repo_root
from agent_team.schemas.common import Autonomy
from agent_team.schemas.environment import EnvironmentSpec

BRANCH_PREFIX = "agent-team/"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class AllocatorError(RuntimeError):
    """The environment cannot be allocated or disposed; the message says why."""


@dataclass(frozen=True)
class WorktreeMeta:
    run_id: str
    path: str
    branch: str
    base_commit: str
    port_slot: int
    created_at: str


@dataclass(frozen=True)
class Environment:
    """Everything a runtime adapter needs to run one agent inside its isolated environment."""

    meta: WorktreeMeta
    sandbox: Sandbox
    autonomy_ceiling: Autonomy
    includes_copied: list[str] = field(default_factory=list)
    includes_skipped: list[str] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return Path(self.meta.path)

    def note(self, spec_name: str) -> str:
        """One line for the run record's transition note."""
        isolation = "srt sandbox" if self.sandbox.sandboxed else "worktree only (srt absent or not requested)"
        return (
            f"environment {spec_name}: {isolation}; autonomy ceiling {self.autonomy_ceiling}; "
            f"port offset {self.sandbox.port_offset}; {len(self.includes_copied)} included file(s)"
        )


@dataclass(frozen=True)
class SweepResult:
    run_id: str
    action: Literal["disposed", "kept"]
    reason: str
    archive: str | None = None


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise AllocatorError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}")
    return result


def _dirs(root: Path) -> dict[str, Path]:
    state = root / STATE_DIR
    return {
        "state": state,
        "worktrees": state / "worktrees",
        "meta": state / "meta",
        "archive": state / "archive",
        "tmp": state / "tmp",
    }


def _check_run_id(run_id: str) -> None:
    if not RUN_ID_RE.match(run_id):
        raise AllocatorError(f"run id {run_id!r} must match {RUN_ID_RE.pattern}")


def _meta_path(root: Path, run_id: str) -> Path:
    return _dirs(root)["meta"] / f"{run_id}.json"


def read_meta(root: Path, run_id: str) -> WorktreeMeta | None:
    path = _meta_path(root, run_id)
    if not path.is_file():
        return None
    return WorktreeMeta(**json.loads(path.read_text(encoding="utf-8")))


def list_worktrees(repo: Path) -> list[WorktreeMeta]:
    """Every worktree the allocator created and has not disposed, by run id."""
    meta_dir = _dirs(repo_root(repo))["meta"]
    if not meta_dir.is_dir():
        return []
    return [
        WorktreeMeta(**json.loads(p.read_text(encoding="utf-8"))) for p in sorted(meta_dir.glob("*.json"))
    ]


def create(
    repo: Path,
    run_id: str,
    spec: EnvironmentSpec,
    *,
    requested_autonomy: Autonomy = "A3",
    store: RunStore | None = None,
    srt: str | None = None,
    home: Path | None = None,
) -> Environment:
    """Allocate a worktree for `run_id` at HEAD, copy `.worktreeinclude` files, prepare the sandbox.

    When `store` is given, the worktree path is written to the run's record.
    """
    _check_run_id(run_id)
    root = repo_root(repo)
    check_state_dir_ignored(root)
    dirs = _dirs(root)
    path = dirs["worktrees"] / run_id
    if path.exists() or _meta_path(root, run_id).exists():
        raise AllocatorError(f"run {run_id} already has a worktree at {path}")

    head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False)
    if head.returncode != 0:
        raise AllocatorError(f"{root} has no commits; a worktree needs a base commit")
    base = head.stdout.strip()
    branch = f"{BRANCH_PREFIX}{run_id}"
    slot = ports.next_slot((m.port_slot for m in list_worktrees(root)), spec.ports.block_size)

    for key in ("worktrees", "meta"):
        dirs[key].mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "--quiet", "-b", branch, str(path), base)

    meta = WorktreeMeta(
        run_id=run_id,
        path=str(path),
        branch=branch,
        base_commit=base,
        port_slot=slot,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    _meta_path(root, run_id).write_text(json.dumps(asdict(meta), indent=2) + "\n", encoding="utf-8")

    copied, skipped = copy_includes(root, path, STATE_DIR)
    git_dir = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    sandbox = prepare(
        spec,
        run_id=run_id,
        state_dir=dirs["state"],
        worktree=path,
        git_dir=git_dir,
        port_offset=ports.offset(slot, spec.ports.block_size),
        home=home,
        srt=srt,
    )
    if store is not None:
        store.set_worktree_path(run_id, str(path))
    return Environment(
        meta=meta,
        sandbox=sandbox,
        autonomy_ceiling=effective_ceiling(requested_autonomy, sandbox.sandboxed),
        includes_copied=copied,
        includes_skipped=skipped,
    )


def _archive(root: Path, worktree: Path, run_id: str, base: str | None) -> Path | None:
    """Save uncommitted changes and commits past `base` as a binary patch. None when there is nothing."""
    dirty = bool(_git(worktree, "status", "--porcelain").stdout.strip())
    ahead = False
    if base is not None:
        ahead = _git(worktree, "rev-list", "--count", f"{base}..HEAD").stdout.strip() != "0"
    if not dirty and not ahead:
        return None
    _git(worktree, "add", "--all")
    diff = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--cached", "--binary", base or "HEAD"],
        capture_output=True,
        check=False,
    )
    if diff.returncode != 0:
        raise AllocatorError(f"could not archive {worktree}; it was left in place: {diff.stderr.decode(errors='replace').strip()}")
    archive_dir = _dirs(root)["archive"]
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"{run_id}.patch"
    target.write_bytes(diff.stdout)
    return target


def _remove(root: Path, run_id: str, meta: WorktreeMeta | None) -> Path | None:
    dirs = _dirs(root)
    worktree = dirs["worktrees"] / run_id
    archive: Path | None = None
    branch = meta.branch if meta is not None else None
    if worktree.is_dir():
        if (worktree / ".git").exists():
            if branch is None:
                current = _git(worktree, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
                branch = current if current.startswith(BRANCH_PREFIX) else None
            archive = _archive(root, worktree, run_id, meta.base_commit if meta else None)
        removed = _git(root, "worktree", "remove", "--force", str(worktree), check=False)
        if removed.returncode != 0 and worktree.is_dir():
            shutil.rmtree(worktree)  # not a registered worktree; it is inside .agent-team/worktrees/
    if branch is not None and branch.startswith(BRANCH_PREFIX):
        _git(root, "branch", "-D", branch, check=False)
    _git(root, "worktree", "prune", check=False)
    _meta_path(root, run_id).unlink(missing_ok=True)
    settings_path(dirs["state"], run_id).unlink(missing_ok=True)
    shutil.rmtree(dirs["tmp"] / run_id, ignore_errors=True)
    return archive


def dispose(repo: Path, run_id: str) -> Path | None:
    """Remove the run's worktree, branch, settings and temp dir. Returns the archive patch, if any."""
    _check_run_id(run_id)
    root = repo_root(repo)
    meta = read_meta(root, run_id)
    if meta is None and not (_dirs(root)["worktrees"] / run_id).exists():
        raise AllocatorError(f"run {run_id} has no worktree")
    return _remove(root, run_id, meta)


def sweep(repo: Path) -> list[SweepResult]:
    """Dispose worktrees whose run ended or has no run record. In-progress runs keep theirs for resume."""
    root = repo_root(repo)
    check_state_dir_ignored(root)
    dirs = _dirs(root)
    known: dict[str, WorktreeMeta | None] = {m.run_id: m for m in list_worktrees(root)}
    if dirs["worktrees"].is_dir():
        for child in dirs["worktrees"].iterdir():
            if child.is_dir() and RUN_ID_RE.match(child.name):
                known.setdefault(child.name, None)
    live = live_run_id(dirs["state"])
    store = RunStore.open(root, create=False)
    results: list[SweepResult] = []
    try:
        for run_id in sorted(known):
            meta = known[run_id]
            if run_id == live:
                results.append(SweepResult(run_id, "kept", "live run holds the lock"))
                continue
            reason: str | None
            if meta is None:
                reason = "no allocator metadata"
            elif store is None:
                reason = "no run record"
            else:
                try:
                    run = store.get_run(run_id)
                except UnknownRunError:
                    reason = "no run record"
                else:
                    reason = f"run ended ({run.outcome})" if run.outcome is not None else None
            if reason is None:
                results.append(SweepResult(run_id, "kept", "run not finished; resume needs it"))
                continue
            archive = _remove(root, run_id, meta)
            results.append(SweepResult(run_id, "disposed", reason, str(archive) if archive else None))
    finally:
        if store is not None:
            store.close()
    _git(root, "worktree", "prune", check=False)
    return results
