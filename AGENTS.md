# agentic-engineering-os

An agent-team CLI that runs Claude Code workers and an independent reviewer in sandboxed git worktrees. Read `docs/ARCHITECTURE.md` before changing anything: it holds the target architecture, the 15-step build order, and the locked decisions.

## Stack

Python 3.12 via uv. `typer` CLI named `agent-team`, `pydantic` schemas, stdlib `sqlite3`. No framework.

## Layout

- `src/agent_team/cli.py`: the typer app and every subcommand.
- `src/agent_team/schemas/`: canonical pydantic models (step 2).
- `src/agent_team/adapters/claude/`: Claude Code runtime adapter (step 6).
- `src/agent_team/environments/`: worktree allocator and `srt` wrapper (step 5).
- `src/agent_team/runs/`: SQLite run record and `trace` (step 4).
- `src/agent_team/bootstrap/`: `init` discovery and `configure` (step 8).
- `tests/`: pytest; every build-order step lands with its own test.
- `docs/ARCHITECTURE.md`: the architecture document. Edit it in the repo, not in chat.

## Rules

- One squash-merged PR per build-order step. `main` is protected; never push to it.
- No `Co-Authored-By`, `Claude-Session` or "Generated with Claude Code" lines in commits, PRs or docs.
- Never touch `~/.claude` or another repo's config. Step 8 (`configure`) is the only thing that writes to a target repo, and only where it is pointed.
- Runtime state lives in `.agent-team/` inside the target repo and is gitignored.
- Never persist secrets. Auth state is reported as present or absent, never by value.
- Tags: `0.1.0` after step 6, `0.2.0` after step 13, `1.0.0` after dogfood. No tag before step 6.
- CI must stay green on both hosted runners (ubuntu, macos) before merge.

## Commands

```bash
uv sync --group dev
uv run pytest -q
uv run agent-team --version
```
