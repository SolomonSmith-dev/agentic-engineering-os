"""The eval harness (build-order step 13): tasks, fixtures, the hidden verifier, budgets, the report.

No test calls a model or spends money. Fakes on PATH stand in for every paid
binary: `claude` plays the implementer and the reviewer (and the probe's Bash
call), `mini` plays mini-swe-agent and writes a trajectory, and `gh` records any
call, so the tests can prove the eval never makes one. Each fake also tries to
read the task's hidden verifier file and records whether it could.

The tests that run arms or the verifier need srt, like the rest of the suite:
they skip when it is absent, and fail instead when AGENT_TEAM_REQUIRE_SRT=1 (CI).
"""

import json
import os
import shutil
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from agent_team.adapters.claude.invoke import StreamParser, normalize
from agent_team.cli import app
from agent_team.environments import srt_path
from agent_team.environments.sandbox import build_settings
from agent_team.eval import CappedExecutor, EvalStore, TaskError, load_tasks, materialize
from agent_team.eval.report import aggregate, decision, render_report
from agent_team.eval.runner import build_plan, charged, estimate
from agent_team.eval.tasks import fixture_commit, git
from agent_team.eval.verify import check_commands, diff_stats, planned, snapshot
from agent_team.orchestrator.machine import NodeResult
from agent_team.runs import RunStore
from agent_team.schemas import EnvironmentSpec
from agent_team.schemas.eval import (
    ArmPlan,
    Caps,
    CellResult,
    CheckResult,
    DiffStats,
    EvalRecord,
    ExistingTestResult,
)

REPO = Path(__file__).resolve().parents[1]
CANONICAL = REPO / "canonical"
TASKS = REPO / "evals" / "tasks"
FAKE_KEY = "sk-ant-api03-FAKEEVALKEY000000000000"
runner = CliRunner()


@pytest.fixture
def srt() -> str:
    path = srt_path()
    if path is None:
        if os.environ.get("AGENT_TEAM_REQUIRE_SRT") == "1":
            pytest.fail("AGENT_TEAM_REQUIRE_SRT=1 but srt is not on PATH")
        pytest.skip("srt not installed")
    return path


# --- a one-task eval tree, the fakes, and the store repository ------------------------------------

CALC_REPO = {
    "calc.py": "def add(a, b):\n    return a - b\n",
    ".gitignore": "__pycache__/\n",
    "tests/__init__.py": "",
    "tests/test_calc.py": (
        "import unittest\n\nfrom calc import add\n\n\nclass CalcTest(unittest.TestCase):\n"
        "    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n"
    ),
}
HIDDEN_TEST = (
    "import unittest\n\nfrom calc import add\n\n\nclass HiddenTest(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
)


def write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def make_evals(tmp_path: Path) -> Path:
    """evals/ with one fixture task, calc-add: add() subtracts; the hidden test wants 2 + 3 == 5."""
    evals = tmp_path / "evals"
    write_tree(evals / "fixtures" / "calc-add" / "repo", CALC_REPO)
    write_tree(evals / "fixtures" / "calc-add" / "verifier", {"tests/test_hidden_calc.py": HIDDEN_TEST})
    ref = fixture_commit(evals / "fixtures" / "calc-add" / "repo", tmp_path / "ref-probe")
    shutil.rmtree(tmp_path / "ref-probe")
    (evals / "tasks").mkdir()
    (evals / "tasks" / "calc-add.yaml").write_text(
        "id: calc-add\nkind: fix-bug\nrepo: fixture:calc-add\n"
        f"ref: {ref}\n"
        "instruction: calc.add subtracts; make add(2, 3) return 5.\n"
        "test_command: python3 -m unittest\n"
        "existing_tests: [python3 -m unittest tests.test_calc.CalcTest.test_zero]\n"
        "planned_files: [calc.py, tests/**]\n"
        "verifier: {command: python3 -m unittest tests.test_hidden_calc, files: ../fixtures/calc-add/verifier, timeout_s: 60}\n",
        encoding="utf-8",
    )
    return evals


FAKE_CLAUDE = r'''#!{python}
"""Fake claude for the eval: the implementer (single session or workflow), the reviewer, the probe."""
import json, os, re, subprocess, sys

if sys.argv[1:] == ["--version"]:
    print("2.1.310 (Claude Code)")
    sys.exit(0)
argv = sys.argv[1:]
agent = argv[argv.index("--agent") + 1]
budget = argv[argv.index("--max-budget-usd") + 1]
prompt = sys.stdin.read()
scenario = json.load(open({scenario!r}))
match = re.match(r"Workflow (\S+), state (\w+), attempt (\d+)\.", prompt)
workflow, state, attempt = match.groups() if match else ("probe", "PROBE", "1")


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def peek():
    try:
        open(scenario["peek"]).read()
        return "read"
    except OSError as exc:
        return type(exc).__name__


key = f"{{workflow}}-{{agent}}-{{state}}-{{attempt}}"
over = key in scenario.get("over_budget", [])
events = [{{"type": "system", "subtype": "init"}}]
if over:
    out = None  # stopped at the budget before doing anything
elif agent == "implementer" and state == "DISCOVERY":
    open("check.sh", "w").write("python3 -c 'from calc import add; assert add(2, 3) == 5'\n")
    git("add", "check.sh")
    git("commit", "-q", "-m", "regression check")
    out = {{"reproduction_command": "sh check.sh", "reproduction_fails": True, "changes": []}}
elif agent == "implementer":
    if scenario.get("fix", True):
        open("calc.py", "w").write("def add(a, b):\n    return a + b\n")
        if workflow == "bug-fix":
            git("commit", "-q", "-am", "fix add")  # the single session leaves it uncommitted
    out = {{"reproduction_command": "sh check.sh", "reproduction_fails": False,
           "changes": [{{"file": "calc.py", "summary": "add"}}]}}
elif workflow == "probe":
    events.append({{"type": "assistant", "message": {{"content": [
        {{"type": "tool_use", "id": "t1", "name": "Bash", "input": {{"command": "git show HEAD"}}}}]}}}})
    events.append({{"type": "user", "message": {{"content": [
        {{"type": "tool_result", "tool_use_id": "t1", "is_error": not scenario.get("bash", True)}}]}}}})
    out = {{"verdict": "changes", "findings": [{{"file": "calc.py", "requirement_id": "AC-1", "severity": "major", "fix": "add"}}]}}
else:
    out = {{"verdict": "approve", "findings": []}}

log = os.path.join(os.environ["TMPDIR"], "fake-" + key + ".json")  # the one place a sandboxed fake may write
json.dump({{"argv": argv, "budget": budget, "peek": peek(), "env_keys": sorted(os.environ)}}, open(log, "w"))
events.append({{"type": "result", "subtype": "error_max_budget_usd" if over else "success", "is_error": over,
               "result": "done", "total_cost_usd": scenario["cost"].get(key, scenario["cost"]["default"]),
               "num_turns": 1, "session_id": "fake", "structured_output": out,
               "usage": {{"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5,
                          "server_tool_use": {{"web_search_requests": 0}}}}}})
for event in events:
    print(json.dumps(event), flush=True)
'''

FAKE_MINI = r'''#!{python}
"""Fake mini-swe-agent: fixes calc.py in the working directory and writes a trajectory."""
import json, os, sys

argv = sys.argv[1:]
arg = lambda flag: argv[argv.index(flag) + 1]
scenario = json.load(open({scenario!r}))
try:
    open(scenario["peek"]).read()
    peek = "read"
except OSError as exc:
    peek = type(exc).__name__
open("calc.py", "w").write("def add(a, b):\n    return a + b\n")
status = scenario.get("mini_status", "Submitted")
traj = {{"info": {{"model_stats": {{"instance_cost": scenario["mini_cost"], "api_calls": 3}}, "exit_status": status,
                 "submission": ""}},
        "messages": [{{"role": "assistant", "extra": {{"response": {{"usage": {{"prompt_tokens": 300, "completion_tokens": 40}}}}}}}}]}}
json.dump(traj, open(arg("--output"), "w"))
env = {{k: ("<set>" if k == "ANTHROPIC_API_KEY" else v) for k, v in os.environ.items()}}
json.dump({{"argv": argv, "peek": peek, "env": env, "cwd": os.getcwd()}}, open(os.path.join(os.environ["TMPDIR"], "fake-mini.json"), "w"))
print("mini done")
'''

FAKE_GH = r'''#!{python}
import json, os, sys
json.dump(sys.argv[1:], open(os.path.join({log!r}, "gh-%d.json" % os.getpid()), "w"))
sys.exit(1)
'''


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.log = tmp_path / "gh-log"  # gh runs outside every sandbox; the sandboxed fakes log into their $TMPDIR
        self.bin.mkdir()
        self.log.mkdir()
        self.evals = make_evals(tmp_path)
        self.host = tmp_path / "host"
        self.host.mkdir()
        git(self.host, "init", "--quiet", "-b", "main")
        (self.host / ".gitignore").write_text(".agent-team/\n", encoding="utf-8")
        self.scenario_path = tmp_path / "scenario.json"
        self.play()
        for name, template in (("claude", FAKE_CLAUDE), ("mini", FAKE_MINI), ("gh", FAKE_GH)):
            path = self.bin / name
            path.write_text(template.format(python=sys.executable, scenario=str(self.scenario_path), log=str(self.log)))
            path.chmod(0o755)

    @property
    def hidden_file(self) -> Path:
        return (self.evals / "fixtures" / "calc-add" / "verifier" / "tests" / "test_hidden_calc.py").resolve()

    def play(self, **scenario: Any) -> None:
        data = {"cost": {"default": 0.1}, "mini_cost": 0.2, "peek": str(self.hidden_file)}
        data.update(scenario)
        self.scenario_path.write_text(json.dumps(data), encoding="utf-8")

    def calls(self) -> dict[str, dict[str, Any]]:
        """What each sandboxed fake recorded, keyed `<workflow>-<agent>-<state>-<attempt>` or `mini`."""
        work = self.host / ".agent-team" / "eval" / "work"
        return {p.stem[len("fake-"):]: json.loads(p.read_text()) for p in sorted(work.rglob("fake-*.json"))}

    def cli(self, *args: str) -> Any:
        return runner.invoke(app, ["eval", "run", "--tasks-dir", str(self.evals / "tasks"), "--canonical", str(CANONICAL),
                                   "--repo", str(self.host), "--timeout", "120", *args])  # fmt: skip

    def store(self) -> EvalStore:
        return EvalStore.open(self.host)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    w = World(tmp_path)
    monkeypatch.setenv("PATH", f"{w.bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return w


# --- tasks and fixtures ------------------------------------------------------------------------------


def test_the_ten_tasks_load_with_the_planned_mix() -> None:
    tasks = load_tasks(TASKS)
    assert len(tasks) == 10
    assert Counter(t.task.kind for t in tasks) == {"locate-bug": 3, "fix-bug": 3, "small-feature": 2, "repair-failing-test": 2}
    for loaded in tasks:
        task = loaded.task
        assert task.repo == f"fixture:{task.id}" and task.test_command and task.existing_tests and task.planned_files
        verifier = loaded.verifier_dir
        assert verifier is not None and verifier.is_dir()
        assert not verifier.is_relative_to(loaded.fixture_dir / "repo"), "the verifier is never inside the repo"


def test_a_bad_task_names_the_file_and_the_field(tmp_path: Path) -> None:
    evals = make_evals(tmp_path)
    path = evals / "tasks" / "calc-add.yaml"
    path.write_text(path.read_text().replace("kind: fix-bug", "kind: refactor"))
    with pytest.raises(TaskError, match=r"calc-add\.yaml: kind: "):
        load_tasks(evals / "tasks")
    path.write_text(path.read_text().replace("kind: refactor", "kind: fix-bug").replace("id: calc-add", "id: other"))
    with pytest.raises(TaskError, match="must match the file stem"):
        load_tasks(evals / "tasks")
    with pytest.raises(TaskError, match="unknown task"):
        load_tasks(TASKS, ["no-such-task"])


def test_every_fixture_materializes_to_its_ref_without_its_verifier(tmp_path: Path) -> None:
    for loaded in load_tasks(TASKS):
        dest = tmp_path / loaded.task.id
        base = materialize(loaded, dest)
        assert base == loaded.task.ref == git(dest, "rev-parse", "HEAD").stdout.strip()
        assert git(dest, "status", "--porcelain").stdout == ""
        assert git(dest, "remote").stdout.strip() == "", "no remote: nothing can push back"
        assert git(dest, "check-ignore", "-q", "--no-index", ".agent-team/runs.db", check=False).returncode == 0
        assert not (dest / ".gitignore").read_text().count(".agent-team"), "ignored through info/exclude, not a tracked file"
        hidden = {p.name for p in loaded.verifier_dir.rglob("test_hidden_*.py")}
        assert hidden and not hidden & {p.name for p in dest.rglob("*.py")}


def test_a_changed_fixture_is_refused(tmp_path: Path) -> None:
    evals = make_evals(tmp_path)
    (evals / "fixtures" / "calc-add" / "repo" / "calc.py").write_text("def add(a, b):\n    return a * b\n")
    [loaded] = load_tasks(evals / "tasks")
    with pytest.raises(TaskError, match="materializes to"):
        materialize(loaded, tmp_path / "out")


def test_a_task_can_point_at_a_real_repository_and_ref(tmp_path: Path) -> None:
    source = tmp_path / "upstream"
    source.mkdir()
    git(source, "init", "--quiet", "-b", "main")
    write_tree(source, CALC_REPO)
    git(source, "add", ".")
    git(source, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-q", "-m", "buggy")
    buggy = git(source, "rev-parse", "HEAD").stdout.strip()
    (source / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git(source, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-q", "-am", "the fix")
    fixed = git(source, "rev-parse", "HEAD").stdout.strip()
    git(source, "tag", "fixed-tag")
    write_tree(tmp_path / "evals" / "hidden", {"tests/test_hidden_calc.py": HIDDEN_TEST})
    (tmp_path / "evals" / "tasks").mkdir(parents=True)
    (tmp_path / "evals" / "tasks" / "real.yaml").write_text(
        f"id: real\nkind: fix-bug\nrepo: {source}\nref: {buggy[:12]}\ninstruction: fix add\n"
        "planned_files: [calc.py]\nverifier: {command: python3 -m unittest tests.test_hidden_calc, files: ../hidden}\n"
    )
    [loaded] = load_tasks(tmp_path / "evals" / "tasks")
    dest = tmp_path / "copy"
    assert materialize(loaded, dest) == buggy
    assert "a - b" in (dest / "calc.py").read_text()
    assert git(dest, "remote").stdout.strip() == ""
    assert git(dest, "cat-file", "-e", fixed, check=False).returncode != 0, "the later commit (the fix) is gone"
    assert git(dest, "tag").stdout.strip() == ""


# --- snapshot, diff, verifier -------------------------------------------------------------------------


def test_planned_globs() -> None:
    assert planned("tests/test_x.py", ["tests/**"]) and planned("tests/a/b.py", ["tests/**"])
    assert planned("calc.py", ["calc.py"]) and not planned("check.sh", ["calc.py", "tests/**"])


@pytest.mark.srt
def test_the_verifier_runs_hidden_and_inside_srt(tmp_path: Path, srt: str) -> None:
    evals = make_evals(tmp_path)
    [loaded] = load_tasks(evals / "tasks")
    repo = tmp_path / "cell" / "repo"
    base = materialize(loaded, repo)
    command = "python3 -m unittest tests.test_hidden_calc"
    deny = [loaded.verifier_dir]

    def verify(commit: str, name: str) -> CheckResult:
        [result] = check_commands(repo, commit, tmp_path / name, [command], files=loaded.verifier_dir, srt=srt,
                                  deny_read=deny, timeout_s=60)  # fmt: skip
        return result

    assert not verify(base, "before").passed, "the bug is there at the ref"
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "notes.txt").write_text("scratch\n")
    snap = snapshot(repo, repo)
    assert snap.committed == 2
    after = verify(snap.commit, "after")
    assert after.passed, after.output_tail
    assert not (repo / "tests" / "test_hidden_calc.py").exists(), "the hidden test never reaches the agent's tree"
    stats = diff_stats(repo, base, snap.commit, ["calc.py", "tests/**"])
    assert stats.files == ["calc.py", "notes.txt"] and stats.unplanned_files == ["notes.txt"] and stats.unplanned_lines == 1

    # Inside the verifier's sandbox: no writes outside its tree, no reads of the denied paths.
    probe = [
        f"cat {loaded.verifier_dir / 'tests' / 'test_hidden_calc.py'}",
        f"touch {tmp_path / 'escape.txt'}",
        "touch inside.txt",
    ]
    results = check_commands(repo, snap.commit, tmp_path / "probe", probe, files=None, srt=srt, deny_read=deny, timeout_s=60)
    assert [r.passed for r in results] == [False, False, True]
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.srt
def test_a_committed_symlink_cannot_aim_the_overlay_outside(tmp_path: Path, srt: str) -> None:
    evals = make_evals(tmp_path)
    [loaded] = load_tasks(evals / "tasks")
    repo = tmp_path / "repo"
    materialize(loaded, repo)
    victim = tmp_path / "victim.py"
    victim.write_text("keep\n")
    (repo / "tests" / "test_hidden_calc.py").symlink_to(victim)
    snap = snapshot(repo, repo)
    [result] = check_commands(repo, snap.commit, tmp_path / "v", ["true"], files=loaded.verifier_dir, srt=srt,
                              deny_read=[], timeout_s=60)  # fmt: skip
    assert victim.read_text() == "keep\n", "the overlay never writes through a symlink the agent committed"
    assert not result.passed and "extracted safely" in (result.error or "")


def test_a_planted_commondir_runs_nothing_when_the_harness_snapshots_a_worktree(tmp_path: Path) -> None:
    """The run's srt settings let an agent rewrite .git/worktrees/<id>/; the snapshot must not trust it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-q", "-m", "init")
    tree = tmp_path / "wt-run1"
    git(repo, "worktree", "add", "--quiet", "-b", "agent-team/run1", str(tree))
    marker = tmp_path / "PWNED"
    evil = tree / "evil.sh"
    evil.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
    evil.chmod(0o755)
    shutil.copytree(repo / ".git", tree / "fakecommon", ignore=shutil.ignore_patterns("worktrees"))
    with (tree / "fakecommon" / "config").open("a") as f:
        f.write(f"[core]\n\tfsmonitor = {evil}\n[filter \"x\"]\n\tclean = {evil}\n")
    (tree / ".gitattributes").write_text("*.txt filter=x\n")
    (tree / "a.txt").write_text("changed\n")
    (repo / ".git" / "worktrees" / tree.name / "commondir").write_text(f"{tree / 'fakecommon'}\n")
    snap = snapshot(tree, repo)
    assert not marker.exists(), "git ran a command from a config the agent planted"
    assert snap.committed >= 2 and git(repo, "cat-file", "-e", snap.commit, check=False).returncode == 0


# --- the dry run and the estimate -------------------------------------------------------------------


def test_the_dry_run_is_the_default_and_calls_no_model(world: World) -> None:
    result = runner.invoke(app, ["eval", "run", "--tasks-dir", str(TASKS), "--canonical", str(CANONICAL),
                                 "--repo", str(world.host)])  # fmt: skip
    assert result.exit_code == 0, result.output
    out = result.output
    assert "10 task(s) x 3 arm(s) x 1 run(s) = 30 cell(s)" in out
    assert "worst case  $30.10: 30 cell(s) at the $1.00 cap plus the probe" in out
    assert "total cap   $15.00 holds: in the worst case 14 cell(s) start and 16 are recorded not_run" in out
    assert "expected    not estimated: no earlier cell in this store has a recorded cost" in out
    assert "refused: ANTHROPIC_API_KEY is not set" in out
    assert "mini-swe-agent 2.4.6, one shot; claude-opus-5-5" in out
    assert "implementer claude-sonnet-5-5, reviewer claude-opus-5-5" in out
    assert "probe  one sandboxed-Bash check on claude-haiku-5-5 first, at most $0.10" in out
    assert "dry run: no model was called" in out
    assert world.calls() == {}, "no fake binary was asked to do any work"
    [dry] = [r for r in world.store().records() if r.record == "dry_run"]
    assert dry.plan.caps == Caps(per_task_usd=1.0, total_usd=15.0) and dry.plan.runs == 1


def test_execute_needs_a_dry_run_of_the_same_plan_then_refuses_an_arm_without_auth(world: World) -> None:
    first = world.cli("--execute")
    assert first.exit_code == 1, first.output
    assert "has not been dry-run before, so this was its dry run" in first.output
    second = world.cli("--execute")
    assert second.exit_code == 2
    assert "cannot run: baseline-a: ANTHROPIC_API_KEY is not set" in second.output
    assert "cannot use a Claude Code login" in second.output
    changed = world.cli("--execute", "--max-total-usd", "9")
    assert changed.exit_code == 1 and "has not been dry-run before" in changed.output, "a different plan needs its own"
    assert world.calls() == {}
    assert world.store().evals() == []


def test_the_estimate_uses_recorded_costs_only() -> None:
    plan = build_plan([], [ArmPlan(arm="baseline-b", description="b", model="m"), ArmPlan(arm="treatment", description="t", model="m")],
                      runs=2, caps=Caps(per_task_usd=1.0, total_usd=3.0), timeout_s=10, probe_on=False)  # fmt: skip
    plan = plan.model_copy(update={"tasks": ["a", "b"]})
    est = estimate(plan, [])
    assert (est.cells, est.worst_case_usd, est.cells_within_total, est.expected_usd) == (8, 8.0, 3, None)
    history = [cell("baseline-b", cost=0.2), cell("baseline-b", cost=0.4), cell("treatment", cost=0.9),
               cell("treatment", status="not_run", cost=None)]  # fmt: skip
    est = estimate(plan, history)
    assert est.observed == {"baseline-b": (2, pytest.approx(0.3)), "treatment": (1, 0.9)}
    assert est.expected_usd == pytest.approx(4 * 0.3 + 4 * 0.9)


# --- end to end with the fakes ------------------------------------------------------------------------


def read_cells(world: World) -> dict[str, CellResult]:
    return {c.arm: c for c in world.store().cells()}


@pytest.mark.srt
def test_all_three_arms_run_capped_verified_and_never_see_the_verifier(
    world: World, srt: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    assert world.cli().exit_code == 0
    result = world.cli("--execute")
    assert result.exit_code == 0, result.output
    store = world.store()
    [record] = store.evals()
    [probe] = store.probes(record.eval_id)
    assert probe.passed and probe.model == "claude-haiku-5-5", probe.reason
    cells = read_cells(world)
    assert set(cells) == {"baseline-a", "baseline-b", "treatment"}
    for arm, c in cells.items():
        assert c.status == "completed", (arm, c.error, c.notes)
        assert c.verifier is not None and c.verifier.passed, (arm, c.verifier)
        assert c.existing_tests == [ExistingTestResult(command="python3 -m unittest tests.test_calc.CalcTest.test_zero",
                                                       base_passed=True, after_passed=True)]  # fmt: skip
        assert c.human_interventions == 0 and c.wall_time_s is not None and not c.over_cap
        assert Path(c.work_dir).is_relative_to(store.work_dir / record.eval_id)

    a, b, t = cells["baseline-a"], cells["baseline-b"], cells["treatment"]
    assert a.cost_usd == 0.2 and a.tokens == {"prompt_tokens": 300, "completion_tokens": 40} and a.retries == 0
    assert b.cost_usd == pytest.approx(0.1) and b.tokens == {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5}
    assert t.cost_usd == pytest.approx(0.3), "implementer twice and the reviewer"
    assert t.tokens == {"input_tokens": 300, "output_tokens": 60, "cache_read_input_tokens": 15} and t.retries == 0
    assert a.model == "claude-opus-5-5 (through litellm)" and b.model == "claude-opus-5-5"
    assert any("committed 1 path(s)" in n for n in b.notes), "the single session left its fix uncommitted"
    assert t.diff.unplanned_files == ["check.sh"] and t.diff.unplanned_lines == 1
    assert a.diff.files == ["calc.py"] and a.diff.unplanned_lines == 0

    # What the fakes saw: the budget, the models, the scrubbed environments, and no verifier file.
    calls = world.calls()
    assert calls["single-session-implementer-IMPLEMENTATION-1"]["budget"] == "1.00", "baseline-b's cap"
    budgets = [calls[f"bug-fix-{k}"]["budget"] for k in
               ("implementer-DISCOVERY-1", "implementer-IMPLEMENTATION-1", "independent-reviewer-CODE_REVIEW-1")]
    assert budgets == ["1.00", "0.90", "0.80"], "the treatment hands each call what is left of the cell's cap"
    model = calls["single-session-implementer-IMPLEMENTATION-1"]["argv"]
    assert model[model.index("--model") + 1] == "claude-opus-5-5"
    mini = calls["mini"]
    assert mini["argv"][:2] == ["--model", "anthropic/claude-opus-5-5"]
    for flag, value in (("--cost-limit", "1.00"), ("--agent-class", "default")):
        assert mini["argv"][mini["argv"].index(flag) + 1] == value
    assert "--yolo" in mini["argv"] and "--exit-immediately" in mini["argv"]
    assert mini["env"]["ANTHROPIC_API_KEY"] == "<set>" and mini["env"]["MSWEA_CONFIGURED"] == "true"
    assert mini["env"]["LITELLM_LOCAL_MODEL_COST_MAP"] == "True" and mini["env"]["LITELLM_MODE"] == "PRODUCTION"
    peeks = {k: v["peek"] for k, v in calls.items() if "peek" in v}
    assert peeks and all(v != "read" for v in peeks.values()), f"an arm read the hidden verifier: {peeks}"
    assert not list(world.log.glob("gh-*.json")), "the eval never calls gh"
    lines = store.path.read_text()
    assert FAKE_KEY not in lines

    report = runner.invoke(app, ["eval", "report", "--repo", str(world.host)])
    assert report.exit_code == 0, report.output
    assert "completion rate" in report.output and "1/1 (100%)" in report.output
    assert "verdict: " in report.output


@pytest.mark.srt
def test_the_per_task_cap_stops_the_treatment_and_the_single_session(world: World, srt: str) -> None:
    world.play(cost={"default": 0.6},
               over_budget=["single-session-implementer-IMPLEMENTATION-1", "bug-fix-implementer-IMPLEMENTATION-1"])
    assert world.cli("--arms", "baseline-b,treatment", "--skip-probe").exit_code == 0
    result = world.cli("--arms", "baseline-b,treatment", "--skip-probe", "--execute")
    assert result.exit_code == 0, result.output
    cells = read_cells(world)
    b, t = cells["baseline-b"], cells["treatment"]
    assert b.status == "aborted" and b.abort_reason.startswith("per-task budget: claude stopped at --max-budget-usd 1.00")
    assert t.status == "aborted" and t.abort_reason.startswith("per-task budget:"), t.abort_reason
    calls = world.calls()
    assert calls["bug-fix-implementer-DISCOVERY-1"]["budget"] == "1.00"
    assert calls["bug-fix-implementer-IMPLEMENTATION-1"]["budget"] == "0.40"
    assert "bug-fix-independent-reviewer-CODE_REVIEW-1" not in calls, "no call starts once the cap is spent"
    assert t.cost_usd == pytest.approx(1.2) and t.over_cap, "one call may overshoot; the cell says so"
    assert not t.verifier.passed and not b.verifier.passed


@pytest.mark.srt
def test_the_total_cap_records_the_cells_it_kept_from_starting(world: World, srt: str) -> None:
    world.play(cost={"default": 0.6})
    args = ("--arms", "baseline-b,treatment", "--skip-probe", "--max-total-usd", "1.5")
    assert world.cli(*args).exit_code == 0
    result = world.cli(*args, "--execute")
    assert result.exit_code == 0, result.output
    cells = read_cells(world)
    assert cells["baseline-b"].status == "completed" and cells["baseline-b"].cost_usd == pytest.approx(0.6)
    t = cells["treatment"]
    assert t.status == "not_run" and t.cost_usd is None and t.work_dir is None
    assert t.abort_reason == "total budget: $0.6000 of $1.50 spent; this cell's $1.00 cap does not fit"
    assert not any(k.startswith("bug-fix") for k in world.calls()), "the treatment never started"


@pytest.mark.srt
def test_a_failed_probe_stops_the_eval_before_any_cell(world: World, srt: str) -> None:
    world.play(bash=False)
    args = ("--arms", "baseline-b")
    assert world.cli(*args).exit_code == 0
    result = world.cli(*args, "--execute")
    assert result.exit_code == 1
    assert "the probe failed, so no cell ran: no Bash call succeeded inside srt" in result.output
    store = world.store()
    [probe] = store.probes(store.evals()[0].eval_id)
    assert not probe.passed and probe.cost_usd == pytest.approx(0.1)
    assert store.cells() == []


# --- budgets and the capped executor, without srt ---------------------------------------------------


class StubInner:
    def __init__(self, cost: float) -> None:
        self.max_budget_usd = 1.0
        self.cost = cost
        self.seen: list[float] = []

    def __call__(self, node: Any, given: Any) -> NodeResult:
        self.seen.append(self.max_budget_usd)
        return NodeResult(cost_usd=self.cost if node.agent else 0.0)


class StubNode:
    def __init__(self, agent: str | None) -> None:
        self.agent = agent


class StubInput:
    state = "IMPLEMENTATION"
    attempt = 2


def test_the_capped_executor_hands_out_what_is_left_and_then_refuses() -> None:
    inner = StubInner(cost=0.48)
    capped = CappedExecutor(inner, cap_usd=1.0)  # type: ignore[arg-type]
    for _ in range(2):
        capped(StubNode("implementer"), StubInput())  # type: ignore[arg-type]
    capped(StubNode(None), StubInput())  # type: ignore[arg-type]  # an in-process step costs nothing and is not capped
    assert inner.seen == [1.0, pytest.approx(0.52), pytest.approx(0.52)]
    blocked = capped(StubNode("independent-reviewer"), StubInput())  # type: ignore[arg-type]
    assert blocked.blocked and capped.aborted.startswith("per-task budget: $0.9600 of $1.00 spent before IMPLEMENTATION")
    assert len(inner.seen) == 3


def test_a_cell_with_unknown_cost_is_charged_its_whole_cap() -> None:
    assert charged(cell("baseline-a", cost=None, status="error")) == 1.0
    assert charged(cell("baseline-a", cost=0.25)) == 0.25
    assert charged(cell("baseline-a", cost=None, status="not_run")) == 0.0


# --- the report ---------------------------------------------------------------------------------------


def cell(arm: str, *, task: str = "t1", run: int = 1, cost: float | None = 0.1, passed: bool = True,
         status: str = "completed", regressed: int = 0, retries: int = 0, unplanned: int = 0,
         tokens: dict[str, int] | None = None) -> CellResult:  # fmt: skip
    tests = [ExistingTestResult(command=f"t{i}", base_passed=True, after_passed=i >= regressed) for i in range(2)]
    ran = status != "not_run"
    return CellResult(
        eval_id="e1", task_id=task, task_kind="fix-bug", arm=arm, run_index=run, status=status,  # type: ignore[arg-type]
        started_at=datetime(2026, 10, 24, tzinfo=UTC), cost_cap_usd=1.0, cost_usd=cost if ran else None,
        wall_time_s=10.0 if ran else None, retries=retries if ran else None, human_interventions=0 if ran else None,
        verifier=CheckResult(command="v", exit_code=0 if passed else 1, duration_ms=1, passed=passed) if ran else None,
        existing_tests=tests if ran else [], diff=DiffStats(unplanned_lines=unplanned) if ran else None,
        tokens=tokens, tokens_note=None if tokens else ("no usage" if ran else None),
        abort_reason=None if ran else "total budget: no room",
    )  # fmt: skip


def test_report_math_and_the_decision_rule() -> None:
    cells = [
        cell("baseline-b", task="t1", cost=0.2), cell("baseline-b", task="t2", cost=0.2, passed=False, regressed=1),
        cell("baseline-b", task="t3", cost=0.2, passed=False),
        cell("treatment", task="t1", cost=0.3, retries=1, unplanned=4), cell("treatment", task="t2", cost=0.3),
        cell("treatment", task="t3", cost=0.3, passed=False),
    ]
    b, t = aggregate("baseline-b", cells[:3]), aggregate("treatment", cells[3:])
    assert (b.completion, t.completion) == (pytest.approx(1 / 3), pytest.approx(2 / 3))
    assert (b.regressions, b.regression_cells, b.regressions_per_cell) == (1, 1, pytest.approx(1 / 3))
    assert (b.tests_passed, b.tests_run) == (5, 6)
    assert (b.cost_per_success, t.cost_per_success) == (pytest.approx(0.6), pytest.approx(0.45))
    assert (t.retries, t.unplanned_lines, t.interventions_per_success) == (1, 4, 0.0)
    lines = decision(cells, planned_tasks=3, runs=1)
    text = "\n".join(lines)
    assert "completion     treatment 67% vs best baseline 33% (baseline-b): beats" in text
    assert "regressions    treatment 0.00/cell vs best baseline 0.33/cell (baseline-b): beats" in text
    assert "interventions  treatment 0.00/pass vs best baseline 0.00/pass (baseline-b): tie, not a win" in text
    assert "cost           treatment $0.45/pass vs 2 x $0.60 (baseline-b) = $1.20: within" in text
    assert lines[-2].startswith("verdict: PASS (2 of 3")
    assert "baseline-a ran nothing and is left out" in text

    # Twice the baseline's cost per pass fails the rule whatever it wins.
    pricey = [c.model_copy(update={"cost_usd": 2.0}) if c.arm == "treatment" else c for c in cells]
    assert decision(pricey, planned_tasks=3, runs=1)[-2].startswith("verdict: FAIL")
    assert decision([c for c in cells if c.arm == "baseline-b"], planned_tasks=3, runs=1)[-1].startswith(
        "verdict: INCONCLUSIVE: the treatment ran no cell")


def test_the_decision_compares_only_task_runs_every_arm_ran() -> None:
    cells = [cell("baseline-b", task="t1"), cell("treatment", task="t1", passed=False),
             cell("baseline-b", task="t2", status="not_run"), cell("treatment", task="t2")]  # fmt: skip
    text = "\n".join(decision(cells, planned_tasks=2, runs=1))
    assert "compared on 1 task-run(s)" in text and "treatment 0% vs best baseline 100%" in text


def test_the_report_prints_not_measured_with_reasons_and_no_invented_numbers() -> None:
    plan = build_plan([], [ArmPlan(arm="baseline-a", description="a", model="m", refusal="ANTHROPIC_API_KEY is not set"),
                           ArmPlan(arm="baseline-b", description="b", model="m"), ArmPlan(arm="treatment", description="t", model="m")],
                      runs=1, caps=Caps(per_task_usd=1.0, total_usd=15.0), timeout_s=10, probe_on=False)  # fmt: skip
    record = EvalRecord(eval_id="e1", started_at=datetime(2026, 10, 24, tzinfo=UTC), plan=plan)
    cells = [cell("baseline-b"), cell("treatment", tokens={"input_tokens": 1500, "output_tokens": 200})]
    out = render_report(record, cells, [])
    rows = {line.split("  ")[0]: line for line in out.splitlines() if line and not line.startswith(("(", " "))}
    assert "not measured (1)" in rows["completion rate"], "baseline-a: every row points at why it did not run"
    assert "(1) baseline-a: no cell ran (ANTHROPIC_API_KEY is not set)" in out
    assert "baseline-b: no usage" in out and "in 1.5k / out 200" in rows["tokens"]
    assert rows["review defects"].count("not measured") == 3 and "review defects: needs the findings a human" in out
    assert rows["context consumed"].count("not measured") == 3 and "context consumed: no runtime reports" in out
    assert "$0.10 / 0.00" in rows["$ / interventions per pass"]
    assert "retries" in rows and "0 (one shot)" in rows["retries"]


# --- eval --last, the adapter's token counts, the environment's deny_read ------------------------------


def test_eval_last_records_the_last_workflow_run_once(tmp_path: Path) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    git(repo, "init", "--quiet", "-b", "main")
    (repo / ".gitignore").write_text(".agent-team/\n")
    with RunStore.open(repo) as store:
        run = store.start_run("bug-fix")
        for state in ("DISCOVERY", "IMPLEMENTATION", "TEST", "CODE_REVIEW", "IMPLEMENTATION", "RESUME", "IMPLEMENTATION"):
            store.transition(run.run_id, state)  # type: ignore[arg-type]
        store.add_cost(run.run_id, 0.42)
        store.finish_run(run.run_id, "blocked")
        store.start_run("agent:implementer")  # a child run is newer, and is not what --last records
    first = runner.invoke(app, ["eval", "--last", "--repo", str(repo)])
    assert first.exit_code == 0, first.output
    assert f"run {run.run_id} (bug-fix)" in first.output and "$0.4200" in first.output
    assert "retries              1" in first.output and "human interventions  1 (RESUME transitions)" in first.output
    assert "completion           not measured: a real task has no hidden verifier" in first.output
    assert "recorded in .agent-team/eval/results.jsonl" in first.output
    again = runner.invoke(app, ["eval", "--last", "--repo", str(repo)])
    assert "already in" in again.output and len(EvalStore.open(repo).fields()) == 1


def test_eval_last_without_a_workflow_run_says_so(tmp_path: Path) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    git(repo, "init", "--quiet", "-b", "main")
    (repo / ".gitignore").write_text(".agent-team/\n")
    result = runner.invoke(app, ["eval", "--last", "--repo", str(repo)])
    assert result.exit_code == 2 and "no run record" in result.output


def test_the_claude_adapter_keeps_integer_token_counts() -> None:
    parser = StreamParser()
    parser.feed(json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01,
                            "usage": {"input_tokens": 7, "output_tokens": 3, "service_tier": "standard",
                                      "server_tool_use": {"web_search_requests": 0}}}), 0.0, datetime.now(UTC))  # fmt: skip
    result = normalize(parser, exit_code=0, timed_out=False, stderr="", output_model=None)
    assert result.usage == {"input_tokens": 7, "output_tokens": 3}


def test_an_environment_can_deny_reading_extra_paths(tmp_path: Path) -> None:
    spec = EnvironmentSpec(kind="environment", name="eval", description="d", tier="srt", deny_read=["/opt/hidden", "~/secret"])
    settings = build_settings(spec, run_id="r", worktree=tmp_path / "w", git_dir=tmp_path / ".git", tmp_dir=tmp_path / "t",
                              home=Path("/home/u"))  # fmt: skip
    assert settings["filesystem"]["denyRead"][-2:] == ["/opt/hidden", "/home/u/secret"]
    with pytest.raises(ValidationError):
        EnvironmentSpec(kind="environment", name="eval", description="d", tier="srt", deny_read=["relative/path"])


def test_the_eval_group_shows_help_without_a_subcommand() -> None:
    result = runner.invoke(app, ["eval"])
    assert result.exit_code == 0 and "run" in result.output and "report" in result.output and "--last" in result.output


def test_mini_swe_agent_is_never_a_default_dependency() -> None:
    import tomllib

    project = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert not any("mini-swe-agent" in d for d in project["project"]["dependencies"])
    assert "mini-swe-agent==2.4.6" in project["dependency-groups"]["baseline"]
    assert not any("mini-swe-agent" in d for d in project["dependency-groups"]["dev"])


def test_the_harness_never_runs_a_task_command_without_srt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_team.eval import arms

    monkeypatch.setattr(arms, "srt_path", lambda: None)
    reason = arms.refusal("baseline-a", claude=None, mini="/bin/true", environ={"ANTHROPIC_API_KEY": "k"})
    assert reason == "srt is not installed; every arm and the verifier run inside it"
