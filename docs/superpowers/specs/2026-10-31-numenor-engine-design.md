# agent-team as Numenor's engine: design

Date: 2026-10-31. Status: approved by Solomon 2026-10-31. Decision record: D-001 in the portfolio decision log; upstream decision: Numenor ADR 0003 ("AEOS is the engine, not a product").

## 1. Intent

agent-team (this repo, `agentic-engineering-os`) stops aiming to be a standalone product and becomes the run engine that Numenor calls. Numenor owns the roles, the Ledger, the Coordinator (Hermes) and the economy. agent-team owns one thing: take one budgeted task, run it safely in a sandboxed worktree with an independent reviewer, end at a draft PR, and report exactly what it cost.

What success looks like: `numenor tick` hands agent-team a Ledger task file, agent-team runs the bug-fix workflow on the target repo, and the task file comes back with tokens, list-price dollars, outcome, PR number and a trace path. A five-PR replay eval says how often that works and what it costs.

## 2. Goals, non-goals, anti-goals

Goals (each names what it adds):

| Goal | Adds |
|---|---|
| G1 Task-file input and result write-back for `run --workflow` | Leverage: the only interface Numenor needs |
| G2 Budget halt on tokens, wall clock and list-price dollars, exit 3 | Risk reduction: overspend cannot happen silently |
| G3 Path locks replace the repo lock; concurrent runs per repo default 2 (D57's Builder cap), configurable up to 3 | Leverage: parallel Builders on one Target |
| G4 Scope check: a run that wrote outside its declared paths fails the TEST gate | Risk reduction |
| G5 Port agent-crew's task schema and validator, and its `crew-audit` rules | Risk reduction, reuse |
| G6 Port agent-factory's agent-definition lint (`agent-team validate --lint`) | Maintenance |
| G7 Codex reviewer on the ChatGPT login, Claude Opus fallback | Cost: no new bill; resilience |
| G8 One `trace.jsonl` per run | Observability; Numenor deliverable |
| G9 Replay eval: five closed PRs from arda and cutclone, replayed from the pre-PR commit | Evidence; Numenor deliverable |
| G10 Paid eval run with numbers in the README | Evidence; portfolio |
| G11 Dogfood: ARDA #70 through a Ledger task file to a draft PR | Step 15; Numenor slice one |
| G12 Archive agent-crew and agent-factory on GitHub after G3 to G6 pass tests (Solomon approves each) | Maintenance: one repo instead of three |

Non-goals: PyPI publishing, a Claude Code plugin wrapper, the twelve unbuilt specialist agents of ARCHITECTURE section 4, a feature workflow, Copilot or Gemini adapters, any Hermes code or import inside agent-team, any scheduler (the tick is Numenor's launchd job), merging PRs.

Anti-goals (a failure even if it works): agent-team merges or pushes to a default branch; a budget is exceeded with exit 0; a task file's fields other than `result` are rewritten; a CourtRules or Saurabh mention reaches this public repo; agent-crew or agent-factory git history is merged in.

## 3. Constraints

- Python 3.12, uv, typer, pydantic, stdlib sqlite3. No framework, no daemon. macOS and Linux; Windows through WSL.
- Autonomy A3: draft PR, never merge, never push the default or a protected branch (already enforced by `orchestrator/pr.py`; this design adds nothing that weakens it).
- Existing security model holds: srt sandbox, `dontAsk` permissions, fail-closed PreToolUse hook, protected paths, scrubbed env, repo-local `.agent-team/` gitignored.
- Existing CLI exit codes: 0 success, 1 failed or blocked, 2 usage or refusal, 130 interrupted. New: 3 budget halt.
- D57 per-task defaults: 2M tokens, 45 minutes, $5 list price. These are Numenor's values, carried in the task file; agent-team has no opinion beyond honoring them.

## 4. Actors and boundaries

| Actor | Lives in | Talks to agent-team by |
|---|---|---|
| Owner (Solomon) | Telegram, GitHub | nothing directly; reads PRs and traces |
| Coordinator (Hermes) | Hermes gateway | nothing; writes Ledger task files |
| `numenor tick` (launchd) | Numenor repo | `agent-team run --workflow bug-fix --task-file <path>`; reads exit code and the `result` block |
| Builder | Claude Code worker inside agent-team (`implementer` agent, model class from the task or the registry) | internal |
| Reviewer | Codex worker inside agent-team (`independent-reviewer`), Claude Opus fallback | internal |
| Housekeeper | Numenor script | `agent-team env sweep`, `agent-team trace --json` |

The boundary is the task file plus the exit code. agent-team never reads the Ledger beyond the one file it was given, never writes outside that file's `result` block and the target repo's `.agent-team/`, and never calls Numenor or Hermes.

## 5. Workflow

```
numenor tick
  -> agent-team run --workflow bug-fix --task-file ledger/tasks/T-0001.yaml
       validate TaskFile (schema + crew-audit rules)  -- refuse: exit 2, result.outcome=refused
       acquire path locks for task.paths (default 2, ceiling 3 runs per repo) -- busy: exit 2, result.outcome=busy
       start run, open trace.jsonl, start BudgetMeter
       INTAKE -> DISCOVERY -> PLAN -> IMPLEMENTATION -> TEST(+scope check) -> CODE_REVIEW -> PR -> HUMAN_DECISION
         every model call: usage -> BudgetMeter; over any limit -> kill process group,
           record halted_budget, write result, release locks, exit 3
       write result block (atomic), release locks
  <- exit 0 (draft PR open) | 1 (failed/blocked/escalated) | 2 (refused/busy) | 3 (budget) | 130
```

The existing `--prompt`, `--repo` and budget flags keep working; `--task-file` supplies them instead and is mutually exclusive with them.

## 6. Components

New or changed modules (paths under `src/agent_team/`):

| Module | Responsibility |
|---|---|
| `schemas/task_file.py` | `TaskFile` pydantic model (section 7); JSON Schema export via the existing `schema export` |
| `tasks/io.py` | load (PyYAML) and validate a task file; atomic write-back of `result` only by text splice: the top-level `result:` block is replaced or appended and every other byte is kept (comments, key order, quoting); write temp, fsync, rename; if the bytes outside `result` changed on disk since load, write `<id>.result.yaml` instead |
| `tasks/audit.py` | ported `crew-audit` rules in three places: on the task file text (prompt-injection markers, bidi and zero-width Unicode, absolute or `..` paths); on the target at `base` before the worktree is cut (declared paths that are symlinks or inside a submodule); and in `orchestrator/scope.py` on the run diff (dependency-source changes: lockfiles, manifests' source or index URLs, `.gitmodules`; allowed only when the task sets `allow_dependency_changes: true`) |
| `environments/locks.py` | path locks replacing `lock.py`: a JSON lock table under flock, one entry per live run with its paths; overlap = equal or nested prefix (agent-crew's `paths_overlap`); stale entries (dead PID, same host) taken over and logged; max concurrent runs per repo from config, default 2, ceiling 3 |
| `orchestrator/scope.py` | scope check after IMPLEMENTATION: files changed on the run branch outside `task.paths`, or under `.claude/` or `.agent-team/`, fail the TEST gate with the list |
| `budget.py` | `BudgetMeter`: tokens (input + output; cache reads and writes are excluded from the token limit because Claude Code spends millions on cache reads per run, but they are priced in dollars), wall clock from run start, list-price dollars from `registry.yaml` prices; checked after every model event; enforces the per-call cap too by passing the remaining budget as each call's `--max-budget-usd` |
| `runs/trace.py` | append-only `trace.jsonl` writer, one line per state transition, tool call, model usage event and budget check; same redaction as the run record |
| `adapters/codex/auth.py` | Codex becomes the default runtime for the `independent` class when usable (the registry's `independent` default moves to `gpt-6.1-sol`, `claude-opus-5-5` becomes its fallback, reversing the MVP lock on Solomon's 2026-10-31 answer). The first real Codex run is a recorded live check inside 0.2.0, before the eval. Reviewer-only ChatGPT login: srt read allow for exactly `~/.codex/auth.json`, network allowlist limited to OpenAI hosts; `CODEX_API_KEY` still preferred when set; fallback to Claude Opus when Codex is unusable (not on PATH, not logged in, refused, or errors before producing a verdict) and the fallback is recorded |
| `lint.py` | agent-definition lint from agent-factory's pattern: frontmatter fields present and strict YAML, least-privilege tools, the required body sections; runs inside `agent-team validate` |
| `workflows/bug-fix.yaml` + DISCOVERY gate | a failing acceptance test counts as the reproduction: for a feature-shaped request the implementer first writes a test that fails until the request is met, and the orchestrator checks it fails in the sandbox before PLAN (Solomon, 2026-10-31; this is how ARDA #70 runs) |
| `eval/replay.py` | replay task kind: clone target at the PR's base commit (`merge^1`, not gh's `baseRefOid`, which differs for stacked squash merges), instruction from the PR's linked issue or title and body, hidden verifier = the PR's own test changes applied after the arm exits |

## 7. Data model

Task file (YAML, one per task, owned by Numenor's Ledger; schema owned here):

```yaml
kind: task
id: T-0001                 # matches the filename stem
target:
  repo: /Users/solomonsmith/Projects/arda   # local path; remote URL optional
  base: main               # branch or commit to cut from
request: |                 # what the Owner asked, as the Coordinator wrote it
  Telegram delivery for the daily brief ... (ARDA #70)
issue: 70                  # optional GitHub issue number
paths: [arda/delivery/, tests/delivery/]   # write scope; locks and scope check use it
allow_dependency_changes: false           # lockfile, manifest source and .gitmodules edits need true
budget:
  tokens: 2000000
  minutes: 45
  usd: 5.00                # list price
workflow: bug-fix
models: {}                 # optional class overrides, e.g. {implementer: high-reasoning}
status: claimed            # Numenor's field; agent-team reads, never writes
result:                    # written by agent-team only
  run_id: 8f3c...
  outcome: pr_opened       # pr_opened | failed | blocked | escalated | halted_budget | refused | busy
  halted: null             # tokens | minutes | usd when outcome is halted_budget
  tokens: 412345
  usd_list: 1.87
  minutes: 22.4
  pr: 112
  trace: /Users/.../arda/.agent-team/runs/8f3c.../trace.jsonl
  finished_at: 2026-11-02T18:04:11Z
```

Run record (SQLite) gains `tokens_total` and `halted` columns via a forward migration. Registry gains `price: {input_per_mtok_usd, output_per_mtok_usd, cache_read_per_mtok_usd, cache_write_per_mtok_usd}` per model, sourced from the vendors' public API price pages with the date in a comment. Codex runs on the ChatGPT login are priced at OpenAI's API list price for the same model, so `usd_list` means what the run would cost at list price whatever the plan paid. A model with no verified price refuses a task-file run rather than counting $0.

## 7a. Replay eval set (approved 2026-10-31)

Each verified fail-before, pass-after in a scratch clone; none needs keys, Redis or network.

| # | Repo | PR | Base (`merge^1`) | Merge | Verifier | Before → after |
|---|---|---|---|---|---|---|
| 1 | arda | 73 | 47967171b1a7 | 04f09d04bed5 | tests/tombombadil/test_letterboxd_loader.py | 2 fail → 23 pass |
| 2 | arda | 72 | f6f3294fb3b2 | f0a286293037 | tests/tombombadil/test_d1_speaker_prefix.py | 6 fail → 12 pass |
| 3 | arda | 60 | fa6c97e65f30 | d1781dc048b0 | tests/tombombadil/test_film_knowledge.py | 5 fail → 7 pass |
| 4 | cutclone (private) | 26 | d75e44f74d71 | 093414bc8249 | tests/unit/auth/rate-limit.test.ts | 1 fail → 6 pass |
| 5 | cutclone (private) | 14 | 57086f96df38 | 6af79ece4d8f | tests/unit/contracts/hardening.test.ts | 15 fail → 20 pass |

Alternates: arda 75, 76, 58. Setup: arda `uv sync --extra dev`, `uv run pytest -q <file>`; cutclone Node 24+, `pnpm install --frozen-lockfile` at each base, `pnpm exec vitest run <file>`. No "repair a failing test" PR qualified.

Private data rule (Solomon, 2026-10-31): cutclone is private. Its task files live in gitignored `evals/private/` and read the local clone; the public README reports cutclone results only as totals labelled "private repo", never PR text or code. arda tasks are public in `evals/tasks/`.

## 8. Integrations and APIs

- Claude Code: `claude -p --output-format stream-json`, unchanged; usage from the result event and per-message usage.
- Codex: `codex exec --json`, unchanged invocation; auth per section 6.
- GitHub: `gh pr create --draft` behind `github.pr.create`, unchanged.
- Numenor: the task file and the exit code; JSON Schema at `docs/spec/task.schema.json` for Numenor to validate against.

## 9. Auth and secrets

No new secret is stored. Claude runs use the existing login or `ANTHROPIC_API_KEY`. Codex prefers `CODEX_API_KEY`, else the ChatGPT login file, read only by the reviewer's process. Threat model addition: a hijacked reviewer can read the ChatGPT token but its network allowlist reaches only OpenAI hosts, so the token can only be sent where it is already valid. The eval's baseline A uses `ANTHROPIC_API_KEY` from the caller's shell, as today.

## 10. Deployment

`uv tool install git+https://github.com/SolomonSmith-dev/agentic-engineering-os@<tag>`. Numenor pins a tag. Tags: `0.2.0` after the ports and task-file I/O, `1.0.0` after the ARDA #70 dogfood. Runs on the M4 now; host-agnostic (no path assumptions beyond `$HOME`).

## 11. Observability

SQLite run record (existing), `trace.jsonl` per run (new), `agent-team trace <run> --json` for the Housekeeper and Numenor's views, the `result` block for the Ledger. Cost appears in three places that must agree; a test asserts the trace sum equals the run record equals the result block.

## 12. Failure modes

| Failure | Behavior |
|---|---|
| Budget hit mid-call | kill the worker's process group, outcome `halted_budget`, worktree kept, exit 3 |
| Lock busy (overlap or the concurrency limit reached) | refuse before the run record is touched, outcome `busy`, exit 2; the tick retries next cycle |
| Stale lock entry | taken over, logged in the run record (existing behavior, now per entry) |
| Task file changed on disk during the run | result written to `<id>.result.yaml` beside it instead, exit unchanged, warning in the trace |
| Codex unusable | Claude Opus reviewer, `reviewer_fallback` noted in the run record and PR body |
| Codex stalls | per-call timeout, then fallback once |
| Crash | resume from the last checkpoint with `agent-team resume <run>` (existing); the lock entry goes stale and is taken over |
| Scope violation | TEST gate fails with the file list; one implementer retry with the list as a finding (existing retry edge) |
| Reviewer requests changes twice | escalate, draft PR marked blocked (existing) |

## 13. Scaling and cost

Scale is one machine, up to two runs per repo by default, three at most, a handful of repos. No queue. Per-task cost is capped by the task file. Eval spend: first pass capped at $20 (5 replays x 3 arms x 1 run, plus the 10 toy tasks only if the cap allows), full pass capped at $60 (3 runs). Ongoing cost of the engine itself is zero; there is no scheduled paid CI.

## 14. Security and privacy

Unchanged guarantees, plus: task-file validation rejects injection markers and hostile Unicode before any text reaches a model; path declarations cannot escape the repo; the scope check makes write scope enforceable after the fact; the reviewer's Codex token exposure is bounded as in section 9. Nothing leaves the machine except model-vendor calls and the draft PR.

## 15. Reuse, build versus buy

Reused: everything already in agent-team (sandbox, adapters, run record, DAG, eval runner, PR step). Ported (copied into fresh commits, no git history): agent-crew `lib.sh` lock and overlap logic, `task-validate.sh` rules, `agent-scope-check.sh`, the `crew-audit` branch rules and their bats fixtures as pytest cases; agent-factory `PATTERNS/agent-definition-pattern.md` as lint rules. Bought: nothing. Rejected: Temporal, LangGraph, queues, LiteLLM (unchanged from ARCHITECTURE section 12).

## 16. Simplest versus sophisticated

Simplest that meets every requirement: file in, file out, flock-guarded JSON lock table, a meter checked after each model event. The sophisticated version (a long-lived engine daemon with a socket API, streaming progress to Numenor) is rejected until Numenor's views need live progress; `trace.jsonl` tailing covers that.

## 17. MVP to scale

1. 0.2.0: G1 to G8 merged, toy eval green.
2. Replay eval set approved, paid eval run, numbers in README (G9, G10).
3. Fresh-repo end-to-end: `uv tool install`, `init`, `run --workflow bug-fix --task-file`, `eval run` on a pinned clone of a small public Python repo.
4. 1.0.0: ARDA #70 dogfood draft PR (G11).
5. Archives after Solomon's yes (G12).
Later, only on evidence: live progress API, more workflows, more runtimes.

## 18. Decided, and left to the builder

Decided by Solomon (grill, 2026-10-31): engine scope per D57; Codex reviewer on the ChatGPT login with Claude Opus fallback; AEOS owns the TaskFile schema and writes back only `result`; budget halt as above; concurrent runs per repo up to 3 (engine default 2 to match D57's Builder cap, per HQ); ports as listed, copied without history; one trace per run; five replay PRs from arda and cutclone; eval caps $20 then $60 with his Anthropic and OpenAI keys available; dogfood is ARDA #70; tags 0.2.0 and 1.0.0; open PR #42 and issue #19 folded in.

My calls (Solomon can override): token limit counts input + output only, cache priced in dollars; Codex priced at OpenAI API list price, unpriced model refuses; dependency changes need `allow_dependency_changes`; write-back by text splice with PyYAML for reading; exit code 3 for a budget halt; `busy` and `refused` as exit 2; task file wins over CLI flags by being mutually exclusive; `<id>.result.yaml` fallback when the task file changed during a run; registry prices dated in comments; the toy tasks stay as a smoke set.

Left to the builder: module internals, migration mechanics, test fixture design, exact trace event names.
