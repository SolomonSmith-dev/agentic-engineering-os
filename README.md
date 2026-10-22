# agentic-engineering-os

`agent-team` runs Claude Code workers and an independent reviewer inside sandboxed git worktrees, records every run in SQLite, and gates pull requests on the reviewer's verdict.

Status: build-order step 2 of 15. The CLI answers `--version`, validates `canonical/` against the pydantic schemas (`agent-team validate`) and exports them as JSON Schema (`agent-team schema export`). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the target design and build order.

## Install

```bash
uv tool install git+https://github.com/SolomonSmith-dev/agentic-engineering-os
agent-team --version
```

Pin to a tag once one exists (`@0.1.0` lands after build-order step 6).

## Canonical files

`canonical/` holds the agents, skills, workflows, tools and policies that every runtime adapter compiles from (see section 6 of the architecture doc). Validate the tree and export the schemas with:

```bash
agent-team validate
agent-team schema export --out build/schemas
```

## Develop

```bash
uv sync --group dev
uv run pytest -q
```

## License

Apache-2.0. See [LICENSE](LICENSE).
