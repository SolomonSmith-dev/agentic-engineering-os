"""Load `evals/tasks/*.yaml` and materialize a task's repository at its ref (build-order step 13).

Two kinds of `repo`:

    fixture:<name>   evals/fixtures/<name>/repo, committed into a new repository with a fixed
                     author, committer and date, so the commit is the same on every machine and
                     must equal the task's `ref`; a fixture that drifted is refused
    anything else    a git URL or path, cloned and checked out (detached) at `ref`; the private
                     task set in the maintainer's repositories uses this

Either way the copy is fresh per cell, `origin` is removed so nothing can push back,
and `.agent-team/` is ignored through `.git/info/exclude`, never a tracked file.
The task's verifier files stay where they are; they are copied in only after the arm ends.

Git here runs on trees agents write: hooks are off, fsmonitor is off, and nothing signs.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from agent_team.runs.store import STATE_DIR
from agent_team.schemas.eval import FIXTURE_PREFIX, EvalTask

GIT_SAFE: tuple[str, ...] = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "commit.gpgsign=false",
    "-c", "core.autocrlf=false",
    "-c", "protocol.ext.allow=never",
)  # fmt: skip

# The identity and date every fixture commit (and every harness snapshot commit) is made with.
IDENTITY = {
    "GIT_AUTHOR_NAME": "agent-team eval",
    "GIT_AUTHOR_EMAIL": "eval@agent-team.invalid",
    "GIT_COMMITTER_NAME": "agent-team eval",
    "GIT_COMMITTER_EMAIL": "eval@agent-team.invalid",
}
FIXTURE_DATE = "2026-10-24T00:00:00+00:00"
FIXTURE_MESSAGE = "eval fixture"
FIXTURE_BRANCH = "main"

# Git environment variables that would point git at another repository or config.
_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CEILING_DIRECTORIES")  # fmt: skip


class TaskError(RuntimeError):
    """A task file is invalid, or its repository cannot be materialized; the message says which and why."""


@dataclass(frozen=True)
class LoadedTask:
    task: EvalTask
    path: Path  # the task file

    @property
    def fixture_dir(self) -> Path | None:
        """evals/fixtures/<name>/ for a fixture task, resolved next to the tasks directory."""
        if not self.task.repo.startswith(FIXTURE_PREFIX):
            return None
        return self.path.parent.parent / "fixtures" / self.task.repo[len(FIXTURE_PREFIX):]

    @property
    def verifier_dir(self) -> Path | None:
        files = self.task.verifier.files
        return None if files is None else (self.path.parent / files).resolve()

    def hidden_paths(self) -> list[Path]:
        """Everything an agent must not read: the task file, its fixture, its verifier files."""
        paths = [self.path.resolve()]
        if self.fixture_dir is not None:
            paths.append(self.fixture_dir.resolve())
        if self.verifier_dir is not None:
            paths.append(self.verifier_dir)
        return paths


def git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.update(extra or {})
    return env


def git(cwd: Path, *args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), *GIT_SAFE, *args], capture_output=True, text=True, check=False, env=env or git_env()
    )
    if check and result.returncode != 0:
        raise TaskError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()[-500:]}")
    return result


def load_task(path: Path) -> LoadedTask:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise TaskError(f"{path}: <file>: {str(exc).splitlines()[0]}") from exc
    try:
        task = EvalTask.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first["loc"]) or "<root>"
        raise TaskError(f"{path}: {field}: {first['msg']}") from exc
    if task.id != path.stem:
        raise TaskError(f"{path}: id: {task.id!r} must match the file stem {path.stem!r}")
    loaded = LoadedTask(task, path)
    if loaded.fixture_dir is not None and not (loaded.fixture_dir / "repo").is_dir():
        raise TaskError(f"{path}: repo: no fixture at {loaded.fixture_dir / 'repo'}")
    if loaded.verifier_dir is not None and not loaded.verifier_dir.is_dir():
        raise TaskError(f"{path}: verifier.files: {loaded.verifier_dir} is not a directory")
    return loaded


def load_tasks(tasks_dir: Path, only: list[str] | None = None) -> list[LoadedTask]:
    """Every task under `tasks_dir`, sorted by id; `only` keeps those ids and refuses unknown ones."""
    if not tasks_dir.is_dir():
        raise TaskError(f"no tasks directory at {tasks_dir}")
    tasks = [load_task(p) for p in sorted(tasks_dir.glob("*.yaml"))]
    if not tasks:
        raise TaskError(f"no task files (*.yaml) in {tasks_dir}")
    if only:
        known = {t.task.id for t in tasks}
        unknown = [t for t in only if t not in known]
        if unknown:
            raise TaskError(f"unknown task(s) {', '.join(unknown)}; known: {', '.join(sorted(known))}")
        tasks = [t for t in tasks if t.task.id in only]
    return tasks


def _copy_fixture(src: Path, dest: Path) -> None:
    """Copy regular files only, with normalized modes, so the commit depends on content alone."""
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if path.is_symlink() or "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o755 if os.access(path, os.X_OK) else 0o644)


def fixture_commit(src: Path, dest: Path) -> str:
    """Commit the files under `src` into a new repository at `dest`; returns the (deterministic) commit."""
    dest.mkdir(parents=True)
    git(dest, "init", "--quiet", "--object-format=sha1", "-b", FIXTURE_BRANCH)
    _copy_fixture(src, dest)
    git(dest, "add", "--all")
    env = git_env({**IDENTITY, "GIT_AUTHOR_DATE": FIXTURE_DATE, "GIT_COMMITTER_DATE": FIXTURE_DATE})
    git(dest, "commit", "--quiet", "--no-verify", "-m", FIXTURE_MESSAGE, env=env)
    return git(dest, "rev-parse", "HEAD").stdout.strip()


def _prepare(dest: Path) -> None:
    """Local config the arms need (an identity to commit with), no origin, `.agent-team/` ignored."""
    for key, value in (("user.name", IDENTITY["GIT_AUTHOR_NAME"]), ("user.email", IDENTITY["GIT_AUTHOR_EMAIL"]),
                       ("commit.gpgsign", "false")):  # fmt: skip
        git(dest, "config", "--local", key, value)
    if git(dest, "remote", check=False).stdout.split():
        for remote in git(dest, "remote").stdout.split():
            git(dest, "remote", "remove", remote)
    exclude = Path(git(dest, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").stdout.strip())
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as f:
        f.write(f"\n{STATE_DIR}/\n")


def _drop_other_history(dest: Path) -> None:
    """Delete every ref and prune what only they reached: a later commit could hold the fix."""
    for remote in git(dest, "remote").stdout.split():
        git(dest, "remote", "remove", remote)
    for ref in git(dest, "for-each-ref", "--format=%(refname)").stdout.split():
        git(dest, "update-ref", "--no-deref", "-d", ref)
    git(dest, "reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
    git(dest, "gc", "--quiet", "--prune=now")


def materialize(loaded: LoadedTask, dest: Path) -> str:
    """A fresh repository for one cell at `dest`, at the task's ref; returns the base commit."""
    task = loaded.task
    if loaded.fixture_dir is not None:
        commit = fixture_commit(loaded.fixture_dir / "repo", dest)
        if commit != task.ref:
            raise TaskError(
                f"{loaded.path}: ref: fixture {task.repo} materializes to {commit}, not {task.ref}; "
                "the fixture changed, so update the task's ref after reviewing the change"
            )
    else:
        source = task.repo
        if "://" not in source and not source.startswith("git@"):
            source = str((loaded.path.parent / source).resolve())  # a local path, relative to the task file
        dest.parent.mkdir(parents=True, exist_ok=True)
        clone = subprocess.run(
            ["git", *GIT_SAFE, "clone", "--quiet", "--no-hardlinks", "--no-checkout", "--", source, str(dest)],
            capture_output=True, text=True, check=False, env=git_env(),
        )  # fmt: skip
        if clone.returncode != 0:
            raise TaskError(f"{loaded.path}: repo: cannot clone {task.repo}: {clone.stderr.strip()[-500:]}")
        resolved = git(dest, "rev-parse", "--verify", "--quiet", f"{task.ref}^{{commit}}", check=False)
        if resolved.returncode != 0:
            raise TaskError(f"{loaded.path}: ref: {task.ref!r} is not a commit in {task.repo}")
        commit = resolved.stdout.strip()
        git(dest, "checkout", "--quiet", "--detach", commit)
        _drop_other_history(dest)
    _prepare(dest)
    return commit
