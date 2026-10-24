"""The default node executor: in-process steps, and agent nodes through `run_agent` (build-order step 10).

An agent node is one `run_agent` call, unchanged: it records its own child run
(workflow `agent:<name>`, its own worktree and sandbox), and its cost is added
to the workflow run. `run_agent` takes the repository lock itself, so the
workflow hands the lock over for the call and takes it back afterwards.

In-process steps are plain functions. `test` and `pr` need the implementer's
worktree and the `github.pr.create` tool, which arrive in step 11; until then
`unavailable` names them and the CLI refuses before recording anything.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_team.adapters.claude.invoke import ClaudeVersionError
from agent_team.environments import AllocatorError, LockHeldError, acquire, release
from agent_team.orchestrator.dag import Dag, DagNode
from agent_team.orchestrator.machine import Echo, NodeInput, NodeResult
from agent_team.run_agent import Canonical, RunAgentError, run_agent
from agent_team.runs.store import STATE_DIR, repo_root


def intake(given: NodeInput) -> NodeResult:
    return NodeResult(evidence={"task": given.task})


def plan(given: NodeInput) -> NodeResult:
    """The bug-fix plan: the task plus one synthetic criterion, the reproduction passes."""
    command = given.inputs.get("reproduction_command")
    criteria = [{"id": "AC-1", "text": f"the reproduction command passes: {command}"}] if command else []
    return NodeResult(evidence={"plan": {"task": given.task, "acceptance_criteria": criteria}})


def human_decision(given: NodeInput) -> NodeResult:
    """The last state: the draft PR waits for a human. The run ends here."""
    return NodeResult(note="escalated: the draft PR is marked blocked" if given.escalated else "awaiting human review")


STEPS: dict[str, Callable[[NodeInput], NodeResult]] = {
    "intake": intake,
    "plan": plan,
    "human_decision": human_decision,
}


def agent_prompt(given: NodeInput) -> str:
    """The prompt for an agent node: the task and only the evidence its node lists as inputs."""
    parts = [f"Workflow {given.workflow}, state {given.state}, attempt {given.attempt}.", "", "## Task", "", given.task]
    for key, value in given.inputs.items():
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
    policy_name: str = "default"
    environment_name: str = "default"
    echo: Echo = print

    def unavailable(self, dag: Dag) -> list[str]:
        """Why this executor cannot run every node of `dag`; empty when it can."""
        agents = {d.spec.name for d in self.canonical.docs if d.kind == "agent" and d.spec is not None}
        problems = [f"agent {a!r} is not in {self.canonical_dir}" for a in dag.agents if a not in agents]
        problems += [f"in-process step {s!r} is not implemented yet" for s in dag.steps if s not in STEPS]
        return problems

    def __call__(self, node: DagNode, given: NodeInput) -> NodeResult:
        if node.step is not None:
            step = STEPS.get(node.step)
            if step is None:
                return NodeResult(blocked=f"in-process step {node.step!r} is not implemented yet")
            return step(given)
        return self._agent(node.agent or "", given)

    def _agent(self, name: str, given: NodeInput) -> NodeResult:
        state_dir = repo_root(self.repo) / STATE_DIR
        release(state_dir, given.run_id)
        try:
            child, result = run_agent(
                self.repo,
                self.canonical_dir,
                name,
                agent_prompt(given),
                registry_path=self.registry_path,
                claude=self.claude,
                max_budget_usd=self.max_budget_usd,
                timeout_s=self.timeout_s,
                test_commands=self.test_commands,
                policy_name=self.policy_name,
                environment_name=self.environment_name,
                echo=lambda line: self.echo(f"    {line}"),
            )
        except (RunAgentError, ClaudeVersionError, AllocatorError, LockHeldError) as exc:
            return NodeResult(blocked=f"agent {name}: {exc}")
        finally:
            acquire(state_dir, given.run_id)
        note = f"agent {name} run {child.run_id}: {result.status}"
        if result.status != "succeeded":
            return NodeResult(cost_usd=result.cost_usd, note=note, blocked=f"agent {name} did not succeed")
        return NodeResult(
            evidence=agent_evidence(result.structured_output, result.result),
            cost_usd=result.cost_usd,
            note=note,
        )
