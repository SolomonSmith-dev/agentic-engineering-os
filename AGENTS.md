# Agent guide

agent-team is a Python CLI that runs Claude Code workers and an independent reviewer in sandboxed git worktrees. It records each run in SQLite and gates the pull request on the reviewer verdict. Read `docs/ARCHITECTURE.md` before changing anything: it holds the target architecture, the build order and the locked decisions.

## Layout

- `src/agent_team/`: the package. `cli.py` is the typer app and holds every subcommand (entry point `agent-team`).
- `src/agent_team/schemas/`: pydantic models; `canonical/` holds the canonical agent files.
- `src/agent_team/adapters/claude/`: Claude Code runtime adapter.
- `src/agent_team/environments/`: worktree allocator and sandbox wrapper.
- `src/agent_team/runs/`: SQLite run record and trace.
- `src/agent_team/bootstrap/`: `init` discovery and `configure`.
- `routing/`: model registry. `evals/`: eval harness. `tests/`: pytest suite.
- `docs/`: architecture, bootstrap, security and threat model.

## Setup and checks

```bash
uv sync --group dev
uv run pytest -q
uv run agent-team --version
```

CI runs `uv sync --locked --group dev` and `uv run pytest -q` with `AGENT_TEAM_REQUIRE_SRT=1`, so sandbox tests must run, not skip.

## Conventions

- Python 3.12, managed with uv. Typer for the CLI, pydantic for schemas, stdlib `sqlite3`; no web framework.
- Each build-order step in `docs/ARCHITECTURE.md` ships with its own tests.
- Runtime state lives in `.agent-team/` inside the target repo and is gitignored. Never commit it.
- Only `agent-team configure` writes into a target repository, and only where it is pointed. Do not touch other repositories' configuration.
- A `CLAUDE.md` that imports `@AGENTS.md` is left alone by `configure`, so keep that one-line import.

## Safety

- Never commit secrets, tokens or `.env` values. Report auth state as present or absent, never by value.
- Do not push to `main`; work on a branch and open a pull request.
- Keep commit messages and pull request text plain, with no tool-generated attribution lines.

## Done means

`uv run pytest -q` passes with the sandbox tests running, and CI is green on the pull request.
