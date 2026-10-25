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
import os
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
    git_dir: Path | None = None  # the repository's common .git, resolved from the root, never from the worktree

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


# Git run in a worktree an agent wrote: no hooks, no fsmonitor, no external diff or textconv.
_AGENT_TREE_GIT: tuple[str, ...] = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
_GIT_ENV_DROP = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG", "GIT_CONFIG_GLOBAL",
                 "GIT_CONFIG_SYSTEM", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")  # fmt: skip


class TamperedWorktreeError(AllocatorError):
    """The worktree's git pointers no longer name the repository agent-team created it in."""


def check_pointers(common: Path, worktree: Path) -> Path:
    """Refuse unless the files that tell git which repository to open are the ones `git worktree add` wrote.

    The worktree's `.git` file and its admin dir's `commondir` (and a `config.worktree`) are files an agent
    can write. Git follows them even when GIT_DIR and GIT_COMMON_DIR are set (the ref store reads `commondir`
    itself), so a repointed worktree would make the git agent-team runs outside the sandbox write refs into,
    or read a config from, a repository the agent planted. Returns the admin dir.
    """
    admin = common / "worktrees" / worktree.name
    if not admin.is_dir():
        raise TamperedWorktreeError(f"{worktree} has no admin directory under {common}; refusing to run git in it")
    problems = []
    try:
        dotgit = (worktree / ".git").read_text(encoding="utf-8").strip()
    except OSError:
        dotgit = ""
    named = Path(dotgit.removeprefix("gitdir:").strip()) if dotgit.startswith("gitdir:") else None
    if named is None or (named if named.is_absolute() else worktree / named).resolve() != admin.resolve():
        problems.append(f"{worktree / '.git'} does not point at {admin}")
    try:
        commondir = (admin / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        commondir = ""
    target = Path(commondir) if Path(commondir).is_absolute() else admin / commondir
    if not commondir or target.resolve() != common.resolve():
        problems.append(f"{admin / 'commondir'} does not point at {common}")
    if (admin / "config.worktree").exists():
        problems.append(f"{admin / 'config.worktree'} exists; agent-team never writes one")
    if problems:
        raise TamperedWorktreeError(
            "the worktree's git pointers were changed, so agent-team will not run git in it; left in place for "
            "a human to inspect: " + "; ".join(problems)
        )
    return admin


def _worktree_env(root: Path, worktree: Path) -> dict[str, str]:
    """GIT_DIR, GIT_COMMON_DIR and GIT_WORK_TREE from the root's common .git, after `check_pointers`."""
    common = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    admin = check_pointers(common, worktree)
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env.update(GIT_DIR=str(admin), GIT_COMMON_DIR=str(common), GIT_WORK_TREE=str(worktree))
    return env


def _agent_git(
    root: Path, worktree: Path, *args: str, check: bool = True, text: bool = True
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(worktree), *_AGENT_TREE_GIT, *args], capture_output=True, text=text, check=False,
        env=_worktree_env(root, worktree),
    )  # fmt: skip
    if check and result.returncode != 0:
        err = result.stderr if text else result.stderr.decode(errors="replace")
        raise AllocatorError(f"git {' '.join(args)} failed in {worktree}: {err.strip()}")
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
    base: str | None = None,
) -> Environment:
    """Allocate a worktree for `run_id` at `base` (HEAD by default), copy `.worktreeinclude` files, prepare the sandbox.

    When `store` is given, the worktree path is written to the run's record.
    """
    _check_run_id(run_id)
    root = repo_root(repo)
    check_state_dir_ignored(root)
    dirs = _dirs(root)
    path = dirs["worktrees"] / run_id
    if path.exists() or _meta_path(root, run_id).exists():
        raise AllocatorError(f"run {run_id} already has a worktree at {path}")

    head = _git(root, "rev-parse", "--verify", "--quiet", f"{base or 'HEAD'}^{{commit}}", check=False)
    if head.returncode != 0:
        if base is not None:
            raise AllocatorError(f"{base!r} is not a commit in {root}")
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
        git_dir=git_dir,
    )


def attach(
    repo: Path,
    run_id: str,
    spec: EnvironmentSpec,
    *,
    requested_autonomy: Autonomy = "A3",
    srt: str | None = None,
    home: Path | None = None,
) -> Environment:
    """The Environment of a worktree `create` made earlier, for a resumed run; the sandbox settings are rewritten."""
    _check_run_id(run_id)
    root = repo_root(repo)
    meta = read_meta(root, run_id)
    if meta is None or not (Path(meta.path) / ".git").exists():
        raise AllocatorError(f"run {run_id} has no worktree to attach to; it was disposed or never created")
    git_dir = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    sandbox = prepare(
        spec,
        run_id=run_id,
        state_dir=_dirs(root)["state"],
        worktree=Path(meta.path),
        git_dir=git_dir,
        port_offset=ports.offset(meta.port_slot, spec.ports.block_size),
        home=home,
        srt=srt,
    )
    return Environment(
        meta=meta,
        sandbox=sandbox,
        autonomy_ceiling=effective_ceiling(requested_autonomy, sandbox.sandboxed),
        git_dir=git_dir,
    )


def _archive(root: Path, worktree: Path, run_id: str, base: str | None) -> Path | None:
    """Save uncommitted changes and commits past `base` as a binary patch. None when there is nothing."""
    dirty = bool(_agent_git(root, worktree, "status", "--porcelain").stdout.strip())
    ahead = False
    if base is not None:
        ahead = _agent_git(root, worktree, "rev-list", "--count", f"{base}..HEAD").stdout.strip() != "0"
    if not dirty and not ahead:
        return None
    _agent_git(root, worktree, "add", "--all")
    diff = _agent_git(
        root, worktree, "diff", "--no-ext-diff", "--no-textconv", "--cached", "--binary", base or "HEAD",
        check=False, text=False,
    )
    if diff.returncode != 0:
        raise AllocatorError(f"could not archive {worktree}; it was left in place: {diff.stderr.decode(errors='replace').strip()}")
    archive_dir = _dirs(root)["archive"]
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"{run_id}.patch"
    target.write_bytes(diff.stdout)
    return target


def _unique_commits(root: Path, branch: str) -> bool:
    """Whether `branch` has commits no other ref reaches; deleting it would lose them."""
    out = _git(root, "rev-list", "--count", branch, "--not", f"--exclude=refs/heads/{branch}", "--all", check=False)
    return out.returncode != 0 or out.stdout.strip() != "0"


def _remove(root: Path, run_id: str, meta: WorktreeMeta | None) -> tuple[Path | None, str | None]:
    """Remove the run's files. Returns (archive patch, branch kept because only it holds commits)."""
    dirs = _dirs(root)
    worktree = dirs["worktrees"] / run_id
    archive: Path | None = None
    branch = meta.branch if meta is not None else None
    if worktree.is_dir():
        if (worktree / ".git").exists():
            if branch is None:
                current = _agent_git(root, worktree, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
                branch = current if current.startswith(BRANCH_PREFIX) else None
            archive = _archive(root, worktree, run_id, meta.base_commit if meta else None)
        removed = _git(root, "worktree", "remove", "--force", str(worktree), check=False)
        if removed.returncode != 0 and worktree.is_dir():
            shutil.rmtree(worktree)  # not a registered worktree; it is inside .agent-team/worktrees/
    kept: str | None = None
    if branch is not None and branch.startswith(BRANCH_PREFIX):
        # Without metadata there is no base to diff against, so the patch cannot hold the
        # branch's commits: keep the branch instead of dropping work nothing else references.
        if meta is None and _unique_commits(root, branch):
            kept = branch
        else:
            _git(root, "branch", "-D", branch, check=False)
    _git(root, "worktree", "prune", check=False)
    _meta_path(root, run_id).unlink(missing_ok=True)
    settings_path(dirs["state"], run_id).unlink(missing_ok=True)
    shutil.rmtree(dirs["tmp"] / run_id, ignore_errors=True)
    return archive, kept


def dispose(repo: Path, run_id: str) -> Path | None:
    """Remove the run's worktree, branch, settings and temp dir. Returns the archive patch, if any.

    A branch whose commits no other ref reaches is kept when the metadata (and so the base
    the patch would be cut from) is missing.
    """
    _check_run_id(run_id)
    root = repo_root(repo)
    meta = read_meta(root, run_id)
    if meta is None and not (_dirs(root)["worktrees"] / run_id).exists():
        raise AllocatorError(f"run {run_id} has no worktree")
    archive, _ = _remove(root, run_id, meta)
    return archive


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
            try:
                archive, kept = _remove(root, run_id, meta)
            except TamperedWorktreeError as exc:
                results.append(SweepResult(run_id, "kept", str(exc)))
                continue
            if kept is not None:
                reason += f"; branch {kept} kept: its commits are on no other ref"
            results.append(SweepResult(run_id, "disposed", reason, str(archive) if archive else None))
    finally:
        if store is not None:
            store.close()
    _git(root, "worktree", "prune", check=False)
    return results
