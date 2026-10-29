"""PostToolUse hook: keep a worker on its task. Advisory, so it fails open.

Claude Code runs this after every tool call with the call as JSON on stdin. It counts
calls in a small state file next to focus.json and, when one of these holds, prints
`additionalContext` that Claude Code adds to the session:

- every `refocus_every` calls: the task again, and the question whether the current
  work still serves it (OpenRig's altitude refocus, without the tmux injection);
- `stall_after` calls in a row with no progress (see `agent_team.focus`): say so, and
  either act on the plan or stop and report the blocker;
- the same shell command `loop_after` times in a row: a loop, try something else or stop;
- a write to a file the plan does not name, when the plan names files: flag the drift and
  ask for a reason in the final report. It never blocks; the PreToolUse hook owns blocking.

Every nudge is appended to focus.log.jsonl beside the state file, so the run directory
shows what the worker was told and when. Any error prints nothing and exits 0: a broken
reminder must never stop a run, unlike the policy hook, which fails closed.

    python -m agent_team.adapters.claude.focus_hook <compiled>/focus.json
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

from agent_team.focus import LOOP_AFTER, REFOCUS_EVERY, STALL_AFTER, is_progress, written_path

EVENT = "PostToolUse"


def _relative(candidate: str, root: Path | None) -> str:
    path = Path(candidate)
    if root is not None and path.is_absolute():
        try:
            return Path(os.path.normpath(path)).relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()
    return Path(os.path.normpath(path)).as_posix()


def step(event: dict[str, Any], focus: dict[str, Any], state: dict[str, Any]) -> list[str]:
    """Update `state` for one tool call and return the nudges to send (possibly none)."""
    tool = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    tests = list(focus.get("test_commands") or [])
    every = int(focus.get("refocus_every") or REFOCUS_EVERY)
    stall_after = int(focus.get("stall_after") or STALL_AFTER)
    loop_after = int(focus.get("loop_after") or LOOP_AFTER)
    root = Path(focus["write_root"]) if focus.get("write_root") else None
    planned = {Path(p).as_posix() for p in focus.get("planned_files") or []}

    state["calls"] = int(state.get("calls", 0)) + 1
    nudges: list[str] = []

    if is_progress(tool, tool_input, tests):
        state["stall"] = 0
    else:
        state["stall"] = int(state.get("stall", 0)) + 1
        if state["stall"] == stall_after:
            nudges.append(
                f"agent-team focus: {stall_after} tool calls in a row have neither changed a file nor run a test. "
                "If you know enough, make the change the plan calls for. If something blocks you, stop and "
                "report the blocker in your output instead of reading further."
            )

    command = str(tool_input.get("command", "")) if tool == "Bash" else ""
    if command and command == state.get("last_command"):
        state["repeats"] = int(state.get("repeats", 0)) + 1
    else:
        state["repeats"] = 0
    state["last_command"] = command or None
    if command and state["repeats"] + 1 == loop_after:
        nudges.append(
            f"agent-team focus: you have run `{command[:120]}` {loop_after} times in a row. Its result will not "
            "change by itself; change the code or the approach, or stop and report what you found."
        )

    path = written_path(tool, tool_input)
    if path and planned:
        rel = _relative(path, root)
        if rel not in planned and rel not in state.setdefault("drift", []):
            state["drift"].append(rel)
            nudges.append(
                f"agent-team focus: you changed {rel}, which the plan does not name ({', '.join(sorted(planned))}). "
                "Keep it only if the fix needs it, and say why in your claimed changes."
            )

    if state["calls"] % every == 0 and focus.get("brief"):
        nudges.append(
            f"agent-team focus check after {state['calls']} tool calls. Your task:\n\n{focus['brief']}\n\n"
            "Is what you are doing right now the shortest path to that, or polish, extra scope, or re-checking "
            "work that is already proven? If it is not on the path, go back to the smallest change that meets "
            "the acceptance criteria."
        )
    return nudges


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        focus_path = Path(args[0])
        focus = json.loads(focus_path.read_text(encoding="utf-8"))
        event = json.loads(sys.stdin.read())
        state_path = focus_path.with_name("focus.state.json")
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        nudges = step(event, focus, state)
        state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
        if not nudges:
            return 0
        with focus_path.with_name("focus.log.jsonl").open("a", encoding="utf-8") as log:
            for nudge in nudges:
                log.write(json.dumps({"call": state["calls"], "tool": event.get("tool_name"), "nudge": nudge}) + "\n")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": EVENT, "additionalContext": "\n\n".join(nudges)}}))
    except Exception:  # noqa: BLE001 - advisory: a failure prints nothing and lets the run go on
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
