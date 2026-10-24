# Bootstrap: day one on a new repository

This is the path from a fresh laptop to a first draft pull request on a repository you were just given. It follows section 9 of [ARCHITECTURE.md](ARCHITECTURE.md) and describes what the code on `main` does today; where the code does less than section 9 plans, the step says so.

Nothing below writes outside the target repository's `.agent-team/` directory until step 5, and step 5 shows a diff of every file it will write and asks first.

```text
install -> doctor -> init -> ask -> configure -> inspect -> run -> trace
```

## Before you start

You need:

- Python 3.12 and [uv](https://docs.astral.sh/uv/).
- `git`.
- Claude Code (`claude`) 2.1.302 or newer, logged in (`claude auth status`) or with `ANTHROPIC_API_KEY` set. `agent-team run` refuses an older `claude` (`MIN_VERSION` in `src/agent_team/adapters/claude/invoke.py`).
- `srt` (the sandbox runtime, `npm i -g @anthropic-ai/sandbox-runtime`). Optional, but without it every run is capped at A2: agents get no general shell (only the test commands you name), agent-team runs no repository command itself, and no draft PR opens (see [SECURITY.md](SECURITY.md#the-srt-sandbox)). On Linux srt also needs `bubblewrap`, `socat` and `ripgrep`; on macOS, `ripgrep`. CI tests against srt 0.0.81.
- `gh`, logged in, only for the draft PR at the end of step 7.

## 1. Install

Clone the repository and install the CLI from the clone. The clone matters: `canonical/` (agents, skills, workflows, tools, policies, environments) and `routing/registry.yaml` are not part of the installed package, and every command that runs an agent reads them through `--canonical`.

```bash
git clone https://github.com/SolomonSmith-dev/agentic-engineering-os ~/agentic-engineering-os
cd ~/agentic-engineering-os
uv tool install .
agent-team --version            # agent-team 0.1.0
agent-team validate canonical   # ok: ... valid under canonical
```

Done check: `which agent-team` prints a path and `agent-team validate canonical` prints `ok`.

The examples below use `AT=~/agentic-engineering-os` and a target repository at `/path/to/repo`.

## 2. Doctor

```bash
agent-team doctor
agent-team doctor --json
```

Doctor is read-only (`src/agent_team/doctor.py`). It reports:

- **host**: OS, arch, Python, uv.
- **runtimes**: `claude`, `srt`, `gh`, `git`, each with its version or `not found`. A binary found through a relative `PATH` entry is treated as not found, so a binary planted in the current directory never runs.
- **auth**: `claude` and `gh`, each `present` or `absent`, never a value. Claude is `present` when `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` is set, or `claude auth status --json` says `loggedIn: true` (only that boolean is read). gh is `present` when `GH_TOKEN` or `GITHUB_TOKEN` is set, or `gh auth status` exits 0 (its output is discarded).
- **egress**: whether `https://api.anthropic.com` answers within 3 seconds.
- **managed settings**: whether a Claude Code managed-settings file exists at the platform's path (`/etc/claude-code/managed-settings.json` on Linux, `/Library/Application Support/ClaudeCode/managed-settings.json` on macOS). Managed settings sit above anything agent-team compiles.

Version probes run with every variable named like a token, key, secret, password or credential removed from their environment.

Done check: `claude` and `srt` show a version, claude auth is `present`, egress is `reachable`. If not, the row tells you what to install or ask for.

Section 9 plans more than this: probes for `codex`, `copilot`, `gemini` and `ollama`, and a usable / blocked / not installed verdict per runtime. Those are not built. The Codex reviewer checks its own binary and auth when you ask for it (`agent-team run --runtime codex`).

## 3. Init

Make sure the target repository ignores `.agent-team/` first. Every command that writes state refuses until git does (`check_state_dir_ignored` in `src/agent_team/runs/store.py`). If you cannot change the repository's `.gitignore` yet, put the line in `.git/info/exclude`, which git honours and nobody else sees.

```bash
echo '.agent-team/' >> /path/to/repo/.git/info/exclude   # or .gitignore
agent-team init /path/to/repo
```

Init discovers the repository read-only (`src/agent_team/bootstrap/discovery.py`) and writes one file, `.agent-team/proposal.yaml`. It records:

- VCS host and remote (userinfo stripped from the URL), the default branch, and protected branches when the repository declares them in `.github/settings.yml`; otherwise `unknown`, because the hosting API needs auth and init makes no network call.
- CI systems and their files.
- Build, test, lint and type-check commands from `Makefile`, `package.json`, `pyproject.toml`, `Cargo.toml` and `go.mod`.
- Instruction files (`CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`, `GEMINI.md`, `.cursorrules` and others), MCP configs (server names only), secret handling by path and kind (`.env` files, key files, vault references; never contents), devcontainer and CODEOWNERS.

Each command gets ✓ or ✗ from a check that runs nothing: ✓ means the tool it needs was found (on `PATH` as an absolute path, or in the repository's `.venv/` or `node_modules/`) and the Makefile target or package script it names exists. It does not mean the command passes; step 6 is where commands actually run. Init does not use `make -n`, `--help` or `--collect-only`, because each can execute repository code.

Init does not follow symlinks out of the repository, refuses when `.agent-team` itself is a symlink, and keeps an existing proposal unless you pass `--force`.

Example on a small Python repository:

```text
vcs           none
default       main (local branch)
protected     unknown (not discoverable without an authenticated API call)
ci            none
commands
  ✓ test       pytest                   pytest on PATH
  ✓ lint       ruff check .             ruff on PATH
instructions  none
mcp           none
secrets       none found
devcontainer  none
codeowners    none
ask           runtimes claude; privacy vendor-cloud; autonomy A3; draft PRs yes; approved no
```

Done check: the proposal lists every detected command with ✓ or ✗.

## 4. Ask permission

The `ask:` block at the bottom of `proposal.yaml` is the request you take to whoever owns the repository:

```yaml
ask:
  runtimes: [claude]
  privacy_classes: [vendor-cloud]   # vendor-cloud, vendor-zdr, local-only
  autonomy_ceiling: A3              # A0 to A3; anything higher fails validation
  draft_prs: true
  approved: false                   # set to true once approved
```

Edit it to what was agreed, fix anything discovery got wrong (unknown keys are errors, not ignored), and set `approved: true`. The bootstrapper never accepts an autonomy ceiling above A3 (`MAX_BOOTSTRAP_AUTONOMY` in `src/agent_team/schemas/bootstrap.py`).

## 5. Configure

```bash
agent-team configure /path/to/repo --canonical $AT/canonical --dry-run   # the diff; works before approval
agent-team configure /path/to/repo --canonical $AT/canonical             # the diff, then asks before writing
```

Configure (`src/agent_team/bootstrap/configure.py`) needs `approved: true` except under `--dry-run`. It prints a unified diff of every file it will write and asks before writing; `--yes` skips the question. It writes:

- `.agent-team/config.yaml`: the approved ask plus the ✓ commands by kind, and the ✗ ones under `unverified_commands` (`inspect` runs both). The bug-fix workflow's TEST step reads these as its test, lint and type-check gates.
- A marked block (`<!-- agent-team:begin -->` to `<!-- agent-team:end -->`) appended to `CLAUDE.md`, and to `AGENTS.md` if it exists. Text outside the block is never changed; a re-run updates the block in place and writes nothing when nothing changed. A `CLAUDE.md` that imports `@AGENTS.md` is left alone.
- `.claude/agents/<name>.md` for each canonical agent, compiled at the lower of its own ceiling, the policy's and the approved one.

It never writes `.claude/settings.json`. An agent file it did not write, a same-named agent under `.github/agents/`, and any path that is or sits under a symlink are left untouched and reported as notes. Section 9 says configure diffs against existing `.github/agents/` files; it reports them instead of changing them.

Done check: `git status` in the target shows only the files you approved (`.agent-team/` stays ignored).

## 6. Inspect

```bash
agent-team inspect /path/to/repo --canonical $AT/canonical
agent-team inspect /path/to/repo --summary
```

Inspect (`src/agent_team/memory/inspect.py`) runs the `repository-archaeologist` agent at A0: it may read files, run `git status/diff/log/show/blame`, `ls`, `pwd`, `wc`, `head` and `tail`, and nothing else. Its structured answer comes back to agent-team, which redacts it and writes repository memory:

```text
.agent-team/memory/repo/
  index.yaml  architecture.yaml  ownership.yaml  commands.yaml  conventions.yaml  pitfalls.yaml
```

After the agent succeeds, agent-team runs each command init detected, inside srt, from a fresh worktree cut at `HEAD` (`src/agent_team/memory/verify.py`): no network beyond the environment allowlist, writes only in the worktree and the run's temp directory, no reads of the policy's protected paths, a scrubbed environment with `CI=1`, no stdin, and `--command-timeout` seconds (600 by default) before the process group is killed. Each command lands in `commands.yaml` as `passed`, `failed`, `timeout` or `unverified`.

- Without srt nothing from the repository runs, and every command is `unverified` with the reason.
- Commands only the archaeologist suggested are listed `unverified` and never run: what runs comes from init's discovery, not from a model that read the repository.
- The worktree is cut from `HEAD`, so gitignored dependencies (`.venv/`, `node_modules/`) are missing unless a `.worktreeinclude` file lists them. A command init marked ✓ can fail here for that reason alone.

`--max-budget-usd` (default 1.00) and `--timeout` (default 1800 s) cap the archaeologist.

Done check: `agent-team inspect --summary` shows the map and every command's result.

## 7. First run

```bash
agent-team run --workflow bug-fix --repo /path/to/repo --canonical $AT/canonical \
  "Fix the crash on empty input."
```

Section 9 writes this as `agent-team run "<issue>"` with bug-fix as the default. The CLI has no default: give exactly one of `--workflow` or `--agent`.

The workflow (`canonical/workflows/bug-fix.yaml`, driven by `src/agent_team/orchestrator/`) walks:

```text
INTAKE -> DISCOVERY -> PLAN -> IMPLEMENTATION -> TEST -> CODE_REVIEW -> PR -> HUMAN_DECISION
```

- **DISCOVERY**: the implementer commits a failing regression check on the run branch `agent-team/<run-id>` and names the command that runs it. The orchestrator re-runs that command in the sandbox; the gate is what it saw fail, not what the agent said. Without srt the orchestrator runs nothing, and the gate falls back to the agent's own report, marked unverified in the run record and the PR body.
- **IMPLEMENTATION**: the implementer fixes the bug in the same worktree.
- **TEST**: the orchestrator runs the reproduction, then every test, lint and type-check command from `config.yaml` plus any `--test-command`, `--lint-command` and `--typecheck-command` flags, in srt, at exactly the diff's head. All must pass. A kind with no command is skipped and named in the PR body.
- **CODE_REVIEW**: the independent reviewer, in a fresh process and a fresh worktree at the same commit, gets the plan, the diff and the check output, never the implementer's transcript. A blocker or major finding turns `approve` into `changes`. One `changes` sends the run back to IMPLEMENTATION; a second, or `escalate`, still goes to PR, where the draft is titled `[blocked]` and the run ends `blocked` at HUMAN_DECISION.
- **PR**: the orchestrator pushes exactly the reviewed commit to `agent-team/<run-id>` on `origin` (never `--force`) and runs `gh pr create --draft`. That needs `gh` logged in, an `origin`, an A3 run (which needs srt), and `draft_prs` not turned off. When any is missing the run ends `blocked` at HUMAN_DECISION, keeps the branch, and records why. When the only problem is `gh`, its login, `origin` or the push itself, `pr-body.md` is already written under `.agent-team/artifacts/<run-id>/` for you to use.

Each agent call is capped by `--max-budget-usd` (default 1.00) and `--timeout` (default 1800 s). Ctrl-C leaves the run open; `agent-team resume <run-id> --repo /path/to/repo --canonical $AT/canonical` re-enters the state that was running.

Done check: a draft PR opens on `agent-team/<run-id>`, its body carries the evidence and unresolved risks, and CI runs on it.

## 8. Trace

```bash
agent-team trace -C /path/to/repo                    # recent runs
agent-team trace <run-id> -C /path/to/repo           # transitions with elapsed time and cost
agent-team trace <run-id> -C /path/to/repo --calls   # plus every tool call, arguments redacted
```

The run record is `.agent-team/runs.db` (SQLite). A workflow run lists its agent nodes as child runs named `agent:<name>`; trace each child for its tool calls. Artifacts (`reproduction.log`, `diff-<n>.patch`, `test-<n>.log`, `review-<n>.json`, `pr-body.md`) are under `.agent-team/artifacts/<run-id>/`.

Done check: `agent-team trace <run-id>` shows every transition, and `--calls` on each child run shows every command and file the agent touched.

## Afterwards

```bash
agent-team env sweep -C /path/to/repo
```

Sweep disposes the worktrees of ended runs. Uncommitted work and commits past the base are saved to `.agent-team/archive/<run-id>.patch` first; a pushed branch stays on the remote. In-progress runs keep their worktrees for `resume`.

Section 9's step 8, `agent-team eval --last`, belongs to build-order step 13 and is not on `main`.

## What the bootstrap never does

- Runs repository code during `init`: discovery reads files and runs only read-only `git` queries.
- Runs repository code outside srt itself: `inspect`'s verification and the workflow's DISCOVERY and TEST commands run nothing when srt is absent. An A2 agent granted `test.run` can still run the named test commands without a sandbox (see [THREAT_MODEL.md](THREAT_MODEL.md#t5-exfiltration-over-the-network)).
- Replaces an instruction file: configure appends or updates one marked block.
- Writes `.claude/settings.json`, `~/.claude`, or anything outside the target repository.
- Accepts an autonomy ceiling above A3.
- Copies anything out of `.agent-team/`.

See [SECURITY.md](SECURITY.md) for how these hold and [THREAT_MODEL.md](THREAT_MODEL.md) for what they do not cover.
