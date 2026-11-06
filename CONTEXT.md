# agentic-engineering-os

agent-team CLI: Claude Code workers + independent reviewer in sandboxed git worktrees, SQLite runs, PR gated on reviewer verdict.

## Task Routing

| Task Type | Go To | Description |
|-----------|-------|-------------|
| Change CLI | `src/agent_team/cli.py` | Typer subcommands |
| Schemas / validate | canonical/ · `agent-team validate` | Pydantic + JSON Schema |
| Runs / SQLite | `src/agent_team/runs/` | RunStore / trace |
| Worktrees | `src/agent_team/environments/` | Allocator + srt |
| Evals | evals/ · `agent-team eval` | Baselines and reports |
| Architecture | `docs/ARCHITECTURE.md` | Locked design + build order |

## Shared Resources

| Resource | Location | Contains |
|----------|----------|----------|
| AGENTS | `AGENTS.md` | Contract (CLAUDE points here) |
| Security | docs/SECURITY.md · docs/THREAT_MODEL.md | Trust boundaries |
| Graph | `graphify-out/` | God node: RunStore |
