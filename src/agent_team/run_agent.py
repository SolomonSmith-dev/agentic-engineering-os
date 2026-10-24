"""`agent-team run --agent`: one canonical agent, one run, end to end (build-order step 6).

Resolve the model, record, then lock, allocate, compile, invoke, record, finish:

    registry -> start_run -> lock -> worktree + sandbox -> <agent's state> -> compile -> claude -p
                          -> tool calls + cost -> finish (BLOCKED with the reason when it did not succeed)

The orchestrator (step 10) replaces this with workflow-driven runs; until then
this is the one path that runs an agent, and it records everything it does.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

from agent_team.adapters.claude.compile import compile_agent
from agent_team.adapters.claude.invoke import build_invocation, check_version, execute, record
from agent_team.canonical import Document, validate_tree
from agent_team.environments import acquire, create, release
from agent_team.routing import ResolvedModel, RoutingError, resolve_model
from agent_team.runs.render import status_line
from agent_team.runs.store import STATE_DIR, RunStore, repo_root
from agent_team.schemas.adapter import OUTPUT_SCHEMAS, AdapterResult
from agent_team.schemas.common import AUTONOMY_ORDER, Autonomy
from agent_team.schemas.registry import RegistrySpec
from agent_team.schemas.runs import RunRecord, RunState


class RunAgentError(RuntimeError):
    """The run could not start; nothing was invoked. The message says what to fix."""


@dataclass(frozen=True)
class Canonical:
    docs: list[Document]

    def one(self, kind: str, name: str) -> Document:
        for doc in self.docs:
            if doc.kind == kind and doc.spec is not None and doc.spec.name == name:
                return doc
        raise RunAgentError(f"no canonical {kind} named {name!r}")

    def registry(self) -> RegistrySpec:
        for doc in self.docs:
            if doc.kind == "registry" and doc.spec is not None:
                return doc.spec
        raise RunAgentError("no model registry: the canonical tree needs routing/registry.yaml")


def load_canonical(path: Path) -> Canonical:
    report = validate_tree(path)
    if not report.ok:
        first = report.problems[0].render(path)
        raise RunAgentError(f"canonical tree at {path} is invalid ({len(report.problems)} problem(s)); first: {first}")
    return Canonical(report.documents)


def requested_autonomy(agent_max: Autonomy, policy_ceiling: Autonomy) -> Autonomy:
    return agent_max if AUTONOMY_ORDER[agent_max] <= AUTONOMY_ORDER[policy_ceiling] else policy_ceiling


def agent_state(triggers: list[str]) -> RunState | None:
    """The run state an agent works in: its first `state:` trigger that names a known state."""
    for trigger in triggers:
        namespace, _, value = trigger.partition(":")
        if namespace == "state" and value in get_args(RunState):
            return value  # type: ignore[return-value]
    return None


def pick_model(canonical: Canonical, model_class: str, override: str | None) -> ResolvedModel:
    try:
        return resolve_model(model_class, canonical.registry(), override)
    except RoutingError as exc:
        raise RunAgentError(str(exc)) from exc


def run_agent(
    repo: Path,
    canonical_dir: Path,
    agent_name: str,
    prompt: str,
    *,
    model: str | None = None,
    claude: str,
    max_budget_usd: float,
    timeout_s: float,
    test_commands: tuple[str, ...] = (),
    policy_name: str = "default",
    environment_name: str = "default",
    echo: Callable[[str], None] = print,
) -> tuple[RunRecord, AdapterResult]:
    """Run one agent against `repo` and return its run record and normalized result.

    The model comes from the registry by the agent's model class; `model` overrides it.
    """
    canonical = load_canonical(canonical_dir)
    agent_doc = canonical.one("agent", agent_name)
    agent = agent_doc.spec
    skills = [(d.spec, d.body) for d in (canonical.one("skill", s) for s in agent.skills)]
    policy = canonical.one("policy", policy_name).spec
    env_spec = canonical.one("environment", environment_name).spec
    resolved = pick_model(canonical, agent.model_class, model)
    check_version(claude)

    root = repo_root(repo)
    state_dir = root / STATE_DIR
    redaction = tuple(r.pattern for r in policy.redaction)
    store = RunStore.open(root, redaction_patterns=redaction)
    assert store is not None
    with store:
        run = store.start_run(f"agent:{agent.name}", model_class=agent.model_class)
        run_id = run.run_id
        echo(f"run {run_id}")
        echo(status_line(store.transitions(run_id)[-1]))
        try:
            replaced = acquire(state_dir, run_id)
        except Exception as exc:
            echo(status_line(store.transition(run_id, "BLOCKED", note=str(exc))))
            store.finish_run(run_id, "blocked")
            raise
        try:
            ceiling = requested_autonomy(agent.max_autonomy, policy.autonomy_ceiling.get(agent.name, policy.default_autonomy))
            environment = create(root, run_id, env_spec, requested_autonomy=ceiling, store=store)
            compiled = compile_agent(
                agent,
                agent_doc.body,
                skills,
                policy,
                ceiling=environment.autonomy_ceiling,
                model=resolved.model,
                fallbacks=resolved.fallbacks,
                out_dir=state_dir / "compiled" / run_id,
                roots=[environment.path, root],
                home=Path.home(),
                test_commands=test_commands,
            )
            notes = [environment.note(env_spec.name), resolved.note()]
            if replaced is not None:
                notes.append(f"took over a stale lock from run {replaced.run_id} (pid {replaced.pid})")
            if compiled.permissions.dropped:
                notes.append("dropped: " + "; ".join(compiled.permissions.dropped))
            state = agent_state(agent.triggers) or "IMPLEMENTATION"
            echo(status_line(store.transition(run_id, state, note=" | ".join(notes))))

            invocation = build_invocation(
                compiled,
                claude=claude,
                sandbox=environment.sandbox,
                base_env=dict(os.environ),
                max_budget_usd=max_budget_usd,
            )
            output_model = next((OUTPUT_SCHEMAS[o] for o in agent.outputs if o in OUTPUT_SCHEMAS), None)
            stderr_path = environment.sandbox.tmp_dir / "claude.stderr"
            try:
                result, parser = execute(
                    invocation,
                    prompt=prompt,
                    cwd=environment.path,
                    stderr_path=stderr_path,
                    timeout_s=timeout_s,
                    output_model=output_model,
                    redaction=redaction,
                )
            finally:
                stderr_path.unlink(missing_ok=True)  # only the redacted tail is kept, in the result
            record(store, run_id, parser, result)
            if result.status == "succeeded":
                store.finish_run(run_id, "succeeded")
            else:
                reason = result.structured_output_error or result.subtype or f"exit {result.exit_code}"
                echo(status_line(store.transition(run_id, "BLOCKED", note=f"{result.status}: {reason}")))
                store.finish_run(run_id, "failed")
        except BaseException as exc:
            if store.get_run(run_id).ended_at is None:
                echo(status_line(store.transition(run_id, "BLOCKED", note=f"{type(exc).__name__}: {exc}")))
                store.finish_run(run_id, "failed")
            raise
        finally:
            release(state_dir, run_id)
        return store.get_run(run_id), result
