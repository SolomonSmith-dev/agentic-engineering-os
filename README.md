# agentic-engineering-os

`agent-team` runs Claude Code workers and an independent reviewer inside sandboxed git worktrees, records every run in SQLite, and gates pull requests on the reviewer's verdict.

Status: build-order step 7 of 15, version 0.1.0. The CLI answers `--version`, checks the host (`agent-team doctor`), runs one canonical agent end to end on the model its class resolves to (`agent-team run --agent`), reads the run record (`agent-team trace`), sweeps stale run worktrees (`agent-team env sweep`), validates `canonical/` against the pydantic schemas (`agent-team validate`) and exports them as JSON Schema (`agent-team schema export`). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the target design and build order.

## Install

```bash
uv tool install git+https://github.com/SolomonSmith-dev/agentic-engineering-os
agent-team --version
```

Pin to a tag: `uv tool install git+https://github.com/SolomonSmith-dev/agentic-engineering-os@0.1.0`.

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

`agent-team run` is its first writer.

## Run one agent

`agent-team run` runs one canonical agent against a target repository: it records the run, takes the repository lock, allocates a worktree and sandbox, compiles the agent for Claude Code, runs `claude -p` headless, and writes every tool call, the cost and the outcome to the run record.

```bash
agent-team run --agent independent-reviewer \
  --repo /path/to/target --canonical ~/Projects/agentic-engineering-os/canonical \
  --test-command "uv run pytest" --max-budget-usd 1.00 \
  --prompt-file review-request.md
```

- Compiled files go to `.agent-team/compiled/<run-id>/` (`agents.json`, `settings.json`, `policy.json`, the native `agents/<name>.md`, and the output schema) and reach Claude Code only through flags. Nothing is written into the target repository's `.claude/`.
- Capabilities compile to permission rules in `dontAsk` mode: a call no rule allows is denied. Capabilities above the run's autonomy ceiling are dropped and listed in the run record.
- A `PreToolUse` hook blocks any tool call that names a protected path (`.env*`, `*.pem`, `~/.ssh`, `~/.aws`, the policy's own list), including through Bash. It fails closed.
- With your Claude login the worker runs with `--setting-sources ""`, so hooks and settings checked into the target repository never load. With `ANTHROPIC_API_KEY` set (CI) it runs with `--bare`.
- The worker gets a scrubbed environment and the prompt on stdin. An agent whose output has a schema (the reviewer's verdict) must return valid structured output, or the run fails.
- The model comes from the registry (below) by the agent's `model_class`; `--model <id>` overrides it for one run. The transition note records the model, why it was chosen and its fallback chain.

After changing the adapter, the sandbox or the run path, run the opt-in live check on a machine with Claude Code, `srt` and a login. It calls a model (Haiku by default, capped at $0.10) and never runs in CI:

```bash
AGENT_TEAM_LIVE=1 uv run pytest -q -m live
```

## Model registry

Agents name a model class, never a vendor. `canonical/routing/registry.yaml` maps each class to a model and gives every model its provider, capabilities, context size, optional list price, privacy class and fallbacks. It is the only canonical file that may name a vendor, and `agent-team validate` checks it, including that every agent's class has an entry.

| Class | Model | Fallback |
| --- | --- | --- |
| `strong-coding` (implements) | `claude-sonnet-5-5` | none |
| `independent` (reviews) | `claude-opus-5-5` | `claude-sonnet-5-5` |

The fallback chain is compiled into the run's `settings.json` as `fallbackModel`, so Claude Code switches to the next model for the rest of a turn when the primary is overloaded or unavailable. With `--model` the chain is kept minus the override itself.

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
