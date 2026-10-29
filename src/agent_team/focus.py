"""Focus: is a worker making progress or circling? (OpenRig's refocus and APM ideas, adapted.)

Two halves share one definition of progress:

- live, inside a Claude Code session: `adapters/claude/focus_hook.py` runs after every tool
  call and, every `refocus_every` calls or when the worker stalls, loops or writes a file the
  plan did not name, hands the task back to it as extra context;
- after the fact, for any runtime: `analyze()` reads the run record's tool calls and reports
  the same signals, which `agent-team trace --focus` prints and the eval can count.

Progress is a call that changes the checkout (a file write) or runs one of the run's test
commands. Everything else (reads, searches, `git log`) is orientation: needed, but a long run
of it with no progress is the stall OpenRig calls ceremony. Messages are never classified by
keyword; only what a call did counts.
"""

import json
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_team.schemas.runs import ToolCallRecord

WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

# Defaults the compiled hook and `analyze()` share; a run can override them through focus.json.
REFOCUS_EVERY = 25  # calls between reminders of the task
STALL_AFTER = 15  # orientation calls in a row before the worker is told it is stalling
LOOP_AFTER = 3  # the same shell command this many times in a row is a loop
BRIEF_CHARS = 1_500  # how much of the task a reminder repeats


def _command_words(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def is_test_command(command: str, test_commands: Sequence[str]) -> bool:
    """Whether a shell command runs one of the run's test commands (same leading words)."""
    words = _command_words(command)
    for test in test_commands:
        want = _command_words(test)
        if want and words[: len(want)] == want:
            return True
    return False


def is_progress(tool: str, tool_input: dict[str, Any], test_commands: Sequence[str]) -> bool:
    if tool in WRITE_TOOLS:
        return True
    if tool == "Bash":
        return is_test_command(str(tool_input.get("command", "")), test_commands)
    return False


def written_path(tool: str, tool_input: dict[str, Any]) -> str | None:
    if tool not in WRITE_TOOLS:
        return None
    value = tool_input.get("file_path") or tool_input.get("notebook_path")
    return str(value) if value else None


def brief(prompt: str, limit: int = BRIEF_CHARS) -> str:
    """The part of a prompt a reminder repeats: its start, cut at a line break near `limit`."""
    text = prompt.strip()
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[: cut if cut > limit // 2 else limit].rstrip() + "\n[task cut; the full prompt is above in this session]"


@dataclass
class FocusReport:
    calls: int = 0
    progress: int = 0
    longest_stall: int = 0
    loops: list[str] = field(default_factory=list)

    @property
    def progress_ratio(self) -> float:
        return self.progress / self.calls if self.calls else 0.0

    def render(self, stall_after: int = STALL_AFTER) -> str:
        if not self.calls:
            return "focus: no tool calls recorded"
        line = (
            f"focus: {self.progress}/{self.calls} calls made progress ({self.progress_ratio:.0%}); "
            f"longest stall {self.longest_stall} calls"
        )
        if self.longest_stall >= stall_after:
            line += f" (over the {stall_after}-call threshold)"
        if self.loops:
            line += "; loops: " + "; ".join(self.loops)
        return line


def _call_input(record: ToolCallRecord) -> dict[str, Any]:
    try:
        value = json.loads(record.args_summary)
    except ValueError:
        return {"command": record.args_summary} if record.tool_name == "Bash" else {}
    return value if isinstance(value, dict) else {}


def analyze(
    calls: Iterable[ToolCallRecord], test_commands: Sequence[str] = (), loop_after: int = LOOP_AFTER
) -> FocusReport:
    """Progress, the longest stall and repeated-command loops, from a run's recorded tool calls."""
    report = FocusReport()
    stall = 0
    last_command: str | None = None
    repeats = 0
    for record in calls:
        tool_input = _call_input(record)
        report.calls += 1
        if is_progress(record.tool_name, tool_input, test_commands):
            report.progress += 1
            stall = 0
        else:
            stall += 1
            report.longest_stall = max(report.longest_stall, stall)
        command = str(tool_input.get("command", "")) if record.tool_name == "Bash" else None
        if command and command == last_command:
            repeats += 1
            if repeats + 1 == loop_after:
                report.loops.append(f"{command[:60]!r} x{loop_after}+")
        else:
            repeats = 0
        last_command = command
    return report
