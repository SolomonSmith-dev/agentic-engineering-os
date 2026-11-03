"""The budget meter (Numenor engine M3): per-message deltas, the final-report reconcile,
fail-closed pricing, overshoot, and halting at (not only past) a limit."""

import pytest

from agent_team.budget import BudgetMeter, Limits, StreamWatch, Usage, price_usd
from agent_team.schemas.registry import ModelCost

PRICE = ModelCost(input_per_mtok_usd=1.0, output_per_mtok_usd=2.0, cache_read_per_mtok_usd=0.5, cache_write_per_mtok_usd=1.5)
MODEL = "m"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


def meter(tokens: int = 1_000_000, usd: float = 100.0, minutes: float = 60.0, **kwargs) -> BudgetMeter:  # noqa: ANN003
    return BudgetMeter(Limits(tokens=tokens, minutes=minutes, usd=usd), {MODEL: PRICE}, **kwargs)


def assistant(mid: str, model: str | None = MODEL, **usage: int) -> dict:
    message = {"id": mid, "usage": usage}
    if model is not None:
        message["model"] = model
    return {"type": "assistant", "message": message}


# --- pricing ------------------------------------------------------------------------------


def test_price_counts_all_four_token_kinds() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    assert price_usd(usage, PRICE) == pytest.approx(5.0)
    assert usage.counted == 2_000_000  # cache tokens are priced, not counted


def test_price_without_cache_prices_is_an_error() -> None:
    with pytest.raises(ValueError):
        price_usd(Usage(input_tokens=1), ModelCost(input_per_mtok_usd=1.0, output_per_mtok_usd=1.0))


# --- claude deltas ------------------------------------------------------------------------


def test_claude_message_usage_is_added_once_per_growth() -> None:
    m = meter()
    watch = StreamWatch(m, "claude", MODEL)
    assert watch(assistant("a", input_tokens=100, output_tokens=10)) is None
    assert watch(assistant("a", input_tokens=100, output_tokens=10)) is None  # repeated per content block
    assert watch(assistant("a", input_tokens=100, output_tokens=30)) is None  # grew by 20 output
    assert watch(assistant("b", input_tokens=50, output_tokens=5)) is None
    assert m.tokens == 185 and watch.tokens == 185
    assert m.usd == pytest.approx((150 * 1.0 + 35 * 2.0) / 1_000_000)
    assert watch.usd == pytest.approx(m.usd)


def test_reconcile_adds_only_what_the_stream_missed() -> None:
    m = meter()
    watch = StreamWatch(m, "claude", MODEL)
    watch(assistant("a", input_tokens=100, output_tokens=10))
    watch.reconcile(Usage(input_tokens=100, output_tokens=40, cache_read_tokens=1000), source="claude:final-report")
    assert m.tokens == 140
    assert m.usd == pytest.approx((100 * 1.0 + 40 * 2.0 + 1000 * 0.5) / 1_000_000)
    watch.reconcile(Usage(input_tokens=100, output_tokens=40, cache_read_tokens=1000), source="claude:final-report")
    assert m.tokens == 140  # idempotent


def test_reconcile_counts_a_quiet_stream() -> None:
    m = meter()
    watch = StreamWatch(m, "codex", MODEL)
    watch.reconcile(Usage(input_tokens=10, output_tokens=5), source="codex:final-report")
    assert m.tokens == 15


def test_codex_turn_usage_splits_cached_input() -> None:
    m = meter()
    watch = StreamWatch(m, "codex", MODEL)
    watch({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10}})
    assert watch.usage == Usage(input_tokens=40, output_tokens=10, cache_read_tokens=60)


# --- fail closed --------------------------------------------------------------------------


def test_an_unpriced_model_halts_rather_than_counting_zero() -> None:
    m = meter()
    sink = Sink()
    m.trace = sink
    watch = StreamWatch(m, "claude", MODEL)
    reason = watch(assistant("a", model="unknown-model", input_tokens=10))
    assert reason is not None and "no verified list price" in reason
    assert m.halted is not None and m.halted.limit == "usd"
    assert [e for e, _ in sink.events].count("budget_halt") == 1


# --- overshoot and the limit itself --------------------------------------------------------


def test_overshoot_is_recorded_with_its_granularity() -> None:
    m = meter(tokens=100)
    sink = Sink()
    m.trace = sink
    watch = StreamWatch(m, "codex", MODEL)
    reason = watch({"type": "turn.completed", "usage": {"input_tokens": 120, "output_tokens": 30}})
    assert reason is not None and "at turn end" in reason
    assert m.halted.overshoot == pytest.approx(50)
    halt = next(f for e, f in sink.events if e == "budget_halt")
    assert halt["overshoot"] == pytest.approx(50) and halt["limit"] == "tokens"


def test_a_halt_is_sticky() -> None:
    m = meter(tokens=10)
    m.add(MODEL, Usage(input_tokens=20), source="t")
    first = m.halted
    m.add(MODEL, Usage(input_tokens=20), source="t")
    assert m.halted is first and m.check() is first


@pytest.mark.parametrize(
    ("limits", "spend"),
    [
        ({"tokens": 100}, Usage(input_tokens=100)),
        ({"usd": 0.0001}, Usage(input_tokens=100)),  # exactly $0.0001
    ],
)
def test_spend_exactly_at_a_limit_halts(limits: dict, spend: Usage) -> None:
    m = meter(**limits)
    assert m.add(MODEL, spend, source="t") is not None


def test_wall_clock_exactly_at_the_limit_halts() -> None:
    clock = Clock()
    m = meter(minutes=1.0, clock=clock)
    clock.now = 60.0
    halt = m.check()
    assert halt is not None and halt.limit == "minutes"


def test_no_call_is_budgeted_once_less_than_the_cli_floor_is_left() -> None:
    m = meter(usd=1.0)
    m.add(MODEL, Usage(output_tokens=497_500), source="t")  # $0.995 spent, $0.005 left
    assert m.remaining_usd() < 0.01
    assert m.call_budget_usd(5.0) == 0.0
    assert m.halted is not None and m.halted.limit == "usd"


def test_call_budget_is_what_is_left_under_the_cap() -> None:
    m = meter(usd=1.0)
    assert m.call_budget_usd(0.25) == 0.25
    m.add(MODEL, Usage(output_tokens=450_000), source="t")  # $0.90
    assert m.call_budget_usd(0.25) == pytest.approx(0.10)
