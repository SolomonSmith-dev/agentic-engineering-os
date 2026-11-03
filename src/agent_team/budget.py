"""Budget meter for task-file runs: tokens, wall clock and list-price dollars (Numenor engine M3).

    tokens   input + output only; cache reads and writes are priced, never counted
    usd      list price from routing/registry.yaml, all four prices; an unpriced model fails closed
    minutes  wall clock from the run's start, seeded with the time already spent on resume

What the meter can and cannot promise:

- A limit is checked after every usage event the runtime reports. Claude Code
  reports usage per assistant message, so a Claude call is stopped (its process
  group killed) after the message that crossed the limit; Codex reports usage
  only when a turn completes, so a Codex call is checked once, at its end. Either
  way the last event can carry the run past the limit: that overshoot is
  recorded (`Halt.overshoot`, the `budget_halt` trace line), never hidden. The
  dollar and token limits are therefore enforced limits, not hard caps.
- Usage a runtime never reported (a Codex turn killed before `turn.completed`, a
  Claude call with no `result` event) is recorded as unknown, never as zero, and
  an unknown spend halts the run: the meter fails closed.
- Wall clock is a hard bound: every agent call and orchestrator command gets a
  timeout no longer than the time left, and the worker's process group is killed
  when it expires.
- Each Claude call also gets `--max-budget-usd` set to what is left, Claude
  Code's own per-call stop, as a second bound.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import Any, Literal

from agent_team.schemas.registry import ModelCost
from agent_team.schemas.task_file import BudgetLimit, TaskBudget

# Where in a call usage becomes visible, per runtime: what the halt reason states.
GRANULARITY = {"claude": "per assistant message", "codex": "at turn end"}

# The smallest per-call dollar stop the Claude CLI accepts. With less than this left, no
# call can be bounded by what remains, so the dollar limit counts as reached.
CALL_FLOOR_USD = 0.01


@dataclass(frozen=True)
class Limits:
    tokens: int
    minutes: float
    usd: float

    @classmethod
    def from_task(cls, budget: TaskBudget) -> "Limits":
        return cls(tokens=budget.tokens, minutes=budget.minutes, usd=budget.usd)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Limits":
        return cls(tokens=int(data["tokens"]), minutes=float(data["minutes"]), usd=float(data["usd"]))

    def as_dict(self) -> dict[str, Any]:
        return {"tokens": self.tokens, "minutes": self.minutes, "usd": self.usd}


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def counted(self) -> int:
        """What the token limit counts: input plus output."""
        return self.input_tokens + self.output_tokens

    def __bool__(self) -> bool:
        return any(getattr(self, f.name) for f in fields(self))

    def __sub__(self, other: "Usage") -> "Usage":
        return Usage(**{f.name: max(0, getattr(self, f.name) - getattr(other, f.name)) for f in fields(self)})

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(**{f.name: getattr(self, f.name) + getattr(other, f.name) for f in fields(self)})

    def most(self, other: "Usage") -> "Usage":
        """Field-wise maximum: a message's usage only ever grows as it streams."""
        return Usage(**{f.name: max(getattr(self, f.name), getattr(other, f.name)) for f in fields(self)})

    def as_dict(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def price_usd(usage: Usage, price: ModelCost) -> float:
    """List-price dollars for `usage`. Unset cache prices are an error: price_of refuses those models first."""
    if price.cache_read_per_mtok_usd is None or price.cache_write_per_mtok_usd is None:
        raise ValueError("cache prices are required to price usage")
    return (
        usage.input_tokens * price.input_per_mtok_usd
        + usage.output_tokens * price.output_per_mtok_usd
        + usage.cache_read_tokens * price.cache_read_per_mtok_usd
        + usage.cache_write_tokens * price.cache_write_per_mtok_usd
    ) / 1_000_000


@dataclass(frozen=True)
class Halt:
    limit: BudgetLimit
    used: float
    allowed: float
    detail: str | None = None

    @property
    def overshoot(self) -> float:
        """How far past the limit the run was when the halt was seen."""
        return max(0.0, self.used - self.allowed)

    def reason(self) -> str:
        text = f"budget halt: {self.limit} {self.used:.4g} >= {self.allowed:.4g}"
        if self.used < self.allowed:
            text = f"budget halt: {self.limit} {self.used:.4g} of {self.allowed:.4g}"
        return f"{text} ({self.detail})" if self.detail else text


class BudgetMeter:
    """One task's spend: every usage event goes through `add`; `check` sees the wall clock."""

    def __init__(
        self,
        limits: Limits,
        prices: Mapping[str, ModelCost],
        *,
        clock: Callable[[], float] = time.monotonic,
        spent_tokens: int = 0,
        spent_usd: float = 0.0,
        elapsed_s: float = 0.0,
        unknown: Sequence[str] = (),
    ) -> None:
        self.limits = limits
        self.prices = dict(prices)
        self._clock = clock
        self._start = clock()
        self._seed_s = float(elapsed_s)
        self.tokens = int(spent_tokens)
        self.usd = float(spent_usd)
        self.unknown: list[str] = list(unknown)  # why spend is not fully known; non-empty halts
        self.halted: Halt | None = None
        self.trace: Any | None = None  # duck-typed: emit(event, **fields)

    def seed(self, *, tokens: int, usd: float, elapsed_s: float, unknown: Sequence[str]) -> None:
        """Carry an earlier session's spend into this meter (a resume). Never lowers a counter."""
        self.tokens = max(self.tokens, int(tokens))
        self.usd = max(self.usd, float(usd))
        self._seed_s = max(self._seed_s, float(elapsed_s))
        self.unknown.extend(u for u in unknown if u not in self.unknown)

    def _emit(self, event: str, **values: Any) -> None:
        if self.trace is not None:
            self.trace.emit(event, **values)

    def elapsed_s(self) -> float:
        return self._seed_s + (self._clock() - self._start)

    def _over(self) -> Halt | None:
        """Spend at a limit is a halt, not only spend past it; unknown spend is a halt too."""
        if self.unknown:
            return Halt("usd", self.usd, self.limits.usd, "spend is unknown: " + "; ".join(self.unknown))
        if self.tokens >= self.limits.tokens:
            return Halt("tokens", self.tokens, self.limits.tokens)
        if self.usd >= self.limits.usd:
            return Halt("usd", self.usd, self.limits.usd)
        if self.limits.usd - self.usd < CALL_FLOOR_USD:
            return Halt("usd", self.usd, self.limits.usd, f"less than the ${CALL_FLOOR_USD} per-call minimum is left")
        minutes = self.elapsed_s() / 60
        if minutes >= self.limits.minutes:
            return Halt("minutes", minutes, self.limits.minutes)
        return None

    def _halt(self, halt: Halt, *, granularity: str | None = None) -> Halt:
        if self.halted is None:
            if granularity and halt.detail is None and halt.limit != "minutes":
                halt = Halt(halt.limit, halt.used, halt.allowed, f"usage is seen {granularity}; overshoot {halt.overshoot:.4g}")
            self.halted = halt
            self._emit(
                "budget_halt", limit=halt.limit, used=halt.used, allowed=halt.allowed,
                overshoot=halt.overshoot, reason=halt.reason(),
            )  # fmt: skip
        return self.halted

    def add(self, model: str, usage: Usage, *, source: str, granularity: str | None = None) -> Halt | None:
        """Count and price `usage`. Raises KeyError for a model with no price."""
        price = self.prices[model]
        usd = price_usd(usage, price)
        self.tokens += usage.counted
        self.usd += usd
        self._emit("usage", model=model, source=source, **usage.as_dict(), tokens=usage.counted, usd=usd)
        return self.check(granularity=granularity)

    def mark_unknown(self, detail: str, *, source: str) -> Halt:
        """Usage happened that no event reported: record it as unknown (never 0) and halt."""
        self.unknown.append(detail)
        self._emit("usage_unknown", source=source, detail=detail)
        halt = self.check()
        assert halt is not None
        return halt

    def refuse_unpriced(self, model: str) -> Halt:
        """A usage event named a model with no price: stop rather than count it as $0."""
        return self.stop("usd", f"model {model!r} has no verified list price")

    def stop(self, limit: BudgetLimit, detail: str) -> Halt:
        """Halt on `limit` for a reason the counters alone do not show (an unpriced model, the runtime's own stop)."""
        used = {"tokens": float(self.tokens), "usd": self.usd, "minutes": self.elapsed_s() / 60}[limit]
        allowed = {"tokens": float(self.limits.tokens), "usd": self.limits.usd, "minutes": self.limits.minutes}[limit]
        return self._halt(Halt(limit, used, allowed, detail))

    def check(self, *, granularity: str | None = None) -> Halt | None:
        if self.halted is not None:
            return self.halted
        halt = self._over()
        self._emit("budget_check", tokens=self.tokens, usd=self.usd, minutes=self.elapsed_s() / 60, halted=halt.limit if halt else None)
        return self._halt(halt, granularity=granularity) if halt is not None else None

    def remaining_usd(self) -> float:
        return max(0.0, self.limits.usd - self.usd)

    def remaining_s(self) -> float:
        return max(0.0, self.limits.minutes * 60 - self.elapsed_s())

    def call_budget_usd(self, cap: float) -> float:
        """A call's own dollar stop: what is left, never above `cap`.

        Never rounded up to the CLI's floor: with less than CALL_FLOOR_USD left the meter
        halts and this returns 0.0, so the caller must not start the call (see `check`).
        """
        left = min(cap, self.remaining_usd())
        if self.check() is not None or left < CALL_FLOOR_USD:
            return 0.0
        return left

    def call_timeout_s(self, cap: float) -> float:
        """A call's or command's timeout: what is left of the wall clock, never above `cap`."""
        return max(1.0, min(cap, self.remaining_s()))


# ---- usage from runtime stream events -----------------------------------------------------


def _int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def claude_usage(raw: Mapping[str, Any]) -> Usage:
    return Usage(
        input_tokens=_int(raw, "input_tokens"),
        output_tokens=_int(raw, "output_tokens"),
        cache_read_tokens=_int(raw, "cache_read_input_tokens"),
        cache_write_tokens=_int(raw, "cache_creation_input_tokens"),
    )


def message_usage(event: Mapping[str, Any]) -> tuple[str, str | None, Usage] | None:
    """Claude stream-json: (message id, model, usage) for an assistant event that carries usage."""
    if event.get("type") != "assistant":
        return None
    message = event.get("message")
    if not isinstance(message, Mapping) or not isinstance(message.get("id"), str):
        return None
    usage = message.get("usage")
    if not isinstance(usage, Mapping):
        return None
    model = message.get("model")
    return message["id"], model if isinstance(model, str) else None, claude_usage(usage)


def turn_usage(event: Mapping[str, Any]) -> Usage | None:
    """Codex `exec --json`: a completed turn's usage; cached input is a cache read, not input."""
    if event.get("type") != "turn.completed" or not isinstance(event.get("usage"), Mapping):
        return None
    usage = event["usage"]
    cached = _int(usage, "cached_input_tokens")
    return Usage(
        input_tokens=max(0, _int(usage, "input_tokens") - cached),
        output_tokens=_int(usage, "output_tokens"),
        cache_read_tokens=cached,
    )


@dataclass
class StreamWatch:
    """The adapters' `watch` hook for one call: meters each usage event, returns a halt reason to stop the worker.

    Claude repeats a message's usage on every content block of that message, so only the
    growth per message id is added. A model the meter has no price for halts the run.
    """

    meter: BudgetMeter
    runtime: Literal["claude", "codex"]
    model: str
    usage: Usage = field(default_factory=Usage)
    usd: float = 0.0
    usage_known: bool = True  # False once the call's final usage report was missing
    _seen: dict[str, Usage] = field(default_factory=dict, repr=False)

    @property
    def tokens(self) -> int:
        return self.usage.counted

    @property
    def granularity(self) -> str:
        return GRANULARITY[self.runtime]

    def _add(self, model: str | None, delta: Usage, source: str) -> None:
        if not delta:
            return
        name = model or self.model
        if name not in self.meter.prices:
            self.meter.refuse_unpriced(name)
            return
        before = self.meter.usd
        self.meter.add(name, delta, source=source, granularity=self.granularity)
        self.usage = self.usage + delta
        self.usd += self.meter.usd - before

    def __call__(self, event: Mapping[str, Any]) -> str | None:
        if self.runtime == "claude":
            got = message_usage(event)
            if got is not None:
                mid, model, usage = got
                prev = self._seen.get(mid, Usage())
                self._seen[mid] = prev.most(usage)
                self._add(model, usage - prev, f"{self.runtime}:message")
        else:
            usage = turn_usage(event)
            if usage is not None:
                self._add(None, usage, f"{self.runtime}:turn")
        halt = self.meter.check(granularity=self.granularity)
        return halt.reason() if halt is not None else None

    def reconcile(self, total: Usage | None, *, source: str) -> None:
        """Add whatever the call's final report shows beyond the streamed events (fail closed on a quiet stream).

        `total` None means the runtime never reported the call's usage: what streamed is a
        lower bound, so the spend is recorded as unknown and the meter halts.
        """
        if total is None:
            self.usage_known = False
            self.meter.mark_unknown(
                f"the {self.runtime} call ended without a final usage report; "
                f"{self.usage.counted} streamed tokens are a lower bound", source=source,
            )  # fmt: skip
            return
        self._add(None, total - self.usage, source)
