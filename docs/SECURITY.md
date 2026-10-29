# Security

What `agent-team` protects, and the code that does it. Everything here describes `main`; where the design in [ARCHITECTURE.md](ARCHITECTURE.md) section 8 goes further than the code, this page says what the code does. Attackers, trust boundaries and the risks that remain are in [THREAT_MODEL.md](THREAT_MODEL.md).

## What it protects

- **Your credentials.** SSH and cloud keys, the GitHub credential, `.env` files, your Claude Code configuration.
- **The target repository.** Its default and protected branches, its git config and hooks, and every file outside the run's own worktree.
- **Your host.** Agents and repository commands run in a git worktree, inside the `srt` sandbox when it is installed.
- **The record.** What every run did, with secrets redacted before anything is stored.

## Autonomy levels

Autonomy is a ceiling set by files you control. An agent never asks for more.

| Level | What it unlocks in the code |
| --- | --- |
| A0 | Read: `fs.read`, `git.read`, `shell.read` |
| A1 | Nothing beyond A0 yet |
| A2 | Workspace writes and execution: `fs.write.workspace`, `shell.execute.workspace`, `test.run` |
| A3 | `git.commit` for the agent; the draft PR for the orchestrator |
| A4 | Valid in a canonical file, but no capability needs it and the bootstrapper refuses it |

There is no A5: the schema (`Autonomy` in `src/agent_team/schemas/common.py`) stops at A4.

A run's ceiling is the lowest of:

1. the agent's `max_autonomy` (`canonical/agents/*.md`),
2. the policy's `autonomy_ceiling` for that agent, or `default_autonomy` (A0) when it has none (`canonical/policies/default.yaml`; `requested_autonomy` in `src/agent_team/run_agent.py`),
3. for a workflow's writing agents, the `autonomy_ceiling` you approved in `.agent-team/config.yaml` (`_writer_ceiling` in `src/agent_team/orchestrator/executors.py`), which the proposal caps at A3 (`MAX_BOOTSTRAP_AUTONOMY` in `src/agent_team/schemas/bootstrap.py`; `config.yaml` itself is not re-checked, so a hand edit above A3 is accepted, though no canonical agent's `max_autonomy` exceeds A3),
4. A2 when srt is not installed (`effective_ceiling` in `src/agent_team/environments/sandbox.py`).

The default policy sets the reviewer to A2, the implementer to A3 and the archaeologist to A0.

## Capabilities

An agent lists capabilities in `capabilities.allow`. The Claude adapter (`src/agent_team/adapters/claude/capabilities.py`) turns each one into Claude Code permission rules and runs the session in `dontAsk` mode, so **a tool call no allow rule covers is denied without a prompt**. A capability is dropped, and the reason written to the run record, when it is:

- above the run's ceiling,
- denied by the agent's `capabilities.deny` or the policy's `denied_capabilities`,
- `shell.execute.workspace` (bare `Bash`) outside srt, because no permission rule can keep Bash inside the worktree,
- `test.run` with no test command,
- one the orchestrator exercises itself: `git.write.branch`, `github.pr.create`, `github.pr.review`,
- unknown to the adapter.

| Capability | Minimum | Rules |
| --- | --- | --- |
| `fs.read` | A0 | `Read`, `Glob`, `Grep` |
| `git.read` | A0 | `git status`, `git diff`, `git log`, `git show`, `git blame` |
| `shell.read` | A0 | `ls`, `pwd`, `wc`, `head`, `tail` |
| `fs.write.workspace` | A2 | `Edit(./**)`, `Write(./**)` |
| `shell.execute.workspace` | A2 | `Bash`, only inside srt |
| `test.run` | A2 | the run's test commands |
| `git.commit` | A3 | `git add`, `git commit` |

Denied in every compiled session, whatever the agent asks for (`ALWAYS_DENY`): `WebFetch`, `WebSearch`, `git push`, `git branch -d`, `git branch -D`, `git reset --hard`, `git config`, and `gh`. Each command is denied both bare and with arguments.

The orchestrator, not the agent, pushes and opens the PR (`src/agent_team/orchestrator/pr.py`). It pushes exactly the reviewed commit to `refs/heads/agent-team/<run-id>` on `origin`, refuses a branch outside `agent-team/` or a `+` force refspec (`push`; the caller checks the worktree is on exactly `agent-team/<run-id>`), never passes `--force`, and disables repository hooks (`core.hooksPath=/dev/null`, `--no-verify`). It opens the PR only as `gh pr create --draft`, only at A3, only when the policy does not deny `github.pr.create`, only when the canonical tool for it is `gh pr create --draft`, only when `draft_prs` is not turned off in the approved proposal, and only when the policy's `required_gates` passed, except for an escalated run, whose draft is titled `[blocked]` and lists the failed gates (`_pr_refusals` and `_failed_gates` in `src/agent_team/orchestrator/executors.py`).

The Codex runtime has no per-command rules. There, the granted capabilities pick Codex's narrowest `sandbox_mode` (`read-only` for the reviewer), and srt does the rest (`src/agent_team/adapters/codex/capabilities.py`).

## Protected paths and the PreToolUse hook

Every compiled session protects the policy's `protected_paths` plus a fixed list (`DEFAULT_PROTECTED` in `src/agent_team/adapters/claude/compile.py`):

```text
.env*   *.pem   ~/.ssh/**   ~/.aws/**   ~/.config/gcloud/**   ~/.claude/**
~/.config/gh/**   ~/.git-credentials   ~/.netrc
~/.docker/**   ~/.kube/**   ~/.gnupg/**   ~/.azure/**   ~/.config/op/**
~/.npmrc   ~/.pypirc   ~/.cargo/credentials   ~/.cargo/credentials.toml   ~/.m2/settings.xml
~/.gradle/gradle.properties   ~/.vault-token   ~/.terraform.d/credentials.tfrc.json
~/.config/containers/auth.json   ~/.gem/credentials   ~/.config/hub   ~/.cache/huggingface/token
```

The home logins come from `HOME_LOGIN_DIRS` and `HOME_LOGIN_FILES` in `src/agent_team/environments/sandbox.py`. Key files by name (`KEY_FILES`: `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `id_ecdsa*`, `id_ed25519*`, anywhere) are added as `Read`, `Edit` and `Write` deny rules but not given to the hook, whose scan of every word in a command would also block `jq .key` (`PROTECTED_FILES` in `compile.py`).

The default policy adds `.agent-team/**` (the run record, other runs' worktrees and the compiled settings), apart from the run's own checkout.

They are enforced twice:

1. **Deny rules.** `Read`, `Edit` and `Write` deny rules for each path, in the compiled `settings.json`.
2. **The PreToolUse hook** (`src/agent_team/adapters/claude/hook.py`), which runs before every tool call:
   - It blocks a file tool, `Glob` or `Grep` whose path or pattern falls under a protected path.
   - It splits a `Bash` command the way the shell would (quotes and escapes joined, `$HOME` read as `~`) and blocks it when any word names a protected path, including a glob the shell would expand to one, such as `.en?`.
   - It blocks a command it cannot parse.
   - It blocks a file write that resolves outside the run's worktree, symlinks followed, or into git's metadata.
   - **It fails closed**: any error evaluating a call blocks it.

The hook is a tripwire, not a boundary. A process that builds a path at run time (`f=.e; cat ${f}nv`) gets past it. Only srt's `denyRead` rules hold against that. Inside srt they do: every run compiles the protected paths into srt read denies (next section). Outside srt nothing does; see [THREAT_MODEL.md](THREAT_MODEL.md#t3-secret-read-through-an-obfuscated-shell-command).

## The srt sandbox

When `srt` is installed and the environment's tier is `srt` (`canonical/environments/default.yaml`), every worker and every repository command agent-team runs is wrapped in it (`src/agent_team/environments/sandbox.py`):

- **Network**: denied except the environment's `allowed_domains`: `api.anthropic.com`, `github.com`, `*.github.com`, `*.githubusercontent.com`, `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org` by default. A Codex run adds only `api.openai.com`.
- **Writes**: only the worktree, the run's temp directory, and the corners of `.git` a commit on the run's branch needs: the object store, the worktree's admin directory, and `refs/heads/agent-team/` with its reflog under `logs/`. `main` and every other ref, `.git/hooks` and `.git/config` stay read-only, so a sandboxed commit cannot plant a hook or config that later runs outside the sandbox. The worktree's `.git` file and its admin directory's `commondir`, `gitdir` and `config.worktree` are write-denied, because they name the repository git opens (`git_pointer_files`).
- **Reads denied in the base settings** (`DENY_READ_HOME`): `~/.ssh`, `~/.aws`, `~/.config/gh`, `~/.git-credentials`, `~/.netrc`, and the home logins above except `~/.gnupg`.
- **Reads denied once an agent is compiled for the run**: the policy's protected paths, `DEFAULT_PROTECTED` and the key files, matched in the worktree and in the target repository, with `.agent-team/` denied around the run's own worktree, temp directory and compiled files (`srt_deny_read` in `src/agent_team/environments/sandbox.py`). A Claude run gets this list minus `~/.claude/**`, which the worker needs for its own login (`deny_read` in `compile_agent`, applied by `extend_settings` in `run_agent`); that list includes `~/.gnupg`. `inspect`'s command verification and Codex runs also deny `~/.codex/**`, and a Codex run denies a `CODEX_HOME` you set (`protect` in `src/agent_team/memory/verify.py`; `codex_home_deny_read`). In a workflow, DISCOVERY and TEST commands run in the same sandbox after the implementer's compile has added these denies.
- **Its own rules are out of reach**: the settings file lives in `.agent-team/sandbox/`, outside the worktree.

Without srt, a run falls back to the worktree alone and its ceiling is capped at A2. That means:

- no bare `Bash`, and no `git.commit` (the orchestrator commits the implementer's edits),
- no draft PR,
- `inspect` marks every command `unverified`,
- the workflow ends BLOCKED at TEST, because the orchestrator runs no command outside srt (`run_command` in `src/agent_team/orchestrator/workspace.py`).

Each run has its own worktree on its own branch under `.agent-team/worktrees/` (`src/agent_team/environments/worktree.py`). One run holds the repository lock at a time (`src/agent_team/environments/lock.py`). `.worktreeinclude` copies only files git ignores, never tracked files, symlinks or `.agent-team/` (`src/agent_team/environments/include.py`). Disposing a worktree saves uncommitted work and unpushed commits to `.agent-team/archive/<run-id>.patch` first.

## Scrubbed worker environment

A worker does not inherit your shell. `build_invocation` in `src/agent_team/adapters/claude/invoke.py` builds its environment from:

- an allowlist: `PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TERM`, `SHELL`,
- the run's own variables: `CLAUDE_CODE_TMPDIR`, `TMPDIR`, `AGENT_TEAM_PORT_OFFSET`, and `CLAUDE_CODE_TOOL_MEMORY_LIMIT=none` inside srt,
- `ANTHROPIC_API_KEY`, and only in API-key mode.

`GH_TOKEN`, `GITHUB_TOKEN`, cloud credentials and Claude Code session variables never reach it. Repository commands run by `inspect` and by the workflow's DISCOVERY and TEST steps get the same allowlist and no stdin; `inspect` adds `CI=1` and points `XDG_CACHE_HOME` into the run's temp directory.

The worker reads its prompt on stdin. It does not load the target repository's settings or hooks:

- With your login it runs with `--setting-sources ""`.
- With `ANTHROPIC_API_KEY` it runs with `--bare`, which also skips auto-discovered skills, plugins, MCP servers and `CLAUDE.md`.
- Both modes pass `--strict-mcp-config` with no MCP config, and `--no-session-persistence`.

Compiled files live in `.agent-team/compiled/<run-id>/` and reach Claude Code only through flags; nothing is written into the target's `.claude/` at run time. Only `agent-team configure` writes there, after you approve its diff (`src/agent_team/bootstrap/configure.py`). It never writes `.claude/settings.json`, and never writes through a symlink.

The Codex worker (`src/agent_team/adapters/codex/invoke.py`) runs on `CODEX_API_KEY` only: there is no login mode. It gets the same allowlist, `CODEX_API_KEY`, and a `CODEX_HOME` pointing at an empty directory in the run's temp directory; your own `CODEX_HOME` is never passed. It runs with `--ignore-user-config --ignore-rules --ephemeral`, so your `config.toml` and execpolicy rules do not load, and with the user config ignored no project is trusted, so the repository's `.codex/config.toml` and hooks do not load either.

## Auth reported as present or absent

agent-team never reads, prints or stores a credential's value.

- `agent-team doctor` reports Claude and GitHub auth as `present` or `absent` (`src/agent_team/doctor.py`). For Claude it reads only the `loggedIn` boolean from `claude auth status --json`; for gh only the exit code of `gh auth status`.
- The Codex adapter checks only that `CODEX_API_KEY` is set (`has_auth`).
- `gh_authenticated` in `src/agent_team/orchestrator/pr.py` reads only the exit code.
- Canonical tool files name a credential by handle (`credential: GH_TOKEN`); the schema rejects anything that is not an upper-case variable name (`src/agent_team/schemas/tool.py`).
- `init` records secret files by path and kind and remote URLs without their userinfo (`src/agent_team/bootstrap/discovery.py`).

## Redaction and the run record

Everything stored in `.agent-team/runs.db` passes through `src/agent_team/runs/redact.py` first (`RunStore._clean` and `_checkpoint_json` in `src/agent_team/runs/store.py`): tool-call arguments, transition notes and checkpoints. Redaction:

- replaces the value under any credential-looking key,
- scrubs known token shapes (Anthropic, OpenAI, GitHub, Slack, AWS, Google, JWTs, private keys, bearer and basic auth, URL userinfo) and `NAME=value` assignments whose name looks like a credential,
- applies the policy's own `redaction` patterns,
- runs before the stored summary is truncated, so the cut cannot leave half a secret. A string longer than 8192 characters is cut before it is scanned, and a token straddling that cut can survive in part.

The same redaction applies to:

- the stderr tail kept from a failed worker,
- `inspect`'s memory files and command output,
- the workflow's `reproduction.log`, `test-<n>.log` and `pr-body.md`.

The store refuses to open unless git ignores `.agent-team/`, so none of it is committed by accident.

## Known limits

Read [THREAT_MODEL.md](THREAT_MODEL.md) before trusting a run with anything valuable. In short:

- Without srt, protected paths are only permission rules and a tripwire hook, and test commands an A2 agent runs are not confined.
- An API key in API-key mode is in the worker's environment.
- `~/.claude` stays readable inside the sandbox, because Claude Code reads its own login there.
- The reviewer and the implementer currently share a vendor.
- The Codex adapter has never run against a real Codex.

## Reporting a vulnerability

Report it privately through GitHub's private vulnerability reporting: on [the repository](https://github.com/SolomonSmith-dev/agentic-engineering-os), open **Security**, then **Report a vulnerability**. If that button is not there, open an issue that asks for a private contact and leave the details out.

Please include:

- the commit or tag,
- the OS and the `claude`, `srt` and `codex` versions involved,
- the smallest canonical files and target repository that reproduce it,
- what you expected the boundary to stop.

A bypass of the hook, srt rules that let a worker reach a credential or write outside its worktree, a credential reaching the run record, and any path that pushes to a branch other than `agent-team/<run-id>` are all in scope.

The project is maintained by one person, so there is no response-time promise. Reports are acknowledged and fixed in the open once a fix is ready.
