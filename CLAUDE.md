# CLAUDE.md: agentic-engineering-os

agent-team CLI: Claude Code workers + independent reviewer in sandboxed git worktrees, SQLite runs, PR gated on reviewer verdict.

## Folder map

- `src/agent_team/`
- `canonical/`
- `docs/`
- `evals/`
- `tests/`
- `routing/`

## Routing

| You want to... | Go to |
|----------------|-------|
| Architecture / build order | `docs/ARCHITECTURE.md` |
| CLI commands | `src/agent_team/cli.py` |
| Schemas | src/agent_team/schemas/ · canonical/ |
| Evals | `evals/` |
| Agent rules | `AGENTS.md` |

## Triggers

| Keyword | Action |
|---------|--------|
| `setup` | Point Solomon at docs/BOOTSTRAP.md and agent-team doctor |
| `status` | agent-team --version; summarize build-order step progress |

## ICM / Graphify

When `graphify-out/graph.json` exists, answer codebase questions with `graphify query "<q>"` (also `path` / `explain`) before grep or explore agents. Rebuild with `graphify update` after large structural changes. Dual Develop: Hermes-first product build; Cursor for git/PR/evals/parallel. No secrets in commits or docs. Ops spine: `~/Projects/personal-tech-ops`.

## Hard rules

- No secrets, tokens, or `.env` values in commits, docs, or chat logs.
- Prefer project `AGENTS.md` when present; do not fork policy into duplicate files.
- Done means verified commands, not summaries of imagined output.
