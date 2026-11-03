"""`trace.jsonl` (Numenor engine G8): append, redaction, torn lines, the spend it carries,
and the resumed run's budget meter seeded from it."""

import json
from pathlib import Path

import pytest
from test_orchestrator import CANONICAL, FakeExecutor, factory_for, happy, ok, repo  # noqa: F401  (fixture by name)

from agent_team.budget import BudgetMeter, Limits, Usage
from agent_team.orchestrator import resume_workflow, run_workflow
from agent_team.runs import RunStore
from agent_team.runs.trace import (
    open_trace,
    read_trace,
    trace_elapsed_s,
    trace_path,
    trace_tokens,
    trace_unknown,
    trace_usd,
)
from agent_team.schemas.registry import ModelCost

PRICE = ModelCost(input_per_mtok_usd=1.0, output_per_mtok_usd=2.0, cache_read_per_mtok_usd=0.5, cache_write_per_mtok_usd=1.5)
LIMITS = Limits(tokens=1_000_000, minutes=60, usd=100.0)


def write_lines(path: Path, *events: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def test_emit_appends_one_redacted_line_per_event(tmp_path: Path) -> None:
    trace = open_trace(tmp_path, "run-a", redaction=(r"sk-[A-Za-z0-9]+",))
    trace.emit("tool_call", args="key sk-SECRETVALUE", tokens=12, ok=True)
    trace.emit("usage", tokens=3, usd=0.5)
    events = read_trace(trace_path(tmp_path, "run-a"))
    assert [e["event"] for e in events] == ["tool_call", "usage"]
    assert "SECRETVALUE" not in json.dumps(events)
    assert events[0]["tokens"] == 12 and events[0]["ok"] is True
    assert events[0]["at"].endswith("Z")


def test_open_trace_shares_one_writer_per_path(tmp_path: Path) -> None:
    assert open_trace(tmp_path, "run-b") is open_trace(tmp_path, "run-b")


def test_a_torn_last_line_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text(json.dumps({"event": "usage", "tokens": 5, "usd": 0.1}) + "\n" + '{"event": "usa', encoding="utf-8")
    assert [e["tokens"] for e in read_trace(path)] == [5]
    assert read_trace(tmp_path / "missing.jsonl") == []


def test_spend_totals_count_only_usage_lines() -> None:
    events = [
        {"event": "usage", "tokens": 10, "usd": 0.25},
        {"event": "budget_check", "tokens": 999, "usd": 9.0},
        {"event": "usage", "tokens": 5, "usd": 0.5},
        {"event": "usage_unknown", "detail": "codex turn killed"},
    ]
    assert trace_tokens(events) == 15
    assert trace_usd(events) == pytest.approx(0.75)
    assert trace_unknown(events) == ["codex turn killed"]


def test_elapsed_counts_each_session_and_skips_the_downtime_between() -> None:
    events = [
        {"event": "run_started", "at": "2026-11-02T10:00:00.000Z"},
        {"event": "usage", "at": "2026-11-02T10:02:00.000Z"},
        # interrupted; resumed an hour later
        {"event": "run_resumed", "at": "2026-11-02T11:02:00.000Z"},
        {"event": "usage", "at": "2026-11-02T11:03:30.000Z"},
    ]
    assert trace_elapsed_s(events) == pytest.approx(120 + 90)
    assert trace_elapsed_s([]) == 0.0


# --- resume seeds the meter from the trace ------------------------------------------------


def test_resume_seeds_the_meter_with_spend_from_a_node_that_crashed_mid_way(repo: Path) -> None:
    first_meter = BudgetMeter(LIMITS, {"m": PRICE})

    def spend_then_crash(given):  # noqa: ANN001, ANN202
        # The node metered usage, then the process died before the node's result was stored.
        first_meter.add("m", Usage(input_tokens=1000, output_tokens=500), source="claude:message")
        raise KeyboardInterrupt

    script = happy()
    script["IMPLEMENTATION"] = [spend_then_crash, ok(diff="+fix", claimed_changes=[])]
    with pytest.raises(KeyboardInterrupt):
        run_workflow(repo, CANONICAL, "bug-fix", "Fix it.", executor_factory=factory_for(FakeExecutor(script)),
                     echo=lambda _: None, meter=first_meter)  # fmt: skip
    with RunStore.open(repo, create=False) as store:
        [run] = store.list_runs()
        assert run.tokens_total == 0, "the store never saw the crashed node's spend"

    resumed = BudgetMeter(LIMITS, {"m": PRICE})
    seen: list[tuple[int, float]] = []

    def record_spend(given):  # noqa: ANN001, ANN202
        seen.append((resumed.tokens, resumed.usd))
        return ok(diff="+fix", claimed_changes=[])

    script["IMPLEMENTATION"] = [record_spend]
    record = resume_workflow(repo, CANONICAL, run.run_id, executor_factory=factory_for(FakeExecutor(script)),
                             echo=lambda _: None, meter=resumed)  # fmt: skip
    assert record.outcome == "succeeded"
    assert seen == [(1500, pytest.approx((1000 * 1.0 + 500 * 2.0) / 1_000_000))]
    assert resumed.elapsed_s() >= 0
    events = read_trace(trace_path(repo, run.run_id))
    resumed_line = next(e for e in events if e["event"] == "run_resumed")
    assert resumed_line["spent_tokens"] == 1500


def test_resume_of_a_run_with_unknown_spend_halts_before_any_call(repo: Path) -> None:
    script = happy()
    script["IMPLEMENTATION"] = [KeyboardInterrupt(), ok(diff="+fix", claimed_changes=[])]
    with pytest.raises(KeyboardInterrupt):
        run_workflow(repo, CANONICAL, "bug-fix", "Fix it.", executor_factory=factory_for(FakeExecutor(script)),
                     echo=lambda _: None)  # fmt: skip
    with RunStore.open(repo, create=False) as store:
        [run] = store.list_runs()
    open_trace(repo, run.run_id).emit("usage_unknown", source="codex:final-report", detail="turn killed mid-way")

    resumed = BudgetMeter(LIMITS, {"m": PRICE})
    halts: list[object] = []
    script["IMPLEMENTATION"] = [lambda given: (halts.append(resumed.check()), ok(diff="+fix", claimed_changes=[]))[1]]
    resume_workflow(repo, CANONICAL, run.run_id, executor_factory=factory_for(FakeExecutor(script)),
                    echo=lambda _: None, meter=resumed)  # fmt: skip
    assert resumed.unknown == ["turn killed mid-way"]
    assert halts and halts[0] is not None and "unknown" in halts[0].reason()


def test_seeding_never_lowers_what_the_meter_already_holds() -> None:
    m = BudgetMeter(LIMITS, {"m": PRICE}, spent_tokens=50, spent_usd=2.0, elapsed_s=30)
    m.seed(tokens=10, usd=5.0, elapsed_s=10, unknown=())
    assert (m.tokens, m.usd) == (50, 5.0) and m.elapsed_s() >= 30


def test_resume_boundary_precedes_reentry_transitions(repo):
    script = happy()
    script["IMPLEMENTATION"] = [KeyboardInterrupt(), ok(diff="+fix", claimed_changes=[])]
    with pytest.raises(KeyboardInterrupt):
        run_workflow(repo, CANONICAL, "bug-fix", "Fix it.", executor_factory=factory_for(FakeExecutor(script)), echo=lambda _: None)
    with RunStore.open(repo, create=False) as store:
        [run] = store.list_runs()
    before = len(read_trace(trace_path(repo, run.run_id)))
    resume_workflow(repo, CANONICAL, run.run_id, executor_factory=factory_for(FakeExecutor(script)), echo=lambda _: None)
    events = read_trace(trace_path(repo, run.run_id))[before:]
    assert events[0]["event"] == "run_resumed"


def test_unfinished_model_call_is_unknown_after_process_crash():
    events = [{"event": "model_call_started", "call_id": "child-1", "runtime": "codex"}]
    assert trace_unknown(events)
    events.append({"event": "model_call_ended", "call_id": "child-1"})
    assert trace_unknown(events) == []
