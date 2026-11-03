"""Edges of `run_agent` (build-order steps 6, 11 and 12): refusals before a run is recorded, and lock notes.

Uses the fake `claude` from test_claude_adapter.py. No test calls a model.
"""

import os
import subprocess
from pathlib import Path

import pytest
from test_claude_adapter import install_fake_claude
from test_codex_adapter import fake_codex, repo, srt  # noqa: F401  (fixture is used by name)

from agent_team.environments import acquire, read_lock
from agent_team.run_agent import (
    RunAgentError,
    agent_state,
    load_canonical,
    requested_autonomy,
    run_agent,
)
from agent_team.runs import RunStore

CANONICAL = Path(__file__).resolve().parents[1] / "canonical"


def call(repo: Path, **kwargs):  # noqa: ANN003, ANN202
    options = {"max_budget_usd": 1.0, "timeout_s": 60, "echo": lambda _: None, **kwargs}
    return run_agent(repo, CANONICAL, options.pop("agent", "independent-reviewer"), "Review it.", **options)


def test_a_missing_binary_is_refused_before_recording(repo: Path) -> None:
    with pytest.raises(RunAgentError, match="the claude runtime needs the claude binary"):
        call(repo)
    with pytest.raises(RunAgentError, match="--runtime codex needs the codex binary"):
        call(repo, runtime="codex")
    assert RunStore.open(repo, create=False) is None


def test_an_invalid_canonical_tree_is_refused(tmp_path: Path, repo: Path) -> None:
    broken = tmp_path / "broken"
    (broken / "agents").mkdir(parents=True)
    (broken / "agents" / "x.md").write_text("---\nkind: agent\nname: x\n---\n", encoding="utf-8")
    with pytest.raises(RunAgentError, match=r"canonical tree at .* is invalid \(\d+ problem\(s\)\); first:"):
        run_agent(repo, broken, "x", "p", claude="claude", max_budget_usd=1.0, timeout_s=5)


def test_an_unknown_policy_or_environment_is_refused(repo: Path) -> None:
    with pytest.raises(RunAgentError, match="no canonical policy named 'strict'"):
        call(repo, claude="claude", policy_name="strict")
    with pytest.raises(RunAgentError, match="no canonical environment named 'gpu'"):
        call(repo, claude="claude", environment_name="gpu")


def test_a_stale_lock_is_taken_over_and_noted(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install_fake_claude(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("agent_team.environments.sandbox.srt_path", lambda: None)
    dead = subprocess.Popen(["true"])
    dead.wait()
    (repo / ".agent-team").mkdir(exist_ok=True)
    acquire(repo / ".agent-team", "20261024-070000-dead00", pid=dead.pid)
    record, result = call(repo, claude=str(fake))
    assert result.status == "succeeded"
    with RunStore.open(repo, create=False) as store:
        notes = [t.note or "" for t in store.transitions(record.run_id)]
    assert any("took over a stale lock from run 20261024-070000-dead00" in n for n in notes)
    assert read_lock(repo / ".agent-team") is None, "the run releases the lock it took over"


def test_a_live_lock_blocks_the_run(repo: Path, tmp_path: Path) -> None:
    fake = install_fake_claude(tmp_path)
    (repo / ".agent-team").mkdir(exist_ok=True)
    acquire(repo / ".agent-team", "20261024-070000-live00", pid=os.getpid())
    with pytest.raises(Exception, match="20261024-070000-live00"):
        call(repo, claude=str(fake))
    with RunStore.open(repo, create=False) as store:
        [run] = store.list_runs()
        assert run.outcome == "blocked" and store.transitions(run.run_id)[-1].to_state == "BLOCKED"
    assert read_lock(repo / ".agent-team").run_id == "20261024-070000-live00"


def test_agent_state_and_requested_autonomy() -> None:
    assert agent_state(["label:bug", "state:CODE_REVIEW", "state:TEST"]) == "CODE_REVIEW"
    assert agent_state(["state:NOT_A_STATE", "pr"]) is None
    assert requested_autonomy("A3", "A1") == "A1" and requested_autonomy("A0", "A3") == "A0"


def test_every_canonical_agent_names_a_known_state() -> None:
    canonical = load_canonical(CANONICAL)
    for doc in canonical.docs:
        if doc.kind == "agent":
            assert agent_state(doc.spec.triggers) is not None, doc.spec.name


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_interrupted_model_call_records_unknown_spend(repo, tmp_path, monkeypatch, fake_codex, srt, runtime):
    import importlib
    from agent_team.budget import BudgetMeter, Limits
    from agent_team.runs.trace import open_trace, read_trace, trace_unknown
    module = importlib.import_module("agent_team.run_agent")
    trace = open_trace(repo, "workflow")
    meter = BudgetMeter(Limits(tokens=10000, minutes=10, usd=1), {})
    meter.trace = trace
    seen = []

    def interrupt(*args, **kwargs):
        # Model runtime boundary only; real Git, sandbox setup, SQLite and trace.
        seen.extend(read_trace(trace.path))
        raise KeyboardInterrupt

    if runtime == "claude":
        binary = install_fake_claude(tmp_path)
        monkeypatch.setattr(module, "execute", interrupt)
        options = {"claude": str(binary)}
    else:
        binary = fake_codex("ok")
        monkeypatch.setattr(module.codex_adapter, "execute", interrupt)
        options = {"codex": str(binary)}
    with pytest.raises(KeyboardInterrupt):
        call(repo, runtime=runtime, meter=meter, trace=trace, **options)
    assert any(e["event"] == "model_call_started" for e in seen)
    assert meter.unknown and meter.check() is not None
    assert any("interrupted" in reason for reason in trace_unknown(read_trace(trace.path)))
