# agentic-engineering-os

`agent-team` runs Claude Code workers and an independent reviewer inside sandboxed git worktrees, records every run in SQLite, and gates pull requests on the reviewer's verdict.

Status: build-order step 6 of 15, version 0.1.0. The CLI answers `--version`, checks the host (`agent-team doctor`), runs one canonical agent end to end (`agent-team run --agent`), reads the run record (`agent-team trace`), sweeps stale run worktrees (`agent-team env sweep`), validates `canonical/` against the pydantic schemas (`agent-team validate`) and exports them as JSON Schema (`agent-team schema export`). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the target design and build order.

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

## Init and configure a target repository

`agent-team init <repo>` discovers a repository read-only and writes `.agent-team/proposal.yaml`, then prints a summary. It records the VCS host and remote (credentials stripped), the default branch, protected branches when the repository itself declares them (`.github/settings.yml`; otherwise `unknown`, because the hosting API needs auth and init makes no network calls), the CI system and its files, build, test, lint and type-check commands (from `Makefile`, `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`), instruction files (`CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`, `GEMINI.md`, `.cursorrules` and friends), MCP configs (server names only), secret handling (`.env` files, key files, vault and secret-store references) by path and kind only, devcontainer and CODEOWNERS. It refuses unless git ignores `.agent-team/`, writes nothing else, and keeps an existing proposal unless you pass `--force`.

Each command gets a ✓ or ✗ from a dry-run check that runs nothing: ✓ means the tool the command needs was found (on PATH as an absolute path, or in the repository's `.venv/` or `node_modules/`) and the Makefile target or package script it names exists. It does not mean the command passes. `make -n`, `--help` and `--collect-only` are not used, because each can run code from the repository.

```bash
agent-team init /path/to/repo
# review .agent-team/proposal.yaml; edit the ask (runtimes, privacy classes, autonomy ceiling up to A3, draft PRs)
# and set ask.approved: true
agent-team configure /path/to/repo --canonical ~/Projects/agentic-engineering-os/canonical --dry-run
agent-team configure /path/to/repo --canonical ~/Projects/agentic-engineering-os/canonical
```

`agent-team configure <repo>` turns the approved proposal into `.agent-team/config.yaml` (verified commands by kind, which later steps use as the test, lint and type-check gates) and compiles runtime files: a marked block (`<!-- agent-team:begin -->` to `<!-- agent-team:end -->`) appended to `CLAUDE.md` and to `AGENTS.md` if it exists, and each canonical agent as `.claude/agents/<name>.md`. It prints a unified diff of every file it will write and asks before writing; `--yes` skips the question and `--dry-run` only shows the diff (it works before approval). Text outside the block is never changed; a re-run updates the block in place and writes nothing when nothing changed. A `CLAUDE.md` that imports `@AGENTS.md` is left alone. An agent file it did not write, a file with the same agent name under `.github/agents/`, and any path that is or sits under a symlink are left untouched and reported. It never writes `.claude/settings.json`. This is the only command that writes a target repository's `.claude/`.

## Inspect: repository memory

`agent-team inspect <repo>` runs the `repository-archaeologist` agent at A0 through the same path as `agent-team run --agent`. The agent only reads: files, `git log` and friends, `ls`. Its structured output (architecture map, module ownership, commands, conventions, pitfalls) comes back to agent-team, and agent-team writes it as repository memory:

```text
.agent-team/memory/repo/
  index.yaml          run id, base commit, whether srt was used, command counts by status
  architecture.yaml   summary and one entry per module (path, purpose, depends_on)
  ownership.yaml      owners per module, from CODEOWNERS or git history
  commands.yaml       every command and its verification result
  conventions.yaml    rules the code follows, each citing a file
  pitfalls.yaml       things that will bite, each citing a file
```

```bash
agent-team init /path/to/repo                      # inspect reads the commands init detected
agent-team inspect /path/to/repo --canonical ~/Projects/agentic-engineering-os/canonical
agent-team inspect /path/to/repo --summary         # the map and every command's result; runs nothing
```

- It refuses unless git ignores `.agent-team/`, and unless `init` has run (it reads `.agent-team/config.yaml` when `configure` wrote one, otherwise `.agent-team/proposal.yaml`). It writes nothing outside `.agent-team/`; inside it, only what every `run --agent` writes (run record, worktree, compiled files) plus the memory files.
- After the agent succeeds, agent-team runs each command `init` detected, in order, inside the run's srt sandbox and worktree: no network beyond the environment allowlist, writes only in the worktree and the run's temp directory, and no reads of the policy's protected paths (`.env*`, `~/.claude`, the rest of `.agent-team/`, and the defaults). Each command gets a scrubbed environment with `CI=1`, no stdin, and `--command-timeout` seconds (600 by default) before its process group is killed. Only a redacted tail of its output is kept.
- Each command lands in `commands.yaml` as `passed` (exit 0), `failed`, `timeout` or `unverified`, with its exit code and duration, and in the run record as an `inspect.verify` tool call (`agent-team trace <run-id> --calls`).
- Without srt nothing from the repository runs: every command is `unverified`, with the reason. Commands only the archaeologist found are listed `unverified` too and never run; what runs comes from `init`'s discovery, not from a model that read the repository.
- Commands run in a worktree cut from `HEAD`, so gitignored dependencies (`.venv/`, `node_modules/`) are missing unless `.worktreeinclude` copies them; a command init marked ✓ can fail here for that reason.
- The archaeologist's output is redacted through the policy before it is written. Re-running `inspect` replaces the memory files.

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
- The model comes from `routing/registry.yaml` (found next to `--canonical`, or pass `--registry`): the agent's `model_class` maps to a pinned model id, and that model's `fallbacks` are emitted as `fallbackModel` in the compiled `settings.json`, so Claude Code switches to them when the model is overloaded or unavailable. The reviewer's class `independent` resolves to `claude-opus-5-5` with `claude-sonnet-5-5` as its one fallback. `--model <id>` overrides the class with another registry entry; an id that is not in the registry is refused.
- A policy's `allowed_privacy_classes` pins which registry privacy classes (`vendor-cloud`, `vendor-zdr`, `local-only`) a model may have: a primary model outside it refuses the run, a fallback outside it is dropped. The run record notes the resolved model, its source and the fallback chain.

After changing the adapter, the sandbox or the run path, run the opt-in live check on a machine with Claude Code, `srt` and a login. It calls a model (Haiku by default, capped at $0.10) and never runs in CI:

```bash
AGENT_TEAM_LIVE=1 uv run pytest -q -m live
```

## Run a workflow

`agent-team run --workflow bug-fix` runs a canonical workflow through the orchestrator instead of a single agent. The orchestrator builds a DAG from `canonical/workflows/bug-fix.yaml` (a linear chain for now) and walks it: each node names its state, who works in it (a canonical agent or an in-process step), the evidence it must leave, and the gate that must pass to move on.

```text
INTAKE -> DISCOVERY -> PLAN -> IMPLEMENTATION -> TEST -> CODE_REVIEW -> PR -> HUMAN_DECISION

gates:  DISCOVERY -> PLAN        reproduction_fails  (reproduction recorded before the fix)
        TEST -> CODE_REVIEW      tests_pass          (orchestrator-run, after the fix)
        CODE_REVIEW -> PR        reviewer_verdict == approve
```

- A gate is `<evidence>` (passes only when that evidence is `true`) or `<evidence> == <value>`. A node that leaves out required evidence, reports a blocker, or fails every gate moves the run to BLOCKED, and the run ends `blocked`.
- The reviewer's `changes` verdict sends the run back to IMPLEMENTATION once (the implementer gets the plan and the findings, in a fresh process). A second `changes`, or an `escalate` verdict, takes the escalate edge to PR: the draft PR is marked blocked and the run ends `blocked` at HUMAN_DECISION.
- Each node sees only the evidence its `inputs` list names; the reviewer gets the plan, the diff and the orchestrator's test output, never an implementer transcript.
- Agent nodes run through the same path as `run --agent`, each as its own child run (`agent:<name>` in `trace`); their cost is added to the workflow run. `--max-budget-usd` and `--timeout` apply to each agent call. `--model` is for `--agent` runs only.
- One terminal line per transition, as for `--agent`.

Every transition writes a checkpoint (the task, the evidence so far, retry counts) in the same transaction, redacted like everything else in the run record. Interrupt a run with Ctrl-C and it stays open; resume re-enters the state that was running, and nothing is disposed:

```bash
agent-team run --workflow bug-fix --repo /path/to/target "Fix the crash on empty input."
agent-team resume <run-id> --repo /path/to/target
```

Until build-order step 11 lands the implementer agent and the `test` and `pr` steps, `run --workflow bug-fix` refuses before recording anything and names what is missing.

## Run the reviewer on Codex

`agent-team run --runtime codex` runs the independent reviewer through `codex exec --json` instead of Claude Code (build-order step 12). It is opt-in: the locked MVP reviewer stays on Claude, and `--runtime codex` is accepted only for `independent-reviewer`. **This adapter has not been run against a real Codex yet**: there is no Codex key in the MVP, and every test drives a fake `codex` that replays the JSONL event stream.

```bash
codex login            # or set CODEX_API_KEY for the shell
agent-team run --agent independent-reviewer --runtime codex \
  --repo /path/to/target --test-command "uv run pytest" --prompt-file review-request.md
```

- The model comes from `runtime_classes.codex` in `routing/registry.yaml`: `independent` resolves to `gpt-6.1-sol` there, and to `claude-opus-5-5` everywhere else. `--model` must name a registry entry on the codex runtime.
- The run is refused before anything is recorded when `codex` is not on PATH, has no auth (`CODEX_API_KEY` unset and `codex login status` failing; its output is never read), lacks a flag the adapter passes, or when `srt` is absent.
- Compiled files go to `.agent-team/compiled/<run-id>/`: `agents/independent-reviewer.toml` (the Codex agent shape), `config.toml` (the session settings, passed as `-c key=value`) and `schema.json` (the verdict schema in OpenAI strict form, passed as `--output-schema`). Nothing is written into the target repository's `.codex/`.
- Codex has no per-command permission rules and this adapter uses no Codex hook, so the reviewer runs in Codex's `read-only` sandbox, and protected paths (`.env*`, `*.pem`, `~/.ssh`, `~/.claude`, `.agent-team/` apart from the run's own checkout) become srt `denyRead` rules that hold for Codex and every command it runs. That is why srt is required. The srt allowlist gains only the hosts the auth mode needs (`chatgpt.com` and `auth.openai.com` for a login, `api.openai.com` for an API key).
- The session ignores your `~/.codex/config.toml` and execpolicy rules and writes no session file (`--ignore-user-config --ignore-rules --ephemeral`). Shell commands get only core environment variables, never one named like a key, secret or token.
- Codex reports tokens, not dollars, and has no budget flag. Cost is recorded only when the registry entry has a `cost` (none does yet), and `--max-budget-usd` is then checked after the run ends; otherwise `--timeout` is the only cap. The run record's transition note lists every capability the Codex runtime ran without.
- Known gap: `~/.codex` stays readable inside the sandbox, because Codex reads its own login there, so the reviewer's commands can read it too. The srt network allowlist is what limits where it could go.

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
