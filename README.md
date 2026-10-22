# agentic-engineering-os

`agent-team` runs Claude Code workers and an independent reviewer inside sandboxed git worktrees, records every run in SQLite, and gates pull requests on the reviewer's verdict.

Status: build-order step 1 of 15. The CLI answers `--version` and nothing else yet. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the target design and build order.

## Install

```bash
uv tool install git+https://github.com/SolomonSmith-dev/agentic-engineering-os
agent-team --version
```

Pin to a tag once one exists (`@0.1.0` lands after build-order step 6).

## Develop

```bash
uv sync --group dev
uv run pytest -q
```

## License

Apache-2.0. See [LICENSE](LICENSE).
