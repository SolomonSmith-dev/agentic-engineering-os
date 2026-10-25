"""Git agent-team runs outside the sandbox never follows repository pointers an agent can write (#27).

An agent inside srt may write the run's worktree and `.git/worktrees/<run-id>/`. Both hold files that tell
git which repository to open: the worktree's `.git` and the admin dir's `commondir`. Repointed at a planted
copy of the repository whose config sets `core.fsmonitor`, the orchestrator's own `git status` (outside
every sandbox) would run the planted command on the host. Git follows `commondir` even with GIT_DIR and
GIT_COMMON_DIR set (its ref store reads the file itself), so agent-team checks the pointers and refuses to run
git in a worktree whose pointers changed. These tests plant exactly that.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_team.environments import create, dispose, sweep
from agent_team.environments.sandbox import build_settings
from agent_team.environments.worktree import TamperedWorktreeError
from agent_team.orchestrator.workspace import WorkspaceError, is_clean, pinned_env, settle
from agent_team.schemas import EnvironmentSpec


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def spec() -> EnvironmentSpec:
    return EnvironmentSpec.model_validate({"kind": "environment", "name": "test", "description": "t", "tier": "worktree"})


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text(".agent-team/\n", encoding="utf-8")
    (root / "app.py").write_text("print('hello')\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    return root.resolve()


def plant(repo: Path, worktree: Path, tmp_path: Path) -> Path:
    """What an implementer inside srt could do: copy the repository, arm its config, repoint git at it."""
    common = repo / ".git"
    admin = common / "worktrees" / worktree.name
    evil = tmp_path / "evil-common"
    shutil.copytree(common, evil, ignore=shutil.ignore_patterns("worktrees"))
    shutil.copytree(admin, evil / "worktrees" / worktree.name)
    marker = tmp_path / "pwned"
    with (evil / "config").open("a", encoding="utf-8") as config:
        config.write(f"[core]\n\tfsmonitor = \"touch {marker}; false\"\n")
    (admin / "commondir").write_text(f"{evil}\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {evil / 'worktrees' / worktree.name}\n", encoding="utf-8")
    (worktree / "app.py").write_text("print('changed')\n", encoding="utf-8")  # dirty, so settle runs status and add
    return marker


def test_the_plant_works_on_unpinned_git(repo: Path, tmp_path: Path) -> None:
    """The attack is real: plain `git status` in the planted worktree runs the planted fsmonitor."""
    env = create(repo, "run-1", spec())
    marker = plant(repo, env.path, tmp_path)
    subprocess.run(["git", "-C", str(env.path), "status", "--porcelain"], capture_output=True, check=False)
    assert marker.exists(), "precondition: git follows the planted pointers"


def test_settle_refuses_a_worktree_with_planted_pointers(repo: Path, tmp_path: Path) -> None:
    env = create(repo, "run-1", spec())
    marker = plant(repo, env.path, tmp_path)
    with pytest.raises(WorkspaceError, match="git pointers were changed"):
        settle(env)
    with pytest.raises(WorkspaceError, match="git pointers were changed"):
        is_clean(env)
    assert not marker.exists(), "the orchestrator's git ran the planted core.fsmonitor"
    assert git(repo, "log", "-1", "--format=%s", env.meta.branch) == "init", "no commit landed anywhere"


@pytest.mark.parametrize("tamper", ["dotgit", "commondir", "config.worktree"])
def test_each_pointer_is_checked(repo: Path, tmp_path: Path, tamper: str) -> None:
    env = create(repo, "run-1", spec())
    admin = repo / ".git" / "worktrees" / "run-1"
    if tamper == "dotgit":
        (env.path / ".git").write_text(f"gitdir: {tmp_path / 'elsewhere'}\n", encoding="utf-8")
    elif tamper == "commondir":
        (admin / "commondir").write_text(f"{tmp_path}\n", encoding="utf-8")
    else:
        (admin / "config.worktree").write_text("[core]\n\tfsmonitor = true\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="git pointers were changed"):
        settle(env)


def test_settle_still_commits_on_an_untouched_worktree(repo: Path) -> None:
    env = create(repo, "run-1", spec())
    (env.path / "app.py").write_text("print('changed')\n", encoding="utf-8")
    settled = settle(env)
    assert settled.committed == 1 and is_clean(env)
    assert git(repo, "rev-parse", env.meta.branch) == settled.head
    assert git(repo, "log", "-1", "--format=%s", env.meta.branch) == "agent-team: changes the implementer left uncommitted"


def test_dispose_and_sweep_leave_a_tampered_worktree_in_place(repo: Path, tmp_path: Path) -> None:
    env = create(repo, "run-1", spec())
    marker = plant(repo, env.path, tmp_path)
    with pytest.raises(TamperedWorktreeError):
        dispose(repo, "run-1")
    [result] = sweep(repo)
    assert (result.action, result.run_id) == ("kept", "run-1") and "git pointers were changed" in result.reason
    assert not marker.exists() and env.path.is_dir(), "left for a human to inspect"


def test_pinned_env_ignores_inherited_git_variables(repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = create(repo, "run-1", spec())
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "planted-config"))
    pinned = pinned_env(env)
    assert pinned["GIT_DIR"] == str(repo / ".git" / "worktrees" / "run-1")
    assert pinned["GIT_COMMON_DIR"] == str(repo / ".git") and pinned["GIT_WORK_TREE"] == str(env.path)
    assert "GIT_CONFIG_GLOBAL" not in pinned


def test_pinned_env_refuses_a_worktree_without_an_admin_dir(repo: Path) -> None:
    env = create(repo, "run-1", spec())
    shutil.rmtree(repo / ".git" / "worktrees" / "run-1")
    with pytest.raises(WorkspaceError, match="refusing to run git"):
        pinned_env(env)


def test_srt_denies_writes_to_the_worktree_git_pointers(repo: Path, tmp_path: Path) -> None:
    env = create(repo, "run-1", spec())
    settings = build_settings(spec(), run_id="run-1", worktree=env.path, git_dir=repo / ".git",
                              tmp_dir=tmp_path / "t", home=tmp_path / "home")  # fmt: skip
    denied = settings["filesystem"]["denyWrite"]  # type: ignore[index]
    admin = repo / ".git" / "worktrees" / "run-1"
    assert str(env.path / ".git") in denied
    assert str(admin / "commondir") in denied and str(admin / "gitdir") in denied
