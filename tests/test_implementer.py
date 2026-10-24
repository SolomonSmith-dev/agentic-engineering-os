"""Implementer, evidence and draft PR (build-order step 11): `run --workflow bug-fix` end to end.

No test calls a model or creates a real PR. A fake `claude` on PATH plays
both agents from a scenario file: the implementer commits in the worktree it
is given (a regression check in DISCOVERY, the fix in IMPLEMENTATION) and the
reviewer returns the scenario's verdict. A fake `gh` records its arguments,
whether it saw GH_TOKEN, and the PR body. `origin` is a local bare repository.

The workflow tests need srt, because the orchestrator runs the reproduction
and the checks only inside it: they skip when srt is absent, and fail instead
when AGENT_TEAM_REQUIRE_SRT=1 (as in CI). One test covers the run without srt.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from agent_team import cli
from agent_team.adapters.claude import resolve
from agent_team.adapters.claude.hook import check
from agent_team.cli import app
from agent_team.environments import create, read_lock, srt_path
from agent_team.orchestrator import DefaultExecutor
from agent_team.orchestrator import pr as pr_mod
from agent_team.orchestrator.workspace import LEFTOVER_MESSAGE, WorkspaceError, settle
from agent_team.run_agent import load_canonical
from agent_team.runs import RunStore

REPO = Path(__file__).resolve().parents[1]
CANONICAL = REPO / "canonical"
TOKEN = "ghp_STEP11WORKERCANARY000000"
TASK = "Fix the value: it still says bug."
PR_URL = "https://github.com/example/target/pull/7"
HAPPY_PATH = ["INTAKE", "DISCOVERY", "PLAN", "IMPLEMENTATION", "TEST", "CODE_REVIEW", "PR", "HUMAN_DECISION"]
# The check command also dumps its environment, to prove the token never reaches the sandbox.
TEST_COMMAND = 'test -f value.txt && env > "$TMPDIR/check-env.txt"'
runner = CliRunner()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


FAKE_CLAUDE = r'''#!{python}
"""Fake claude for the workflow: plays the implementer and the reviewer from a scenario file."""
import json, os, re, subprocess, sys

if sys.argv[1:] == ["--version"]:
    print("2.1.310 (Claude Code)")
    sys.exit(0)

argv = sys.argv[1:]
agent = argv[argv.index("--agent") + 1]
prompt = sys.stdin.read()
state, attempt = re.match(r"Workflow \S+, state (\w+), attempt (\d+)\.", prompt).groups()
attempt = int(attempt)
scenario = json.load(open({scenario!r}))


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def write(name, text):
    with open(name, "w") as f:
        f.write(text)


if agent == "implementer" and state == "DISCOVERY":
    write("check.sh", "grep -q fixed value.txt\n")
    git("add", "check.sh")
    git("commit", "-q", "-m", "Add a regression check")
    out = {{"reproduction_command": "sh check.sh", "reproduction_fails": True,
           "changes": [{{"file": "check.sh", "summary": "CLAIM-CANARY regression check"}}]}}
elif agent == "implementer":
    action = scenario["implement"][attempt - 1]
    write("value.txt", "fixed\nand handled\n" if action == "fix-more" else "fixed\n")
    if action != "fix-uncommitted":
        git("commit", "-q", "-am", f"Fix the value, attempt {{attempt}}")
    out = {{"reproduction_command": "sh check.sh", "reproduction_fails": False,
           "changes": [{{"file": "value.txt", "summary": f"CLAIM-CANARY attempt {{attempt}}"}}]}}
else:
    verdict = scenario["reviews"][attempt - 1]
    severity = {{"approve": "minor", "changes": "major", "escalate": "blocker"}}[verdict]
    out = {{"verdict": verdict, "findings": [{{"file": "value.txt", "line": 1, "requirement_id": "AC-1",
                                              "severity": severity, "fix": f"FINDING-{{verdict}}-{{attempt}}"}}]}}

seen = {{"argv": argv, "env": dict(os.environ), "stdin": prompt, "cwd": os.getcwd(), "head": git("rev-parse", "HEAD")}}
with open(os.path.join(os.environ["TMPDIR"], f"fake-claude-{{agent}}-{{state}}-{{attempt}}.json"), "w") as f:
    json.dump(seen, f)
for event in [{{"type": "system", "subtype": "init"}},
              {{"type": "result", "subtype": "success", "is_error": False, "result": "done", "total_cost_usd": 0.1,
                "num_turns": 1, "session_id": "fake", "structured_output": out}}]:
    print(json.dumps(event), flush=True)
'''

FAKE_GH = r'''#!{python}
"""Fake gh: records every call; `auth status` and `pr create` answer by the mode file."""
import json, os, sys

mode = json.load(open({mode!r}))["mode"]
args = sys.argv[1:]
os.makedirs({log!r}, exist_ok=True)
entry = {{"args": args, "has_gh_token": "GH_TOKEN" in os.environ, "prompt_disabled": os.environ.get("GH_PROMPT_DISABLED")}}
if args[:2] == ["pr", "create"]:
    entry["body"] = open(args[args.index("--body-file") + 1]).read()
with open(os.path.join({log!r}, f"{{len(os.listdir({log!r})):02d}}.json"), "w") as f:
    json.dump(entry, f)
if args[:2] == ["auth", "status"]:
    sys.exit(1 if mode == "unauthenticated" else 0)
if args[:2] == ["pr", "create"]:
    if mode == "fail":
        print("GraphQL: could not create the pull request ghp_GHSTDERRCANARY0000000000", file=sys.stderr)
        sys.exit(1)
    print({url!r})
    sys.exit(0)
sys.exit(2)
'''


class World:
    """The fakes' files: the scenario the fake claude plays, the gh mode, and gh's call log."""

    def __init__(self, tmp_path: Path) -> None:
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.scenario = tmp_path / "scenario.json"
        self.mode = tmp_path / "gh-mode.json"
        self.gh_log = tmp_path / "gh-log"
        self.play(implement=["fix-commit"], reviews=["approve"])
        self.gh_mode("ok")
        for name, text in (("claude", FAKE_CLAUDE.format(python=sys.executable, scenario=str(self.scenario))),
                           ("gh", FAKE_GH.format(python=sys.executable, mode=str(self.mode), log=str(self.gh_log), url=PR_URL))):  # fmt: skip
            path = self.bin / name
            path.write_text(text, encoding="utf-8")
            path.chmod(0o755)

    def play(self, *, implement: list[str], reviews: list[str]) -> None:
        self.scenario.write_text(json.dumps({"implement": implement, "reviews": reviews}), encoding="utf-8")

    def gh_mode(self, mode: str) -> None:
        self.mode.write_text(json.dumps({"mode": mode}), encoding="utf-8")

    def gh_calls(self) -> list[dict[str, Any]]:
        if not self.gh_log.is_dir():
            return []
        return [json.loads(p.read_text()) for p in sorted(self.gh_log.glob("*.json"))]

    def pr_creates(self) -> list[dict[str, Any]]:
        return [c for c in self.gh_calls() if c["args"][:2] == ["pr", "create"]]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text(".agent-team/\n", encoding="utf-8")
    (root / "value.txt").write_text("bug\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "-q", "origin", "main")
    return root.resolve()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    w = World(tmp_path)
    monkeypatch.setenv("PATH", f"{w.bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return w


@pytest.fixture
def srt() -> str:
    path = srt_path()
    if path is None:
        if os.environ.get("AGENT_TEAM_REQUIRE_SRT") == "1":
            pytest.fail("AGENT_TEAM_REQUIRE_SRT=1 but srt is not on PATH")
        pytest.skip("srt not installed")
    return path


def run_cli(repo: Path, *extra: str):
    return runner.invoke(app, ["run", "--workflow", "bug-fix", "--repo", str(repo), "--canonical", str(CANONICAL),
                               "--test-command", TEST_COMMAND, *extra, TASK])  # fmt: skip


def workflow_run(repo: Path):
    with RunStore.open(repo, create=False) as store:
        runs = store.list_runs(100)
        [run] = [r for r in runs if r.workflow == "bug-fix"]
        children = [r for r in runs if r.workflow.startswith("agent:")]
        return run, store.transitions(run.run_id), store.last_checkpoint(run.run_id), children


def seen(repo: Path, agent: str, state: str, attempt: int) -> dict[str, Any]:
    [path] = (repo / ".agent-team" / "tmp").glob(f"*/fake-claude-{agent}-{state}-{attempt}.json")
    return json.loads(path.read_text())


def remote_branch(repo: Path, branch: str) -> str | None:
    out = subprocess.run(["git", "-C", str(repo), "ls-remote", "origin", f"refs/heads/{branch}"],
                         capture_output=True, text=True, check=True).stdout.split()  # fmt: skip
    return out[0] if out else None


# --- end to end -----------------------------------------------------------------------


@pytest.mark.srt
def test_happy_path_opens_a_draft_pr_from_the_reviewed_commit(repo: Path, world: World, srt: str) -> None:
    main_before = git(repo, "rev-parse", "main")
    result = run_cli(repo)
    assert result.exit_code == 0, result.output
    run, transitions, checkpoint, children = workflow_run(repo)
    assert run.outcome == "succeeded" and [t.to_state for t in transitions] == HAPPY_PATH
    evidence = checkpoint.data["evidence"]
    branch = f"agent-team/{run.run_id}"
    head = evidence["diff"]["head"]
    assert transitions[-1].note.endswith(f"draft PR {PR_URL} on {branch} at {head[:12]}")

    # One commit for everything: the diff ends at it, the checks ran on it, the reviewer read it, it was pushed.
    assert evidence["test_output"]["head"] == head and evidence["review"]["head"] == head
    assert seen(repo, "independent-reviewer", "CODE_REVIEW", 1)["head"] == head
    assert remote_branch(repo, branch) == head and evidence["pr"]["head"] == head
    assert git(repo, "ls-remote", "origin", "refs/heads/main").split()[0] == main_before, "main is never pushed"
    assert git(repo, "rev-parse", "main") == main_before

    # The reproduction failed before the fix and passed after, both run by the orchestrator in srt.
    assert evidence["reproduction_fails"] is True and evidence["reproduction_check"]["verified"] is True
    assert evidence["reproduction_check"]["exit_code"] == 1
    assert [(r["kind"], r["exit_code"]) for r in evidence["test_output"]["results"]] == [("reproduction", 0), ("test", 0)]
    assert evidence["test_output"]["skipped"] == ["lint: no command known", "typecheck: no command known"]

    # Artifacts are files under .agent-team/; the checkpoint holds their paths, not their content.
    for key, name in (("diff", "diff-1.patch"), ("test_output", "test-1.log"), ("review", "review-1.json")):
        assert evidence[key]["path"] == f".agent-team/artifacts/{run.run_id}/{name}"
        assert (repo / evidence[key]["path"]).is_file()
    patch = (repo / evidence["diff"]["path"]).read_text()
    assert "+fixed" in patch and "+grep -q fixed value.txt" in patch
    assert "+fixed" not in json.dumps(checkpoint.data)
    assert json.loads((repo / evidence["review"]["path"]).read_text())["verdict"] == "approve"
    assert evidence["diff"]["files"] == ["check.sh", "value.txt"]
    assert evidence["claimed_changes"]["unclaimed"] == [], "DISCOVERY's check.sh is not this attempt's to explain"

    # The reviewer saw the plan, the inlined diff and the check output; never the implementer's claims.
    reviewer = seen(repo, "independent-reviewer", "CODE_REVIEW", 1)
    assert "## plan" in reviewer["stdin"] and "+fixed" in reviewer["stdin"] and "== reproduction: sh check.sh" in reviewer["stdin"]
    assert "CLAIM-CANARY" not in reviewer["stdin"] and "claimed_changes" not in reviewer["stdin"]
    assert reviewer["cwd"] != seen(repo, "implementer", "IMPLEMENTATION", 1)["cwd"], "the reviewer has its own worktree"

    # The implementer's calls share the workflow worktree; each is its own child run.
    implementers = [c for c in children if c.workflow == "agent:implementer"]
    assert len(implementers) == 2 and {c.worktree_path for c in implementers} == {run.worktree_path}
    assert run.worktree_path == str(repo / ".agent-team" / "worktrees" / run.run_id)
    assert run.cost_usd == pytest.approx(0.3), "three agent calls"

    # The draft PR: run branch, draft, evidence, verdict, minor finding as a follow-up.
    [create] = world.pr_creates()
    args = create["args"]
    assert args[:3] == ["pr", "create", "--draft"]
    assert args[args.index("--head") + 1] == branch and "--base" not in args
    assert args[args.index("--title") + 1] == f"agent-team: {TASK}"
    body = create["body"]
    for part in ("## Evidence", "## Review", "Verdict: **approve**", "## Unresolved risks", "## Follow-ups",
                 "- [ ] `value.txt:1` (AC-1, minor): FINDING-approve-1", "Gate skipped: lint: no command known."):  # fmt: skip
        assert part in body, part
    assert "Blocked" not in body and "Generated with" not in body and "Co-Authored-By" not in body
    assert body == (repo / evidence["pr"]["body"]["path"]).read_text()

    # The token: gh got it, no worker and no sandboxed check ever did.
    assert all(c["has_gh_token"] and c["prompt_disabled"] == "1" for c in world.gh_calls())
    workers = list((repo / ".agent-team" / "tmp").glob("*/fake-claude-*.json"))
    assert len(workers) == 3
    for path in workers:
        env = json.loads(path.read_text())["env"]
        assert "GH_TOKEN" not in env and TOKEN not in json.dumps(env)
    [check_env] = (repo / ".agent-team" / "tmp").glob("*/check-env.txt")
    assert TOKEN not in check_env.read_text() and "GH_TOKEN" not in check_env.read_text()
    assert TOKEN not in (repo / ".agent-team" / "runs.db").read_bytes().decode("utf-8", "replace")
    assert read_lock(repo / ".agent-team") is None


@pytest.mark.srt
def test_changes_then_one_fresh_retry_then_approve(repo: Path, world: World, srt: str) -> None:
    world.play(implement=["fix-uncommitted", "fix-more"], reviews=["changes", "approve"])
    result = run_cli(repo)
    assert result.exit_code == 0, result.output
    run, transitions, checkpoint, children = workflow_run(repo)
    assert run.outcome == "succeeded"
    assert [t.to_state for t in transitions] == [
        "INTAKE", "DISCOVERY", "PLAN", "IMPLEMENTATION", "TEST", "CODE_REVIEW",
        "IMPLEMENTATION", "TEST", "CODE_REVIEW", "PR", "HUMAN_DECISION",
    ]  # fmt: skip
    first_test = next(t for t in transitions if t.to_state == "TEST")
    assert "committed 1 path(s) the implementer left uncommitted" in first_test.note
    assert LEFTOVER_MESSAGE in git(repo, "log", "--format=%s", f"agent-team/{run.run_id}")

    # The retry is a fresh process: the plan and the findings, never attempt one's output.
    retry = seen(repo, "implementer", "IMPLEMENTATION", 2)
    assert "## findings" in retry["stdin"] and "FINDING-changes-1" in retry["stdin"] and "## plan" in retry["stdin"]
    assert "CLAIM-CANARY" not in retry["stdin"] and "## diff" not in retry["stdin"]
    assert "--no-session-persistence" in retry["argv"]
    assert len({c.run_id for c in children if c.workflow == "agent:implementer"}) == 3

    # The second diff is cumulative from the base and is what the second review and the PR saw.
    evidence = checkpoint.data["evidence"]
    assert evidence["diff"]["path"].endswith("diff-2.patch") and "+and handled" in (repo / evidence["diff"]["path"]).read_text()
    assert seen(repo, "independent-reviewer", "CODE_REVIEW", 2)["head"] == evidence["diff"]["head"]
    assert remote_branch(repo, f"agent-team/{run.run_id}") == evidence["diff"]["head"]
    [create] = world.pr_creates()
    assert not create["args"][create["args"].index("--title") + 1].startswith("[blocked]")


@pytest.mark.srt
def test_retry_then_changes_again_escalates_to_a_blocked_draft(repo: Path, world: World, srt: str) -> None:
    world.play(implement=["fix-commit", "fix-more"], reviews=["changes", "changes"])
    result = run_cli(repo)
    assert result.exit_code == 1, result.output
    run, transitions, checkpoint, _ = workflow_run(repo)
    assert run.outcome == "blocked" and run.state == "HUMAN_DECISION"
    assert "escalated: reviewer_verdict == changes after 1/1 retries" in transitions[-2].note
    assert transitions[-1].note.endswith(f"draft PR {PR_URL} on agent-team/{run.run_id} at {checkpoint.data['evidence']['diff']['head'][:12]}, marked blocked")
    [create] = world.pr_creates()
    title = create["args"][create["args"].index("--title") + 1]
    assert title == f"[blocked] agent-team: {TASK}"
    body = create["body"]
    assert "**Blocked: this draft needs a human decision" in body
    assert "still asked for changes after the one retry" in body
    assert "`value.txt:1` (AC-1, major): FINDING-changes-2" in body
    assert checkpoint.data["evidence"]["pr"]["blocked"] is True


@pytest.mark.srt
@pytest.mark.parametrize(
    ("mode", "missing", "reason", "pushed"),
    [
        ("fail", False, "gh pr create exited 1: GraphQL: could not create the pull request", True),
        ("unauthenticated", False, "gh is not authenticated", False),
        ("ok", True, "gh is not on PATH", False),
    ],
)
def test_gh_problems_end_at_human_decision_with_the_branch_kept(
    repo: Path, world: World, srt: str, monkeypatch: pytest.MonkeyPatch, mode: str, missing: bool, reason: str, pushed: bool
) -> None:
    world.gh_mode(mode)
    if missing:
        real = cli._which
        monkeypatch.setattr(cli, "_which", lambda name: None if name == "gh" else real(name))
    result = run_cli(repo)
    assert result.exit_code == 1, result.output
    run, transitions, checkpoint, _ = workflow_run(repo)
    assert run.outcome == "blocked" and [t.to_state for t in transitions] == HAPPY_PATH
    branch = f"agent-team/{run.run_id}"
    pr = checkpoint.data["evidence"]["pr"]
    assert pr["status"] == "not_opened" and reason in pr["reason"] and pr["pushed"] is pushed
    assert transitions[-1].note.endswith(f"no draft PR: {pr['reason']}; branch {branch} kept")
    assert git(repo, "rev-parse", branch) == pr["head"], "the local branch is kept"
    assert (remote_branch(repo, branch) == pr["head"]) is pushed
    assert "GHSTDERRCANARY" not in json.dumps(checkpoint.data), "gh's stderr is redacted"
    assert (repo / ".agent-team" / "artifacts" / run.run_id / "pr-body.md").is_file(), "the body is ready for a human"


@pytest.mark.srt
def test_a_proposal_without_draft_prs_opens_none(repo: Path, world: World, srt: str) -> None:
    config = {"version": 1, "default_branch": "main", "protected_branches": ["main"], "runtimes": ["claude"],
              "privacy_classes": ["vendor-cloud"], "autonomy_ceiling": "A3", "draft_prs": False,
              "commands": {"lint": ["true"]}}  # fmt: skip
    (repo / ".agent-team").mkdir(exist_ok=True)
    (repo / ".agent-team" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    result = run_cli(repo)
    assert result.exit_code == 1, result.output
    _, _, checkpoint, _ = workflow_run(repo)
    evidence = checkpoint.data["evidence"]
    assert ("lint", "true", 0) in [(r["kind"], r["command"], r["exit_code"]) for r in evidence["test_output"]["results"]]
    assert "draft_prs: false" in evidence["pr"]["reason"] and not world.gh_calls()


@pytest.mark.srt
@pytest.mark.parametrize("where", ["TEST", "PR"])
def test_resume_across_the_test_and_pr_steps(repo: Path, world: World, srt: str, monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    calls = {"n": 0}
    if where == "TEST":
        original = DefaultExecutor._test

        def flaky(self, given):  # noqa: ANN001, ANN202
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt
            return original(self, given)

        monkeypatch.setattr(DefaultExecutor, "_test", flaky)
    else:
        original_create = pr_mod.create

        def flaky_create(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt  # after the push, before gh pr create
            return original_create(*args, **kwargs)

        monkeypatch.setattr(pr_mod, "create", flaky_create)
    result = run_cli(repo)
    assert result.exit_code == 130, result.output
    run, transitions, _, _ = workflow_run(repo)
    assert run.ended_at is None and transitions[-1].to_state == where

    result = runner.invoke(app, ["resume", run.run_id, "--repo", str(repo), "--canonical", str(CANONICAL), "--test-command", TEST_COMMAND])
    assert result.exit_code == 0, result.output
    run, transitions, checkpoint, children = workflow_run(repo)
    assert run.outcome == "succeeded"
    states = [t.to_state for t in transitions]
    assert states[states.index("RESUME") - 1 : states.index("RESUME") + 2] == [where, "RESUME", where]
    assert len([c for c in children if c.workflow == "agent:independent-reviewer"]) == 1, "earlier nodes are not re-run"
    assert len(world.pr_creates()) == 1
    assert remote_branch(repo, f"agent-team/{run.run_id}") == checkpoint.data["evidence"]["diff"]["head"]


def test_without_srt_nothing_runs_and_the_run_blocks_at_test(repo: Path, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_team.environments.sandbox.srt_path", lambda: None)
    result = run_cli(repo)
    assert result.exit_code == 1, result.output
    run, transitions, checkpoint, _ = workflow_run(repo)
    assert [t.to_state for t in transitions] == ["INTAKE", "DISCOVERY", "PLAN", "IMPLEMENTATION", "TEST", "BLOCKED"]
    evidence = checkpoint.data["evidence"]
    assert evidence["reproduction_check"]["verified"] is False and "srt is absent" in evidence["reproduction_check"]["reason"]
    assert evidence["diff"]["files"], "the implementer's work is on the branch"
    assert "no checks run: srt is absent" in transitions[-1].note and "gate failed: tests_pass (got False)" in transitions[-1].note
    assert not list((repo / ".agent-team" / "tmp").glob("*/check-env.txt")), "nothing ran outside the sandbox"
    assert not world.gh_calls()
    with RunStore.open(repo, create=False) as store:
        child = next(r for r in store.list_runs(100) if r.workflow == "agent:implementer")
        note = store.transitions(child.run_id)[1].note
    assert "autonomy ceiling A2" in note and "shell.execute.workspace: needs srt" in note and "git.commit: needs A3" in note


# --- units ----------------------------------------------------------------------------


def implementer_caps() -> list[str]:
    return load_canonical(CANONICAL).one("agent", "implementer").spec.capabilities.allow


def test_implementer_capabilities_inside_and_outside_srt() -> None:
    inside = resolve(implementer_caps(), ["github.pr.*"], "A3", test_commands=("uv run pytest",), sandboxed=True)
    assert inside.granted == ["fs.read", "git.read", "shell.read", "fs.write.workspace", "git.commit",
                              "shell.execute.workspace", "test.run"]  # fmt: skip
    assert {"Edit(./**)", "Write(./**)", "Bash", "Bash(git commit *)"} <= set(inside.allow)
    assert "Edit" not in inside.allow and "Write" not in inside.allow, "writes stay under the working directory"
    assert "git.write.branch: exercised by the orchestrator, not the agent" in inside.dropped
    assert {"Bash(git push)", "Bash(git push *)", "Bash(gh)", "Bash(gh *)"} <= set(inside.deny)

    outside = resolve(implementer_caps(), ["github.pr.*"], "A2", test_commands=("uv run pytest",), sandboxed=False)
    assert "Bash" not in outside.allow and "Bash(uv run pytest)" in outside.allow
    assert "shell.execute.workspace: needs srt; outside it Bash is not confined to the worktree" in outside.dropped
    assert "git.commit: needs A3, ceiling is A2" in outside.dropped


@pytest.fixture
def write_policy(tmp_path: Path) -> dict:
    worktree = tmp_path / "repo" / ".agent-team" / "worktrees" / "run-1"
    (worktree / "src").mkdir(parents=True)
    (worktree / "escape").symlink_to(tmp_path)
    return {"protected_paths": [".agent-team/**"], "roots": [str(worktree), str(tmp_path / "repo")],
            "home": str(tmp_path / "home"), "write_root": str(worktree)}  # fmt: skip


@pytest.mark.parametrize(
    ("tool", "path", "blocked"),
    [
        ("Write", "src/app.py", False),
        ("Edit", "./src/app.py", False),
        ("Write", "../../../outside.py", True),
        ("Write", "/etc/profile.d/x.sh", True),
        ("Edit", "~/.bashrc", True),
        ("Write", "escape/planted.py", True),
        ("Read", "/etc/hostname", False),
    ],
)
def test_hook_keeps_file_writes_inside_the_worktree(write_policy: dict, tool: str, path: str, blocked: bool) -> None:
    reason = check({"tool_name": tool, "tool_input": {"file_path": path}}, write_policy)
    assert (reason is not None) == blocked, reason
    if blocked:
        assert "outside the run's worktree" in reason


def test_gh_and_git_credentials_are_protected_from_workers(tmp_path: Path) -> None:
    policy = {"protected_paths": ["~/.config/gh/**", "~/.git-credentials", "~/.netrc"], "roots": [str(tmp_path)],
              "home": str(tmp_path / "home")}  # fmt: skip
    for command in ("cat ~/.config/gh/hosts.yml", "cat $HOME/.git-credentials", "grep pass ~/.netrc"):
        assert check({"tool_name": "Bash", "tool_input": {"command": command}}, policy) is not None, command


def test_settle_commits_leftovers_and_refuses_a_moved_branch(repo: Path) -> None:
    spec = load_canonical(CANONICAL).one("environment", "default").spec
    env = create(repo, "settle-run", spec)
    (env.path / "value.txt").write_text("fixed\n", encoding="utf-8")
    (env.path / "new.txt").write_text("new\n", encoding="utf-8")
    settled = settle(env)
    assert settled.committed == 2 and git(env.path, "log", "-1", "--format=%s") == LEFTOVER_MESSAGE
    assert settle(env).committed == 0, "a clean worktree is left alone"
    git(env.path, "checkout", "-q", "-b", "elsewhere")
    with pytest.raises(WorkspaceError, match="not on agent-team/settle-run"):
        settle(env)
    git(env.path, "checkout", "-q", "agent-team/settle-run")
    git(env.path, "reset", "-q", "--hard", env.meta.base_commit)
    git(env.path, "commit", "-q", "--amend", "-m", "rewritten base")
    with pytest.raises(WorkspaceError, match="no longer contains its base"):
        settle(env)


def test_push_refuses_anything_but_the_run_branch(tmp_path: Path) -> None:
    for head, branch in (("abc", "main"), ("+abc", "agent-team/r"), ("abc", "feature/x")):
        with pytest.raises(pr_mod.PrError, match="only the run's agent-team/ branch"):
            pr_mod.push(tmp_path, head, branch)


def test_title_is_short_and_marks_a_blocked_draft() -> None:
    assert pr_mod.title("\n  Fix it  \nmore", False) == "agent-team: Fix it"
    long = pr_mod.title("x" * 200, True)
    assert long.startswith("[blocked] agent-team: x") and long.endswith("...") and len(long) == 72 + len("[blocked] ")


def test_body_lists_risks_redacts_and_never_carries_attribution() -> None:
    given = pr_mod.PrInputs(
        run_id="r1", task=f"Fix it; GH_TOKEN={TOKEN}", plan={"acceptance_criteria": [{"id": "AC-1", "text": "passes"}]},
        reproduction_command="sh check.sh", reproduction_check={"verified": False, "reason": "srt is absent"},
        diff={"base": "a" * 40, "head": "b" * 40, "files": ["x.py", "y.py"], "insertions": 3, "deletions": 1},
        claimed_changes={"attempt": 1, "changes": [{"file": "x.py", "summary": "fix"}], "unclaimed": ["y.py"]}, test_output={"head": "b" * 40, "sandboxed": True,
        "results": [{"kind": "test", "command": "pytest", "exit_code": 0}], "skipped": ["lint: no command known"]},
        verdict="escalate", findings=[{"file": "x.py", "line": 2, "requirement_id": "AC-1", "severity": "blocker", "fix": "no"}],
        blocked=True, blocked_reasons=["The reviewer escalated."],
    )  # fmt: skip
    text = pr_mod.body(given)
    assert TOKEN not in text and "[REDACTED]" in text
    assert "**Blocked: this draft needs a human decision" in text and "- The reviewer escalated." in text and "Blocking findings:" in text
    assert "The reproduction was not verified before the fix: srt is absent." in text
    assert "Changed in the last attempt but not named in the implementer's report: `y.py`." in text
    assert "Generated with" not in text and "Co-Authored-By" not in text and "Claude-Session" not in text
