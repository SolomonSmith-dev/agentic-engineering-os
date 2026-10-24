"""Environment allocator (build-order step 5): worktrees, .worktreeinclude, ports, lock, sweep.

Every test works in a throwaway git repository under tmp_path. Sandbox
behavior under a real srt lives in tests/test_sandbox.py.
"""

import json
import multiprocessing
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_team.cli import app
from agent_team.environments import (
    AllocatorError,
    LockHeldError,
    NoFreeSlotError,
    acquire,
    create,
    dispose,
    list_worktrees,
    live_run_id,
    read_lock,
    release,
    sweep,
)
from agent_team.environments import ports
from agent_team.environments.include import matching_paths
from agent_team.runs import RunStore, RunStoreError
from agent_team.schemas import EnvironmentSpec

runner = CliRunner()


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def spec(tier: str = "worktree", block_size: int = 100) -> EnvironmentSpec:
    return EnvironmentSpec.model_validate(
        {
            "kind": "environment",
            "name": "test",
            "description": "test environment",
            "tier": tier,
            "network": {"allowed_domains": ["github.com"]},
            "ports": {"block_size": block_size},
        }
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "commit.gpgsign", "false")  # host git config may sign through a service the sandbox blocks
    (root / ".gitignore").write_text(".agent-team/\n.env\nsecrets/\nbuild/\n", encoding="utf-8")
    (root / "app.py").write_text("print('hello')\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    return root.resolve()


def state(repo: Path) -> Path:
    return repo / ".agent-team"


def branches(repo: Path) -> list[str]:
    return git(repo, "branch", "--format=%(refname:short)").splitlines()


# --- create -------------------------------------------------------------------


def test_create_makes_worktree_on_own_branch_at_head(repo: Path) -> None:
    env = create(repo, "run-1", spec())
    assert env.path == state(repo) / "worktrees" / "run-1"
    assert (env.path / "app.py").read_text() == "print('hello')\n"
    assert git(env.path, "rev-parse", "--abbrev-ref", "HEAD") == "agent-team/run-1"
    assert env.meta.base_commit == git(repo, "rev-parse", "HEAD")
    assert "agent-team/run-1" in branches(repo)
    assert git(repo, "status", "--porcelain") == "", "the main checkout must stay clean"


def test_create_refuses_when_state_dir_not_ignored(repo: Path) -> None:
    (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
    git(repo, "commit", "-qam", "unignore")
    with pytest.raises(RunStoreError, match="not gitignored"):
        create(repo, "run-1", spec())
    assert not state(repo).exists()


def test_create_refuses_repo_without_commits(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text(".agent-team/\n", encoding="utf-8")
    with pytest.raises(AllocatorError, match="no commits"):
        create(root, "run-1", spec())


def test_create_rejects_duplicate_and_unsafe_run_ids(repo: Path) -> None:
    create(repo, "run-1", spec())
    with pytest.raises(AllocatorError, match="already has a worktree"):
        create(repo, "run-1", spec())
    for bad in ("../escape", "a/b", "-flag", "has space", "x" * 65):
        with pytest.raises(AllocatorError, match="must match"):
            create(repo, bad, spec())


def test_create_records_worktree_path_in_run_record(repo: Path) -> None:
    store = RunStore.open(repo)
    assert store is not None
    with store:
        run = store.start_run("bug-fix")
        env = create(repo, run.run_id, spec(), store=store)
        assert store.get_run(run.run_id).worktree_path == str(env.path)


def test_worktree_only_tier_caps_autonomy_at_a2(repo: Path) -> None:
    env = create(repo, "run-1", spec("worktree"), requested_autonomy="A3")
    assert not env.sandbox.sandboxed
    assert env.autonomy_ceiling == "A2"
    assert env.sandbox.wrap(["pytest", "-q"]) == ["pytest", "-q"]
    assert "worktree only" in env.note("test") and "autonomy ceiling A2" in env.note("test")


def test_srt_tier_without_srt_falls_back_and_caps(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_team.environments.sandbox.srt_path", lambda: None)
    env = create(repo, "run-1", spec("srt"), requested_autonomy="A3")
    assert not env.sandbox.sandboxed
    assert env.autonomy_ceiling == "A2"
    assert not (state(repo) / "sandbox" / "run-1.json").exists()


def test_srt_tier_with_srt_keeps_requested_autonomy(repo: Path, tmp_path: Path) -> None:
    env = create(repo, "run-1", spec("srt"), requested_autonomy="A3", srt="/usr/local/bin/srt", home=tmp_path)
    assert env.sandbox.sandboxed
    assert env.autonomy_ceiling == "A3"
    settings = state(repo) / "sandbox" / "run-1.json"
    tmp = state(repo) / "tmp" / "run-1"
    assert env.sandbox.wrap(["pytest"]) == [
        "/usr/local/bin/srt", "--settings", str(settings), "--", "env", f"TMPDIR={tmp}", "pytest"
    ]
    assert "TMPDIR" not in env.sandbox.env({}), "srt itself keeps the caller's TMPDIR"
    assert not settings.is_relative_to(env.path), "the sandbox must not be able to edit its own rules"


def test_sandbox_env_sets_tmpdir_and_port_offset(repo: Path) -> None:
    env = create(repo, "run-1", spec())
    out = env.sandbox.env({"PATH": "/usr/bin"})
    assert out["PATH"] == "/usr/bin"
    assert out["TMPDIR"] == str(state(repo) / "tmp" / "run-1")
    assert Path(out["TMPDIR"]).is_dir()
    assert out["AGENT_TEAM_PORT_OFFSET"] == "100"


# --- .worktreeinclude -----------------------------------------------------------


def test_worktreeinclude_copies_only_ignored_matches(repo: Path) -> None:
    (repo / ".env").write_text("TOKEN=not-a-real-value\n", encoding="utf-8")
    (repo / "secrets").mkdir()
    (repo / "secrets" / "dev.json").write_text("{}\n", encoding="utf-8")
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_text("artifact\n", encoding="utf-8")
    (repo / "notes.txt").write_text("untracked, not ignored\n", encoding="utf-8")
    (repo / ".worktreeinclude").write_text(".env\nsecrets/\nnotes.txt\napp.py\n.agent-team/\n", encoding="utf-8")
    store = RunStore.open(repo)
    assert store is not None
    store.close()

    env = create(repo, "run-1", spec())
    assert env.includes_copied == [".env", "secrets/dev.json"]
    assert (env.path / ".env").read_text() == "TOKEN=not-a-real-value\n"
    assert not (env.path / "build").exists(), "ignored but not listed"
    assert not (env.path / "notes.txt").exists(), "listed but not ignored"
    assert not (env.path / ".agent-team").exists(), "state never enters a worktree"
    assert git(env.path, "status", "--porcelain") == "", "included files stay ignored in the worktree"


def test_worktreeinclude_skips_symlinks(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("host file\n", encoding="utf-8")
    os.symlink(outside, repo / ".env")
    (repo / ".worktreeinclude").write_text(".env\n", encoding="utf-8")
    env = create(repo, "run-1", spec())
    assert env.includes_copied == []
    assert env.includes_skipped == [".env"]
    assert not (env.path / ".env").exists()


def test_no_worktreeinclude_copies_nothing(repo: Path) -> None:
    (repo / ".env").write_text("X=1\n", encoding="utf-8")
    assert matching_paths(repo, ".agent-team") == []
    assert create(repo, "run-1", spec()).includes_copied == []


# --- ports --------------------------------------------------------------------


def test_each_worktree_gets_lowest_free_port_slot(repo: Path) -> None:
    a = create(repo, "run-a", spec())
    b = create(repo, "run-b", spec())
    assert (a.sandbox.port_offset, b.sandbox.port_offset) == (100, 200)
    dispose(repo, "run-a")
    c = create(repo, "run-c", spec())
    assert c.sandbox.port_offset == 100, "a freed slot is reused"


def test_port_slots_are_bounded() -> None:
    assert ports.slot_limit(1000) == 60
    assert ports.next_slot(range(1, 5), 100) == 5
    with pytest.raises(NoFreeSlotError, match="env sweep"):
        ports.next_slot(range(1, 61), 1000)


# --- dispose ------------------------------------------------------------------


def test_dispose_clean_worktree_removes_everything(repo: Path) -> None:
    env = create(repo, "run-1", spec())
    assert dispose(repo, "run-1") is None
    assert not env.path.exists()
    assert "agent-team/run-1" not in branches(repo)
    assert list_worktrees(repo) == []
    assert not (state(repo) / "tmp" / "run-1").exists()
    assert "run-1" not in git(repo, "worktree", "list")


def test_dispose_archives_uncommitted_and_committed_work(repo: Path) -> None:
    env = create(repo, "run-1", spec())
    (env.path / "app.py").write_text("print('fixed')\n", encoding="utf-8")
    git(env.path, "commit", "-qam", "fix")
    (env.path / "new_test.py").write_text("def test_x(): pass\n", encoding="utf-8")

    archive = dispose(repo, "run-1")
    assert archive == state(repo) / "archive" / "run-1.patch"
    patch = archive.read_text()
    assert "print('fixed')" in patch and "new_test.py" in patch
    assert not env.path.exists()

    git(repo, "apply", "--check", str(archive))  # the patch applies cleanly to the base


def test_dispose_unknown_run_fails(repo: Path) -> None:
    with pytest.raises(AllocatorError, match="has no worktree"):
        dispose(repo, "nope")


# --- lock ---------------------------------------------------------------------


def test_lock_is_exclusive_while_holder_lives(repo: Path) -> None:
    assert acquire(state(repo), "run-1") is None
    with pytest.raises(LockHeldError, match="run-1"):
        acquire(state(repo), "run-2")
    assert live_run_id(state(repo)) == "run-1"
    assert release(state(repo), "run-2") is False, "only the holder releases"
    assert release(state(repo), "run-1") is True
    assert read_lock(state(repo)) is None


def test_dead_pid_lock_is_taken_over_and_reported(repo: Path) -> None:
    dead = subprocess.Popen(["true"])
    dead.wait()
    acquire(state(repo), "run-old", pid=dead.pid)
    assert live_run_id(state(repo)) is None
    replaced = acquire(state(repo), "run-new")
    assert replaced is not None and replaced.run_id == "run-old" and replaced.pid == dead.pid
    assert live_run_id(state(repo)) == "run-new"


def _race_for_lock(state_dir: str, run_id: str, holder_pid: int, start, results) -> None:  # noqa: ANN001
    start.wait()
    try:
        acquire(Path(state_dir), run_id, pid=holder_pid)
        results.put(run_id)
    except LockHeldError:
        results.put(None)


def test_stale_lock_takeover_has_one_winner(repo: Path) -> None:
    dead = subprocess.Popen(["true"])
    dead.wait()
    acquire(state(repo), "run-old", pid=dead.pid)
    ctx = multiprocessing.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    racers = [
        ctx.Process(target=_race_for_lock, args=(str(state(repo)), f"run-{i}", os.getpid(), start, results))
        for i in range(6)
    ]
    for proc in racers:
        proc.start()
    start.set()
    for proc in racers:
        proc.join(timeout=30)
    winners = [w for w in (results.get(timeout=5) for _ in racers) if w is not None]
    assert len(winners) == 1
    assert live_run_id(state(repo)) == winners[0]


def test_lock_from_another_host_is_never_taken_over(repo: Path) -> None:
    state(repo).mkdir()
    (state(repo) / "lock").write_text(
        json.dumps({"run_id": "remote", "pid": 1, "host": "some-other-host", "acquired_at": ""}), encoding="utf-8"
    )
    with pytest.raises(LockHeldError, match="some-other-host"):
        acquire(state(repo), "run-1")


def test_unreadable_lock_is_treated_as_stale(repo: Path) -> None:
    state(repo).mkdir()
    (state(repo) / "lock").write_text("not json", encoding="utf-8")
    replaced = acquire(state(repo), "run-1")
    assert replaced is not None and replaced.run_id == "<unreadable>"


# --- sweep --------------------------------------------------------------------


def test_sweep_disposes_ended_and_orphaned_keeps_live_and_unfinished(repo: Path) -> None:
    store = RunStore.open(repo)
    assert store is not None
    with store:
        ended = store.start_run("bug-fix").run_id
        unfinished = store.start_run("bug-fix").run_id
        live = store.start_run("bug-fix").run_id
        for run_id in (ended, unfinished, live):
            create(repo, run_id, spec(), store=store)
        store.finish_run(ended, "abandoned")
    create(repo, "orphan", spec())
    acquire(state(repo), live)

    results = {r.run_id: r for r in sweep(repo)}
    assert results[ended].action == "disposed" and "run ended (abandoned)" in results[ended].reason
    assert results["orphan"].action == "disposed" and results["orphan"].reason == "no run record"
    assert results[unfinished].action == "kept" and "resume" in results[unfinished].reason
    assert results[live].action == "kept" and "lock" in results[live].reason
    assert {m.run_id for m in list_worktrees(repo)} == {unfinished, live}


def test_sweep_cleans_worktree_dir_without_metadata(repo: Path) -> None:
    env = create(repo, "run-1", spec())
    (state(repo) / "meta" / "run-1.json").unlink()
    (env.path / "wip.txt").write_text("work in progress\n", encoding="utf-8")
    [result] = sweep(repo)
    assert result.action == "disposed" and result.reason == "no allocator metadata"
    assert result.archive is not None and "wip.txt" in Path(result.archive).read_text()
    assert not env.path.exists()
    assert "agent-team/run-1" not in branches(repo)


def test_missing_metadata_never_drops_committed_work(repo: Path) -> None:
    env = create(repo, "run-1", spec())
    (env.path / "feature.py").write_text("def feature(): return 1\n", encoding="utf-8")
    git(env.path, "add", "feature.py")
    git(env.path, "commit", "-qm", "feature")
    work = git(env.path, "rev-parse", "HEAD")
    (state(repo) / "meta" / "run-1.json").unlink()

    [result] = sweep(repo)
    assert result.action == "disposed" and "branch agent-team/run-1 kept" in result.reason
    assert not env.path.exists()
    assert git(repo, "rev-parse", "agent-team/run-1") == work, "the commit stays reachable"


def test_missing_metadata_still_deletes_a_branch_with_nothing_unique(repo: Path) -> None:
    env = create(repo, "run-1", spec())
    (state(repo) / "meta" / "run-1.json").unlink()
    assert dispose(repo, "run-1") is None
    assert not env.path.exists()
    assert "agent-team/run-1" not in branches(repo)


def test_env_sweep_command(repo: Path) -> None:
    result = runner.invoke(app, ["env", "sweep", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "no worktrees"

    create(repo, "orphan", spec())
    result = runner.invoke(app, ["env", "sweep", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert result.output.split() == ["disposed", "orphan", "no", "run", "record"]


def test_env_sweep_refuses_unignored_state_dir(repo: Path) -> None:
    (repo / ".gitignore").write_text("", encoding="utf-8")
    result = runner.invoke(app, ["env", "sweep", "--repo", str(repo)])
    assert result.exit_code == 2
    assert "not gitignored" in result.output
