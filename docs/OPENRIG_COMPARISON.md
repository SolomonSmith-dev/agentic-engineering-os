# OpenRig vs agent-team

Source: an AI-generated build spec of "OpenRig", from the video *I Run an AI Civilization in Herdr* (Google Doc, read 2026-10-28). Compared against `docs/ARCHITECTURE.md` and `main` at d50bae7.

## Verdict per idea

| OpenRig idea | agent-team today | Verdict | Where it landed |
| --- | --- | --- | --- |
| Refocus: re-inject project → mission → slice intent mid-session | Task given once, at the start of each worker process | **Adopted** | `focus_hook.py` repeats the task every 25 calls |
| APM: ceremony vs progress ratio | Tool calls recorded, never analyzed | **Adopted, rebuilt** | Progress = file write or test run, not keyword matching. Live stall and loop nudges, plus `trace --focus` |
| Doghouse vs moonbase scope creep | Reviewer counts unplanned diff lines after the fact | **Adopted** | Hook flags a write outside the plan's files as it happens (active once plans name files) |
| Tribal wisdom per seat | Repo memory from `inspect` only, static | **Adopted, gated** | `agent-team lessons`: reviewer findings → proposed → human-approved → prompt |
| Memetic epidemiology (trace and purge a bad idea) | Nothing; no agent-written memory existed to infect | **Adopted, small** | Every lesson keeps its source run; `lessons quarantine` purges every match and logs where it came from |
| Culture charter: stop when proven, no meta-checks | Eval decision rule cuts agents that do not move a metric | Partly covered | Refocus text asks "polish, extra scope, or re-checking proven work?" |
| Project → Mission → Slice, waves of parallel slices | One issue per run, linear DAG | Later | Revisit after dogfood, when several issues share a release |
| Live TUI dashboard | `trace`, one status line per state | Later | `trace --follow` reading the SQLite record is the cheap version |
| Seats with stable addresses (`builder@workshop`) | Capabilities, no personas | Rejected | Conflicts with the no-personas rule |
| Agents messaging each other over tmux `send-keys` | Workers never talk; orchestrator passes evidence | Rejected | Chat between agents is where ceremony comes from; also unsafe input injection |
| `--dangerously-skip-permissions`, `codex --full-auto` on every seat | `dontAsk` + allowlist + fail-closed hook + srt | Rejected | Fails your standing rejection rule |
| `proof.md` passes on the text "EXIT_CODE: 0" | Orchestrator runs the tests itself in srt | Rejected | An agent can type that text |

## Bugs in the OpenRig spec, for the record

- `tmux send-keys` without `-l` sends each newline of a multi-line prompt as Enter.
- The GPS moves a slice from build to test without checking that anything was built.
- The `between_waves` merge step is a string; nothing runs it.

## What changed in this branch

- `src/agent_team/focus.py`: one definition of progress, `analyze()` for recorded calls.
- `src/agent_team/adapters/claude/focus_hook.py`: PostToolUse hook, advisory, fails open.
- `src/agent_team/adapters/claude/compile.py`: `focus.json` and the PostToolUse hook when a brief is given; lessons section after the definition of done.
- `src/agent_team/run_agent.py`: every Claude worker gets the focus hook (`focus=False` turns it off) and its approved lessons.
- `src/agent_team/lessons.py` + `agent-team lessons {propose,approve,list,quarantine}`.
- `RunStore.checkpoints()`: every checkpoint, so a retried review's first findings are not lost.
- `agent-team trace --focus`.
- Tests: `tests/test_focus.py`, `tests/test_lessons.py`, one compile test. 438 passed, 32 skipped.

## Open

- Codex has no hooks, so Codex runs get `trace --focus` only, no live nudges.
- Drift flags need plans that list files; the bug-fix `plan` step does not yet.
- Thresholds (25 / 15 / 3) are guesses. The step 13 eval should measure whether nudges change completion or cost, and the hook goes if they do not.
