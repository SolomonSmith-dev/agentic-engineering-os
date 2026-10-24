# Review: build-order steps 10-12 (and PR #20)

Independent, read-only review of:

- Step 10 - bug-fix workflow YAML and the orchestrator DAG builder (`#10`): `canonical/workflows/bug-fix.yaml`, `src/agent_team/orchestrator/dag.py`, `machine.py`, `run.py`.
- Step 11 - implementer and independent-reviewer agents, orchestrator-run evidence, draft PR via `gh` (`#17`): `src/agent_team/orchestrator/executors.py`, `pr.py`, `workspace.py`, `canonical/agents/implementer.md`.
- Step 12 - Codex adapter for the reviewer (`#11`): `src/agent_team/adapters/codex/`.
- The Linux shell fix (`#20`): `src/agent_team/adapters/claude/invoke.py`.

Checked against the matching build-order lines (ARCHITECTURE section 11), the security model (section 8) and the Locked sub-decisions (section 11 assumptions). No code was changed. The suite was run as CI runs it (`uv sync --group dev`, srt 0.0.81 + bubblewrap + socat, `AGENT_TEAM_REQUIRE_SRT=1 uv run pytest -q`): **326 passed, 1 skipped**.

## Verdict

One **blocker**: the orchestrator runs `git` on the agent's worktree outside srt with only two safety overrides, and a sandboxed implementer can turn one of those `git` calls into code execution in the orchestrator's own environment, which holds the credentials the whole design is built to keep away from workers. Reproduced end to end. Everything else in the three steps holds up: reviewer isolation, verdict-gated PR creation, one-retry-then-escalate, the per-call budget, no worker credential, and Codex login state never exposed are all implemented as the locked decisions require. The rest of the findings are minor.

| # | Severity | Where | One line |
| --- | --- | --- | --- |
| 1 | blocker | `orchestrator/workspace.py:46,58-62,118-143` + `environments/sandbox.py:44-66` | A sandboxed worker redirects the worktree `commondir`; the orchestrator's next `git` runs an agent-planted `core.fsmonitor` program outside srt. |
| 2 | minor | `orchestrator/machine.py:198-204` | The terminal node's `NodeResult` (the `human_decision` summary line) is computed and silently dropped; the final human-facing status is never recorded as a transition. |
| 3 | minor | `adapters/codex/invoke.py:269-277` | `estimate_cost` bills cached input tokens at the full input rate and never reads `reasoning_output_tokens`; the recorded cost and the `--max-budget-usd` check can be wrong in both directions. |

---

## Blocker 1 - sandbox escape: orchestrator `git` executes agent-controlled code outside srt

**Files:** `src/agent_team/orchestrator/workspace.py:46` (`SAFE_GIT`), `:58-62` (`git`), `:118-143` (`settle`, `head_of`, `is_clean`, `restore`); `src/agent_team/environments/sandbox.py:44-66` (`git_write_paths`, `allowWrite`).

### What the code does

Every `git` the orchestrator runs on the implementer's tree goes through `workspace.git()`, which prepends exactly two overrides:

```python
SAFE_GIT = ("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false")
```

These run **outside srt** (plain `subprocess.run(["git", ...])`, no `sandbox.wrap`), in the orchestrator process, right after the implementer exits: `settle()` runs `git status`/`add`/`commit`, `restore()` runs `reset --hard`/`clean -fd`, and `diff_evidence()` runs `git diff`.

Meanwhile the srt allowlist deliberately makes the worktree's own git admin directory writable so the sandboxed worker can commit (`sandbox.py:44-66`):

```python
def git_write_paths(git_dir, run_id):
    return [git_dir/"objects", git_dir/"worktrees"/run_id,
            git_dir/"refs"/"heads"/BRANCH_NAMESPACE, git_dir/"logs"/"refs"/"heads"/BRANCH_NAMESPACE]
# allowWrite: [worktree, tmp_dir, *git_write_paths]; denyWrite: [worktree/.git]
```

`.git/worktrees/<run-id>/` is writable, and it contains `commondir`, the file that tells git where the shared object store and **config** live. `SAFE_GIT` neutralises hooks and GPG, but not the config knobs that run external programs: `core.fsmonitor`, `core.sshCommand`, clean/smudge `filter.*`, aliases, `core.pager`. The shared `config` is read-only to the worker, so those knobs look unreachable - until the worker rewrites `commondir` to point at a config file it controls.

### Attack path (realistic)

The implementer runs at A3 inside srt with `shell.execute.workspace`, acting on whatever the task text and the repository under review tell it (the threat model's untrusted intake). With one `Bash` call it:

1. copies `objects/`, `refs/`, `HEAD`, `config` into its own run temp dir (writable), adds `core.fsmonitor = <its own script>` to the copy's `config`;
2. overwrites `.git/worktrees/<run-id>/commondir` to point at that copy.

When the node finishes, `_after_implementation` calls `settle(env)` (`executors.py:332`), whose `git status --porcelain` fires the fsmonitor program - **in the orchestrator's environment, outside srt**.

### Reproduction (run against this repo, srt present)

A self-contained PoC (`scratchpad/poc/commondir_escape.py`) creates a throwaway repo, allocates a real srt worktree, runs the two steps above inside `env.sandbox.wrap(...)`, then calls `settle(env)`. Output:

```
sandboxed step: 0 wrote commondir
sandbox cannot write outside (expected)        <- the worker itself cannot touch the parent dir
marker before settle: False
settle: Settled(head=89ea308..., committed=0)
marker after settle: True uid=0(root) ...      <- settle's git ran the planted program
```

The marker file lives outside every path srt allows the worker to write; it appears only after `settle()`, proving the orchestrator executed agent-chosen code outside the sandbox.

### Impact

This defeats section 8 wholesale. At `settle`/`commit` time the orchestrator process still holds its full `os.environ`; later in the same run `pr.py` will put `GH_TOKEN` there. The planted program can read those, write anywhere on the host, or tamper with the run record and other runs' `.agent-team/` state - exactly the "no agent process ever has a token" and "secret exfiltration through tool output" guarantees the design advertises. It needs only an A3 implementer run, which is the MVP default.

### Why the tests miss it

`test_implementer.py`'s fake worker commits honestly; nothing exercises a worker that writes to `.git/worktrees/<run-id>/` beyond a normal commit. The happy path and the escape are indistinguishable to the current suite.

### Fix

Make the orchestrator's git calls refuse an agent-redirected worktree and refuse agent-supplied code config. Concretely, in `workspace.git()`:

- Before operating, assert the worktree is still anchored to the real repo: read `.git/worktrees/<run-id>/commondir` (or run `git rev-parse --git-common-dir`) and fail closed with a `WorkspaceError` if it does not resolve to the repository's real common dir. A redirected `commondir` is tampering - end the run blocked.
- Extend `SAFE_GIT` to neutralise the code-exec knobs regardless: add `-c core.fsmonitor=false -c core.sshCommand= -c core.pager=cat`, and run with `GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_GLOBAL=/dev/null` in the subprocess env so no outside config applies either. (The `-c` flags override even a redirected local config, so this alone closes the fsmonitor vector; the `commondir` assertion closes the class.)
- Longer term, narrow `allowWrite` so the worker cannot write `commondir`/`config`/`config.worktree` inside its admin dir (allow only `index`, `ORIG_HEAD`, `logs/HEAD`, and the ref/namespace it needs), or run `settle`/`restore` themselves inside srt.

A regression test should write `commondir` from inside the sandbox and assert `settle()` raises rather than executing anything.

---

## Minor 2 - the final human-facing status line is dropped

**File:** `src/agent_team/orchestrator/machine.py:198-204`.

`human_decision` (`executors.py:85-93`) builds a useful terminal note ("awaiting human review of draft PR <url>", or "escalated: ... marked blocked", or "no PR: <reason>; branch kept"). But `walk` returns `store.finish_run(...)` the moment `node.terminal` is true, before `result.note` is echoed or recorded. ARCHITECTURE line 346 acknowledges this ("the terminal node's note is not recorded"), so it is a known gap, not a surprise - but the consequence is that the single most useful line for the operator (where the PR is, or why there is none) exists only in the `pr` evidence and never in the transition log `agent-team trace` prints. Consider recording the terminal node's note on the last transition, or emitting it via `echo` before `finish_run`. Low risk, no correctness effect.

---

## Minor 3 - Codex cost estimate is systematically off

**File:** `src/agent_team/adapters/codex/invoke.py:269-277`.

`estimate_cost` charges `input_tokens` entirely at `input_per_mtok_usd` (cached-input discount ignored) and sums only `input_tokens` + `output_tokens`, with a code comment flagging that it is unverified whether `output_tokens` already includes `reasoning_output_tokens`. Both are openly documented as assumptions, and the registry currently lists no Codex price (so the estimate is `0.0` today), so this bites nothing in the MVP. But the same number feeds the `--max-budget-usd` check at `:309-312`: once a price is added, the budget gate for the Codex reviewer can trip late or not at all. Worth a note in the PR body and a follow-up once a first-party price is confirmed. No action needed for step 12 as shipped.

---

## What was verified and holds

- **Reviewer never sees the implementer transcript.** `CODE_REVIEW` inputs are `[plan, diff, test_output]` only (`bug-fix.yaml`); `claimed_changes` and the implementer's structured output are never routed to the reviewer node. `agent_prompt` inlines only the node's declared inputs (`executors.py:106-122`). The reviewer runs in a fresh worktree cut at the diff head (`_agent` -> `base_commit`, `executors.py:253-254`). Confirmed.
- **Verdict `changes` blocks PR creation.** `_after_review` downgrades `approve`+blocker/major to `changes` (`:362-367`); the only non-escalated edge into `PR` is gated `reviewer_verdict == approve`; a failing `tests_pass` or `reviewer_verdict` leaves no passable edge and the run goes BLOCKED (`machine.choose_edge` + `dag.py`). Confirmed by `test_changes_then_one_fresh_retry_then_approve` and the escalation test.
- **One implementer retry, then escalate.** The retry edge is `max_traversals: 1`; `choose_edge` takes the escalate edge once it is spent (`machine.py:choose_edge`). Confirmed.
- **Budget cap per call.** `--max-budget-usd` (default 1.00) is threaded into every `run_agent`/adapter call; Codex checks it after the run since it cannot abort mid-run (`invoke.py:309-312`), as the decision allows. Confirmed.
- **No credential in the worker environment.** Workers get the scrubbed `ENV_ALLOWLIST` only; `gh`/push run in the orchestrator's own env (`pr.gh_env`), and srt `denyRead` covers `~/.config/gh`, `~/.git-credentials`, `~/.netrc` (`sandbox.py:26`). Holds - except that Blocker 1 lets a worker reach into the orchestrator env anyway, which is the whole point of that finding.
- **Codex login state never exposed.** `has_auth`/`auth_mode` only check exit codes and never read `~/.codex`; `--runtime codex` is refused without srt so the login path's `denyRead` rules apply (`run_agent.py:158-162`). Confirmed.
- **PR #20** sets `CLAUDE_CODE_TOOL_MEMORY_LIMIT=none` only for sandboxed workers, with a unit test asserting it; correct and minimal. No finding.

## How to reproduce

```
uv sync --group dev
npm i -g @anthropic-ai/sandbox-runtime@0.0.81   # + bubblewrap, socat, ripgrep
AGENT_TEAM_REQUIRE_SRT=1 uv run pytest -q        # 326 passed, 1 skipped
# Blocker 1: the PoC in scratchpad/poc/commondir_escape.py (not committed) prints the
# out-of-sandbox marker only after settle(); see the finding for the two sandboxed steps.
```
