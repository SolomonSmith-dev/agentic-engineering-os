"""Append-only `trace.jsonl` per workflow run (Numenor engine G8).

One JSON line per event: transitions, tool calls, model usage, budget checks and
halts, PR recovery, the result write-back. Strings are redacted as the run
record is; numbers pass through (a `tokens` count is not a credential). A torn
last line (a crash mid-write) is skipped on read.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_team.runs.redact import redact_text, redact_value
from agent_team.runs.store import STATE_DIR

TRACE_NAME = "trace.jsonl"

_OPEN: dict[Path, "Trace"] = {}


def trace_path(root: Path, run_id: str) -> Path:
    return root / STATE_DIR / "runs" / run_id / TRACE_NAME


def _clean(value: Any, redaction: Sequence[str]) -> Any:
    if isinstance(value, str):
        return redact_text(value, redaction)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return redact_value(value, redaction)


class Trace:
    def __init__(self, path: Path, redaction: Sequence[str] = ()) -> None:
        self.path = path
        self.redaction = tuple(redaction)

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        line = {
            "at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "event": event,
            **{k: _clean(v, self.redaction) for k, v in fields.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=False, default=str) + "\n")
            fh.flush()
        return line


def open_trace(root: Path, run_id: str, redaction: Sequence[str] = ()) -> Trace:
    """One instance per path in this process, so every writer appends to the same file."""
    path = trace_path(root, run_id)
    trace = _OPEN.get(path)
    if trace is None:
        trace = _OPEN[path] = Trace(path, redaction)
    elif redaction:
        trace.redaction = tuple(dict.fromkeys((*trace.redaction, *redaction)))
    return trace


def read_trace(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn line from a crash mid-write
        if isinstance(event, dict):
            events.append(event)
    return events


def trace_usd(events: Iterable[Mapping[str, Any]]) -> float:
    return sum(float(e.get("usd") or 0) for e in events if e.get("event") == "usage")


def trace_tokens(events: Iterable[Mapping[str, Any]]) -> int:
    return sum(int(e.get("tokens") or 0) for e in events if e.get("event") == "usage")


def trace_unknown(events: Iterable[Mapping[str, Any]]) -> list[str]:
    """Why spend is not fully known: one entry per `usage_unknown` line."""
    return [str(e.get("detail") or "usage unknown") for e in events if e.get("event") == "usage_unknown"]


def _at(event: Mapping[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(event["at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None


def trace_elapsed_s(events: Iterable[Mapping[str, Any]]) -> float:
    """Wall clock the run has spent working: each session (start or resume to its last line), summed.

    The downtime between an interruption and the resume is not counted; the last line a
    session wrote is the lower bound on when it stopped.
    """
    total = 0.0
    first: datetime | None = None
    last: datetime | None = None
    for event in events:
        at = _at(event)
        if at is None:
            continue
        if event.get("event") in ("run_started", "run_resumed") and first is not None and last is not None:
            total += max(0.0, (last - first).total_seconds())
            first = None
        if first is None:
            first = at
        last = at
    if first is not None and last is not None:
        total += max(0.0, (last - first).total_seconds())
    return total
