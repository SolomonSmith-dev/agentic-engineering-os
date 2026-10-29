"""Edges of the orchestrator (build-order steps 10 and 11): the PR step's refusals, the TEST step's
guards, agent-node failures, and state-machine refusals.

The PR and TEST steps are called directly on a `DefaultExecutor` with hand-built
evidence, against the throwaway repository and fake `gh` from test_implementer.py.
Tests that need the run's ceiling at A3 need srt (skipped without it, failed
instead when AGENT_TEAM_REQUIRE_SRT=1). No test calls a model or opens a real PR.
"""

import dataclasses
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_implementer import (  # noqa: F401  (fixtures are used by name)
    World,
    git,
    repo,
    srt,
    world,
)
from test_memory_edges import FAKE_SRT
from typer.testing import CliRunner

from agent_team import cli
from agent_team.cli import app
from agent_team.environments import acquire, read_lock, release
from agent_team.environments.lock import LockHeldError
from agent_team.environments.sandbox import Sandbox
from agent_team.environments.worktree import AllocatorError
from agent_team.orchestrator import (
    DefaultExecutor,
    NodeInput,
    OrchestratorError,
    load_dag,
    resume_workflow,
    run_workflow,
    start,
    walk,
)
from agent_team.orchestrator import executors as executors_mod
from agent_team.orchestrator.executors import (
    agent_evidence,
    agent_prompt,
    human_decision,
)
from agent_team.orchestrator.machine import Context, reenter
from agent_team.orchestrator.workspace import (
    WorkspaceError,
    ensure_run_branch,
    read_artifact,
    run_command,
    write_artifact,
)
from agent_team.run_agent import load_canonical
from agent_team.runs import RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL = REPO_ROOT / "canonical"
RUN = "20261024-090000-abcdef"
TASK = "Fix the value."


def canonical_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "canonical-copy" / "canonical"
    shutil.copytree(CANONICAL, copy)
    shutil.copytree(CANONICAL.parent / "routing", copy.parent / "routing")
    return copy


def edit_yaml(path: Path, **changes: object) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def executor(repo: Path, canonical_dir: Path = CANONICAL, gh: str | None = None, **kwargs) -> DefaultExecutor:  # noqa: ANN003
    return DefaultExecutor(repo=repo, canonical_dir=canonical_dir, canonical=load_canonical(canonical_dir), claude="claude",
                           max_budget_usd=1.0, timeout_s=60, gh=gh, echo=lambda _: None, **kwargs)  # fmt: skip


def at_pr(head: str, *, verdict: str = "approve", escalated: bool = False, **inputs: object) -> NodeInput:
    evidence = {"diff": {"head": head, "files": ["value.txt"]}, "test_output": {"head": head}, "review": {"head": head},
                "reviewer_verdict": verdict, "tests_pass": True, "findings": [], **inputs}  # fmt: skip
    return NodeInput(run_id=RUN, workflow="bug-fix", state="PR", attempt=1, task=TASK, inputs=evidence, escalated=escalated)


def pr_reason(result) -> str:  # noqa: ANN001
    pr = result.evidence["pr"]
    assert pr["status"] == "not_opened" and result.escalate and result.blocked is None
    return pr["reason"]


@pytest.fixture
def no_srt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_team.environments.sandbox.srt_path", lambda: None)


# --- PR: github.pr.create stays behind its checks ---------------------------------------


def test_pr_refuses_when_policy_denies_the_capability(repo: Path, world: World, tmp_path: Path) -> None:
    copy = canonical_copy(tmp_path)
    edit_yaml(copy / "policies" / "default.yaml", denied_capabilities=["git.push.main", "fs.write.home", "github.pr.*"])
    result = executor(repo, copy, gh=str(world.bin / "gh"))(load_dag(load_canonical(copy), "bug-fix", "default").node("PR"),
                                                             at_pr(git(repo, "rev-parse", "HEAD")))  # fmt: skip
    assert "policy default denies github.pr.create" in pr_reason(result)
    assert world.gh_calls() == [] and not (repo / ".agent-team" / "artifacts" / RUN / "pr-body.md").exists()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("delete", "no canonical cli tool implements github.pr.create"),
        ("gh pr create --fill", "the github.pr.create tool must be 'gh pr create --draft ...'"),
        ("gh pr merge --draft", "the github.pr.create tool must be 'gh pr create --draft ...'"),
        ("curl --draft https://api.github.com", "the github.pr.create tool must be 'gh pr create --draft ...'"),
    ],
)
def test_pr_refuses_a_tool_that_is_not_gh_pr_create_draft(repo: Path, world: World, tmp_path: Path, change: str, message: str) -> None:
    copy = canonical_copy(tmp_path)
    tool = copy / "tools" / "gh-pr-create.yaml"
    if change == "delete":
        tool.unlink()
    else:
        edit_yaml(tool, implementation={"type": "cli", "command": change})
    ex = executor(repo, copy, gh=str(world.bin / "gh"))
    result = ex(load_dag(ex.canonical, "bug-fix", "default").node("PR"), at_pr(git(repo, "rev-parse", "HEAD")))
    assert message in pr_reason(result)
    assert world.gh_calls() == []


def test_pr_without_srt_is_capped_below_a3(repo: Path, world: World, no_srt) -> None:
    ex = executor(repo, gh=str(world.bin / "gh"))
    result = ex(load_dag(ex.canonical, "bug-fix", "default").node("PR"), at_pr(git(repo, "rev-parse", "HEAD")))
    assert "github.pr.create needs A3; this run's ceiling is A2 (srt is absent)" in pr_reason(result)
    assert world.gh_calls() == []


def test_pr_refuses_when_the_approved_ceiling_is_below_a3(repo: Path, world: World, srt: str) -> None:
    config = {"version": 1, "default_branch": "main", "protected_branches": ["main"], "runtimes": ["claude"],
              "privacy_classes": ["vendor-cloud"], "autonomy_ceiling": "A2", "draft_prs": True, "commands": {}}  # fmt: skip
    (repo / ".agent-team").mkdir(exist_ok=True)
    (repo / ".agent-team" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    ex = executor(repo, gh=str(world.bin / "gh"))
    result = ex(load_dag(ex.canonical, "bug-fix", "default").node("PR"), at_pr(git(repo, "rev-parse", "HEAD")))
    reason = pr_reason(result)
    assert "github.pr.create needs A3; this run's ceiling is A2" in reason and "srt is absent" not in reason
    assert world.gh_calls() == []


@pytest.mark.parametrize("which", ["test_output", "review"])
def test_pr_refuses_when_the_tested_or_reviewed_commit_differs(repo: Path, world: World, which: str) -> None:
    head = git(repo, "rev-parse", "HEAD")
    ex = executor(repo, gh=str(world.bin / "gh"))
    result = ex(load_dag(ex.canonical, "bug-fix", "default").node("PR"), at_pr(head, **{which: {"head": "f" * 40}}))
    assert pr_reason(result) == "the diffed, tested and reviewed commits are not the same commit"
    assert world.gh_calls() == [] and not (repo / ".agent-team" / "worktrees" / RUN).exists()


def test_a_changes_verdict_that_was_not_escalated_opens_no_pr(repo: Path, world: World, srt: str) -> None:
    ex = executor(repo, gh=str(world.bin / "gh"))
    result = ex(load_dag(ex.canonical, "bug-fix", "default").node("PR"), at_pr(git(repo, "rev-parse", "HEAD"), verdict="changes"))
    assert pr_reason(result) == "required gate(s) did not pass: reviewer_verdict == approve (got 'changes')"
    assert world.gh_calls() == []


def test_failing_tests_open_no_pr_even_with_an_approval(repo: Path, world: World, srt: str) -> None:
    ex = executor(repo, gh=str(world.bin / "gh"))
    result = ex(load_dag(ex.canonical, "bug-fix", "default").node("PR"), at_pr(git(repo, "rev-parse", "HEAD"), tests_pass=False))
    assert "required gate(s) did not pass: tests_pass (got False)" in pr_reason(result)
    assert world.gh_calls() == []


def test_pr_without_an_origin_remote_keeps_the_branch(repo: Path, world: World, srt: str) -> None:
    git(repo, "remote", "remove", "origin")
    ex = executor(repo, gh=str(world.bin / "gh"))
    result = ex(load_dag(ex.canonical, "bug-fix", "default").node("PR"), at_pr(git(repo, "rev-parse", "HEAD")))
    assert pr_reason(result) == "the repository has no 'origin' remote to push to"
    assert result.evidence["pr"]["pushed"] is False
    assert world.pr_creates() == []


def test_a_failed_push_opens_no_pr(repo: Path, world: World, srt: str, tmp_path: Path) -> None:
    git(repo, "remote", "set-url", "origin", str(tmp_path / "no-such-origin.git"))
    ex = executor(repo, gh=str(world.bin / "gh"))
    result = ex(load_dag(ex.canonical, "bug-fix", "default").node("PR"), at_pr(git(repo, "rev-parse", "HEAD")))
    assert pr_reason(result).startswith(f"git push origin agent-team/{RUN} exited ")
    assert result.evidence["pr"]["pushed"] is False and world.pr_creates() == []


# --- TEST: checks run at exactly the diffed commit -----------------------------------------


def at_test(diff: object, reproduction: str | None = None) -> NodeInput:
    inputs = {"diff": diff} if diff is not None else {}
    if reproduction:
        inputs["reproduction_command"] = reproduction
    return NodeInput(run_id=RUN, workflow="bug-fix", state="TEST", attempt=1, task=TASK, inputs=inputs)


def test_test_step_needs_a_diff_head(repo: Path) -> None:
    ex = executor(repo)
    node = load_dag(ex.canonical, "bug-fix", "default").node("TEST")
    for diff in (None, {"files": []}, "not a mapping"):
        assert ex(node, at_test(diff)).blocked == "TEST needs the diff and the commit it ends at"


def test_test_step_refuses_a_worktree_at_another_commit_or_dirty(repo: Path) -> None:
    ex = executor(repo)
    node = load_dag(ex.canonical, "bug-fix", "default").node("TEST")
    result = ex(node, at_test({"head": "f" * 40}))
    assert result.blocked and result.blocked.startswith("the worktree is at ") and "not the diffed ffffffffffff" in result.blocked
    worktree = repo / ".agent-team" / "worktrees" / RUN
    (worktree / "value.txt").write_text("edited outside a commit\n", encoding="utf-8")
    result = ex(node, at_test({"head": git(worktree, "rev-parse", "HEAD")}))
    assert result.blocked == "the worktree has uncommitted changes the diff does not show"


def test_a_check_that_commits_fails_the_gate_and_is_rolled_back(repo: Path, srt: str) -> None:
    ex = executor(repo, test_commands=("echo moved >> value.txt && git commit -qam moved",))
    node = load_dag(ex.canonical, "bug-fix", "default").node("TEST")
    worktree = repo / ".agent-team" / "worktrees" / RUN
    first = ex(node, at_test({"head": "pending"}))  # creates the worktree
    assert first.blocked and "not the diffed" in first.blocked
    head = git(worktree, "rev-parse", "HEAD")
    result = ex(node, at_test({"head": head}))
    assert result.blocked is None
    assert result.evidence["tests_pass"] is False, "a command that moves the branch never passes the gate"
    assert "the commands changed the worktree; restored it to" in (result.note or "")
    assert git(worktree, "rev-parse", "HEAD") == head and git(worktree, "status", "--porcelain") == ""


def test_a_check_that_only_dirties_the_tree_is_restored(repo: Path, srt: str) -> None:
    ex = executor(repo, test_commands=("echo dirty >> value.txt && touch new-file",))
    node = load_dag(ex.canonical, "bug-fix", "default").node("TEST")
    ex(node, at_test({"head": "pending"}))
    worktree = repo / ".agent-team" / "worktrees" / RUN
    head = git(worktree, "rev-parse", "HEAD")
    result = ex(node, at_test({"head": head}))
    assert "restored it to" in (result.note or "")
    assert git(worktree, "status", "--porcelain") == "" and not (worktree / "new-file").exists()


# --- agent nodes ----------------------------------------------------------------------


def test_an_artifact_outside_the_artifacts_dir_is_never_inlined(repo: Path) -> None:
    ex = executor(repo)
    (repo / ".agent-team").mkdir(exist_ok=True)
    (repo / "secret.txt").write_text("TOP-SECRET-CANARY\n", encoding="utf-8")
    given = NodeInput(run_id=RUN, workflow="bug-fix", state="CODE_REVIEW", attempt=1, task=TASK,
                      inputs={"diff": {"path": "secret.txt", "sha256": "x", "head": "a" * 40}})  # fmt: skip
    result = ex(load_dag(ex.canonical, "bug-fix", "default").node("CODE_REVIEW"), given)
    assert result.blocked and "is not under .agent-team/artifacts/" in result.blocked
    assert RunStore.open(repo, create=False) is None, "no child run was started"


def test_an_old_claude_blocks_the_node_and_the_lock_comes_back(repo: Path, tmp_path: Path) -> None:
    old = tmp_path / "old-claude"
    old.write_text(f"#!{sys.executable}\nprint('1.0.0 (Claude Code)')\n", encoding="utf-8")
    old.chmod(0o755)
    ex = dataclasses.replace(executor(repo), claude=str(old))
    state_dir = repo / ".agent-team"
    acquire(state_dir, RUN)
    try:
        given = NodeInput(run_id=RUN, workflow="bug-fix", state="CODE_REVIEW", attempt=1, task=TASK, inputs={})
        result = ex(load_dag(ex.canonical, "bug-fix", "default").node("CODE_REVIEW"), given)
        assert result.blocked and result.blocked.startswith("agent independent-reviewer: claude 1.0.0 is older than")
        lock = read_lock(state_dir)
        assert lock is not None and lock.run_id == RUN, "the workflow takes its lock back"
    finally:
        release(state_dir, RUN)


def implement(ex: DefaultExecutor, attempt: int):  # noqa: ANN201
    given = NodeInput(run_id=RUN, workflow="bug-fix", state="IMPLEMENTATION", attempt=attempt, task=TASK,
                      inputs={"reproduction_command": "sh check.sh"})  # fmt: skip
    return ex(load_dag(ex.canonical, "bug-fix", "default").node("IMPLEMENTATION"), given)


def test_an_implementer_that_commits_nothing_blocks(repo: Path, world: World, no_srt) -> None:
    world.play(implement=["fix-commit", "fix-uncommitted"], reviews=[])
    ex = dataclasses.replace(executor(repo), claude=str(world.bin / "claude"))
    first = implement(ex, 1)
    assert first.blocked is None and first.evidence["diff"]["files"] == ["value.txt"]
    second = implement(ex, 2)  # rewrites value.txt with what it already holds
    assert second.blocked == "the implementer committed nothing in this attempt"
    assert "diff" not in second.evidence


def test_an_implementer_that_crashes_blocks_with_its_cost(repo: Path, world: World, no_srt) -> None:
    world.play(implement=[], reviews=[])  # the fake has no action for attempt 1 and exits non-zero
    ex = dataclasses.replace(executor(repo), claude=str(world.bin / "claude"))
    result = implement(ex, 1)
    assert result.blocked == "agent implementer did not succeed"
    assert result.note and result.note.startswith("agent implementer run ") and result.evidence == {}
    with RunStore.open(repo, create=False) as store:
        [child] = store.list_runs()
        assert child.outcome == "failed"


SCRIPTED_CLAUDE = r'''#!{python}
"""Fake implementer: runs the scenario's shell script in its worktree, then reports the scenario's output."""
import json, subprocess, sys

if sys.argv[1:] == ["--version"]:
    print("2.1.310 (Claude Code)")
    sys.exit(0)
sys.stdin.read()
scenario = json.load(open({scenario!r}))
subprocess.run(["/bin/sh", "-c", scenario["script"]], check=True)
for event in [{{"type": "system", "subtype": "init"}},
              {{"type": "result", "subtype": "success", "is_error": False, "result": "done", "total_cost_usd": 0.1,
                "num_turns": 1, "session_id": "fake", "structured_output": scenario["out"]}}]:
    print(json.dumps(event), flush=True)
'''


@pytest.fixture
def scripted(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An executor whose implementer runs `script` and reports `out`."""
    monkeypatch.setattr("agent_team.environments.sandbox.srt_path", lambda: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    scenario = tmp_path / "scripted.json"
    fake = tmp_path / "scripted-bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(SCRIPTED_CLAUDE.format(python=sys.executable, scenario=str(scenario)), encoding="utf-8")
    fake.chmod(0o755)

    def play(script: str, **out: object) -> DefaultExecutor:
        report = {"reproduction_command": "sh check.sh", "reproduction_fails": False, "changes": [], **out}
        scenario.write_text(json.dumps({"script": script, "out": report}), encoding="utf-8")
        return dataclasses.replace(executor(repo), claude=str(fake))

    return play


def discover(ex: DefaultExecutor):  # noqa: ANN201
    given = NodeInput(run_id=RUN, workflow="bug-fix", state="DISCOVERY", attempt=1, task=TASK, inputs={})
    return ex(load_dag(ex.canonical, "bug-fix", "default").node("DISCOVERY"), given)


def test_a_blank_reproduction_command_blocks_discovery(scripted) -> None:
    result = discover(scripted("true", reproduction_command="   ", reproduction_fails=True))
    assert result.blocked == "the implementer returned no reproduction command"
    assert "reproduction_fails" not in result.evidence, "nothing is taken on the implementer's word"


def test_an_implementer_that_leaves_the_run_branch_blocks(scripted, repo: Path) -> None:
    result = implement(scripted("git checkout -q -b elsewhere && echo fixed > value.txt && git commit -qam elsewhere"), 1)
    assert result.blocked and f"the worktree is not on agent-team/{RUN} any more" in result.blocked
    assert result.note and result.note.startswith("agent implementer run "), "the child run is still named"
    assert "diff" not in result.evidence


def test_commits_that_cancel_out_change_nothing_and_block(scripted) -> None:
    script = "echo fixed > value.txt && git commit -qam fix && git revert --no-edit HEAD >/dev/null"
    result = implement(scripted(script), 1)
    assert result.blocked and result.blocked.startswith("the run branch changes nothing against its base ")


def test_absolute_claimed_paths_are_made_relative_to_the_worktree(scripted, repo: Path, tmp_path: Path) -> None:
    worktree = repo / ".agent-team" / "worktrees" / RUN
    changes = [{"file": str(worktree / "value.txt"), "summary": "fix"},
               {"file": str(tmp_path / "elsewhere.txt"), "summary": "outside the worktree"}]  # fmt: skip
    result = implement(scripted("echo fixed > value.txt && git commit -qam fix", changes=changes), 1)
    assert result.blocked is None
    claimed = result.evidence["claimed_changes"]
    assert [c["file"] for c in claimed["changes"]] == ["value.txt", str(tmp_path / "elsewhere.txt")]
    assert claimed["unclaimed"] == [], "the absolute claim covers value.txt"


def test_an_unknown_in_process_step_blocks(repo: Path) -> None:
    ex = executor(repo)
    node = dataclasses.replace(load_dag(ex.canonical, "bug-fix", "default").node("INTAKE"), step="deploy")
    given = NodeInput(run_id=RUN, workflow="bug-fix", state="INTAKE", attempt=1, task=TASK, inputs={})
    assert ex(node, given).blocked == "in-process step 'deploy' is not implemented yet"


def test_an_unreadable_config_yaml_is_reported_before_the_run(repo: Path) -> None:
    (repo / ".agent-team").mkdir(exist_ok=True)
    (repo / ".agent-team" / "config.yaml").write_text("autonomy_ceiling: [\n", encoding="utf-8")
    ex = executor(repo)
    [problem] = ex.unavailable(load_dag(ex.canonical, "bug-fix", "default"))
    assert "config.yaml is unreadable (ParserError); re-run 'agent-team configure'" in problem
    (repo / ".agent-team" / "config.yaml").write_text("autonomy_ceiling: A9\n", encoding="utf-8")
    [problem] = executor(repo).unavailable(load_dag(ex.canonical, "bug-fix", "default"))
    assert "config.yaml is unreadable (ValidationError)" in problem


def test_agent_evidence_mapping() -> None:
    assert agent_evidence(None, None) == {} and agent_evidence(None, "text") == {"result": "text"}
    assert agent_evidence({"verdict": "changes"}, None) == {"reviewer_verdict": "changes", "findings": []}
    assert agent_evidence({"reproduction_command": "x"}, "ignored") == {"reproduction_command": "x"}


def test_agent_prompt_cuts_a_large_artifact(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executors_mod, "MAX_INLINE_CHARS", 10)
    ref = write_artifact(repo, RUN, "diff-1.patch", "0123456789abcdef")
    given = NodeInput(run_id=RUN, workflow="bug-fix", state="CODE_REVIEW", attempt=2, task=TASK, inputs={"diff": ref})
    prompt = agent_prompt(given, repo)
    assert prompt.startswith("Workflow bug-fix, state CODE_REVIEW, attempt 2.")
    assert "0123456789\n" in prompt and "abcdef" not in prompt
    assert "[cut at 10 characters of 16 bytes; read the rest with git]" in prompt
    assert ref["sha256"] not in prompt, "the reference's path and hash are not repeated"


def test_human_decision_notes() -> None:
    def note(pr: object = None, escalated: bool = False) -> str | None:
        inputs = {"pr": pr} if pr is not None else {}
        return human_decision(NodeInput(run_id=RUN, workflow="bug-fix", state="HUMAN_DECISION", attempt=1, task=TASK,
                                        inputs=inputs, escalated=escalated)).note  # fmt: skip

    assert note({"status": "opened", "url": "u"}) == "awaiting human review of draft PR u"
    assert note({"status": "opened", "url": "u", "blocked": True}) == "escalated: draft PR u is marked blocked"
    assert note({"status": "not_opened", "reason": "r", "branch": "b", "head": "a" * 40}) == "no PR: r; branch b kept at aaaaaaaaaaaa"
    assert note() == "awaiting human review" and note(escalated=True) == "escalated: the draft PR is marked blocked"


# --- the state machine and the run wrapper ------------------------------------------------


@pytest.mark.parametrize("data", [{}, {"task": "t", "attempts": {"PLAN": "two"}}, {"task": "t", "evidence": 3}])
def test_an_unreadable_checkpoint_is_refused(data: dict) -> None:
    with pytest.raises(OrchestratorError, match="checkpoint is unreadable"):
        Context.load(data)


def test_reenter_refuses_another_workflow_and_a_state_the_dag_lost(repo: Path) -> None:
    dag = load_dag(load_canonical(CANONICAL), "bug-fix", "default")
    with RunStore.open(repo) as store:
        run = start(store, dag, TASK, echo=lambda _: None)
        with pytest.raises(OrchestratorError, match="is a bug-fix run, not feature"):
            reenter(store, dataclasses.replace(dag, workflow="feature"), run.run_id, echo=lambda _: None)
        smaller = dataclasses.replace(dag, nodes=tuple(n for n in dag.nodes if n.state != "INTAKE"))
        with pytest.raises(OrchestratorError, match="stopped in INTAKE, which workflow bug-fix no longer has"):
            reenter(store, smaller, run.run_id, echo=lambda _: None)
        assert [t.to_state for t in store.transitions(run.run_id)] == ["INTAKE"], "nothing recorded"


def test_walk_refuses_a_run_that_left_its_checkpoint(repo: Path) -> None:
    dag = load_dag(load_canonical(CANONICAL), "bug-fix", "default")
    with RunStore.open(repo) as store:
        run = start(store, dag, TASK, echo=lambda _: None)
        store.transition(run.run_id, "DISCOVERY", note="moved without a checkpoint")
        with pytest.raises(OrchestratorError, match="is not at a checkpoint; use resume"):
            walk(store, dag, lambda node, given: None, run.run_id, echo=lambda _: None)  # type: ignore[arg-type,return-value]


def test_a_live_lock_blocks_a_new_workflow_run(repo: Path) -> None:
    state_dir = repo / ".agent-team"
    state_dir.mkdir(exist_ok=True)
    acquire(state_dir, "20261024-080000-000000", pid=os.getpid())
    try:
        with pytest.raises(LockHeldError):
            run_workflow(repo, CANONICAL, "bug-fix", TASK, executor_factory=lambda c: executor(repo), echo=lambda _: None)
        with RunStore.open(repo, create=False) as store:
            [run] = store.list_runs()
            assert run.outcome == "blocked" and store.transitions(run.run_id)[-1].to_state == "BLOCKED"
        assert read_lock(state_dir).run_id == "20261024-080000-000000", "the holder's lock is untouched"
    finally:
        release(state_dir, "20261024-080000-000000")


def test_resume_without_a_run_record(repo: Path) -> None:
    with pytest.raises(OrchestratorError, match="no run record exists yet"):
        resume_workflow(repo, CANONICAL, RUN, executor_factory=lambda c: executor(repo), echo=lambda _: None)


def test_an_invalid_canonical_tree_or_workflow_is_an_orchestrator_error(repo: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty-canonical"
    (empty / "agents").mkdir(parents=True)
    (empty / "agents" / "broken.md").write_text("---\nkind: agent\n---\n", encoding="utf-8")
    with pytest.raises(OrchestratorError, match="canonical tree at"):
        run_workflow(repo, empty, "bug-fix", TASK, executor_factory=lambda c: executor(repo), echo=lambda _: None)
    with pytest.raises(OrchestratorError, match="no canonical workflow named 'feature'"):
        load_dag(load_canonical(CANONICAL), "feature", "default")


# --- workspace and push guards ----------------------------------------------------------


def test_read_artifact_refuses_paths_outside_the_artifacts_dir(tmp_path: Path) -> None:
    (tmp_path / ".agent-team" / "artifacts" / RUN).mkdir(parents=True)
    (tmp_path / ".agent-team" / "runs.db").write_text("db", encoding="utf-8")
    (tmp_path / ".agent-team" / "artifacts" / RUN / "link").symlink_to(tmp_path / ".agent-team" / "runs.db")
    for path in (".agent-team/runs.db", f".agent-team/artifacts/{RUN}/../../runs.db", f".agent-team/artifacts/{RUN}/link", "/etc/hostname"):
        with pytest.raises(WorkspaceError, match="is not under .agent-team/artifacts/"):
            read_artifact(tmp_path, {"path": path, "sha256": "x"}, 100)


def test_the_orchestrator_runs_no_command_outside_srt(tmp_path: Path) -> None:
    env = SimpleNamespace(path=tmp_path, sandbox=Sandbox(srt=None, settings_path=None, tmp_dir=tmp_path, port_offset=0))
    with pytest.raises(WorkspaceError, match="runs no command outside the sandbox"):
        run_command(env, "test", f"touch {tmp_path / 'ran'}", timeout_s=5, base_env={})  # type: ignore[arg-type]
    assert not (tmp_path / "ran").exists()


def test_a_command_past_its_timeout_is_killed(tmp_path: Path) -> None:
    fake = tmp_path / "srt"
    fake.write_text(FAKE_SRT, encoding="utf-8")
    fake.chmod(0o755)
    env = SimpleNamespace(path=tmp_path, sandbox=Sandbox(srt=str(fake), settings_path=tmp_path / "s.json", tmp_dir=tmp_path, port_offset=0))
    result = run_command(env, "test", "echo started; sleep 60 & sleep 60", timeout_s=1,  # type: ignore[arg-type]
                         base_env={"PATH": os.environ["PATH"], "GH_TOKEN": "ghp_x"})  # fmt: skip
    assert result.exit_code is None and not result.passed and "started" in result.output


def test_a_worktree_on_another_branch_is_not_the_run_branch() -> None:
    env = SimpleNamespace(meta=SimpleNamespace(branch="main"))
    with pytest.raises(AllocatorError, match="is not the run branch agent-team/r1"):
        ensure_run_branch(env, "r1")  # type: ignore[arg-type]
    assert ensure_run_branch(SimpleNamespace(meta=SimpleNamespace(branch="agent-team/r1")), "r1") == "agent-team/r1"  # type: ignore[arg-type]


def test_a_non_utf8_config_yaml_is_reported_before_the_run(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / ".agent-team").mkdir(exist_ok=True)
    (repo / ".agent-team" / "config.yaml").write_bytes(b"autonomy_ceiling: caf\xe9\n")
    ex = executor(repo)
    [problem] = ex.unavailable(load_dag(ex.canonical, "bug-fix", "default"))
    assert "config.yaml is unreadable (UnicodeDecodeError)" in problem
    monkeypatch.setattr(cli, "_which", lambda name: f"/opt/bin/{name}")
    result = CliRunner().invoke(app, ["run", "--workflow", "bug-fix", "--repo", str(repo), "--canonical", str(CANONICAL), TASK])
    assert result.exit_code == 2 and "config.yaml is unreadable (UnicodeDecodeError)" in result.output
    assert RunStore.open(repo, create=False) is None, "refused before recording a run"


def test_orchestrator_commands_write_no_bytecode(tmp_path: Path) -> None:
    """A check that compiled the old file must not leave a .pyc a same-second, same-size fix is judged against."""
    fake = tmp_path / "srt"
    fake.write_text(FAKE_SRT, encoding="utf-8")
    fake.chmod(0o755)
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    env = SimpleNamespace(path=tmp_path, sandbox=Sandbox(srt=str(fake), settings_path=tmp_path / "s.json", tmp_dir=tmp_path, port_offset=0))
    result = run_command(env, "test", f"{sys.executable} -c 'import calc'", timeout_s=30,  # type: ignore[arg-type]
                         base_env={"PATH": os.environ["PATH"]})  # fmt: skip
    assert result.passed, result.output
    assert not (tmp_path / "__pycache__").exists(), "the check compiled calc.py to disk"
def test_orchestrator_commands_keep_tool_caches_inside_the_sandbox(tmp_path: Path) -> None:
    """srt mounts home read-only, so uv's default ~/.cache/uv must never be the cache a check uses."""
    fake = tmp_path / "srt"
    fake.write_text(FAKE_SRT, encoding="utf-8")
    fake.chmod(0o755)
    sandbox_tmp = tmp_path / "sandbox-tmp"
    sandbox_tmp.mkdir()
    env = SimpleNamespace(path=tmp_path, sandbox=Sandbox(srt=str(fake), settings_path=tmp_path / "s.json", tmp_dir=sandbox_tmp, port_offset=0))
    result = run_command(env, "test", 'echo "uv=$UV_CACHE_DIR xdg=$XDG_CACHE_HOME ci=$CI"', timeout_s=10,  # type: ignore[arg-type]
                         base_env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "UV_CACHE_DIR": "/elsewhere"})  # fmt: skip
    assert result.passed, result.output
    assert f"uv={sandbox_tmp / 'cache' / 'uv'} xdg={sandbox_tmp / 'cache'} ci=1" in result.output
