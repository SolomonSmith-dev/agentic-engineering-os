# agentic-engineering-os

`agent-team` runs Claude Code workers and an independent reviewer inside sandboxed git worktrees, records every run in SQLite, and gates pull requests on the reviewer's verdict.

Status: build-order steps 1 to 13 of 15 are merged, version 0.1.0. The CLI answers `--version`, checks the host (`agent-team doctor`), discovers and configures a target repository (`agent-team init`, `agent-team configure`), builds repository memory with the archaeologist (`agent-team inspect`), runs one canonical agent end to end (`agent-team run --agent`, on Claude Code or, for the reviewer, Codex), runs the bug-fix workflow from reproduction to a draft PR (`agent-team run --workflow bug-fix`, `agent-team resume`), compares the team with two single-agent baselines on ten tasks with hidden verifiers (`agent-team eval run`, `agent-team eval report`, `agent-team eval --last`; no paid eval has been run yet), reads the run record (`agent-team trace`), sweeps stale run worktrees (`agent-team env sweep`), validates `canonical/` against the pydantic schemas (`agent-team validate`) and exports them as JSON Schema (`agent-team schema export`). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the target design and build order.

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
- After the agent succeeds, agent-team runs each command `init` detected, in order, inside the run's srt sandbox and worktree: no network beyond the environment allowlist, writes only in the worktree and the run's temp directory, and no reads of the policy's protected paths: `.env*`, `~/.claude`, the rest of `.agent-team/`, and the defaults, which cover key files anywhere in the repository (`*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `id_ecdsa*`, `id_ed25519*`) and the registry, cluster and cloud logins a build tool would find in home (`~/.docker`, `~/.kube`, `~/.npmrc`, `~/.pypirc`, `~/.gnupg`, `~/.azure`, `~/.config/op`, Cargo, Maven and Gradle credentials, and `~/.codex`). Each command gets a scrubbed environment with `CI=1`, no stdin, and `--command-timeout` seconds (600 by default) before its process group is killed. Only a redacted tail of its output is kept. Every sandboxed run, whatever the runtime, also gets srt read denies for those home logins (all but `~/.gnupg`, which a worker's own signed commit needs), and key files are denied as files through permission rules and srt rather than by the hook, whose word scan would block `jq .key`.
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
- A `PreToolUse` hook blocks any tool call that names a protected path (`.env*`, `*.pem`, `~/.ssh`, `~/.aws`, registry and cloud logins in home, the policy's own list), including through Bash. It fails closed.
- With your Claude login the worker runs with `--setting-sources ""`, so hooks and settings checked into the target repository never load. With `ANTHROPIC_API_KEY` set (CI) it runs with `--bare`.
- The worker gets a scrubbed environment and the prompt on stdin. An agent whose output has a schema (the reviewer's verdict) must return valid structured output, or the run fails.
- The model comes from `routing/registry.yaml` (found next to `--canonical`, or pass `--registry`): the agent's `model_class` maps to a pinned model id, and that model's `fallbacks` are emitted as `fallbackModel` in the compiled `settings.json`, so Claude Code switches to them when the model is overloaded or unavailable. The reviewer's class `independent` resolves to `claude-opus-5-5` with `claude-sonnet-5-5` as its one fallback. `--model <id>` overrides the class with another registry entry; an id that is not in the registry is refused.
- A policy's `allowed_privacy_classes` pins which registry privacy classes (`vendor-cloud`, `vendor-zdr`, `local-only`) a model may have: a primary model outside it refuses the run, a fallback outside it is dropped. The run record notes the resolved model, its source and the fallback chain.

After changing the adapter, the sandbox or the run path, run the opt-in live check on a machine with Claude Code, `srt` and a login. It calls a model (Haiku by default, capped at $0.10) and never runs in CI:

```bash
AGENT_TEAM_LIVE=1 uv run pytest -q -m live
```

On Linux, Claude Code may run each Bash call in a memory cgroup of its own, which srt's read-only `/sys` refuses (`EROFS ... clone3 /sys/fs/cgroup/...`), leaving the agent without a shell. agent-team sets `CLAUDE_CODE_TOOL_MEMORY_LIMIT=none` for every sandboxed worker. Set it yourself when you run Claude Code under srt by hand:

```bash
srt --settings srt.json -- env CLAUDE_CODE_TOOL_MEMORY_LIMIT=none claude -p "..."
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

A workflow whose agents or steps the executor does not have refuses before recording anything and names what is missing.

## Implementer, evidence and the draft PR

Build-order step 11 completes the bug-fix workflow: the `implementer` agent (`canonical/agents/implementer.md`, model class `strong-coding`, at most A3), the orchestrator-run `test` step, and the `pr` step that opens a draft pull request through `gh`.

```bash
agent-team run --workflow bug-fix --repo /path/to/target \
  --test-command "uv run pytest" --lint-command "uv run ruff check ." --typecheck-command "uv run mypy ." \
  "Fix the crash on empty input."
```

- **One worktree per workflow run.** The run owns `.agent-team/worktrees/<run-id>/` on branch `agent-team/<run-id>`. The implementer works there in DISCOVERY (it commits a failing regression check and names the command that runs it), in IMPLEMENTATION, and in the retry, so the retry builds on attempt one's commits. Each call is still a fresh `claude -p` process with its own child run; the retry gets the plan and the reviewer's findings, never attempt one's output.
- **The agent's word is a claim.** After each implementer call the orchestrator checks the worktree is still on the run branch above its base, commits anything left uncommitted, and takes that commit as the one everything else uses: it re-runs the reproduction command in the sandbox before the fix (the DISCOVERY gate is what it saw, not what the agent said), cuts the diff from the branch, and compares the files this attempt committed with the files the implementer's report names.
- **TEST** runs the reproduction command, then every test, lint and type-check command it knows (the `--test-command`, `--lint-command` and `--typecheck-command` flags, plus the verified ones in `.agent-team/config.yaml` from `configure`), inside srt, from the worktree, at exactly the diff's head, with the worker's scrubbed environment. All must pass. A kind with no command is skipped and named in the run record and the PR body.
- **CODE_REVIEW** runs the reviewer in a fresh worktree of its own, cut at that same commit. Its prompt carries the plan, the diff and the check output, inlined from files; never the implementer's transcript or report. A blocker or major finding turns an `approve` into `changes`.
- **Artifacts** are files under `.agent-team/artifacts/<run-id>/`: `reproduction.log`, `diff-<n>.patch`, `test-<n>.log`, `review-<n>.json` and `pr-body.md`. The checkpoint holds their paths, sizes and hashes, not their content.
- **PR** pushes exactly the reviewed commit to `agent-team/<run-id>` on `origin` (never `--force`, never another branch, repository hooks off) and runs `gh pr create --draft --head agent-team/<run-id>`. It needs the `github.pr.create` tool in `canonical/tools/`, an A3 run (which needs srt), `draft_prs` not turned off in the approved proposal, and the policy's `required_gates` passed. The body carries the evidence summary, the verdict, the unresolved risks and the minor findings as follow-up checkboxes. An escalated run's draft is titled `[blocked]` and lists the blocking findings.
- **The GitHub credential stays with the orchestrator.** `git push` and `gh` run in the orchestrator's own process, outside every sandbox. Workers get a scrubbed environment (no `GH_TOKEN`), their sandbox cannot read `~/.config/gh`, `~/.git-credentials` or `~/.netrc`, and `gh` is a denied command in every compiled session.
- **When no PR can be opened** (gh missing or not logged in, no `origin`, the push or `gh pr create` failing, a run below A3, draft PRs not approved), the run still reaches HUMAN_DECISION, ends `blocked`, keeps the branch, and records why; `pr-body.md` is ready for a human to use.
- **Without srt** the implementer runs at A2 with no Bash and no commits of its own (the orchestrator commits its edits), the orchestrator runs no command at all, and the run ends BLOCKED at TEST with the branch kept. Every command it would run executes code the agent wrote, and the reproduction command comes from the agent itself.

`agent-team env sweep` later disposes the workflow worktree like any other ended run's, deleting the local branch after saving its commits as a patch; a pushed branch stays on the remote.

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

## Eval: the team against two baselines

`agent-team eval` is build-order step 13: it runs the same tasks through three arms and prints the section 13 metrics side by side, so the gate after step 13 is decided by numbers.

| Arm | What runs | Model |
| --- | --- | --- |
| `baseline-a` | mini-swe-agent 2.4.6, one shot, inside srt | `claude-opus-5-5` (the `high-reasoning` class) |
| `baseline-b` | one `claude -p` session: the implementer agent once, no plan, reviewer or orchestrator checks | the same |
| `treatment` | `agent-team run --workflow bug-fix` | implementer `claude-sonnet-5-5`, reviewer `claude-opus-5-5` |

```bash
agent-team eval run                     # the dry run: the matrix, the caps, the worst case; calls no model
agent-team eval run --execute           # the same plan, for real (refused for a plan never dry-run)
agent-team eval report                  # the latest eval's metrics and verdict; --eval <id> for another
agent-team eval run --arms baseline-b,treatment --tasks lru-recency,report-median --runs 1
agent-team eval --last -C /path/to/target   # record that repository's last `run --workflow` against the baseline
```

- **Tasks.** `evals/tasks/*.yaml`, Harbor-shaped: `repo`, `ref`, `instruction`, `kind`, a hidden `verifier`, `existing_tests` (for regressions) and `planned_files` (for unnecessary diff). The ten shipped tasks are tiny Python projects under `evals/fixtures/<task>/repo` (3 locate-bug, 3 fix-bug, 2 small-feature, 2 repair-failing-test), each committed into a fresh repository with a fixed identity and date, so it lands on exactly the task's `ref`; a fixture that changed is refused. A task may instead name a git URL or path and a ref: that is how the private 30-task set plugs in. History past the ref and every remote are removed from the copy.
- **Fresh copy, hidden verifier.** Every cell (task x arm x run) gets its own copy. The verifier's files (`evals/fixtures/<task>/verifier`) are copied in only after the arm exits, into a fresh export of the arm's final tree, and the verifier and the existing tests run there inside srt with no network. While an arm runs, its sandbox cannot read the task files, the fixtures, the verifier files or `.agent-team/eval/` (earlier cells, the results).
- **Budgets.** `--max-budget-usd-per-task` (default `1.00`) caps one cell, every model call in it together: mini-swe-agent's `--cost-limit`, Claude Code's `--max-budget-usd`, and for the treatment each agent call gets only what is left of the cell's cap. `--max-total-usd` (default `15.00`) caps the eval, probe included: a cell starts only when the spend so far plus its cap fits, and the cells that do not are recorded `not_run` with the reason. A runtime checks its cap between turns, so a cell can overshoot by one turn; the result says so (`over_cap`). `--runs` defaults to 1: 10 tasks x 3 arms x 3 runs at $1.00 each is twice the total cap.
- **The default matrix.** 10 tasks x 3 arms x 1 run is 30 cells: worst case $30.10 with the probe, so the $15.00 total cap holds and, in the worst case, 14 cells start. The dry run prints an expected figure only once this store holds recorded costs for every arm.
- **Probe.** Before the first paid cell, the eval runs the live reviewer check in-process: the reviewer on the cheap class (`claude-haiku-5-5`, at most $0.10) must run a Bash command inside srt, or no cell runs. It catches a worker with no shell (the step 11 cgroup failure). `--skip-probe` skips it, for example right after `AGENT_TEAM_LIVE=1 uv run pytest -q -m live`.
- **mini-swe-agent needs an API key.** It calls the Anthropic API through litellm, which reads `ANTHROPIC_API_KEY`; it cannot use a Claude Code login. Without the key (or without `uv sync --group dev --group baseline`, which installs it; it is never a default dependency) `baseline-a` is refused before anything runs, and the eval refuses until you set the key or leave the arm out with `--arms baseline-b,treatment`. With the key set, the Claude arms also run with it (`--bare`), as in CI. Known gap: inside mini-swe-agent the key is in the environment its commands see.
- **Results.** `.agent-team/eval/results.jsonl`, one record per line (`eval`, `probe`, `cell`, `dry_run`, `field`). Each cell's directory moves to `.agent-team/eval/work/<eval-id>/<task>--<arm>--<run>/` afterwards; `agent-team trace -C <that>/repo` reads its run record.
- **Report.** Per arm: completion rate (verifier passes), test pass rate and regressions (existing tests), retries, wall time, tokens, dollars, human interventions (RESUME transitions, 0 in an automated eval), unnecessary diff lines (outside `planned_files`), budget aborts, and the headline: cost per successful task beside interventions per successful task. Review defects and context consumed print "not measured" with the reason, as does any metric an arm's records cannot support. The decision rule's verdict compares the treatment with the best baseline on the task-runs every compared arm ran.

The treatment opens no PR in the eval: the task repository has no remote and the eval passes no `gh`, so its runs end `blocked` at HUMAN_DECISION by design; completion is the verifier's verdict, not the run's outcome.

## Environments

Each run gets its own git worktree at `.agent-team/worktrees/<run-id>/` on branch `agent-team/<run-id>`, cut from `HEAD`. Gitignored files listed in `.worktreeinclude` (gitignore syntax, the same convention Claude Code uses) are copied in; tracked files, symlinks and `.agent-team/` never are. Each worktree gets a port slot, exported as `AGENT_TEAM_PORT_OFFSET` (slot × `ports.block_size`), so parallel dev servers do not collide.

When `srt` is installed, commands run inside it: no network except the allowlist in `canonical/environments/default.yaml`, writes only in the worktree, the run's temp directory and `.git` (minus `.git/hooks` and `.git/config`), and no reads of `~/.ssh`, `~/.aws` or the GitHub credentials (`~/.config/gh`, `~/.git-credentials`, `~/.netrc`). The settings file lives in `.agent-team/sandbox/`, outside the worktree. Without `srt` the run falls back to the worktree alone and its autonomy is capped at A2.

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
uv sync --group dev --group baseline   # adds mini-swe-agent, for the eval's baseline-a only
```

## License

Apache-2.0. See [LICENSE](LICENSE).
