"""Orchestrator (build-order step 10): DAG from the bug-fix YAML, the state machine, checkpoints, resume.

Nodes run through a scripted fake executor, except the in-process steps that
exist (intake, plan, human_decision) and, in one test, the real reviewer path
through `run_agent` with the fake `claude` from the adapter tests. No test
calls a model.
"""

import json
import os
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from test_claude_adapter import install_fake_claude
from typer.testing import CliRunner

from agent_team import cli
from agent_team.canonical import validate_tree
from agent_team.cli import app
from agent_team.environments import acquire, read_lock
from agent_team.orchestrator import (
    STEPS,
    DagError,
    DagNode,
    DefaultExecutor,
    NodeInput,
    NodeResult,
    OrchestratorError,
    build_dag,
    default_executor_factory,
    load_dag,
    resume_workflow,
    run_workflow,
    start,
    walk,
)
from agent_team.run_agent import load_canonical
from agent_team.runs import RunStore, migrate
from agent_team.runs.render import status_line
from agent_team.runs.store import MIGRATIONS, SCHEMA_VERSION
from agent_team.schemas import WorkflowSpec

REPO = Path(__file__).resolve().parents[1]
CANONICAL = REPO / "canonical"
BUG_FIX = CANONICAL / "workflows" / "bug-fix.yaml"
T0 = datetime(2026, 10, 24, 9, 0, 0, tzinfo=UTC)
SECRET = "ghp_ORCHESTRATORLEAKCANARY0000"
runner = CliRunner()

HAPPY_PATH = ["INTAKE", "DISCOVERY", "PLAN", "IMPLEMENTATION", "TEST", "CODE_REVIEW", "PR", "HUMAN_DECISION"]


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=5)
        return self.now


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text(".agent-team/\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    return root.resolve()


@pytest.fixture
def store(repo: Path):
    opened = RunStore.open(repo, clock=FakeClock())
    assert opened is not None
    with opened:
        yield opened


@pytest.fixture
def dag():
    canonical = load_canonical(CANONICAL)
    return load_dag(canonical, "bug-fix", "default")


Script = dict[str, list[NodeResult | Callable[[NodeInput], NodeResult] | BaseException]]


def ok(**evidence: Any) -> NodeResult:
    return NodeResult(evidence=evidence, cost_usd=0.01)


def happy() -> Script:
    return {
        "DISCOVERY": [ok(reproduction_command="uv run pytest tests/test_bug.py", reproduction_fails=True)],
        "IMPLEMENTATION": [ok(diff="--- a/app.py\n+++ b/app.py\n")],
        "TEST": [ok(test_output="1 passed", tests_pass=True)],
        "CODE_REVIEW": [ok(reviewer_verdict="approve", findings=[])],
        "PR": [ok(pr="draft")],
    }


FINDING = {"file": "app.py", "line": 1, "requirement_id": "AC-1", "severity": "major", "fix": "handle None"}


class FakeExecutor:
    """Pops a scripted result per state; real in-process steps run when a state has no script."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.calls: list[NodeInput] = []

    def __call__(self, node: DagNode, given: NodeInput) -> NodeResult:
        self.calls.append(given)
        queue = self.script.get(given.state)
        if not queue:
            assert node.step in STEPS, f"no script for {given.state}"
            return STEPS[node.step](given)
        item = queue.pop(0) if len(queue) > 1 else queue[0]  # the last entry repeats
        if isinstance(item, BaseException):
            raise item
        return item(given) if callable(item) else item

    def states(self) -> list[str]:
        return [c.state for c in self.calls]


def run_fake(store: RunStore, dag, script: Script, task: str = "Fix the crash on empty input.") -> tuple[Any, FakeExecutor, list[str]]:
    lines: list[str] = []
    fake = FakeExecutor(script)
    run = start(store, dag, task, echo=lines.append)
    record = walk(store, dag, fake, run.run_id, echo=lines.append)
    return record, fake, lines


def path(store: RunStore, run_id: str) -> list[str]:
    return [t.to_state for t in store.transitions(run_id)]


# --- DAG built from the YAML ---------------------------------------------------------


def test_bug_fix_yaml_validates_and_builds_a_linear_chain(dag) -> None:
    assert validate_tree(CANONICAL).ok
    assert [n.state for n in dag.nodes] == HAPPY_PATH
    assert [n.worker for n in dag.nodes] == [
        "step intake", "agent implementer", "step plan", "agent implementer",
        "step test", "agent independent-reviewer", "step pr", "step human_decision",
    ]  # fmt: skip
    assert [str(n.gate) if n.gate else None for n in dag.nodes[:-1]] == [
        None, "reproduction_fails", None, None, "tests_pass", "reviewer_verdict == approve", None,
    ]  # fmt: skip
    review = dag.node("CODE_REVIEW")
    assert [(e.kind, e.to_state, str(e.gate), e.max_traversals) for e in review.edges] == [
        ("advance", "PR", "reviewer_verdict == approve", None),
        ("retry", "IMPLEMENTATION", "reviewer_verdict == changes", 1),
        ("escalate", "PR", "reviewer_verdict == escalate", None),
    ]
    assert review.evidence == ("reviewer_verdict", "findings")
    assert review.inputs == ("plan", "diff", "test_output"), "the reviewer never sees a transcript"
    assert dag.node("HUMAN_DECISION").terminal
    assert dag.agents == ("implementer", "independent-reviewer")


def _spec(**changes: Any) -> WorkflowSpec:
    data = yaml.safe_load(BUG_FIX.read_text())
    data.update(changes)
    return WorkflowSpec.model_validate(data)


def _transitions(*replace: tuple[int, dict[str, Any]], drop: int | None = None) -> list[dict[str, Any]]:
    edges = yaml.safe_load(BUG_FIX.read_text())["transitions"]
    for index, edge in replace:
        edges[index] = edge
    if drop is not None:
        del edges[drop]
    return edges


@pytest.mark.parametrize(
    ("transitions", "message"),
    [
        (_transitions((2, {"from": "PLAN", "to": "TEST"})), "only the next state is reachable"),
        (_transitions(drop=0), "INTAKE needs exactly one edge to the next state"),
        (_transitions((6, {"from": "CODE_REVIEW", "to": "IMPLEMENTATION", "max_traversals": 1})), "retry edge"),
        (_transitions(drop=7), "no escalate edge"),
        (_transitions((4, {"from": "TEST", "to": "CODE_REVIEW", "gate": "reviewer_verdict == approve"})), "does not produce"),
        (_transitions((0, {"from": "INTAKE", "to": "DISCOVERY", "escalate": True})), "escalate edge goes forward"),
    ],
)  # fmt: skip
def test_shapes_the_linear_builder_refuses(transitions: list[dict[str, Any]], message: str) -> None:
    with pytest.raises(DagError, match=message):
        build_dag(_spec(transitions=transitions))


def test_policy_required_gates_must_appear() -> None:
    build_dag(_spec(), required_gates=["tests_pass", "reviewer_verdict"])
    with pytest.raises(DagError, match="security_review"):
        build_dag(_spec(), required_gates=["tests_pass", "security_review"])


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"states": ["INTAKE", "IMPLEMENT"]}, "run state vocabulary"),
        ({"nodes": {"INTAKE": {"step": "intake"}}}, "nodes must cover every state"),
        ({"transitions": [{"from": "INTAKE", "to": "DISCOVERY", "gate": "tests pass"}]}, "gate"),
    ],
)
def test_schema_rejects_bad_workflows(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _spec(**changes)


def test_a_node_names_one_worker() -> None:
    nodes = {**_spec().model_dump(by_alias=True)["nodes"], "PR": {"agent": "implementer", "step": "pr"}}
    with pytest.raises(ValidationError, match="exactly one of agent or step"):
        _spec(nodes=nodes)


# --- the state machine -----------------------------------------------------------------


def test_happy_path_walks_the_chain_and_checkpoints_every_state(store: RunStore, dag) -> None:
    record, fake, _ = run_fake(store, dag, happy())
    assert record.outcome == "succeeded" and record.state == "HUMAN_DECISION"
    assert path(store, record.run_id) == HAPPY_PATH
    assert fake.states() == HAPPY_PATH
    assert record.cost_usd == pytest.approx(0.05), "each node's cost lands on the workflow run"
    review = next(c for c in fake.calls if c.state == "CODE_REVIEW")
    assert set(review.inputs) == {"plan", "diff", "test_output"}, "only the inputs its node lists"
    plan = next(c for c in fake.calls if c.state == "IMPLEMENTATION").inputs["plan"]
    assert plan["acceptance_criteria"] == [
        {"id": "AC-1", "text": "the reproduction command passes: uv run pytest tests/test_bug.py"}
    ]
    checkpoint = store.last_checkpoint(record.run_id)
    assert checkpoint is not None and checkpoint.state == "HUMAN_DECISION"
    assert checkpoint.data["evidence"]["reviewer_verdict"] == "approve"
    seqs = [t.seq for t in store.transitions(record.run_id)]
    conn = sqlite3.connect(store.path)
    stored = [r[0] for r in conn.execute("SELECT seq FROM transitions WHERE checkpoint IS NOT NULL ORDER BY seq")]
    conn.close()
    assert stored == seqs, "a checkpoint with every transition"


def test_changes_gets_one_retry_then_escalates(store: RunStore, dag) -> None:
    script = happy()
    script["CODE_REVIEW"] = [ok(reviewer_verdict="changes", findings=[FINDING])]
    record, fake, _ = run_fake(store, dag, script)
    assert path(store, record.run_id) == [
        "INTAKE", "DISCOVERY", "PLAN", "IMPLEMENTATION", "TEST", "CODE_REVIEW",
        "IMPLEMENTATION", "TEST", "CODE_REVIEW", "PR", "HUMAN_DECISION",
    ]  # fmt: skip
    assert record.outcome == "blocked", "an escalated run ends blocked, with its draft PR"
    notes = [t.note or "" for t in store.transitions(record.run_id)]
    assert "retry 1/1: reviewer_verdict == changes" in notes[6]
    assert "escalated: reviewer_verdict == changes after 1/1 retries" in notes[9]
    first, second = [c for c in fake.calls if c.state == "IMPLEMENTATION"]
    assert (first.attempt, second.attempt) == (1, 2)
    assert "findings" not in first.inputs and second.inputs["findings"] == [FINDING]
    assert next(c for c in fake.calls if c.state == "PR").escalated


def test_changes_then_approve_succeeds(store: RunStore, dag) -> None:
    script = happy()
    script["CODE_REVIEW"] = [ok(reviewer_verdict="changes", findings=[FINDING]), ok(reviewer_verdict="approve", findings=[])]
    record, fake, _ = run_fake(store, dag, script)
    assert record.outcome == "succeeded"
    assert fake.states().count("IMPLEMENTATION") == 2 and not any(c.escalated for c in fake.calls)


def test_escalate_verdict_skips_the_retry(store: RunStore, dag) -> None:
    script = happy()
    script["CODE_REVIEW"] = [ok(reviewer_verdict="escalate", findings=[FINDING])]
    record, fake, _ = run_fake(store, dag, script)
    assert fake.states().count("IMPLEMENTATION") == 1
    assert path(store, record.run_id)[-3:] == ["CODE_REVIEW", "PR", "HUMAN_DECISION"]
    assert record.outcome == "blocked"


def test_failed_gate_routes_to_blocked(store: RunStore, dag) -> None:
    script = happy()
    script["TEST"] = [ok(test_output="1 failed", tests_pass=False)]
    record, fake, _ = run_fake(store, dag, script)
    assert path(store, record.run_id)[-2:] == ["TEST", "BLOCKED"]
    assert record.outcome == "blocked" and record.ended_at is not None
    assert "gate failed: tests_pass (got False)" in store.transitions(record.run_id)[-1].note
    assert "CODE_REVIEW" not in fake.states()


def test_a_string_true_does_not_pass_a_boolean_gate(store: RunStore, dag) -> None:
    script = happy()
    script["DISCOVERY"] = [ok(reproduction_command="pytest", reproduction_fails="true")]
    record, _, _ = run_fake(store, dag, script)
    assert path(store, record.run_id)[-1] == "BLOCKED"


def test_missing_evidence_routes_to_blocked(store: RunStore, dag) -> None:
    script = happy()
    script["IMPLEMENTATION"] = [ok()]
    record, _, _ = run_fake(store, dag, script)
    assert path(store, record.run_id)[-1] == "BLOCKED" and record.outcome == "blocked"
    assert "IMPLEMENTATION left no evidence ['diff']" in store.transitions(record.run_id)[-1].note


def test_a_node_blocker_routes_to_blocked(store: RunStore, dag) -> None:
    script = happy()
    script["DISCOVERY"] = [NodeResult(blocked="cannot reproduce", cost_usd=0.2)]
    record, fake, _ = run_fake(store, dag, script)
    assert path(store, record.run_id) == ["INTAKE", "DISCOVERY", "BLOCKED"]
    assert record.outcome == "blocked" and record.cost_usd == pytest.approx(0.2)
    assert "blocked: cannot reproduce" in store.transitions(record.run_id)[-1].note


def test_an_executor_error_fails_the_run_and_is_raised(store: RunStore, dag) -> None:
    script = happy()
    script["TEST"] = [RuntimeError("runner exploded")]
    with pytest.raises(RuntimeError, match="runner exploded"):
        run_fake(store, dag, script)
    [run] = store.list_runs()
    assert run.outcome == "failed" and run.state == "BLOCKED"
    assert "TEST step test: RuntimeError: runner exploded" in store.transitions(run.run_id)[-1].note


def test_one_status_line_per_transition(store: RunStore, dag) -> None:
    script = happy()
    script["CODE_REVIEW"] = [ok(reviewer_verdict="changes", findings=[FINDING])]
    record, _, lines = run_fake(store, dag, script)
    assert lines[0] == f"run {record.run_id}  workflow bug-fix"
    assert lines[1:] == [status_line(t) for t in store.transitions(record.run_id)]


def test_checkpoints_are_redacted(store: RunStore, dag) -> None:
    record, _, _ = run_fake(store, dag, happy(), task=f"Fix the login; GH_TOKEN={SECRET} reproduces it.")
    assert SECRET not in store.path.read_bytes().decode("utf-8", "replace")
    assert "[REDACTED]" in store.last_checkpoint(record.run_id).data["task"]


# --- run, interrupt, resume ------------------------------------------------------------


def factory_for(fake: FakeExecutor):
    return lambda canonical: fake


def test_resume_after_an_interrupt_reenters_the_checkpoint(repo: Path) -> None:
    script = happy()
    script["IMPLEMENTATION"] = [KeyboardInterrupt(), ok(diff="+fix")]
    first = FakeExecutor(script)
    lines: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        run_workflow(repo, CANONICAL, "bug-fix", "Fix it.", executor_factory=factory_for(first), echo=lines.append)
    assert lines[-1].startswith("interrupted in IMPLEMENTATION; the checkpoint is kept")
    assert read_lock(repo / ".agent-team") is None, "the lock is released on interrupt"
    with RunStore.open(repo, create=False) as store:
        [run] = store.list_runs()
        assert run.ended_at is None and run.state == "IMPLEMENTATION"

    second = FakeExecutor(script)
    lines.clear()
    record = resume_workflow(repo, CANONICAL, run.run_id, executor_factory=factory_for(second), echo=lines.append)
    assert record.outcome == "succeeded"
    assert second.states() == ["IMPLEMENTATION", "TEST", "CODE_REVIEW", "PR", "HUMAN_DECISION"], "earlier nodes are not re-run"
    assert second.calls[0].attempt == 1 and second.calls[0].inputs["plan"]["task"] == "Fix it."
    with RunStore.open(repo, create=False) as store:
        transitions = store.transitions(run.run_id)
        assert [t.to_state for t in transitions] == [
            "INTAKE", "DISCOVERY", "PLAN", "IMPLEMENTATION", "RESUME", "IMPLEMENTATION",
            "TEST", "CODE_REVIEW", "PR", "HUMAN_DECISION",
        ]  # fmt: skip
        assert lines[1:] == [status_line(t) for t in transitions[4:]]
    assert lines[0] == f"run {run.run_id}  workflow bug-fix  resuming at IMPLEMENTATION"


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_resume_takes_over_a_killed_runs_lock_and_logs_it(repo: Path) -> None:
    script = happy()
    script["TEST"] = [KeyboardInterrupt(), ok(test_output="ok", tests_pass=True)]
    with pytest.raises(KeyboardInterrupt):
        run_workflow(repo, CANONICAL, "bug-fix", "Fix it.", executor_factory=factory_for(FakeExecutor(script)), echo=lambda _: None)
    with RunStore.open(repo, create=False) as store:
        [run] = store.list_runs()
    acquire(repo / ".agent-team", run.run_id, pid=_dead_pid())  # as if the process had been killed
    record = resume_workflow(repo, CANONICAL, run.run_id, executor_factory=factory_for(FakeExecutor(script)), echo=lambda _: None)
    assert record.outcome == "succeeded"
    with RunStore.open(repo, create=False) as store:
        resume = next(t for t in store.transitions(run.run_id) if t.to_state == "RESUME")
    assert "interrupted in TEST" in resume.note and f"took over a stale lock from run {run.run_id}" in resume.note


def test_resume_refuses_ended_single_agent_and_live_runs(repo: Path, store: RunStore, dag) -> None:
    record, _, _ = run_fake(store, dag, happy())
    factory = factory_for(FakeExecutor(happy()))
    with pytest.raises(OrchestratorError, match="already ended"):
        resume_workflow(repo, CANONICAL, record.run_id, executor_factory=factory, echo=lambda _: None)
    single = store.start_run("agent:independent-reviewer")
    with pytest.raises(OrchestratorError, match="single-agent run"):
        resume_workflow(repo, CANONICAL, single.run_id, executor_factory=factory, echo=lambda _: None)
    live = start(store, dag, "Fix it.", echo=lambda _: None)
    acquire(repo / ".agent-team", live.run_id)  # this process is alive
    before = len(store.transitions(live.run_id))
    with pytest.raises(Exception, match="holds"):
        resume_workflow(repo, CANONICAL, live.run_id, executor_factory=factory, echo=lambda _: None)
    assert len(store.transitions(live.run_id)) == before, "a refused resume records nothing"


def test_resume_refuses_a_run_from_an_older_database(repo: Path) -> None:
    """A v1 run record (before checkpoints) migrates in place; its runs read back but cannot resume."""
    db = repo / ".agent-team" / "runs.db"
    db.parent.mkdir()
    conn = sqlite3.connect(db, isolation_level=None)
    for statement in MIGRATIONS[0]:
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 1")
    at = T0.isoformat(timespec="microseconds")
    conn.execute(
        "INSERT INTO runs (run_id, workflow, state, started_at, last_event_at, cost_usd)"
        " VALUES ('20261020-120000-abcdef', 'bug-fix', 'PLAN', ?, ?, 0.5)",
        (at, at),
    )
    conn.execute(
        "INSERT INTO transitions (run_id, seq, from_state, to_state, at, cost_usd) VALUES ('20261020-120000-abcdef', 1, NULL, 'PLAN', ?, 0)",
        (at,),
    )
    conn.close()

    with RunStore.open(repo, create=False) as store:
        assert store.get_run("20261020-120000-abcdef").cost_usd == 0.5
        assert [t.to_state for t in store.transitions("20261020-120000-abcdef")] == ["PLAN"]
        assert store.last_checkpoint("20261020-120000-abcdef") is None
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2
    assert "checkpoint" in [row[1] for row in conn.execute("PRAGMA table_info(transitions)")]
    assert migrate(conn) == SCHEMA_VERSION
    conn.close()
    with pytest.raises(OrchestratorError, match="no checkpoint"):
        resume_workflow(repo, CANONICAL, "20261020-120000-abcdef", executor_factory=factory_for(FakeExecutor(happy())), echo=lambda _: None)


# --- the default executor and the real reviewer path --------------------------------


def test_default_executor_names_what_is_missing_until_step_11(repo: Path, dag) -> None:
    executor = DefaultExecutor(repo=repo, canonical_dir=CANONICAL, canonical=load_canonical(CANONICAL),
                               claude="claude", max_budget_usd=1.0, timeout_s=60)  # fmt: skip
    assert executor.unavailable(dag) == [
        f"agent 'implementer' is not in {CANONICAL}",
        "in-process step 'test' is not implemented yet",
        "in-process step 'pr' is not implemented yet",
    ]


def test_reviewer_node_runs_through_run_agent(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_claude = install_fake_claude(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_claude.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    lines: list[str] = []
    real = default_executor_factory(repo=repo, canonical_dir=CANONICAL, claude=str(fake_claude),
                                    max_budget_usd=1.0, timeout_s=60, echo=lines.append)  # fmt: skip
    script = happy()
    del script["CODE_REVIEW"]
    fake = FakeExecutor(script)

    def factory(canonical):
        default = real(canonical)
        return lambda node, given: default(node, given) if node.agent == "independent-reviewer" else fake(node, given)

    record = run_workflow(repo, CANONICAL, "bug-fix", "Fix the crash.", executor_factory=factory, echo=lines.append)
    assert record.outcome == "succeeded", lines
    assert record.cost_usd == pytest.approx(0.04 + 0.37), "the child run's cost lands on the workflow run"
    with RunStore.open(repo, create=False) as store:
        child = next(r for r in store.list_runs() if r.workflow == "agent:independent-reviewer")
        assert child.outcome == "succeeded"
        to_pr = next(t for t in store.transitions(record.run_id) if t.to_state == "PR")
        assert f"agent independent-reviewer run {child.run_id}: succeeded" in to_pr.note
        assert store.last_checkpoint(record.run_id).data["evidence"]["reviewer_verdict"] == "approve"
    seen = json.loads((repo / ".agent-team" / "tmp" / child.run_id / "fake-claude.json").read_text())
    assert "## diff" in seen["stdin"] and "## test_output" in seen["stdin"] and "## plan" in seen["stdin"]
    assert "reproduction_fails" not in seen["stdin"], "only the reviewer node's inputs"
    assert any(line.startswith("    run ") for line in lines), "the child run's lines are nested"
    assert read_lock(repo / ".agent-team") is None


def test_missing_agent_at_run_time_blocks(repo: Path, dag) -> None:
    executor = DefaultExecutor(repo=repo, canonical_dir=CANONICAL, canonical=load_canonical(CANONICAL),
                               claude="claude", max_budget_usd=1.0, timeout_s=60)  # fmt: skip
    given = NodeInput(run_id="20261024-090000-abcdef", workflow="bug-fix", state="IMPLEMENTATION", attempt=1, task="t", inputs={})
    result = executor(dag.node("IMPLEMENTATION"), given)
    assert result.blocked and "no canonical agent named 'implementer'" in result.blocked


# --- CLI -----------------------------------------------------------------------------


@pytest.fixture
def claude_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_which", lambda name: f"/opt/bin/{name}")


def test_cli_workflow_refuses_before_recording_until_step_11(repo: Path, claude_on_path) -> None:
    result = runner.invoke(app, ["run", "--workflow", "bug-fix", "--repo", str(repo), "--canonical", str(CANONICAL), "Fix it."])
    assert result.exit_code == 2
    assert "workflow bug-fix cannot run yet: agent 'implementer'" in result.output
    assert RunStore.open(repo, create=False) is None


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "Fix it."],
        ["run", "--agent", "independent-reviewer", "--workflow", "bug-fix", "Fix it."],
        ["run", "--workflow", "bug-fix", "--model", "claude-opus-5-5", "Fix it."],
    ],
)
def test_cli_run_mode_flags(repo: Path, claude_on_path, argv: list[str]) -> None:
    result = runner.invoke(app, [*argv, "--repo", str(repo)])
    assert result.exit_code == 2 and "error:" in result.output


def test_cli_workflow_run_interrupt_and_resume(repo: Path, claude_on_path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = happy()
    script["CODE_REVIEW"] = [KeyboardInterrupt(), ok(reviewer_verdict="approve", findings=[])]
    fake = FakeExecutor(script)
    monkeypatch.setattr(cli, "default_executor_factory", lambda **_: factory_for(fake))
    base = ["--repo", str(repo), "--canonical", str(CANONICAL)]

    result = runner.invoke(app, ["run", "--workflow", "bug-fix", *base, "Fix it."])
    assert result.exit_code == 130
    assert "Resume with: agent-team resume" in result.output
    with RunStore.open(repo, create=False) as store:
        [run] = store.list_runs()

    result = runner.invoke(app, ["resume", run.run_id, *base])
    assert result.exit_code == 0, result.output
    assert f"succeeded: workflow bug-fix ended in HUMAN_DECISION, $0.0500; details: agent-team trace {run.run_id}" in result.output
    assert "CODE_REVIEW -> RESUME" in result.output

    result = runner.invoke(app, ["resume", "20991231-000000-ffffff", *base])
    assert result.exit_code == 1 and "no run" in result.output


def test_cli_blocked_workflow_exits_1(repo: Path, claude_on_path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = happy()
    script["TEST"] = [ok(test_output="1 failed", tests_pass=False)]
    monkeypatch.setattr(cli, "default_executor_factory", lambda **_: factory_for(FakeExecutor(script)))
    result = runner.invoke(app, ["run", "--workflow", "bug-fix", "--repo", str(repo), "--canonical", str(CANONICAL), "Fix it."])
    assert result.exit_code == 1
    assert "TEST -> BLOCKED" in result.output and "blocked: workflow bug-fix ended in BLOCKED" in result.output
