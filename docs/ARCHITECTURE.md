# Agentic Engineering OS Architecture

Oct 22, 2026 · @Solomon

Recommendation: build a thin, provider-neutral control layer (canonical YAML specs + adapter compiler + worktree isolation + evidence-gated review) on top of Claude Code as the first runtime, with Codex and Copilot adapters as the proof that the core is not Claude-shaped. Phase 0 is read-only; nothing on the maintainer's machine changes until the decision gate at the end is approved.

## 1. Current state assessment

The maintainer's existing setup was inventoried privately; machine names, home-network details and employer specifics are kept out of this public repo. What carries into the design:

| Existing piece | Verdict | Why |
| --- | --- | --- |
| Claude Code (CLI, subagents, skills, hooks, settings.json) | KEEP | First runtime adapter target; richest observed feature set |
| CLAUDE.md files per owned repo | KEEP | Becomes the Claude adapter's compile target for repository memory |
| User-scope plugins and custom skills | INTEGRATE | Compare each against a canonical skill; keep whichever wins in evals |
| MCP servers (Context7, GitHub, Serena, Playwright) | INTEGRATE | Register as tools in the capability registry, not as the architecture |
| A self-hosted model gateway (LiteLLM) and trace sink (Langfuse) in ARDA | INTEGRATE | Candidate gateway and trace sink for later phases; provider-neutral |
| An existing Redis / BullMQ / PM2 agent stack | REPLACE | Useful pattern, wrong default; orchestration state lives in files + SQLite until concurrency proves a need |
| Local models (Ollama) | INTEGRATE | Local-model adapter for privacy class `local-only`; not installed by default |
| Retired CLIs and named-persona agents | RETIRE | Codex and Gemini return only as adapters; named personas conflict with "agents are capabilities" |
| A second home machine | KEEP | Later remote-host environment target |
| Docker | KEEP | Container environment target; optional, never mandatory |
| ARDA (FastAPI/Redis/Docker multi-agent) | INTEGRATE | Portfolio anchor; becomes the first eval repository, not the orchestrator |
| An employer team repo | KEEP as test bed | Real team repo with rules the maintainer does not own; the honest onboarding eval, used only with its owner's permission |
| Personal notes and tracking systems | OUT OF SCOPE | Personal systems; user memory only, never organization memory |

## 2. Industry architecture findings

The four runtimes have converged on the same shape (Markdown-or-TOML agent files with frontmatter, an instructions file, MCP for tools, a shell sandbox, a headless JSON mode) but with incompatible field names and tool vocabularies. That convergence is what makes a canonical spec plus compiler realistic now; the incompatibility is why the compiler has to exist. Every claim below was read in primary docs this session unless marked otherwise.

| Area | OBSERVED | INFERRED | PROPOSED (our choice) |
| --- | --- | --- | --- |
| Claude Code agents | `.claude/agents/*.md`, frontmatter `tools`, `disallowedTools`, `model`, `effort`, `permissionMode`, `isolation: worktree`, `mcpServers`, `hooks`, `memory`; nested to 3 layers ([sub-agents](https://code.claude.com/docs/en/sub-agents)) | Richest target; the compiler emits here first | Primary adapter |
| Claude Code agent teams | Experimental, env-flag gated, interactive only, does not work under `-p` or SDK, no per-teammate permission mode, "significantly more tokens" ([agent-teams](https://code.claude.com/docs/en/agent-teams)) | Not a base for unattended runs | Not used in MVP; revisit when it leaves experimental |
| Claude Code headless | `claude -p --output-format json`, `--json-schema`, `--max-budget-usd`, `--bare` skips hooks/skills/MCP/CLAUDE.md and will become the default for `-p` ([headless](https://code.claude.com/docs/en/headless)) | Orchestrator drives workers as `-p --bare` processes with explicit `--settings` | Worker invocation primitive |
| Claude Code hooks | 33 events incl. `PreToolUse`, `SubagentStop`, `WorktreeCreate`, `TaskCompleted`; exit 2 blocks; `onFailure: block` for fail-closed ([hooks](https://code.claude.com/docs/en/hooks)) | Policy engine can be enforced as hooks, not just prompts | Compile policies to hooks |
| Claude Code permissions | deny → ask → allow, first match wins; `auto` and `bypassPermissions` ignored from project settings; managed settings highest precedence ([permissions](https://code.claude.com/docs/en/permissions)) | Corporate managed settings will override whatever we ship; design to live under them | Capability registry compiles to allow/deny rules |
| Claude Code sandbox | OS-level for Bash only (Seatbelt / bubblewrap); file tools, MCP and hooks run outside it ([sandboxing](https://code.claude.com/docs/en/sandboxing)); `srt` wraps the whole process, deny-all network ([sandbox-runtime](https://github.com/anthropics/sandbox-runtime)) | Worktree alone is not isolation; A3+ needs `srt` or a container | Environment tiers: worktree → srt → container |
| Claude cloud and scheduling | Cloud sessions on Anthropic VMs, Trusted network allowlist, user settings not read; scheduled tasks 1-hour minimum, research preview ([on the web](https://code.claude.com/docs/en/claude-code-on-the-web)) | Good for personal repos; corporate repos need the GitHub App installed | Optional environment target, never required |
| Codex | TOML config, `.codex/agents/*.toml` with `name`, `description`, `developer_instructions`; agents cannot override sandbox, approval or MCP; `codex exec --json`; AGENTS.md walk root-down, 32 KiB cap ([subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents), [exec](https://learn.chatgpt.com/docs/non-interactive-mode)) | Compiler emits sandbox/approval at config.toml level, not per agent | Second adapter; also the independent-reviewer vendor |
| Copilot | `.github/agents/*.agent.md`, `tools` aliases (`execute`, `read`, `edit`, `search`), `mcp-servers` YAML, 30k-char body, 59-minute cloud cap, one PR per task, reads AGENTS.md and root CLAUDE.md ([custom agents](https://docs.github.com/en/copilot/reference/custom-agents-configuration), [coding agent](https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-coding-agent)) | Most likely to be the only runtime allowed at a locked-down employer | Third adapter; degrade to single-agent-per-task |
| Gemini CLI | `.gemini/agents/*.md`, `max_turns`, `timeout_mins`; sandbox via Seatbelt/Docker/gVisor; `-p --output-format json` ([subagents](https://github.com/google-gemini/gemini-cli/blob/main/docs/core/subagents.md)) | Same shape as Claude; cheap to add | Fourth adapter, after evals justify it |
| AGENTS.md | Linux Foundation stewarded, plain Markdown, no schema; adopted by Codex, Gemini, Copilot, Cursor and others ([agents.md](https://agents.md)); Claude Code accepts it as a CLAUDE.md substitute ([memory](https://code.claude.com/docs/en/memory)) | One repository-memory file can serve every runtime | Repository memory compiles to AGENTS.md |
| Cross-runtime compilers | [rulesync](https://github.com/dyoshikawa/rulesync) (\~65 targets, mature, v14), [agent-harness](https://github.com/madebywild/agent-harness) (13 stars, closest design), gitagent (different category) | rulesync covers rules/skills/agents/MCP but not workflows, policies, environments or evals | Trial rulesync as the compile backend before writing our own emitter |
| Model gateways | LiteLLM (self-hosted, fallbacks, spend tracking, some features enterprise), Bifrost (Apache-2.0), Portkey (MIT), OpenRouter (hosted only) | You already run LiteLLM in ARDA | LiteLLM behind a `ModelRegistry` interface; swappable |
| Observability | Claude Code emits OTel metrics and events natively, traces beta, prompts redacted by default ([monitoring](https://code.claude.com/docs/en/monitoring-usage)); Codex has `[otel]`; GenAI semconv still Development, not Stable | A run record of our own is still required; OTel is a sink, not the source of truth | SQLite run record + OTel export to Langfuse or Phoenix |
| Evaluation | OpenAI reports SWE-bench Verified contaminated (59% of audited tasks broken), recommends SWE-bench Pro; Terminal-Bench and SWE-bench Pro run on Harbor with custom tasks ([Harbor](https://docs.harborframework.com/)); mini-swe-agent is a \~100-line bash-only baseline | Public benchmarks measure models, not teams; we need our own task set on our own repos | Harbor-format private task set; mini-swe-agent as the single-agent baseline |
| Secrets | `op run` injects into the child env (agent can read it); deny rules like `Read(./.env)`; cloud sessions hold GitHub creds behind a proxy | Only proxy-held credentials keep secrets out of the agent | Tool gateway holds credentials; agents call capabilities, never see tokens |
| Governance | Managed settings via MDM or console; ZDR per-org on Enterprise; OWASP Agentic Top 10 published Dec 2025; NIST agent profile unpublished until Q4 2026 | Employers will ask for an allowlist, a data path and an audit log, in that order | `agent-team doctor` prints exactly those three |

UNVERIFIED and left out of the design: Mythos-tier models (no first-party listing found), Codex cloud environment details, Managed Agents multi-agent support, OpenHands and SWE-Lancer harness status.

## 3. Target architecture

&#91;embedded content: target architecture · 5 layers, top down\]

Read top down: the CLI is the only entry point; the control layer owns specs, DAG and the run record; adapters translate to each runtime; every task runs in an explicit environment whose isolation sets the autonomy ceiling; the gateway is the only thing that ever holds a credential. Memory sits beside the control layer in five scopes (user, organization, repository, task, scratch) with the audit history in the run record; organization and repository memory live inside the employer's repo under `.agent-team/` and never leave it.

## 4. Agent team

Three permanent control roles and a bench of specialists spawned per task. No personas, no names; each row is a capability with a hard tool boundary. `A` levels are defined in section 8. Model classes map to the registry in section 7, not to vendors.

| Agent | Triggers | Allowed tools | Max autonomy | Inputs → Outputs | Model class | Definition of done |
| --- | --- | --- | --- | --- | --- | --- |
| orchestrator | every `agent-team run` | fs.read, git.read, task.state, spawn.agent, policy.read | A1 | intent + repo memory → DAG, assignments, run record | high-reasoning | DAG built, every node has an environment and an evidence requirement, no production code written |
| engineering-lead | DAG node PLAN, PLAN REVIEW | fs.read, git.read, github.issue.read | A1 | requirements + repo memory → approved plan, specialist list, missing constraints | high-reasoning | Plan names files, tests and risks; rejects scope the task did not ask for |
| independent-reviewer | DAG node CODE REVIEW, PLAN REVIEW; any A3+ change | fs.read, git.read, shell.read, test.run | A2 | diff + plan + evidence → approve / request changes / escalate | independent (different vendor from implementer) | Every requirement traced to a diff hunk and a test; never approves on green tests alone |
| repository-archaeologist | `inspect`; unfamiliar repo; large diff radius | fs.read, git.read, shell.read | A0 | repo → architecture map, commands, conventions, pitfalls (repository memory) | cheap or strong-coding | Build, test and lint commands verified by running them read-only |
| requirements-analyst | ambiguous intake; issue lacks acceptance criteria | fs.read, github.issue.read | A1 | issue + context → acceptance criteria, open questions | high-reasoning | Each criterion is machine-checkable or marked human-judged |
| system-architect | lead flags architectural implication | fs.read, git.read | A1 | plan → ADR draft, interface changes, migration notes | high-reasoning | ADR has alternatives and a rollback path |
| implementer (backend, frontend, api, data, infra, ml variants) | DAG node IMPLEMENTATION | fs.write.workspace, git.write.branch, git.commit, shell.execute(workspace), test.run | A3 | plan slice → commits in an isolated worktree | strong-coding | Tests for the slice pass locally; diff touches only the planned files or explains why not |
| debugger | failing test or CI; incident | fs.read, shell.execute(workspace), test.run | A2 | failure + logs → root cause, minimal fix proposal | high-reasoning | Reproduction command recorded before any fix is proposed |
| test-engineer | DAG node TEST; coverage gap flagged by reviewer | fs.write.workspace(tests only), test.run | A3 | plan + diff → tests, coverage delta | strong-coding | New tests fail on the pre-change tree and pass after |
| security-engineer | SECURITY REVIEW; diff touches auth, secrets, input parsing, deps, migrations | fs.read, sast.run, deps.audit | A0 | diff → findings with CWE, severity, fix | independent | Every finding has a reproduction or a reason it is theoretical |
| qa-engineer | QA; UI or API surface changed | browser.test, api.contract.test | A2 | running app in sandbox → pass/fail evidence, screenshots | strong-coding | Acceptance criteria exercised end to end, artifacts attached |
| performance-engineer | hot path or query touched; perf budget in policy | shell.execute(workspace), bench.run | A2 | diff → before/after numbers | strong-coding | Numbers from the same machine and inputs, both sides |
| accessibility-reviewer | frontend diff | browser.test, fs.read | A0 | rendered UI → WCAG findings | cheap | Automated checks run plus a keyboard pass |
| documentation-engineer | public interface or command changed | fs.write.workspace(docs only) | A3 | diff → doc updates | cheap | Every changed public symbol or flag appears in docs |
| release-engineer | SHIP; human approved merge | github.pr.create, github.pr.update, ci.read | A4 | approved branch → PR with evidence summary, changelog | cheap | PR body lists evidence, unresolved risks, rollback |
| incident-investigator | post-release failure | logs.read, ci.read, git.read | A0 | signals → timeline, suspected change, revert recommendation | high-reasoning | Timeline cites log lines; revert is proposed, never executed |

Rules that hold across the table: an implementer and its reviewer never share a model vendor when policy allows two providers; no agent holds `deploy.*` without an explicit A5 grant; specialists are created only when the orchestrator's DAG has a node for them.

## 5. Control flow

The orchestrator classifies each task on three axes (diff radius, risk surface, familiarity) and builds the minimal DAG. The full 17-state machine exists as a vocabulary; most runs visit six to nine states. Every edge into IMPLEMENTATION allocates a worktree; every edge into PR requires evidence attached to the run record.

| Workflow | States visited | Parallelism | Gates that cannot be skipped |
| --- | --- | --- | --- |
| Bug fix (small radius, known repo) | INTAKE → DISCOVERY (debugger reproduces) → PLAN → IMPLEMENTATION → TEST → CODE REVIEW → PR → HUMAN DECISION | none | Reproduction recorded before fix; regression test fails before, passes after |
| Feature (cross-stack) | INTAKE → REQUIREMENTS → PLAN → PLAN REVIEW → IMPLEMENTATION (backend ‖ frontend ‖ migration, each own worktree) → integration TEST → CODE REVIEW → QA → PR → HUMAN DECISION | fan-out only when the lead marks slices independent; merge into one integration worktree before tests | Plan review by a different model than the planner; QA evidence for every acceptance criterion |
| Architecture change | INTAKE → DISCOVERY (archaeologist) → REQUIREMENTS → PLAN (architect writes ADR) → PLAN REVIEW → HUMAN DECISION on the ADR → then a feature DAG per slice | sequential until the ADR is approved | Human approves the ADR before any code; rollback path written first |
| Security-sensitive change (auth, secrets, parsing, deps, migrations) | bug-fix or feature DAG plus SECURITY REVIEW before CODE REVIEW, independent model mandatory, autonomy capped at A3 | none | Security findings resolved or explicitly accepted by a human; no `deploy.*` capability granted |
| Unfamiliar repository onboarding (`agent-team inspect`) | INTAKE → DISCOVERY (archaeologist, A0) → repository memory written → HUMAN DECISION on proposed config → RETROSPECTIVE | read-only fan-out across directories is allowed | Nothing written outside `.agent-team/` until the human approves the proposal |

Failure edges: any state may transition to BLOCKED (needs human), RETRY (bounded, never on destructive ops) or ABANDON (worktree archived, run record kept). RESUME re-enters at the last checkpointed state.

## 6. Canonical schema

Decision: YAML frontmatter over a Markdown body for agents and skills, plain YAML for workflows, tools, policies, models and environments, all validated by JSON Schema. Reason: three of four runtimes already read Markdown-plus-frontmatter, so the agent body compiles with zero transformation; YAML carries comments (TOML does too, JSON does not), and JSON Schema gives one validator for every file and `agent-team doctor`. Codex's TOML is emitted, never authored.

```yaml
# canonical/agents/independent-reviewer.md  (frontmatter shown; body is the prompt)
kind: agent
name: independent-reviewer
purpose: Review plans and diffs against requirements; reject unsupported claims.
triggers: [state:PLAN_REVIEW, state:CODE_REVIEW, policy:autonomy>=A3]
inputs: [plan, diff, evidence]
outputs: [review_verdict]
capabilities:
  allow: [fs.read, git.read, shell.read, test.run]
  deny:  [fs.write.*, git.commit, github.pr.*]
skills: [review-checklist, requirement-tracing]
model_class: independent          # resolved by routing/registry.yaml, never a vendor name
max_autonomy: A2
workspace: read_only_mirror
definition_of_done:
  - every requirement maps to a diff hunk and a test
  - verdict is approve | changes | escalate, never implied by green CI
escalate_when: [unresolved_security_finding, requirement_unmapped]
```

| File | Holds | Compiles to |
| --- | --- | --- |
| `canonical/agents/*.md` | role, capabilities, model class, autonomy, DoD | `.claude/agents/*.md`, `.codex/agents/*.toml`, `.github/agents/*.agent.md`, `.gemini/agents/*.md` |
| `canonical/skills/*/SKILL.md` | repeatable procedure (agentskills.io shape) | copied as-is to Claude and Gemini; inlined into Codex `developer_instructions`; appended to Copilot body |
| `canonical/workflows/*.yaml` | state list, DAG rules, gates, DoD per workflow | orchestrator runtime only; no runtime has an equivalent |
| `canonical/tools/*.yaml` | capability name → implementation (CLI, API, MCP server, local service), credential handle | permission allow/deny rules per runtime; MCP config; gateway routes |
| `canonical/policies/*.yaml` | autonomy ceiling per agent, protected paths, redaction rules, required gates | Claude hooks (`PreToolUse` exit 2), Codex `[permissions]`, Copilot repository rules, orchestrator checks |
| `routing/registry.yaml` | model entries (provider, capabilities, cost, privacy class, fallbacks) and class → model resolution | per-agent `model` fields; LiteLLM config |
| `canonical/environments/*.yaml` | isolation tier (worktree, srt; later docker, devcontainer, remote host, CI), network allowlist, port block per worktree | orchestrator allocator; `srt` settings per run |
| `evals/tasks/*.yaml` | Harbor-shaped task: repo, ref, instruction, verifier command | Harbor dataset; mini-swe-agent baseline run |

The five concerns stay separate by construction: an agent names a `model_class`, not a model; a skill is a directory, not an agent; a tool is a capability name, not a permission; the runtime and the environment are chosen by the orchestrator at run time and never appear in an agent file.

## 7. Runtime adapter strategy

An adapter does three things: declare capabilities, compile canonical files into the runtime's native config, and invoke a worker headlessly returning a normalized result. The orchestrator never speaks a runtime's dialect.

| Capability | Claude Code | Codex | Copilot cloud agent | Generic CLI | Local (Ollama via LiteLLM) |
| --- | --- | --- | --- | --- | --- |
| subagents | true | true (TOML) | true (repo-level agents) | false | false |
| parallel\_agents | true (worktree isolation) | true (`max_concurrent_threads_per_session`) | one PR per task | orchestrator-managed | orchestrator-managed |
| mcp | true | true | true (YAML) | false | false |
| hooks | true (33 events) | not used (`[hooks]` exists, trust-gated) | false | false | false |
| sandbox | bash-only native; full via srt | `sandbox_mode` | ephemeral cloud, 59 min | whatever the environment gives | same |
| headless | `-p --output-format stream-json --settings <compiled>`; `--setting-sources ""` with a login, `--bare` with `ANTHROPIC_API_KEY` | `codex exec --json` | issue assignment → PR | stdin/stdout contract | OpenAI-compatible API |
| remote\_execution | cloud sessions | cloud (unverified) | always | no | no |
| per\_agent\_model | true | true | true | n/a | fixed |
| cost\_report | `total_cost_usd` (estimate) | OTel events | premium requests | none | tokens only |

Degradation rules: a workflow asks for capabilities by name; when the runtime lacks one the orchestrator substitutes (no hooks → policy enforced in orchestrator before dispatch; no parallel agents → serialize the DAG; no sandbox → worktree only, autonomy capped at A2). Nothing is faked: the run record states which capabilities were unavailable. The independent-reviewer role is always compiled to a different vendor from the implementer when the organization permits two.

Compile backend: trial [rulesync](https://github.com/dyoshikawa/rulesync) for agents, skills, MCP and rules first; write our own emitter only for the things it does not cover (workflows, policies, environments, evals). Rejection criteria for rulesync are your standing ones: it may not send content to a third party, self-update from an unpinned source, or edit config outside the paths we hand it.

## 8. Security model

Autonomy is a ceiling set by policy, never a request made by an agent. Capabilities are named, composable, and resolved through a gateway that holds the credentials, so no agent process ever has a token in its environment.

| Level | May | Environment required | Default for |
| --- | --- | --- | --- |
| A0 OBSERVE | read repo and environment | cwd, read-only | archaeologist, security-engineer, accessibility, incident-investigator |
| A1 PLAN | A0 + write plans and run records under `.agent-team/` | cwd | orchestrator, lead, analyst, architect |
| A2 SANDBOX | A1 + modify an isolated environment, run tests | worktree + srt, or container | reviewer, debugger, qa, performance |
| A3 DEVELOPMENT | A2 + branch, commit, draft PR | worktree + srt, or container | implementer, test-engineer, documentation |
| A4 INTEGRATION | A3 + approved CI, staging | CI runner or container | release-engineer only |
| A5 PRODUCTION | deploy, migrate prod | disabled; requires written org policy and a human approval per run | none |

Capability registry (compiles to runtime permission rules and gateway routes): `fs.read`, `fs.write.workspace`, `git.read`, `git.write.branch`, `git.commit`, `github.issue.read`, `github.pr.create`, `github.pr.review`, `shell.read`, `shell.execute.workspace`, `test.run`, `browser.test`, `sast.run`, `deps.audit`, `db.schema.read`, `db.migrate.dev`, `ci.read`, `logs.read`, `deploy.staging`, `deploy.production`. A capability absent from an agent's allow list does not exist for that agent.

Protected by default (deny rules compiled into every runtime, plus redaction before any model call or trace): `.env*`, `~/.ssh`, `~/.aws`, `~/.config/gcloud`, keychains, signing keys, `*.pem`, cloud config, anything matched by the org's secret-scanner patterns, customer and employee data paths the org names in policy.

Corporate deployment concerns and how the design answers them:

| Concern | Answer |
| --- | --- |
| "Which models see our code?" | `routing/registry.yaml` has a `privacy_class`; org policy pins allowed classes; `doctor` prints the resulting allowlist |
| "Where does data go?" | Only to runtimes the org has licensed; telemetry is local SQLite by default, OTel export opt-in, prompts redacted |
| "Can it push to main or deploy?" | Not above A3 without a signed policy file; A5 is a stub that refuses |
| "Does it bypass our controls?" | Managed settings and repository rules sit above ours by construction; we never ship `bypassPermissions` or `dangerously-skip-permissions` |
| "Audit trail?" | Every run has an append-only record: agent, model, environment, files touched, commands, approvals, cost |
| "What leaves with the employee?" | Only `canonical/` and user memory; organization memory lives in the org repo under `.agent-team/` and is never synced out |

Threat model headline items for THREAT\_MODEL.md: prompt injection via repo content or issue text (mitigation: capability ceilings, independent reviewer, no A4+ from untrusted intake), secret exfiltration through tool output (gateway-held credentials, redaction), runaway cost (per-run `--max-budget-usd`, orchestrator budget), destructive git operations (no force-push or branch deletion capability exists), supply chain in skills and plugins (pinned sources, no self-update).

## 9. Bootstrap: day one at a new job

Everything below is read-only until step 6, and step 6 writes only inside the repo's `.agent-team/` directory plus the runtime config files you approve one by one.

1. **Install** on the laptop you are given: `git clone <your-repo> ~/agentic-engineering-os && cd ~/agentic-engineering-os && uv tool install .` You should see `agent-team --version`. Done check: `which agent-team` prints a path.
2. **Doctor**: `agent-team doctor`. Prints host (OS, arch, shell, package managers, container runtimes, language runtimes), AI runtimes found (`claude`, `codex`, `copilot`, `gemini`, `ollama` with versions and auth state, never credential values), managed-settings detected, network egress test to each provider, and a verdict per runtime: usable / blocked by policy / not installed. Done check: at least one runtime shows usable, or doctor tells you exactly which permission to request.
3. **Init**: `agent-team init /path/to/company-repo`. Read-only repo discovery: VCS host, default branch, protected branches, CI system and its config, build/test/lint commands (detected, then verified by dry run), existing instruction files (CLAUDE.md, AGENTS.md, copilot-instructions.md, GEMINI.md, .cursorrules), existing MCP config, secrets handling (`.env` patterns, vault references), devcontainer, codeowners. Output: `.agent-team/proposal.yaml` and a printed summary. Nothing else is written. Done check: the proposal lists every detected command with a ✓ or ✗ beside it.
4. **Ask permission** with the proposal in hand. The proposal doubles as the ask: which runtimes, which model privacy class, which autonomy ceiling (recommend A3 for a new hire), whether draft PRs from agents are acceptable. Your manager edits the YAML or says yes.
5. **Configure**: `agent-team configure` turns the approved proposal into `.agent-team/config.yaml` and compiles runtime files. It diffs against any existing CLAUDE.md, AGENTS.md or `.github/agents/` and shows the diff before writing; existing files are extended with a marked block, never replaced. Done check: `git status` shows only files you approved.
6. **Inspect**: `agent-team inspect`. Archaeologist at A0 builds repository memory (architecture map, module ownership, commands, conventions, pitfalls) into `.agent-team/memory/repo/`. Done check: `agent-team inspect --summary` shows the map and every command it verified.
7. **First run**: `agent-team run "<small issue your manager picked>"`. Default workflow is bug-fix at A3 with the independent reviewer on. Ends with a draft PR whose body carries evidence and unresolved risks. Done check: PR opens, CI passes, and `agent-team trace <run>` shows every command and file touched.
8. **Retro**: `agent-team eval --last` records the run against the baseline so the second week starts with numbers.

What the bootstrapper never does: installs software during discovery, overwrites an existing instruction file, enables cloud sessions, sets any autonomy above A3, or copies anything from `.agent-team/` into your personal memory.

## 10. MVP

The smallest version worth building is one command, one workflow, one runtime, two vendors, and a baseline to beat. Everything else waits for evidence.

- `agent-team doctor`, `init`, `configure`, `inspect`, `run`, `trace`: six commands, no `ask`, `plan`, `ship`, `costs`, `eval` subcommands yet (eval runs as a script).
- One workflow: bug-fix (DISCOVERY → PLAN → IMPLEMENTATION → TEST → CODE REVIEW → PR). Feature and the others are YAML added later, not code.
- Four agents compiled: orchestrator (runs in-process, not as a model call where logic suffices), archaeologist, implementer, independent-reviewer. The reviewer runs on Codex when `codex` is installed, otherwise on a different Claude model class.
- One runtime adapter: Claude Code via `claude -p --output-format stream-json --settings <compiled>`, with `--setting-sources ""` under a subscription login and `--bare` only when `ANTHROPIC_API_KEY` is set. Codex adapter only as far as the reviewer needs it.
- Environment: git worktree per task, wrapped in `srt` on macOS and Linux; falls back to worktree-only with autonomy capped at A2 when `srt` is absent.
- Policy engine: deny-list plus autonomy ceiling, compiled to Claude `permissions` and a `PreToolUse` hook.
- Run record: SQLite file in `.agent-team/runs.db`, one row per run, one per tool call. `trace` reads it.
- Model registry: YAML with five classes mapped to Claude IDs plus one Codex entry; routing is a lookup, not a learned policy.
- Eval: ten bug-fix tasks on ARDA and one of your other repos, Harbor-shaped, run against mini-swe-agent as the single-agent baseline.

Out of the MVP on purpose: Copilot and Gemini adapters, LiteLLM, Langfuse, containers, cloud sessions, scheduled tasks, agent teams, parallel implementers, organization-memory sync, the `ship` command.

## 11. Build order

Each step is one PR, reversible, with its own test. Python 3.12 via uv, `typer` for the CLI, `pydantic` for schemas, `sqlite3` from the stdlib. No framework.

1. Repo skeleton, `pyproject.toml`, `agent-team --version`, CI running pytest on macOS and Linux.
2. Canonical schemas as pydantic models + JSON Schema export; `agent-team validate` over `canonical/`.
3. `doctor`: host and runtime detection, auth state (present / absent, never values), egress test, managed-settings detection. Snapshot test against fixture outputs.
4. Run record (SQLite) and `trace`. Written before any agent runs so nothing is ever unrecorded.
5. Environment allocator: worktree create / dispose / stale sweep, `.worktreeinclude` handling, `srt` wrapper with network allowlist, port offset per worktree. Tests use a throwaway repo.
6. Claude adapter: compile one canonical agent to `.agent-team/compiled/<run-id>/` (passed by flag, never written to the target's `.claude/`), compile policy to permissions + hook, invoke `-p` (`--setting-sources ""` with a login, `--bare` with an API key), parse stream-json, normalize result and cost into the run record.
7. Model registry YAML and class resolution; `fallbackModel` chain emitted into compiled settings.
8. `init` read-only discovery and `proposal.yaml`; `configure` with diff-before-write and marked blocks in existing instruction files.
9. `inspect`: archaeologist agent at A0 writing repository memory; verification of detected commands by dry run.
10. Bug-fix workflow YAML and the orchestrator's DAG builder (minimal: linear chain with gates).
11. Implementer and independent-reviewer agents; evidence attachment; draft PR via `gh` behind the `github.pr.create` capability.
12. Codex adapter for the reviewer only: compile to `.codex/agents/*.toml`, invoke `codex exec --json`.
13. Eval harness: ten Harbor-shaped tasks, mini-swe-agent baseline, comparison script printing the section 13 metrics.
14. Docs: README, ARCHITECTURE, BOOTSTRAP, SECURITY, THREAT\_MODEL first; the remaining spec docs generated from the JSON Schemas.
15. Dogfood on an employer team repo with its owner's permission, at A3, bug-fix only. This is the real day-one rehearsal.

Gate after step 13: proceed to Copilot adapter, feature workflow and parallel implementers only if the eval shows the team beating the baseline on at least two of completion rate, regressions and human interventions without more than 2x cost.

## 12. Rejected complexity

| Not building yet | Why | Revisit when |
| --- | --- | --- |
| Claude Code agent teams as the orchestration layer | Experimental, interactive-only, no headless or SDK support, no per-teammate permission mode | It leaves experimental and works under `-p` |
| Redis / BullMQ / PM2 queue | Runs are sequential per repo in MVP; SQLite + files resume fine | Two concurrent runs on one repo are a real need |
| Kubernetes, Docker as mandatory | Locked-down laptops often have neither; worktree + srt covers A3 | An org's policy requires container isolation |
| LiteLLM in the path | One vendor plus Codex needs no proxy; adds a daemon and a failure mode | Three or more providers, or local models, in daily use |
| Langfuse / Phoenix | SQLite run record answers `trace` and `costs`; OTel export is a flag later | You want dashboards across many repos |
| Generic workflow engine (Temporal, Prefect, LangGraph) | One linear workflow does not justify one | Fan-out feature workflow with retries proves painful by hand |
| Learned or LLM-based model routing | A five-row lookup table is auditable; a router is not | Cost per successful task shows a clear win from routing |
| Organization memory sync | Must never leave the employer anyway; a repo-local directory is the whole design | Never, by policy |
| Agent personas and names | Conflicts with "capabilities, not characters" | Never |
| Gemini and Copilot adapters | Zero evidence yet that a third runtime improves outcomes | An employer allows only one of them |
| Browser QA, performance, accessibility agents | Not needed for bug-fix MVP | Feature workflow lands |
| `ship`, `ask`, `plan` CLI commands | `run` with a workflow flag covers them; aliases are drift | Users ask for them twice |
| Cloud sessions and scheduled tasks as defaults | Require GitHub App install and Anthropic VMs; many employers will refuse | Personal repos only, as an optional environment |

## 13. Evaluation plan

The claim to prove: the orchestrated team produces more merged-quality PRs per dollar and per human intervention than one excellent single agent on the same tasks. Public benchmarks cannot answer this (they measure models, and SWE-bench Verified is reported contaminated), so the task set is private and lives in your repos.

| Element | Choice |
| --- | --- |
| Task set | 30 tasks across ARDA, one personal repo, and (with permission) the employer test-bed repo: 10 locate-bug, 10 fix-bug, 5 small feature, 5 repair-failing-test; each a Harbor-shaped YAML with repo, ref, instruction, hidden verifier |
| Baseline A | mini-swe-agent on the strongest Claude coding model, one shot, same budget cap |
| Baseline B | a single Claude Code `-p` session with the same CLAUDE.md and skills, no team |
| Treatment | `agent-team run` with the MVP bug-fix workflow (archaeologist, implementer, independent reviewer on Codex) |
| Runs | 3 per task per arm, fixed seeds where the runtime allows, same machine |
| Metrics | completion rate (hidden verifier passes), test pass rate, regressions (existing tests broken), review defects (findings a human reviewer adds after the agent reviewer), retries, wall time, tokens, dollars, human interventions, unnecessary diff lines (lines changed outside the planned files), context consumed |
| Headline number | cost per successful task = dollars ÷ verifier passes; reported beside interventions per successful task |
| Decision rule | Treatment must beat the best baseline on at least two of completion, regressions, interventions, at no more than 2x cost per successful task; otherwise the extra agent is cut |

Every agent added after MVP gets the same test: run the suite with and without it. An agent that does not move a metric is removed, whatever it felt like in a demo.

## 14. Decision gate

Recommendation: build the thin control layer described above in Python, with Claude Code as runtime one and Codex as the independent reviewer, git worktree plus `srt` as the default environment, SQLite as the only state, and a private 30-task eval as the gate for every addition. Do not build on agent teams, queues, containers or gateways until the eval asks for them.

Everything else in this document is discoverable or defaulted. These five are yours:

| Decision | Options | Default if you say nothing |
| --- | --- | --- |
| Repo home and name | `agentic-engineering-os` under your GitHub, public from day one; or private until the eval passes | Public; it is portfolio material and the SCaLE talk's successor |
| Reviewer vendor | Codex (requires an OpenAI key or ChatGPT plan you currently do not pay for); or Claude on a different model class with a "no shared context" rule | Claude-different-class for MVP, Codex once there is a key; the adapter is built either way |
| Employer repo as dogfood | Ask the repo's owner now for A3 draft-PR permission on a small issue; or dogfood only on ARDA until the eval passes | Ask after step 13 passes |
| Where orchestration runs | The maintainer's laptop only; or also a second machine as a remote-host environment | Laptop only; the second machine is a later `environments/remote-host.yaml` |
| Time box | Build order steps 1 to 13 before January (about 4 weeks of focused evenings); or steps 1 to 6 only this semester and the rest after graduation | Steps 1 to 6 this semester, 7 to 13 over winter break, dogfood in January |

On your approval, implementation starts at build-order step 1 in a new repo, each step as its own reviewed PR, nothing touching your existing Claude config until step 8's `configure` runs on a repo you point it at.

### Locked sub-decisions (interview, 2026-10-22)

Four of the five decisions are now locked; the time box is the only one still open.

| Decision | Locked value |
| --- | --- |
| Reviewer > vendor | Claude, different model class; no Codex key in MVP |
| Reviewer > models | Sonnet 5.5 implements, Opus 5.5 reviews; registry classes `strong-coding` and `independent` |
| Reviewer > isolation | Fresh process; receives diff, plan with acceptance criteria, orchestrator-run test output, lint and type-check output; never the implementer transcript or its summary |
| Reviewer > verdict power | Blocks PR creation on `changes`; one implementer retry; then escalate |
| Reviewer > retry input | Fresh implementer process with reviewer findings plus original plan; no memory of attempt one |
| Reviewer > escalation | Draft PR opened and marked blocked, findings in the PR body |
| Reviewer > output format | JSON through `--json-schema`: `verdict` plus `findings[]` with file, line, requirement id, severity, fix |
| Reviewer > severities | blocker, major, minor; blocker and major block; minor becomes PR follow-ups |
| Reviewer > scope | Requirement-to-hunk tracing; every criterion maps to code and a test |
| Reviewer > budget | `--max-budget-usd 1.00` per review call; abort is recorded with reason |
| Reviewer > checklist | `canonical/skills/review-checklist/SKILL.md`, named in the agent's `skills` field and inlined into its compiled prompt |
| Repo > visibility | Public from the first commit |
| Repo > names | Repo `agentic-engineering-os`, CLI `agent-team` |
| Repo > license | Apache-2.0 |
| Repo > disk | `~/Projects/agentic-engineering-os`, own CLAUDE.md |
| Repo > branching | `main` protected; one PR per build-order step; squash merge |
| Repo > CI | GitHub Actions, hosted ubuntu + macos, Python 3.12; no self-hosted runner on a public repo |
| Repo > install | `uv tool install git+https://github.com/SolomonSmith-dev/agentic-engineering-os@<tag>` |
| Repo > versioning | 0.x semver; tag at each gate: 0.1 after step 6, 0.2 after step 13, 1.0 after dogfood |
| Dogfood > timing | ARDA first, then one personal repo, both before the employer test-bed repo; the employer repo only after the stand-in dogfood meets its pass bar (2026-10-29) |
| Dogfood > ask | Permission settled with the repo's owner. Own defaults still hold: a branch you own, nothing committed outside it, `.agent-team/` gitignored |
| Dogfood > fallback | ARDA plus one personal repo, recorded as the onboarding eval |
| Dogfood > stand-in pass bar | Up to three of ARDA's open issues, each pinned by a failing test on main first; pass means at least two of three draft PRs a human would merge without edits, zero writes outside the worktree, no reviewer transcript leak, under $3 per run in `trace` (2026-10-29) |
| Dogfood > stand-in stack | ARDA's compose stack runs on the host, started by the human; the agent never starts, stops or rebuilds containers; srt allows the stack's localhost ports and keeps the docker socket denied; pre-flight is `doctor` in the repo plus one request to each stack port (2026-10-29) |
| Eval > first pass | Step 13 eval runs on the Air in iTerm under `caffeinate`, N=1, hard ceiling $30; N=3 only if pass one costs under $10 (2026-10-29) |
| Runs > host | The maintainer's laptop only, foreground; a second machine later as a remote-host environment for test runs over SSH, never the orchestrator |
| Runs > state | `.agent-team/` inside the target repo, gitignored: run record SQLite, worktrees, lock file |
| Runs > auth | Claude subscription login through the `claude` CLI |
| Runs > concurrency | One run per repo; lock file holds the PID; a dead PID is taken over and logged |
| Runs > interrupt | Checkpoint after every state; `agent-team resume <run>` re-enters there; worktree kept; no auto-restart |
| Runs > status | One terminal line per state transition with elapsed time and cost so far; full log in the run record |

Assumptions added:

- `--bare` needs `ANTHROPIC_API_KEY` (it reads neither the subscription login nor `CLAUDE_CODE_OAUTH_TOKEN`), so local runs use `claude -p --setting-sources "" --settings <compiled> --strict-mcp-config` without `--bare`; CI uses an API key with `--bare`. `--setting-sources ""` is what keeps a target repository's own `.claude/settings.json` hooks out of the worker (verified 2026-10-24 on Claude Code 2.1.310); our `--settings` hook still loads. Revisit when `--bare` becomes the `-p` default.
- Compiled agents land in `.agent-team/compiled/<run-id>/` and reach Claude Code through `--agents`, `--agent` and `--settings`; only step 8 `configure` writes a target repository's `.claude/`. Skills are inlined into the agent prompt instead of preloaded through the `skills` field.
- Model IDs in the registry are the pinned snapshots `claude-sonnet-5-5` and `claude-opus-5-5`; a `fallbackModel` chain of one step (Opus to Sonnet for review) is emitted.
- The registry has four classes for now (`high-reasoning`, `independent`, `strong-coding`, `cheap`, from the section 4 table); the Codex entry landed with step 12 (below). `fallbackModel` goes into the compiled `settings.json` as a list, which Claude Code reads (verified 2026-10-24 on 2.1.310); a fallback must run on the same runtime as its primary. `--model` overrides a class only with another registry entry, so every model a run uses has a known privacy class.
- Lint and type-check commands come from `init` discovery; if none are detected, the gate is skipped and the run record says so.
- `init` "verifies by dry run" without executing anything: ✓ means the command's tool resolves (an absolute PATH hit, or the repository's `.venv/` or `node_modules/`) and the Makefile target or package script exists. `make -n`, `--help` and `--collect-only` all run repository code, so none is used. Step 9's `inspect` adds real execution inside the sandbox (below); init's ✓ and `config.yaml`'s `commands` keep their narrow meaning. Init makes no network calls, so protected branches come only from files in the repository (`.github/settings.yml`) and are otherwise `unknown`.
- `proposal.yaml` carries the ask under `ask:` (runtimes, privacy classes, autonomy ceiling capped at A3, draft PRs) and an `approved` flag; `configure` writes only after `approved: true`, and `--dry-run` shows the diff before approval. The proposal and config schemas are exported from `agent_team.schemas` like `DoctorReport`, not added to the canonical `SCHEMAS`, because they live in a target repository, not in `canonical/`.
- `configure` compiles every canonical agent to `.claude/agents/<name>.md` at the lowest of the agent's own maximum, its policy ceiling and the proposal's ceiling, with the proposal's verified test commands as its `test.run` rules. A file there without agent-team's marker is left untouched. `.claude/settings.json` is never written; runs keep reaching Claude Code through compiled flags. The marked block goes into `CLAUDE.md` (created if missing) and into `AGENTS.md` only if it exists; a `CLAUDE.md` that imports or links `AGENTS.md` is left alone so the block is not loaded twice. `.github/agents/` is reported, never written: there is no Copilot adapter in the MVP.
- The reviewer's requirement ids are the acceptance-criteria ids the requirements-analyst or the plan assigns; a bug-fix plan with no explicit criteria gets one synthetic criterion: the reproduction command passes.
- Step 10 bug-fix workflow: INTAKE → DISCOVERY → PLAN → IMPLEMENTATION → TEST → CODE_REVIEW → PR → HUMAN_DECISION, the section 5 row. Workflow states must come from the section 5 vocabulary (the run record stores nothing else), so the earlier draft's IMPLEMENT, PR_DRAFT, DONE and ESCALATED are gone; BLOCKED and RESUME are the orchestrator's, not the workflow's. The MVP has no debugger or engineering-lead, so the implementer reproduces in DISCOVERY (in its own process) and PLAN is an in-process step that writes the synthetic criterion.
- Workflow nodes: each state names one worker (`agent:` or in-process `step:`), the `evidence` keys it must produce, and the `inputs` it may see; a node gets no other evidence, which is how the reviewer's isolation is enforced. A gate is `<evidence>` (passes only on literal `true`) or `<evidence> == <value>`, with no other expression language. The retry is an edge with `max_traversals: 1`; when it runs out, the edge marked `escalate: true` from the same state is taken. The DAG builder accepts only a linear chain plus those back and escalate edges, and checks that every policy `required_gates` entry gates some edge.
- Escalation and the end of a run: an escalated run still reaches PR (the draft PR is marked blocked, per the reviewer escalation decision) and ends `blocked` at HUMAN_DECISION; an unescalated run ends `succeeded` there. A failed gate, missing evidence or a node blocker ends the run `blocked` in BLOCKED; an executor error ends it `failed`.
- Checkpoints: run-record schema version 2 adds a nullable `transitions.checkpoint` (redacted JSON: task, evidence, attempt and retry counts, escalated flag), written in the same transaction as the transition into the state it re-enters; older databases migrate in place and their runs cannot be resumed. `agent-team resume` records `<state> → RESUME → <state>` and re-runs the interrupted node as the same attempt. Ctrl-C leaves the run open and releases the lock; any other error ends it. Redaction truncates strings at 8 KiB, so a large diff in a checkpoint is cut short; step 11 should keep artifacts such as the diff as files and checkpoint their paths.
- Agent nodes run through `run_agent` unchanged, each as a child run (`agent:<name>`) with its own worktree; the child's cost is added to the workflow run, so the workflow total includes its children. `run_agent` takes the repository lock itself, so the orchestrator releases its lock for the call and takes it back after; another run that takes the lock in that window makes this one stop BLOCKED at that node. The workflow run allocates no worktree of its own until step 11's implementer needs one. Until step 11 adds the implementer and the `test` and `pr` steps, `run --workflow bug-fix` refuses before recording anything and names what is missing.
- Step 11 (implementer, evidence, draft PR): the workflow run owns one worktree (`agent-team/<run-id>`), allocated when the first agent whose `workspace` is `worktree` runs; DISCOVERY, IMPLEMENTATION and the retry all run the implementer there through `run_agent(environment=...)`, so the retry builds on attempt one's commits while staying a fresh process with only the plan and the findings. The reviewer (`read_only_mirror`) gets a fresh child worktree cut at the diff's head (`run_agent(base_commit=...)`). After each implementer call the orchestrator settles the worktree: still on the run branch, base still an ancestor, anything uncommitted committed by the orchestrator. That one commit is what the diff ends at, what TEST runs on (it refuses a moved or dirty worktree), what the reviewer reads and what is pushed.
- The implementer's structured output (`implementer_report`: reproduction command, a claimed `reproduction_fails`, claimed changes per file) is a claim. The orchestrator re-runs the reproduction in srt after DISCOVERY and gates on what it saw (`reproduction_check`); IMPLEMENTATION's evidence is the orchestrator-cut diff plus the claims, which are checked against the files that attempt committed and never shown to the reviewer. A reproduction command the implementer reports in IMPLEMENTATION is ignored in favour of the plan's. The reviewer's `approve` with a blocker or major finding is recorded as `changes`.
- The orchestrator runs no command outside srt: the reproduction command is agent-supplied and every check runs agent-written code. Without srt the run is capped at A2 (no Bash, no agent commits: `shell.execute.workspace` is granted only inside srt), DISCOVERY's gate falls back to the agent's claim marked unverified, and TEST records nothing run and blocks. TEST runs reproduction, test, lint and type-check commands (flags first, then `config.yaml`'s verified ones) with the worker's scrubbed environment; lint and type-check failures fail `tests_pass`; a kind with no command is skipped and named. Commands that dirty the worktree are undone (`reset --hard` to the tested commit, `clean -fd`).
- `fs.write.workspace` compiles to `Edit(./**)` and `Write(./**)` (not bare `Edit`/`Write`), and the PreToolUse hook refuses a file write whose target resolves, symlinks followed, outside the worktree (`write_root` in `policy.json`) or into any `.git` path (the worktree's `.git` file names the gitdir the orchestrator's own git reads). `configure` compiles agents with no Bash grant, since its files load in a Claude Code srt does not wrap. Every compiled session denies `gh`; srt denyRead and the protected paths gain `~/.config/gh`, `~/.git-credentials` and `~/.netrc`.
- Artifacts are files in `.agent-team/artifacts/<run-id>/` (`reproduction.log`, `diff-<n>.patch`, `test-<n>.log`, `review-<n>.json`, `pr-body.md`); evidence holds `{path, bytes, sha256}` plus the commit it describes, and an agent's prompt inlines the file (up to 100,000 characters) because `.agent-team/**` is unreadable to it. Logs are redacted line by line; the diff is stored as git produced it.
- The `pr` step is the orchestrator exercising `github.pr.create`: it needs the canonical tool (`gh pr create --draft ...`), the capability not denied by policy, a run ceiling of A3 (the writers' policy ceiling, the proposal's ceiling and srt), `draft_prs` not false in `config.yaml`, and the policy's `required_gates` passing on their advance edges; an escalated run opens its draft anyway, titled `[blocked]`, with the failing gates and blocking findings in the body. It pushes `<commit>:refs/heads/agent-team/<run-id>` to `origin` with `--no-verify`, never `--force`, and runs `gh` without `--base` (the repository default) and with `GH_PROMPT_DISABLED=1`, in the orchestrator's own environment. Git run on agent-written trees disables hooks, external diff drivers and textconv. Any refusal or failure (gh missing or unauthenticated, no `origin`, push or `gh` failing) sets the node's new `escalate` flag: the run still reaches HUMAN_DECISION, ends `blocked`, keeps the branch, and records the reason. The terminal node's note is not recorded (the machine finishes the run without a transition), so the reason lives on the PR → HUMAN_DECISION transition and in the `pr` evidence.
- Not verified against a real Claude Code (no model call was made): that `Edit(./**)` resolves against the session's working directory, and that a compiled implementer can run `git commit` inside srt with a real worker; the fake worker in the tests commits inside the real srt. Not handled: a PR already open for the branch (a resume after `gh pr create` succeeded but before the checkpoint fails `gh` and ends blocked); the implementer editing the reproduction script itself, which only the reviewer catches.
- Step 12 (Codex adapter): `agent-team run --runtime codex` runs only `independent-reviewer`; the locked default stays Claude. The registry gains `gpt-6.1-sol` (runtime `codex`, listed in openai/codex `models-manager/models.json` as the latest coding workhorse; Codex slugs carry no date, so the slug is the pin) and a `runtime_classes` table that maps `independent` to it only for codex runs. No cost is listed: no first-party price was verified.
- The compiled Codex agent file (`name`, `description`, `model`, `developer_instructions`) goes to `.agent-team/compiled/<run-id>/agents/`; it is not loaded by the run, because `codex exec` does not run as an agent role. The session gets the same instructions and its settings through `-c key=value` overrides (`sandbox_mode`, `approval_policy = "never"`, `web_search = "disabled"`, `shell_environment_policy.inherit = "core"` with the KEY/SECRET/TOKEN excludes on), plus `--ignore-user-config --ignore-rules --ephemeral --cd <worktree> --output-schema <file> -` with the prompt on stdin.
- Codex in this version does have `[hooks]` with `PreToolUse`, gated by persisted hook trust; this adapter does not use them, so the section 7 table reads "not used". Protected paths are srt `denyRead` rules instead (globs per root, and the entries around the run's own worktree, temp and compiled directories for `.agent-team/**`), so the codex runtime refuses to run without srt rather than degrade to unenforced paths.
- Codex's `--output-schema` is sent with `strict: true`, so the verdict schema is compiled to strict form: every property required (optional ones nullable), `additionalProperties: false`, no `default`.
- Codex reports token usage, not dollars, and has no budget flag. Cost is a list-price estimate from the registry's `cost` when one exists; `--max-budget-usd` is then checked after the run ends and maps to `budget_exceeded`. Without a price the timeout is the only cap. The run record's transition note lists every unavailable capability (hooks, per-command rules, cost report, budget cap, fallback chain).
- Codex auth is `CODEX_API_KEY` only (issue #12, option 1); `codex login` is not used. The key is passed to the `codex` process alone, and `shell_environment_policy` (core variables, KEY/SECRET/TOKEN excludes on) keeps it from the commands Codex runs. srt denies `~/.codex/**` for the whole run, and also the caller's `CODEX_HOME` when one is set (as given and as resolved; only its `auth.json` when it is the home directory or holds the run). Codex itself runs with `CODEX_HOME` pointed at an empty `codex-home/` in the run's temp directory, so it never needs the denied directory. On Linux, srt also hides `/proc/<pid>/environ` of the `codex` process from its children (checked with srt 0.0.81: permission denied), so the key cannot be read back from there. Moving the key out of the sandbox altogether is option 4, the gateway of section 8.
- Unverified, because developers.openai.com was unreachable and no Codex binary or key was available (facts above were read from the openai/codex source at commit 970111f, 2026-10-23): whether Codex's own Linux sandbox nests inside srt's bubblewrap; whether `codex exec` starts with an empty `CODEX_HOME` when `CODEX_API_KEY` is set, and writes no copy of the key into it (the reviewer's commands can read the run's temp directory); whether `shell_environment_policy` keeps the KEY/SECRET/TOKEN excludes on for `command_execution`; whether srt on macOS hides the `codex` process's environment from its children; whether strict mode accepts `minLength` and `minimum` in the verdict schema; whether `output_tokens` already includes reasoning tokens; the minimum Codex version with every flag used (the adapter checks `codex exec --help` instead); and the model's price. The adapter has not been run against a real Codex.
- Step 9 (`inspect`): "verified" now has two meanings, kept apart by name. init's ✓ (`dry_run` in memory, `commands` in `config.yaml`) still means only that the tool and target exist. A command is `passed` in repository memory when agent-team ran it inside the run's srt sandbox, in a worktree at `HEAD`, and it exited 0 within the command timeout; `failed`, `timeout` and `unverified` (not run, with the reason) are the other results. The section 4 "verified by running them read-only" is read as "run in the sandbox": a command may write its worktree and the run's temp directory, nothing else.
- The archaeologist never runs a repository command; it has `fs.read`, `git.read` and `shell.read` at A0 and no `test.run`. agent-team runs the commands through a `run_agent` `after` callback, once the agent has succeeded and before the run is finished, so they share the agent's run, lock, worktree and srt settings, and are recorded as `inspect.verify` tool calls in its DISCOVERY state. They run after the agent, so build output never changes what it read, and their results go to memory, not to the agent.
- Only commands `init` detected are run, from `config.yaml` when `configure` wrote one, otherwise from `proposal.yaml`; init's ✗ commands run too, since the sandbox makes that safe and the result is informative. Commands only the archaeologist proposed are recorded `unverified` and never run, so repository text that steers the model cannot choose what executes. `inspect` refuses without either file rather than run discovery itself.
- No hook stands between a repository command and the files it opens, so before the first command the run's srt settings gain `denyRead` rules for the policy's protected paths, compiled as for the Codex adapter (keeping the run's own worktree and temp directory). Commands get the worker's environment allowlist plus `CI=1` and `XDG_CACHE_HOME` in the run's temp directory, no stdin, and a per-command timeout (600 s by default) that kills the process group. Output is read from a pipe as it arrives and only the last 32,000 bytes (8,000 characters at up to 4 bytes each) are held, so a command that prints without stopping uses no disk and bounded memory, and nothing is read back from a path a command could swap out; only a 1,000-character tail is kept, cut from that redacted 8,000-character window so a token at the cut is never kept in part. Once the command exits, output is still read for up to 2 s; if a child it left running still holds the pipe after that, the process group is killed, so a stray background process cannot hold the run until the timeout. A child that leaves the process group (`setsid`) is not killed, as before.
- Without srt, `inspect` still runs the archaeologist (A0 needs no sandbox) but runs no repository command: each is `unverified` with the reason, recorded as a `denied` tool call.
- Repository memory is YAML, one file per concern (`architecture`, `ownership`, `commands`, `conventions`, `pitfalls`) plus `index.yaml`, in `.agent-team/memory/repo/`; a re-run replaces the files. The archaeologist's output is redacted through the policy before it is written. Memory is not compiled into `CLAUDE.md` or `AGENTS.md` yet, and `config.yaml` is not updated with the results. The archaeologist's model class is `strong-coding`, not `cheap`: every later run reads this memory and `inspect` runs about once per repository, so a shallow map costs more than the model does.
- The run's worktree is left for `agent-team env sweep`, as for every `run_agent` run, with whatever the commands built in it.
- On Linux, Claude Code (2.1.310) may run each Bash call in a memory cgroup created under its own, behind a server-side flag; inside srt `/sys` is read-only, so every Bash call failed with `EROFS ... clone3 /sys/fs/cgroup/...` and the worker had no shell (found in the step 11 dogfood, #18). A sandboxed worker therefore gets `CLAUDE_CODE_TOOL_MEMORY_LIMIT=none`, which turns the per-tool cgroup off; srt is the boundary there. Unsandboxed workers keep Claude Code's default. The opt-in live test now passes on Linux with real srt and fails without the variable.
- Step 13 (eval), deviation from section 13's task set: the 30 tasks in the maintainer's private repositories are out of reach of the environment that built the harness, so it ships 10 self-contained tasks under `evals/tasks/` (3 locate-bug, 3 fix-bug, 2 small-feature, 2 repair-failing-test), each a tiny Python project in `evals/fixtures/<task>/repo` with stdlib `unittest` tests. A fixture is committed into a fresh repository with a fixed identity, date and sha1 object format, so it materializes to the same commit everywhere; that commit is the task's `ref`, and a fixture that changed is refused. A task whose `repo` is a git URL or path is cloned and checked out at its `ref` instead; that is the slot the private set fills. Every copy loses its remotes, and a cloned one also loses every ref and the objects only they reached, because a later commit can hold the fix.
- The task schema (`EvalTask`) keeps section 6's repo, ref, instruction and verifier command, and adds `kind`, `test_command` (the visible suite the agents may run and the treatment's TEST step runs), `existing_tests` (one command per test, for regressions), `planned_files` (globs, for unnecessary diff) and `verifier.files` (the hidden files). Like `DoctorReport` it is exported from `agent_team.schemas` and is not a canonical kind, so `agent-team validate` does not read `evals/` and a broken task never stops `agent-team run`; the eval validates its tasks when it loads them. `locate-bug` tasks give the symptom only and `fix-bug` tasks name the location; both are verified by a hidden test, because the treatment is the bug-fix workflow and fixes what it finds.
- The verifier is hidden by construction: its files are copied over a fresh `git archive` export of the arm's final tree only after the arm has exited (extracted with tarfile's `data` filter, so a committed symlink cannot aim the copy outside), and it runs there inside srt with no network and writes only to that directory. For a repair-failing-test task the verifier files include the pristine failing test, so editing the test does not pass. While an arm runs, its sandbox denies reading the task files, `evals/fixtures/`, the verifier files and `.agent-team/eval/`: `EnvironmentSpec` gains `deny_read` (absolute or `~/` paths added to srt's `denyRead`), and the eval runs on its own copy of `canonical/` with an `eval` environment (the default plus those paths), kept beside its cells. A cell's repository lives in a fresh temporary directory while the arm runs and moves under `.agent-team/eval/work/<eval-id>/` after.
- Arms. Both baselines run on the model behind the `high-reasoning` class (`claude-opus-5-5`): the registry has no strength ranking, and section 13 asks for "one excellent single agent". The treatment keeps the locked classes (Sonnet implements, Opus reviews). `--baseline-model` overrides the baselines with another registry entry. Baseline A is mini-swe-agent 2.4.6 (with litellm 1.104.2, the newest release more than two weeks old when it was locked) in a `baseline` dependency group, never a default dependency. Its CLI, config, cost and auth were read from the 2.4.6 sdist and the litellm wheel: `mini --model anthropic/<id> --task ... --yolo --exit-immediately --cost-limit <cap> --output <trajectory> --agent-class default` (the default agent never prompts), cost from the trajectory's `info.model_stats.instance_cost`, priced from litellm's bundled cost map (`LITELLM_LOCAL_MODEL_COST_MAP=True`), and auth only through `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`) in litellm, never a Claude Code login; without the key the arm is refused and the eval will not start with it selected. `MSWEA_CONFIGURED`, `MSWEA_GLOBAL_CONFIG_DIR` (the cell's temp directory) and `LITELLM_MODE=PRODUCTION` keep it off the user's mini-swe-agent config and any `.env` file litellm would load. It runs inside srt on the checkout, with the checkout's `.git` read-only. Baseline B is the implementer agent run once through `run_agent` in state IMPLEMENTATION: the same adapter, hook, srt and skills as the team, with no plan, reviewer or orchestrator check. The treatment is `run_workflow` with no `gh` and no remote, so it ends `blocked` at HUMAN_DECISION by design; completion is the verifier's, never the run's outcome.
- Budgets. The per-task cap covers a whole cell: mini-swe-agent's `--cost-limit`, Claude Code's `--max-budget-usd`, and for the treatment a `CappedExecutor` that gives each agent call only what is left of the cell's cap and refuses the next call (the node blocks, the run ends) once less than $0.05 is left. A runtime checks its own cap between turns, so a cell can overshoot by a turn; the cell records the reported cost and `over_cap`. The total cap is a reservation: a cell starts only when the spend so far plus its full cap fits, so every arm gets the same cap; the rest are recorded `not_run` with the reason. Cells run task by task with every arm back to back, so a total-cap stop leaves complete comparisons. A cell whose cost is unknown is charged its whole cap. The probe counts toward the total.
- `agent-team eval run` is a dry run unless `--execute` is given, and `--execute` is refused (and prints the dry run) for a plan whose hash (task files, arms and models, runs, caps, timeout, probe) was never dry-run in this store. The dry run's worst case is cells x per-task cap plus the probe; an expected figure appears only once the store holds recorded costs for every arm. The default matrix (10 x 3 x 1) is $30.10 at worst, so the $15 total cap holds and at most 14 cells start in that case.
- Section 9 item 8's live check: before the first paid cell the eval runs, in-process, what `tests/test_live.py` asserts: the reviewer on the `cheap` class, capped at $0.10, must run Bash inside srt. A failed probe is recorded and stops the eval before any cell. `--skip-probe` skips it.
- Results are JSONL (`.agent-team/eval/results.jsonl`), not a second SQLite file: each record is written once, a cell is one self-contained line, and the run record in each cell's repository stays the source for what agents did. Tokens: the Claude adapter now keeps the result event's integer `usage` counts in `AdapterResult.usage`, and `DefaultExecutor` gains an `observe` callback through which the treatment's per-call usage is summed; mini-swe-agent's come from its trajectory's per-response `usage` (unverified key names; counted only when present). Human interventions are the run record's RESUME transitions, 0 in an automated eval. Review defects and context consumed print "not measured" with the reason. The decision rule is applied on the task-runs every compared arm ran; with interventions 0 for every arm, the treatment must win both completion and regressions.
- `agent-team eval --last` (section 9 item 8) records the target repository's last finished workflow run in that repository's `.agent-team/eval/results.jsonl` (once per run): outcome, wall time, cost, retries, RESUME count and escalation; completion is "not measured", because a real task has no hidden verifier. The latest eval in the same store is printed beside it.
- The eval's git calls on an arm's tree pass `GIT_DIR`, `GIT_COMMON_DIR` and `GIT_WORK_TREE` from its own paths: the run's srt settings let an agent rewrite `.git/worktrees/<id>/commondir`, and a planted config with `core.fsmonitor` or a clean filter then ran on a plain `git status` or `git add` outside the sandbox (reproduced). Diffs and exports read the main checkout's objects only. The orchestrator's own `settle()` (step 11) has the same exposure and is left for its own fix. The version stays 0.1.0; the 0.2.0 tag is the maintainer's after this step merges.
- Not verified, because no model was called: mini-swe-agent and litellm under srt (read-only home, the allowlist as the only network), the Anthropic API accepting `claude-opus-5-5` through litellm, litellm's price for it ($4 and $20 per million input and output tokens in its bundled map, not a first-party price), the trajectory's token keys, and the Claude result event's `usage` key names. Known gap: mini-swe-agent's commands see `ANTHROPIC_API_KEY` in their environment and could call the API outside its cost accounting.
- Git that agent-team runs outside the sandbox on an agent-written worktree (settle, the clean check, restore, the dispose archive) first checks the worktree's pointers: its `.git` file must name its admin dir under the root's common `.git`, the admin dir's `commondir` must name that common `.git`, and no `config.worktree` may exist. Git follows `commondir` even with `GIT_DIR` and `GIT_COMMON_DIR` set (its ref store reads the file), so a repointed worktree would have made that git read a planted config (`core.fsmonitor`, filters) and run it on the host (#27). A changed pointer stops the run and `env sweep` keeps the worktree for a human. The same git also runs with those variables pinned and `core.fsmonitor=false`, and srt denies writes to `commondir`, `gitdir` and the worktree's `.git`.

Time box, as it happened: steps 1 to 14 landed between 2026-10-22 and 2026-10-29 (35 PRs). Next gates: the step 13 eval and its decision this week (by 2026-11-02), stand-in dogfood on ARDA the week of 2026-11-03, the employer repo only after that pass bar. Debt carried into the eval: issue 19 (step 11 follow-ups) lands as one PR after the eval; `trace --follow` lands before dogfood; the focus hook's thresholds (25 / 15 / 3) are guesses the eval judges.

## Sources

All opened 2026-10-22.

- [Claude Code: subagents](https://code.claude.com/docs/en/sub-agents), [agent teams](https://code.claude.com/docs/en/agent-teams), [headless](https://code.claude.com/docs/en/headless), [hooks](https://code.claude.com/docs/en/hooks), [permissions](https://code.claude.com/docs/en/permissions), [settings](https://code.claude.com/docs/en/settings), [sandboxing](https://code.claude.com/docs/en/sandboxing), [sandbox environments](https://code.claude.com/docs/en/sandbox-environments), [worktrees](https://code.claude.com/docs/en/worktrees), [memory](https://code.claude.com/docs/en/memory), [MCP](https://code.claude.com/docs/en/mcp), [monitoring](https://code.claude.com/docs/en/monitoring-usage), [managed settings](https://code.claude.com/docs/en/managed-settings), [data usage](https://code.claude.com/docs/en/data-usage), [on the web](https://code.claude.com/docs/en/claude-code-on-the-web), [GitHub Actions](https://code.claude.com/docs/en/github-actions), [Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview), [Managed Agents](https://platform.claude.com/docs/en/managed-agents/overview), [models](https://platform.claude.com/docs/en/models/overview)
- [anthropics/sandbox-runtime](https://github.com/anthropics/sandbox-runtime)
- Codex: [config basics](https://learn.chatgpt.com/docs/config-file/config-basic), [config reference](https://learn.chatgpt.com/docs/config-file/config-reference), [AGENTS.md](https://learn.chatgpt.com/codex/agent-configuration/agents-md), [subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents), [non-interactive](https://learn.chatgpt.com/docs/non-interactive-mode), [SDK](https://learn.chatgpt.com/docs/codex-sdk), [advanced config / OTel](https://learn.chatgpt.com/docs/config-file/config-advanced)
- Copilot: [coding agent](https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-coding-agent), [custom agents](https://docs.github.com/en/copilot/reference/custom-agents-configuration), [repository instructions](https://docs.github.com/en/copilot/how-tos/configure-custom-instructions/add-repository-instructions), [Claude and Codex on GitHub](https://github.blog/changelog/2026-02-04-claude-and-codex-are-now-available-in-public-preview-on-github/)
- Gemini CLI: [GEMINI.md](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/gemini-md.md), [subagents](https://github.com/google-gemini/gemini-cli/blob/main/docs/core/subagents.md), [sandbox](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/sandbox.md)
- [agents.md](https://agents.md), [rulesync](https://github.com/dyoshikawa/rulesync), [agent-harness](https://github.com/madebywild/agent-harness)
- Sandboxes: [Docker Sandboxes](https://docs.docker.com/ai/sandboxes.md), [Vercel Sandbox](https://vercel.com/docs/vercel-sandbox), [E2B](https://docs.e2b.dev/), [Daytona](https://www.daytona.io/docs/en/), [Modal](https://modal.com/docs/guide/sandbox), [apple/container](https://github.com/apple/container)
- Gateways: [LiteLLM](https://github.com/BerriAI/litellm), [Bifrost](https://github.com/maximhq/bifrost), [Portkey](https://github.com/Portkey-AI/gateway), [OpenRouter routing](https://openrouter.ai/docs/features/model-routing)
- Observability: [Langfuse self-hosting](https://langfuse.com/self-hosting), [Arize Phoenix](https://github.com/Arize-ai/phoenix), [OTel GenAI semconv repo](https://github.com/open-telemetry/semantic-conventions-genai)
- Evals: [SWE-bench contamination report (secondary)](https://decrypt.co/359012/openai-benchmark-measure-ai-coding-supremacy-contaminated), [swebench.com](https://www.swebench.com), [Harbor](https://docs.harborframework.com/), [Terminal-Bench](https://github.com/harbor-framework/terminal-bench), [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)
- Secrets and governance: [1Password op run](https://www.1password.dev/cli/secrets-environment-variables), [OWASP Top 10 for Agentic Applications](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026), [CSA note on NIST agent standards](https://labs.cloudsecurityalliance.org/research/csa-research-note-nist-ai-agent-standards-initiative-2026040/)
