# Threat model

What `agent-team` defends, against whom, and what is still open. Every mitigation names the code that enforces it on `main`. [SECURITY.md](SECURITY.md) describes the controls themselves; this page weighs them against attacks.

Residual risks are stated plainly. Where a control does less than [ARCHITECTURE.md](ARCHITECTURE.md) section 8 plans, the residual says so.

## Assets

| Asset | Where it lives |
| --- | --- |
| A1. Host credentials | `~/.ssh`, `~/.aws`, `~/.config/gcloud`, keychains, signing keys |
| A2. The GitHub credential | `GH_TOKEN` / `GITHUB_TOKEN` in your shell, or `~/.config/gh`, `~/.git-credentials`, `~/.netrc` |
| A3. Model-runtime credentials | Claude login under `~/.claude`, `ANTHROPIC_API_KEY`; Codex login under `~/.codex`, `CODEX_API_KEY` |
| A4. Repository secrets | `.env*`, `*.pem` and similar files in the target repository |
| A5. The target repository's integrity | its default and protected branches, `.git/config`, `.git/hooks`, files outside the run's worktree |
| A6. The run record and artifacts | `.agent-team/runs.db`, `.agent-team/artifacts/`, `.agent-team/memory/` |
| A7. Source code confidentiality | everything an agent reads is sent to the model vendor |
| A8. Money | model spend per run |

## Trust boundaries

```text
 you (trusted)
   │  agent-team CLI, orchestrator, git push, gh          ── holds A2, runs outside every sandbox
   │
   ├── canonical/ and routing/ from your clone           ── trusted input, schema-validated
   │
   ├── target repository content                          ── UNTRUSTED: files, instruction files, issue text, commands
   │
   └── srt sandbox, one per run                           ── boundary: network allowlist, write allowlist, read denies
         └── worker (claude -p / codex exec)              ── UNTRUSTED output: steered by repository content
               └── tools the worker calls                 ── PreToolUse hook + permission rules (Claude only)
```

- **B1. You → orchestrator.** Trusted. The orchestrator holds the GitHub credential and runs `git push` and `gh` itself (`src/agent_team/orchestrator/pr.py`).
- **B2. Orchestrator → worker.** The worker's output is treated as a claim. The orchestrator re-runs the reproduction, cuts the diff from the branch itself, compares the files changed with the files claimed, and runs every check itself (`_after_discovery`, `_after_implementation`, `_test` in `src/agent_team/orchestrator/executors.py`).
- **B3. Worker → host.** srt when installed (`src/agent_team/environments/sandbox.py`). Without srt, only the worktree, permission rules and the hook, with the ceiling capped at A2.
- **B4. Worker → network.** srt's domain allowlist (`canonical/environments/default.yaml`).
- **B5. Repository → agent-team.** `init` and `configure` read repository files as data and run nothing from them (`src/agent_team/bootstrap/`).

## Attackers

- **R1. A hostile target repository.** Someone who controls committed content: source, tests, build files, `CLAUDE.md` / `AGENTS.md`, `.mcp.json`, `.claude/`, `.codex/`, package scripts.
- **R2. Prompt injection through repository content or the task.** Text that a model reads and follows: a comment, a README, a test fixture, an issue body pasted as the task, a reviewer finding echoed back into a prompt. It turns the worker against you.
- **R3. A compromised or misbehaving model.** The same effect as R2, without an attacker: an agent that does more than it was asked.
- **R4. A compromised dependency or binary.** A package the repository's commands install, or a `claude`, `srt`, `codex`, `gh` or `git` on `PATH`.
- **R5. A local user or process on your machine.** Out of scope: it already has your files. Listed so nothing below is read as defending against it.

## Threats

### T1. Prompt injection makes an agent act beyond its task

Attackers: R2, R3.

Mitigations:

- **Capabilities are the boundary, not the prompt.** Sessions run in `dontAsk`, so any call no allow rule covers is denied without a prompt (`resolve` and `PERMISSION_MODE` in `src/agent_team/adapters/claude/capabilities.py` and `compile.py`).
- **Some commands are denied for every agent.** `git push`, `git branch -d/-D`, `git reset --hard`, `git config`, `gh`, `WebFetch` and `WebSearch` (`ALWAYS_DENY`).
- **Autonomy ceilings cap what a session can be given.** They are the lowest of the agent, the policy and the approved config, and A2 without srt (`requested_autonomy` in `src/agent_team/run_agent.py`, `_writer_ceiling` in `executors.py`, `effective_ceiling` in `sandbox.py`). The bootstrapper never accepts above A3 (`Ask._at_most_a3` in `src/agent_team/schemas/bootstrap.py`), and no capability needs more than A3.
- **The reviewer is independent of the implementer.** It runs in a fresh process and a fresh worktree, reads only the plan, the diff and the orchestrator's check output, never the implementer's transcript or report (`canonical/workflows/bug-fix.yaml` node `inputs`, `CODE_REVIEW` in `executors.py`). A blocker or major finding cannot approve (`_after_review` in `executors.py`).
- **The only outward action is a draft PR on the run's own branch, opened by the orchestrator, and a human decides.** (`push`, `create` in `src/agent_team/orchestrator/pr.py`; the `HUMAN_DECISION` state.)
- **`inspect` runs only what discovery found.** Commands only the archaeologist suggested are recorded `unverified` and never run (`src/agent_team/memory/inspect.py`).

Residual:

- Injection can still steer the code the implementer writes, within its capabilities. The reviewer and the draft PR are the checks, and both can be fooled: the diff the reviewer reads is attacker-influenced text too.
- The reviewer's class `independent` and the implementer's `strong-coding` both resolve to Claude models (`routing/registry.yaml`). Section 4's rule that they never share a vendor is not met in the MVP; `--runtime codex` for the reviewer is opt-in and untested against a real Codex.
- In login mode the worker runs with `--setting-sources ""`, which keeps the repository's settings and hooks out. Whether the target's `CLAUDE.md` still reaches the worker as context in that mode is not verified here. API-key mode uses `--bare`, which skips `CLAUDE.md`.

### T2. A worker reads a host or GitHub credential

Attackers: R2, R3. Assets A1, A2, A3.

Mitigations:

- **The worker's environment is built from an allowlist,** not inherited, so `GH_TOKEN`, `GITHUB_TOKEN` and cloud credentials never reach it (`ENV_ALLOWLIST` and `build_invocation` in `src/agent_team/adapters/claude/invoke.py` and `src/agent_team/adapters/codex/invoke.py`).
- **srt denies reads of `~/.ssh`, `~/.aws`, `~/.config/gh`, `~/.git-credentials` and `~/.netrc` for every sandboxed process** (`DENY_READ_HOME` in `sandbox.py`).
- **The same paths, plus `~/.config/gcloud` and `~/.claude`, are compiled into deny rules and the PreToolUse hook** (`DEFAULT_PROTECTED` in `compile.py`, `src/agent_team/adapters/claude/hook.py`).
- **`gh` is denied in every session,** and the orchestrator alone holds the GitHub credential (`pr.py`).

Residual:

- **API-key mode puts the key in the worker's environment.** In that mode (`ANTHROPIC_API_KEY`, or `CODEX_API_KEY` for Codex) the key is in the worker's environment, readable by any command it runs.
- **The runtimes' own logins stay readable.** `~/.claude` (Claude) and `~/.codex` (Codex) are not srt read-denied, because each runtime reads its own login there. For Claude only the hook stands between a Bash command and `~/.claude`, and the hook is a tripwire (T3).
- **`~/.config/gcloud` is a hook and permission rule only.** It is not an srt read deny on the Claude path.

### T3. Secret read through an obfuscated shell command

Attackers: R1, R2, R3. Asset A4 (and A3 for `~/.claude`).

The hook reads Bash commands the way the shell would and blocks any word that names a protected path, but a command that builds the path at run time (`f=.e; cat ${f}nv`) passes it. That is documented in the hook's own docstring.

Mitigations:

- **The Codex runtime compiles protected paths into srt `denyRead` rules,** keeping only the run's checkout readable under `.agent-team/` (`srt_deny_read` in `src/agent_team/adapters/codex/compile.py`, applied in `_run_codex` in `run_agent.py`).
- **`inspect`'s command verification does the same before the first repository command runs** (`protect` in `src/agent_team/memory/verify.py`).

Residual, verified on this branch:

- **On the Claude path, srt does not read-deny the policy's protected paths inside the target repository.** This covers a single `run --agent` and the bug-fix workflow: the implementer's Bash, and the DISCOVERY and TEST commands the orchestrator runs. Only `DENY_READ_HOME` is applied.
- **Demonstration.** A process inside the run's sandbox, started from the worktree, read the target repository's `.env` with `f=.e; cat ../../../${f}nv`.
- **What else is exposed.** The same holds for `.agent-team/runs.db` and other runs' artifacts. Because TEST runs code the implementer wrote, a test can read them, and the hook never sees it.
- **What partly limits it.** Redaction catches only known token shapes in what is stored (T4). The network allowlist limits where the data can go (T5).
- **The fix.** Apply the same `srt_deny_read` extension the Codex path uses, minus `~/.claude` for the Claude worker, to every Claude run and workflow environment.

### T4. A secret leaks into the run record, an artifact or the PR

Attackers: R2, R3, or accident. Assets A2–A4, A6.

Mitigations:

- **Everything the store writes is redacted first:** tool-call arguments, transition notes and checkpoints. That means credential-looking keys, known token shapes, `NAME=value` assignments and the policy's own patterns, applied before truncation (`src/agent_team/runs/redact.py`, `RunStore._clean`, `_checkpoint_json` in `src/agent_team/runs/store.py`).
- **The same redaction covers files and output outside the database:** the stderr tail, `inspect`'s memory files and command output, `reproduction.log`, `test-<n>.log` and `pr-body.md` (`normalize` in `invoke.py`, `write_memory` in `inspect.py`, `redact_lines` in `workspace.py`, `body` in `pr.py`).
- **The store refuses to open unless git ignores `.agent-team/`,** so none of it is committed by accident (`check_state_dir_ignored` in `store.py`).
- **Discovery records secrets by path and kind and strips URL userinfo** (`discover_secrets`, `sanitize_remote` in `discovery.py`).

Residual:

- **Redaction is pattern-based.** A secret with no known shape and no credential-like name passes through.
- **The code itself is not redacted.** `diff-<n>.patch` and the pushed commit are code, not logs. A secret the implementer writes into a file is pushed with the draft PR, and only the reviewer and you stand in the way.

### T5. Exfiltration over the network

Attackers: R1, R2, R3, R4.

Mitigations:

- **srt denies all network except the environment's allowlist** (`build_settings` in `sandbox.py`, `canonical/environments/default.yaml`).
- **`WebFetch` and `WebSearch` are denied in every Claude session** (`ALWAYS_DENY`).

Residual:

- **The default allowlist has hosts that accept uploads.** It includes `github.com`, `*.github.com`, `*.githubusercontent.com`, `pypi.org`, `registry.npmjs.org` and the model API. Any of them that accepts data from an unauthenticated or attacker-authenticated client is a channel.
- **Without srt there is no network control at all.** The A2 cap removes bare Bash, but a test command an A2 agent may run (`test.run`) is not network-confined.
- **Narrow the allowlist per repository** in its own environment file.

### T6. A hostile repository runs code on the host

Attacker: R1, R4. Assets A1, A5.

Mitigations:

- **`init` runs nothing from the repository.** Files are parsed as data (`yaml.safe_load`, `tomllib`, `json`), capped in size (`SCAN_MAX_BYTES`), and read without following symlinks out of the root. Binaries are looked up only as absolute paths, so a binary planted in the repository is never found (`_which` in `src/agent_team/doctor.py`). There are no `make -n`, `--help` or `--collect-only` probes (`src/agent_team/bootstrap/discovery.py`).
- **Repository commands run only inside srt,** with a scrubbed environment, no stdin and a timeout that kills the process group. Without srt they do not run at all (`run_command` in `src/agent_team/orchestrator/workspace.py`, `verify_commands` in `src/agent_team/memory/verify.py`).
- **The worker never loads the repository's Claude Code settings, hooks or MCP servers.** It runs with `--setting-sources ""` or `--bare`, plus `--strict-mcp-config` (`build_invocation` in `invoke.py`). Codex runs with `--ignore-user-config --ignore-rules --ephemeral` (`src/agent_team/adapters/codex/invoke.py`).
- **`configure` never writes through a symlink and never writes `.claude/settings.json`** (`src/agent_team/bootstrap/configure.py`).

Residual:

- **agent-team trusts the local `.git/`.** Its own `git` calls (discovery, the worktree allocator in `src/agent_team/environments/worktree.py`) honour the repository's `.git/config` and hooks. A clone you made yourself has a `.git/config` git wrote. A repository directory received as an archive can carry one that runs code on any git command (`core.fsmonitor`, hooks), before agent-team runs anything. Clone repositories yourself.
- **The allocator's `git worktree add` does not set `core.hooksPath=/dev/null`.** Unlike the orchestrator's `SAFE_GIT`, it runs your local `post-checkout` hook on the host.
- **Repository commands can still reach the network allowlist (T5) and read what srt does not deny (T3).**

### T7. A sandboxed process plants code that later runs outside the sandbox

Attackers: R1, R2, R3. Asset A5.

Mitigations:

- **srt allows writes only where a commit on the run branch needs them:** the worktree, the run's temp directory, `.git/objects`, the worktree's admin directory, and `refs/heads/agent-team/`. `.git/hooks` and `.git/config` are read-only, and the worktree's `.git` file is write-denied (`git_write_paths`, `build_settings` in `sandbox.py`).
- **The hook blocks file-tool writes into git metadata,** symlinks followed (`check` in `hook.py`).
- **The orchestrator's own git runs with hooks off and signing off** (`SAFE_GIT` in `src/agent_team/orchestrator/workspace.py`), and pushes with `--no-verify` (`push` in `pr.py`).
- **srt's settings file lives outside the worktree** (`settings_path` in `sandbox.py`).

Residual:

- **A sandboxed process can rewrite any `refs/heads/agent-team/*` ref,** not only its own run's. The orchestrator pushes the commit it reviewed, by hash, so a moved ref is not what gets pushed, but another run's local branch can be changed under it.

### T8. Destructive git operations

Attackers: R2, R3. Asset A5.

Mitigations:

- **Destructive git commands are denied for every agent:** `git push`, branch deletion, `git reset --hard` and `git config` (`DENIED_COMMANDS`).
- **The sandbox keeps every ref outside `agent-team/` read-only,** including `main` (T7).
- **The orchestrator pushes only `<head>:refs/heads/agent-team/<run-id>`.** It refuses any other branch or a `+` refspec, never passes `--force`, and opens the PR only as a draft (`push`, `create` in `pr.py`; `_pr_refusals` in `executors.py`).
- **Disposing a worktree saves its work as a patch first** (`_archive` in `worktree.py`).

Residual:

- **The push is never forced,** so a run branch that already exists on `origin` and has diverged refuses the push rather than being overwritten.
- **Inside its own worktree an agent with Bash can still rewrite its run branch's history** (`git commit --amend`, for one). The orchestrator checks after each implementer call that the worktree is still on the run branch and the branch still contains its base (`settle` in `workspace.py`).

### T9. Runaway cost

Attacker: R2, R3. Asset A8.

Mitigations:

- **Every Claude call is capped by `--max-budget-usd`,** passed through to `claude -p` (default 1.00) (`build_invocation` in `invoke.py`).
- **Every call is capped by `--timeout`,** which kills the worker's process group (`execute` in `invoke.py`).
- **The review loop is bounded.** The reviewer's `changes` retry is limited by `max_traversals: 1` (`canonical/workflows/bug-fix.yaml`, `src/agent_team/orchestrator/dag.py`).
- **Cost is recorded per run and per transition** (`add_cost` in `store.py`).

Residual:

- **There is no budget for a whole workflow.** Each agent call has its own cap, so a bug-fix run can spend roughly one cap per agent node visited. Section 8's orchestrator budget is not built.
- **Codex has no budget flag.** Cost is estimated only when the registry has a price, none does yet, and the estimate is checked after the run (`UNAVAILABLE` in `src/agent_team/adapters/codex/capabilities.py`).
- **Claude's figure is an estimate.** `total_cost_usd` is what Claude reports, not what is billed.

### T10. Supply chain in canonical files, skills and runtimes

Attacker: R4.

Mitigations:

- **Agents, skills and policies come only from the `canonical/` you pass.** Every file is validated with unknown keys rejected (`CanonicalModel` in `src/agent_team/schemas/common.py`, `validate_tree` in `src/agent_team/canonical.py`).
- **Skills are inlined into the prompt from `canonical/skills/`,** not discovered (`build_prompt` in `compile.py`).
- **No plugin, user skill or MCP server loads into a worker** (T6).
- **Model ids are pinned in `routing/registry.yaml`.** An id that is not in the registry is refused (`resolve_model` in `src/agent_team/routing.py`).
- **CI pins srt (`0.0.81`) and installs Python dependencies from `uv.lock`** (`.github/workflows/ci.yml`). agent-team has no self-update.

Residual:

- **Nothing verifies a signature.** `uv tool install .` trusts your clone.
- **The runtime binaries on `PATH` are trusted.** `claude`, `srt`, `codex`, `gh` and `git` must be absolute paths, but are not otherwise checked beyond a version or flag probe (`check_version`, `check_flags`).
- **Dependencies the target repository's commands install are R4 inside the sandbox.**

### T11. Code reaches a model vendor you did not approve

Asset A7.

Mitigations:

- **The policy's `allowed_privacy_classes` limits which registry privacy classes a model may have.** A primary model outside them refuses the run, and a fallback outside them is dropped (`resolve_model` in `routing.py`).
- **`configure` intersects them with the approved `privacy_classes`** (`plan_configure` in `configure.py`).
- **The run record notes the resolved model and its fallback chain.**

Residual:

- **Everything an agent reads goes to that vendor.** Privacy classes describe the registry entry; agent-team cannot verify the vendor's handling.
- **Login mode uses your account's data terms.**

### T12. The run record is altered or hidden

Asset A6.

Mitigations:

- **Workers cannot write `.agent-team/` outside their own worktree and temp directory.** The hook's `.agent-team/**` protection and the confinement of file tools to the worktree hold for the agent's own tool calls; srt's write allowlist holds for every process. Without srt, a test command an A2 agent runs is not confined.
- **Every run is recorded before its first state, and the store's API only appends** (`start_run`, `transition`, `record_tool_call` in `store.py`).

Residual:

- **Append-only is a property of the code, not the storage.** Anyone with your user account can edit `runs.db`.
- **It is not tamper-evident.** There is no hash chain or signature.

## Out of scope

- A local attacker with your user account (R5).
- Managed settings and repository rules that sit above agent-team. They are honoured, not enforced, by it.
- Threats from the model vendors themselves.
- Anything at A4 or above: agent-team grants nothing there.
