# agentic-engineering-os

`agent-team` runs Claude Code workers and an independent reviewer inside sandboxed git worktrees, records every run in SQLite, and gates pull requests on the reviewer's verdict.

Status: build-order step 3 of 15. The CLI answers `--version`, checks the host (`agent-team doctor`), validates `canonical/` against the pydantic schemas (`agent-team validate`) and exports them as JSON Schema (`agent-team schema export`). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the target design and build order.

## Install

```bash
uv tool install git+https://github.com/SolomonSmith-dev/agentic-engineering-os
agent-team --version
```

Pin to a tag once one exists (`@0.1.0` lands after build-order step 6).

## Doctor

`agent-team doctor` reports the host (OS, arch, Python, uv), the runtimes it needs (`claude`, `srt`, `gh`, `git`) with versions, Claude and GitHub auth state as present or absent, whether `api.anthropic.com` is reachable over HTTPS, and whether a Claude Code managed-settings file is deployed on this host. It is read-only: it writes nothing and never reads `~/.claude`. Add `--json` for the machine-readable report.

```bash
agent-team doctor
agent-team doctor --json
```

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
