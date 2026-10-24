# agentic-engineering-os

`agent-team` runs Claude Code workers and an independent reviewer inside sandboxed git worktrees, records every run in SQLite, and gates pull requests on the reviewer's verdict.

Status: build-order step 5 of 15. The CLI answers `--version`, checks the host (`agent-team doctor`), reads the run record (`agent-team trace`), sweeps stale run worktrees (`agent-team env sweep`), validates `canonical/` against the pydantic schemas (`agent-team validate`) and exports them as JSON Schema (`agent-team schema export`). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the target design and build order.

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

## Run record and trace

Every run is recorded in `.agent-team/runs.db` inside the target repository: one row per run (state, start and end time, cost so far, model class, worktree, outcome), one per state transition, and one per tool call (tool, redacted argument summary, result, duration, cost). The store refuses to open unless git already ignores `.agent-team/`, and it writes nothing outside that directory. Tool-call arguments are redacted and truncated before they are stored.

```bash
agent-team trace                 # recent runs
agent-team trace <run-id>        # state transitions with elapsed time and cost
agent-team trace <run-id> --calls
```

No agent writes to the record yet; the runtime adapter (build-order step 6) is its first writer.

## Environments

Each run gets its own git worktree at `.agent-team/worktrees/<run-id>/` on branch `agent-team/<run-id>`, cut from `HEAD`. Gitignored files listed in `.worktreeinclude` (gitignore syntax, the same convention Claude Code uses) are copied in; tracked files, symlinks and `.agent-team/` never are. Each worktree gets a port slot, exported as `AGENT_TEAM_PORT_OFFSET` (slot × `ports.block_size`), so parallel dev servers do not collide.

When `srt` is installed, commands run inside it: no network except the allowlist in `canonical/environments/default.yaml`, writes only in the worktree, the run's temp directory and `.git` (minus `.git/hooks` and `.git/config`), and no reads of `~/.ssh` or `~/.aws`. The settings file lives in `.agent-team/sandbox/`, outside the worktree. Without `srt` the run falls back to the worktree alone and its autonomy is capped at A2.

One run per repository: `.agent-team/lock` holds the live run's PID, and a lock left by a dead process is taken over. Disposing a worktree never drops work silently: uncommitted changes and commits past the base are saved to `.agent-team/archive/<run-id>.patch` first.

```bash
agent-team env sweep   # dispose worktrees of ended or unrecorded runs; in-progress runs keep theirs
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
