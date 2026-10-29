"""The default node executor: in-process steps, and agent nodes through `run_agent` (build-order steps 10 and 11).

An agent node is one `run_agent` call: it records its own child run (workflow
`agent:<name>`), and its cost is added to the workflow run. `run_agent` takes
the repository lock itself, so the workflow hands the lock over for the call
and takes it back afterwards. Where the agent works follows its `workspace`:

    worktree           the workflow run's own worktree (workspace.py): every implementer attempt commits there
    read_only_mirror   a fresh child worktree cut at the diff's head: the reviewer reads exactly what it reviews

The agent's output is a claim; the evidence comes from the orchestrator:

    DISCOVERY        reproduction_command (the agent's), reproduction_fails (re-run in the sandbox)
    IMPLEMENTATION   diff (cut from the branch after settling it), claimed_changes (the agent's, checked against the diff)
    TEST             the reproduction, test, lint and type-check commands, run in the sandbox at the diff's head
    CODE_REVIEW      the verdict, normalized (a blocker or major finding never approves), saved as a file
    PR               push + gh pr create --draft, behind github.pr.create (pr.py)

Large evidence is a file under `.agent-team/artifacts/<run-id>/` with its
reference checkpointed; an agent's prompt inlines the file, since its sandbox
and hook keep `.agent-team/` unreadable.
"""

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agent_team.adapters.claude.capabilities import matches
from agent_team.adapters.claude.invoke import ClaudeVersionError
from agent_team.bootstrap.configure import CONFIG_NAME
from agent_team.environments import AllocatorError, Environment, LockHeldError, acquire, release
from agent_team.orchestrator import pr as pr_mod
from agent_team.orchestrator.dag import Dag, DagNode, OrchestratorError, build_dag
from agent_team.orchestrator.machine import Echo, NodeInput, NodeResult
from agent_team.orchestrator.workspace import (
    CommandResult,
    WorkspaceError,
    changed_files,
    diff_evidence,
    head_of,
    is_artifact,
    is_clean,
    read_artifact,
    redact_lines,
    render_results,
    restore,
    run_command,
    settle,
    short,
    workflow_environment,
    write_artifact,
)
from agent_team.run_agent import Canonical, RunAgentError, requested_autonomy, run_agent
from agent_team.runs.store import STATE_DIR, RunStore, repo_root
from agent_team.schemas.adapter import AdapterResult
from agent_team.schemas.bootstrap import RepoConfig
from agent_team.schemas.common import AUTONOMY_ORDER, Autonomy

# The capability the pr step exercises, and the autonomy it needs (architecture section 8).
PR_CAPABILITY = "github.pr.create"
PR_AUTONOMY: Autonomy = "A3"

# An agent's prompt carries at most this much of one artifact; the rest stays in the file.
MAX_INLINE_CHARS = 100_000

COMMAND_KINDS = ("test", "lint", "typecheck")


def intake(given: NodeInput) -> NodeResult:
    return NodeResult(evidence={"task": given.task})


def plan(given: NodeInput) -> NodeResult:
    """The bug-fix plan: the task plus one synthetic criterion, the reproduction passes."""
    command = given.inputs.get("reproduction_command")
    criteria = [{"id": "AC-1", "text": f"the reproduction command passes: {command}"}] if command else []
    return NodeResult(evidence={"plan": {"task": given.task, "acceptance_criteria": criteria}})


def human_decision(given: NodeInput) -> NodeResult:
    """The last state: the draft PR (or the kept branch) waits for a human. The run ends here."""
    pr = given.inputs.get("pr")
    if isinstance(pr, Mapping) and pr.get("status") == "opened":
        what = f"draft PR {pr.get('url')}"
        return NodeResult(note=f"escalated: {what} is marked blocked" if pr.get("blocked") else f"awaiting human review of {what}")
    if isinstance(pr, Mapping):
        return NodeResult(note=f"no PR: {pr.get('reason')}; branch {pr.get('branch')} kept at {short(pr.get('head'))}")
    return NodeResult(note="escalated: the draft PR is marked blocked" if given.escalated else "awaiting human review")


STEPS: dict[str, Callable[[NodeInput], NodeResult]] = {
    "intake": intake,
    "plan": plan,
    "human_decision": human_decision,
}

# In-process steps that need the executor's worktree, sandbox and tools.
EXECUTOR_STEPS: tuple[str, ...] = ("test", "pr")


def agent_prompt(given: NodeInput, root: Path | None = None) -> str:
    """The prompt for an agent node: the task and only the evidence its node lists as inputs.

    An artifact reference is replaced by the file's content (cut at MAX_INLINE_CHARS) under its metadata.
    """
    parts = [f"Workflow {given.workflow}, state {given.state}, attempt {given.attempt}.", "", "## Task", "", given.task]
    for key, value in given.inputs.items():
        if root is not None and is_artifact(value):
            meta = {k: v for k, v in value.items() if k not in ("path", "sha256")}
            text, cut = read_artifact(root, value, MAX_INLINE_CHARS)
            parts += ["", f"## {key}", "", json.dumps(meta, indent=2, sort_keys=True), "", text.rstrip()]
            if cut:
                parts.append(f"[cut at {MAX_INLINE_CHARS} characters of {value.get('bytes')} bytes; read the rest with git]")
            continue
        text = value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True)
        parts += ["", f"## {key}", "", text]
    return "\n".join(parts) + "\n"


def agent_evidence(structured: dict[str, Any] | None, result: str | None) -> dict[str, Any]:
    """Map an agent's output to evidence keys. The reviewer's verdict becomes `reviewer_verdict` and `findings`."""
    if structured is None:
        return {"result": result} if result else {}
    if "verdict" in structured:
        return {"reviewer_verdict": structured["verdict"], "findings": structured.get("findings", [])}
    return dict(structured)


def _join(*parts: str | None) -> str | None:
    kept = [p for p in parts if p]
    return " | ".join(kept) or None


def _dedupe(*groups: tuple[str, ...] | list[str]) -> list[str]:
    return list(dict.fromkeys(c for group in groups for c in group))


@dataclass
class DefaultExecutor:
    repo: Path
    canonical_dir: Path
    canonical: Canonical
    claude: str
    max_budget_usd: float
    timeout_s: float
    registry_path: Path | None = None
    test_commands: tuple[str, ...] = ()
    lint_commands: tuple[str, ...] = ()
    typecheck_commands: tuple[str, ...] = ()
    gh: str | None = None  # resolved by the caller; None means gh is not on PATH
    policy_name: str = "default"
    environment_name: str = "default"
    echo: Echo = print
    # Called with each agent node's normalized result (the eval reads token counts from it, step 13).
    observe: Callable[[str, AdapterResult], None] | None = None
    _config: RepoConfig | None = field(default=None, init=False, repr=False)
    _config_read: bool = field(default=False, init=False, repr=False)

    @property
    def root(self) -> Path:
        return repo_root(self.repo)

    @property
    def redaction(self) -> tuple[str, ...]:
        return tuple(r.pattern for r in self.canonical.one("policy", self.policy_name).spec.redaction)

    def repo_config(self) -> RepoConfig | None:
        """`.agent-team/config.yaml` from `configure`, when it exists: commands, ceiling, draft PRs."""
        if not self._config_read:
            path = self.root / STATE_DIR / CONFIG_NAME
            if path.is_file():
                try:
                    self._config = RepoConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
                except (yaml.YAMLError, ValidationError, OSError, UnicodeDecodeError) as exc:
                    raise OrchestratorError(f"{path} is unreadable ({type(exc).__name__}); re-run 'agent-team configure'") from exc
            self._config_read = True
        return self._config

    def commands(self) -> dict[str, list[str]]:
        """Test, lint and type-check commands: the flags first, then config.yaml's verified ones."""
        config = self.repo_config()
        known = config.commands if config is not None else {}
        flags = {"test": self.test_commands, "lint": self.lint_commands, "typecheck": self.typecheck_commands}
        return {kind: _dedupe(flags[kind], known.get(kind, [])) for kind in COMMAND_KINDS}  # type: ignore[call-overload]

    def unavailable(self, dag: Dag) -> list[str]:
        """Why this executor cannot run every node of `dag`; empty when it can."""
        agents = {d.spec.name for d in self.canonical.docs if d.kind == "agent" and d.spec is not None}
        problems = [f"agent {a!r} is not in {self.canonical_dir}" for a in dag.agents if a not in agents]
        problems += [
            f"in-process step {s!r} is not implemented yet" for s in dag.steps if s not in STEPS and s not in EXECUTOR_STEPS
        ]
        try:
            self.repo_config()
        except OrchestratorError as exc:
            problems.append(str(exc))
        return problems

    def __call__(self, node: DagNode, given: NodeInput) -> NodeResult:
        if node.step == "test":
            return self._test(given)
        if node.step == "pr":
            return self._pr(given)
        if node.step is not None:
            step = STEPS.get(node.step)
            if step is None:
                return NodeResult(blocked=f"in-process step {node.step!r} is not implemented yet")
            return step(given)
        return self._agent(node, given)

    # --- the workflow worktree ------------------------------------------------------

    def _writer_ceiling(self, workflow: str) -> Autonomy:
        """The autonomy of the workflow's agents that commit in its worktree, capped by policy and config."""
        policy = self.canonical.one("policy", self.policy_name).spec
        names = {n.agent for n in self.canonical.one("workflow", workflow).spec.nodes.values() if n.agent}
        ceilings: list[Autonomy] = [
            requested_autonomy(d.spec.max_autonomy, policy.autonomy_ceiling.get(d.spec.name, policy.default_autonomy))
            for d in self.canonical.docs
            if d.kind == "agent" and d.spec is not None and d.spec.name in names and d.spec.workspace == "worktree"
        ]
        config = self.repo_config()
        if config is not None:
            ceilings.append(config.autonomy_ceiling)
        return min(ceilings, key=lambda a: AUTONOMY_ORDER[a], default="A0")

    def _workflow_env(self, run_id: str, workflow: str) -> Environment:
        spec = self.canonical.one("environment", self.environment_name).spec
        env, created = workflow_environment(self.root, run_id, spec, requested_autonomy=self._writer_ceiling(workflow))
        if created:
            store = RunStore.open(self.root, create=False)
            if store is not None:
                with store:
                    store.set_worktree_path(run_id, str(env.path))
        return env

    # --- agent nodes ------------------------------------------------------------------

    def _agent(self, node: DagNode, given: NodeInput) -> NodeResult:
        name = node.agent or ""
        root = self.root
        environment: Environment | None = None
        base_commit: str | None = None
        before: str | None = None
        try:
            agent = self.canonical.one("agent", name).spec
            if agent.workspace == "worktree":
                environment = self._workflow_env(given.run_id, given.workflow)
                before = head_of(environment)
            elif isinstance(given.inputs.get("diff"), Mapping):
                base_commit = given.inputs["diff"].get("head")
            prompt = agent_prompt(given, root)
        except (RunAgentError, AllocatorError, WorkspaceError, OSError) as exc:
            return NodeResult(blocked=f"agent {name}: {exc}")

        state_dir = root / STATE_DIR
        release(state_dir, given.run_id)
        try:
            child, result = run_agent(
                self.repo,
                self.canonical_dir,
                name,
                prompt,
                registry_path=self.registry_path,
                claude=self.claude,
                max_budget_usd=self.max_budget_usd,
                timeout_s=self.timeout_s,
                test_commands=tuple(self.commands()["test"]),
                policy_name=self.policy_name,
                environment_name=self.environment_name,
                environment=environment,
                base_commit=base_commit,
                state=given.state,  # type: ignore[arg-type]
                echo=lambda line: self.echo(f"    {line}"),
            )
        except (RunAgentError, ClaudeVersionError, AllocatorError, LockHeldError) as exc:
            return NodeResult(blocked=f"agent {name}: {exc}")
        finally:
            acquire(state_dir, given.run_id)
        if self.observe is not None:
            self.observe(name, result)
        note = f"agent {name} run {child.run_id}: {result.status}"
        if result.status != "succeeded":
            return NodeResult(cost_usd=result.cost_usd, note=note, blocked=f"agent {name} did not succeed")

        structured = result.structured_output or {}
        try:
            if "diff" in node.evidence and environment is not None and before is not None:
                out = self._after_implementation(environment, given, structured, before)
            elif "reproduction_fails" in node.evidence and environment is not None:
                out = self._after_discovery(environment, given, structured)
            elif "reviewer_verdict" in node.evidence:
                out = self._after_review(given, structured, child.run_id)
            else:
                out = NodeResult(evidence=agent_evidence(result.structured_output, result.result))
        except (WorkspaceError, OSError) as exc:
            return NodeResult(cost_usd=result.cost_usd, note=note, blocked=str(exc))
        return NodeResult(
            evidence=out.evidence,
            cost_usd=result.cost_usd,
            note=_join(note, out.note),
            blocked=out.blocked,
        )

    def _after_discovery(self, env: Environment, given: NodeInput, report: Mapping[str, Any]) -> NodeResult:
        """Record the reproduction and check, in the sandbox, that it really fails before any fix."""
        command = str(report.get("reproduction_command") or "").strip()
        if not command:
            return NodeResult(blocked="the implementer returned no reproduction command")
        settled = settle(env)
        notes = [f"committed {settled.committed} path(s) the implementer left uncommitted" if settled.committed else None]
        if env.sandbox.sandboxed:
            ran = run_command(env, "reproduction", command, timeout_s=self.timeout_s, base_env=os.environ)
            text = render_results([ran], f"reproduction before the fix, at {settled.head}, in srt")
            ref = write_artifact(self.root, given.run_id, "reproduction.log", redact_lines(text, self.redaction))
            fails = not ran.passed
            check: dict[str, Any] = {**ref, "verified": True, "head": settled.head, "exit_code": ran.exit_code}
            notes.append(f"reproduction {'fails' if fails else 'passes'} at {short(settled.head)} (exit {ran.exit_code})")
        else:
            fails = report.get("reproduction_fails") is True
            reason = "srt is absent, and the orchestrator runs no agent-supplied command outside the sandbox"
            check = {"verified": False, "head": settled.head, "reason": reason}
            notes.append(f"reproduction not verified: {reason}")
        return NodeResult(
            evidence={"reproduction_command": command, "reproduction_fails": fails, "reproduction_check": check},
            note=_join(*notes),
        )

    def _after_implementation(self, env: Environment, given: NodeInput, report: Mapping[str, Any], before: str) -> NodeResult:
        """Settle the branch, cut the diff from it, and check the implementer's claims against this attempt's commits."""
        settled = settle(env)
        base = env.meta.base_commit
        if settled.head == before:
            return NodeResult(blocked="the implementer committed nothing in this attempt")
        diff = diff_evidence(self.root, given.run_id, base, settled.head, given.attempt)
        if not diff["files"]:
            return NodeResult(blocked=f"the run branch changes nothing against its base {short(base)}")
        changes = [dict(c) for c in report.get("changes", []) if isinstance(c, Mapping)]
        for change in changes:
            path = Path(str(change.get("file", "")))
            if path.is_absolute() and path.is_relative_to(env.path):
                change["file"] = path.relative_to(env.path).as_posix()
        # The report covers this attempt only: compare it with what this attempt committed.
        touched = changed_files(self.root, before, settled.head)
        unclaimed = [f for f in touched if f not in {c.get("file") for c in changes}]
        claimed = {"attempt": given.attempt, "changes": changes, "unclaimed": unclaimed}
        planned = given.inputs.get("reproduction_command")
        return NodeResult(
            evidence={"diff": diff, "claimed_changes": claimed},
            note=_join(
                f"committed {settled.committed} path(s) the implementer left uncommitted" if settled.committed else None,
                f"diff {short(base)}..{short(settled.head)}: {len(diff['files'])} file(s), "
                f"+{diff['insertions']} -{diff['deletions']}",
                f"changed but not claimed: {', '.join(unclaimed)}" if unclaimed else None,
                "the implementer reported another reproduction command; the plan's is kept"
                if planned and report.get("reproduction_command") not in (None, planned)
                else None,
            ),
        )

    def _after_review(self, given: NodeInput, verdict: Mapping[str, Any], child_run_id: str) -> NodeResult:
        """Normalize the verdict (blocker and major findings block), save it as a file, and say which commit it covers."""
        reported = verdict.get("verdict")
        findings = [dict(f) for f in verdict.get("findings", []) if isinstance(f, Mapping)]
        blocking = [f for f in findings if f.get("severity") in ("blocker", "major")]
        decided = "changes" if reported == "approve" and blocking else reported
        diff = given.inputs.get("diff")
        head = diff.get("head") if isinstance(diff, Mapping) else None
        saved = {"verdict": decided, "reported_verdict": reported, "findings": findings, "head": head, "child_run": child_run_id}
        ref = write_artifact(self.root, given.run_id, f"review-{given.attempt}.json", json.dumps(saved, indent=2) + "\n")
        return NodeResult(
            evidence={"reviewer_verdict": decided, "findings": findings, "review": {**ref, "head": head}},
            note=f"verdict approve with {len(blocking)} blocker/major finding(s) counts as changes" if decided != reported else None,
        )

    # --- in-process steps that need the worktree --------------------------------------

    def _test(self, given: NodeInput) -> NodeResult:
        """Run the reproduction, test, lint and type-check commands in the sandbox, at exactly the diff's head."""
        diff = given.inputs.get("diff")
        command = given.inputs.get("reproduction_command")
        if not isinstance(diff, Mapping) or not diff.get("head"):
            return NodeResult(blocked="TEST needs the diff and the commit it ends at")
        try:
            env = self._workflow_env(given.run_id, given.workflow)
            head = head_of(env)
            if head != diff["head"]:
                return NodeResult(blocked=f"the worktree is at {short(head)}, not the diffed {short(diff['head'])}")
            if not is_clean(env):
                return NodeResult(blocked="the worktree has uncommitted changes the diff does not show")
        except (AllocatorError, WorkspaceError) as exc:
            return NodeResult(blocked=str(exc))

        known = self.commands()
        planned = ([("reproduction", str(command))] if command else []) + [
            (kind, c) for kind in COMMAND_KINDS for c in known[kind]
        ]
        skipped = [f"{kind}: no command known" for kind in COMMAND_KINDS if not known[kind]]
        summary: dict[str, Any] = {"head": head, "sandboxed": env.sandbox.sandboxed, "skipped": skipped}
        if not env.sandbox.sandboxed:
            text = f"nothing run at {head}: srt is absent, and every command here runs code the agent wrote\n"
            ref = write_artifact(self.root, given.run_id, f"test-{given.attempt}.log", text)
            return NodeResult(
                evidence={"test_output": {**ref, **summary, "results": []}, "tests_pass": False},
                note="no checks run: srt is absent; install srt, then resume",
            )
        results: list[CommandResult] = [
            run_command(env, kind, c, timeout_s=self.timeout_s, base_env=os.environ) for kind, c in planned
        ]
        text = render_results(results, f"checks at {head}, run by the orchestrator in srt")
        ref = write_artifact(self.root, given.run_id, f"test-{given.attempt}.log", redact_lines(text, self.redaction))
        notes = []
        moved = head_of(env) != head
        if moved or not is_clean(env):
            restore(env, head)
            notes.append(f"the commands changed the worktree; restored it to {short(head)}")
        passed = sum(r.passed for r in results)
        notes.insert(0, f"checks at {short(head)}: {passed}/{len(results)} passed" + (f"; skipped {', '.join(skipped)}" if skipped else ""))
        return NodeResult(
            evidence={
                "test_output": {**ref, **summary, "results": [r.summary() for r in results]},
                "tests_pass": bool(results) and passed == len(results) and not moved,
            },
            note=_join(*notes),
        )

    def _pr_refusals(self, env: Environment, workflow: str) -> list[str]:
        """Why this run may not open a draft PR: capability, tool, autonomy, the approved proposal."""
        policy = self.canonical.one("policy", self.policy_name).spec
        problems = []
        if any(matches(p, PR_CAPABILITY) for p in policy.denied_capabilities):
            problems.append(f"policy {policy.name} denies {PR_CAPABILITY}")
        tool = next((d.spec for d in self.canonical.docs if d.kind == "tool" and d.spec is not None
                     and d.spec.capability == PR_CAPABILITY), None)  # fmt: skip
        if tool is None or tool.implementation.type != "cli" or not tool.implementation.command:
            problems.append(f"no canonical cli tool implements {PR_CAPABILITY}")
        elif tool.implementation.command.split()[:3] != ["gh", "pr", "create"] or "--draft" not in tool.implementation.command.split():
            problems.append(f"the {PR_CAPABILITY} tool must be 'gh pr create --draft ...'")
        ceiling = requested_autonomy(self._writer_ceiling(workflow), env.autonomy_ceiling)
        if AUTONOMY_ORDER[ceiling] < AUTONOMY_ORDER[PR_AUTONOMY]:
            why = " (srt is absent)" if not env.sandbox.sandboxed else ""
            problems.append(f"{PR_CAPABILITY} needs {PR_AUTONOMY}; this run's ceiling is {ceiling}{why}")
        config = self.repo_config()
        if config is not None and not config.draft_prs:
            problems.append("the approved proposal does not allow draft PRs (draft_prs: false)")
        return problems

    def _gh_argv(self) -> list[str]:
        tool = next(d.spec for d in self.canonical.docs if d.kind == "tool" and d.spec is not None
                    and d.spec.capability == PR_CAPABILITY)  # fmt: skip
        assert self.gh is not None and tool.implementation.command is not None
        return [self.gh, *tool.implementation.command.split()[1:]]

    def _failed_gates(self, given: NodeInput) -> list[str]:
        """The policy's required gates whose advance edge does not pass on this run's evidence."""
        policy = self.canonical.one("policy", self.policy_name).spec
        dag = build_dag(self.canonical.one("workflow", given.workflow).spec, required_gates=policy.required_gates)
        failed = []
        for key in policy.required_gates:
            gates = [e.gate for n in dag.nodes for e in n.edges if e.kind == "advance" and e.gate and e.gate.key == key]
            if not any(g.passes(given.inputs) for g in gates):
                failed.append(f"{gates[0] if gates else key} (got {given.inputs.get(key)!r})")
        return failed

    def _pr(self, given: NodeInput) -> NodeResult:
        """Open the draft PR on the run branch, or end at HUMAN_DECISION with the branch kept and the reason."""
        inputs = given.inputs
        diff, tests, review = (inputs.get(k) if isinstance(inputs.get(k), Mapping) else {} for k in ("diff", "test_output", "review"))
        head = str(diff.get("head") or "")
        branch = f"agent-team/{given.run_id}"

        def no_pr(reason: str, pushed: bool = False) -> NodeResult:
            evidence = {"status": "not_opened", "reason": reason, "branch": branch, "head": head, "pushed": pushed}
            return NodeResult(evidence={"pr": evidence}, note=f"no draft PR: {reason}; branch {branch} kept", escalate=True)

        if not head or tests.get("head") != head or review.get("head") != head:
            return no_pr("the diffed, tested and reviewed commits are not the same commit")
        try:
            env = self._workflow_env(given.run_id, given.workflow)
        except AllocatorError as exc:
            return no_pr(str(exc))
        if env.meta.branch != branch:
            return no_pr(f"the worktree is on {env.meta.branch}, not the run branch")
        refusals = self._pr_refusals(env, given.workflow)
        if refusals:
            return no_pr("; ".join(refusals))
        failed = self._failed_gates(given)
        if failed and not given.escalated:
            return no_pr("required gate(s) did not pass: " + ", ".join(failed))

        reasons = []
        verdict = str(inputs.get("reviewer_verdict"))
        if given.escalated:
            reasons.append(
                "The reviewer escalated." if verdict == "escalate"
                else f"The reviewer still asked for changes after the one retry (verdict `{verdict}`)."
            )  # fmt: skip
        reasons += [f"Required gate not passed: {g}." for g in failed]
        plan_, check = (inputs.get(k) if isinstance(inputs.get(k), Mapping) else {} for k in ("plan", "reproduction_check"))
        body_inputs = pr_mod.PrInputs(
            run_id=given.run_id,
            task=given.task,
            plan=plan_,
            reproduction_command=inputs.get("reproduction_command"),
            reproduction_check=check,
            diff=diff,
            claimed_changes=inputs.get("claimed_changes") if isinstance(inputs.get("claimed_changes"), Mapping) else {},
            test_output=tests,
            verdict=verdict,
            findings=[f for f in inputs.get("findings") or [] if isinstance(f, Mapping)],
            blocked=given.escalated,
            blocked_reasons=reasons,
        )
        ref = write_artifact(self.root, given.run_id, "pr-body.md", pr_mod.body(body_inputs, self.redaction))
        if self.gh is None:
            return no_pr("gh is not on PATH; install the GitHub CLI, or push the branch and open the draft yourself")
        if not pr_mod.gh_authenticated(self.gh, self.root):
            return no_pr("gh is not authenticated; run 'gh auth login', or push the branch and open the draft yourself")
        if not pr_mod.has_remote(self.root):
            return no_pr(f"the repository has no '{pr_mod.REMOTE}' remote to push to")
        try:
            pr_mod.push(self.root, head, branch, self.redaction)
        except (pr_mod.PrError, OSError) as exc:
            return no_pr(str(exc))
        try:
            url = pr_mod.create(self._gh_argv(), self.root, branch, pr_mod.title(given.task, given.escalated),
                                self.root / ref["path"], self.redaction)  # fmt: skip
        except (pr_mod.PrError, OSError) as exc:
            return no_pr(str(exc), pushed=True)
        opened = {"status": "opened", "url": url, "branch": branch, "head": head, "draft": True,
                  "blocked": given.escalated, "body": ref}  # fmt: skip
        return NodeResult(
            evidence={"pr": opened},
            note=f"draft PR {url} on {branch} at {short(head)}" + (", marked blocked" if given.escalated else ""),
        )
