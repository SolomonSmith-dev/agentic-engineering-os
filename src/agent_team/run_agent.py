"""`agent-team run --agent`: one canonical agent, one run, end to end (build-order step 6).

Record first, then lock, allocate, compile, invoke, record, finish:

    start_run -> lock -> worktree + sandbox -> <agent's state> -> resolve model -> compile -> claude -p
              -> tool calls + cost -> finish (BLOCKED with the reason when it did not succeed)

`runtime="codex"` (step 12) runs the independent reviewer through `codex exec`
instead: the model resolves for the codex runtime, the srt settings gain the
compiled denyRead rules and the OpenAI hosts, and the transition note lists the
capabilities Codex runs without.

`after` (step 9) runs once the agent has succeeded and its tool calls are
recorded, before the run is finished: still inside the run's lock, worktree
and sandbox, so `inspect` can verify commands there and record them on the
same run. If it raises, the run ends `failed`.

The orchestrator (step 10) replaces this with workflow-driven runs; until then
this is the one path that runs an agent, and it records everything it does.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

from agent_team.adapters import codex as codex_adapter
from agent_team.adapters.claude.compile import compile_agent
from agent_team.adapters.claude.invoke import RUNTIME, build_invocation, check_version, execute, record
from agent_team.adapters.codex.invoke import API_DOMAINS
from agent_team.adapters.codex.invoke import auth_mode as codex_auth_mode
from agent_team.canonical import Document, validate_tree
from agent_team.environments import acquire, create, release, srt_path
from agent_team.environments.sandbox import extend_settings
from agent_team.environments.worktree import Environment
from agent_team.routing import RegistryError, default_registry_path, load_registry, resolve_model
from agent_team.runs.render import status_line
from agent_team.runs.store import STATE_DIR, RunStore, repo_root
from agent_team.schemas.adapter import OUTPUT_SCHEMAS, AdapterResult
from agent_team.schemas.agent import AgentSpec
from agent_team.schemas.common import AUTONOMY_ORDER, Autonomy
from agent_team.schemas.policy import PolicySpec
from agent_team.schemas.registry import ModelCost, Runtime
from agent_team.schemas.runs import RunRecord, RunState
from agent_team.schemas.skill import SkillSpec

# The one agent the Codex adapter runs (architecture section 11, step 12: the reviewer only).
CODEX_AGENTS: tuple[str, ...] = ("independent-reviewer",)


class RunAgentError(RuntimeError):
    """The run could not start; nothing was invoked. The message says what to fix."""


@dataclass(frozen=True)
class AgentRun:
    """What an `after` callback gets: the open run, its environment and the agent's result."""

    store: RunStore
    run_id: str
    root: Path
    environment: Environment
    policy: PolicySpec
    result: AdapterResult


AfterAgent = Callable[[AgentRun], None]


@dataclass(frozen=True)
class Canonical:
    docs: list[Document]

    def one(self, kind: str, name: str) -> Document:
        for doc in self.docs:
            if doc.kind == kind and doc.spec is not None and doc.spec.name == name:
                return doc
        raise RunAgentError(f"no canonical {kind} named {name!r}")


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


def run_agent(
    repo: Path,
    canonical_dir: Path,
    agent_name: str,
    prompt: str,
    *,
    model: str | None = None,
    registry_path: Path | None = None,
    runtime: Runtime = RUNTIME,
    claude: str | None = None,
    codex: str | None = None,
    max_budget_usd: float,
    timeout_s: float,
    test_commands: tuple[str, ...] = (),
    policy_name: str = "default",
    environment_name: str = "default",
    after: AfterAgent | None = None,
    echo: Callable[[str], None] = print,
) -> tuple[RunRecord, AdapterResult]:
    """Run one agent against `repo` and return its run record and normalized result.

    The model comes from the agent's model class in the registry for `runtime`; `model`
    overrides it with another registry entry on that runtime.
    """
    canonical = load_canonical(canonical_dir)
    agent_doc = canonical.one("agent", agent_name)
    agent = agent_doc.spec
    skills = [(d.spec, d.body) for d in (canonical.one("skill", s) for s in agent.skills)]
    policy = canonical.one("policy", policy_name).spec
    env_spec = canonical.one("environment", environment_name).spec
    try:
        registry = load_registry(registry_path or default_registry_path(canonical_dir))
        resolved = resolve_model(
            registry,
            agent.model_class,
            runtime=runtime,
            allowed_privacy=policy.allowed_privacy_classes,
            override=model,
        )
    except RegistryError as exc:
        raise RunAgentError(str(exc)) from exc
    if runtime == "codex":
        if agent.name not in CODEX_AGENTS:
            raise RunAgentError(f"--runtime codex runs only {', '.join(CODEX_AGENTS)}, not {agent.name}")
        if codex is None:
            raise RunAgentError("--runtime codex needs the codex binary")
        codex_adapter.check_flags(codex, dict(os.environ))
        if env_spec.tier != "srt" or srt_path() is None:
            raise RunAgentError(
                "--runtime codex needs srt: Codex has no hook this adapter uses, so srt's denyRead rules "
                "are what keep protected paths unreadable; install srt and use an srt environment"
            )
    elif claude is None:
        raise RunAgentError("the claude runtime needs the claude binary")
    else:
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
            if runtime == "codex":
                result = _run_codex(
                    store, run_id, root, state_dir, environment, agent, agent_doc.body, skills, policy,
                    resolved_model=resolved.model, price=registry.models[resolved.model].cost,
                    notes=[environment.note(env_spec.name), resolved.note()]
                    + ([f"took over a stale lock from run {replaced.run_id} (pid {replaced.pid})"] if replaced else []),
                    codex=codex, prompt=prompt, max_budget_usd=max_budget_usd, timeout_s=timeout_s,
                    test_commands=test_commands, redaction=redaction, echo=echo,
                )  # fmt: skip
                if after is not None and result.status == "succeeded":
                    after(AgentRun(store, run_id, root, environment, policy, result))
                _finish(store, run_id, result, echo)
                return store.get_run(run_id), result
            assert claude is not None
            compiled = compile_agent(
                agent,
                agent_doc.body,
                skills,
                policy,
                ceiling=environment.autonomy_ceiling,
                model=resolved.model,
                fallback_models=resolved.fallbacks,
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
            if after is not None and result.status == "succeeded":
                after(AgentRun(store, run_id, root, environment, policy, result))
            _finish(store, run_id, result, echo)
        except BaseException as exc:
            if store.get_run(run_id).ended_at is None:
                echo(status_line(store.transition(run_id, "BLOCKED", note=f"{type(exc).__name__}: {exc}")))
                store.finish_run(run_id, "failed")
            raise
        finally:
            release(state_dir, run_id)
        return store.get_run(run_id), result


def _finish(store: RunStore, run_id: str, result: AdapterResult, echo: Callable[[str], None]) -> None:
    if result.status == "succeeded":
        store.finish_run(run_id, "succeeded")
        return
    reason = result.structured_output_error or result.reason or result.subtype or f"exit {result.exit_code}"
    echo(status_line(store.transition(run_id, "BLOCKED", note=f"{result.status}: {reason}")))
    store.finish_run(run_id, "failed")


def _run_codex(
    store: RunStore,
    run_id: str,
    root: Path,
    state_dir: Path,
    environment: Environment,
    agent: AgentSpec,
    body: str,
    skills: list[tuple[SkillSpec, str]],
    policy: PolicySpec,
    *,
    resolved_model: str,
    price: ModelCost | None,
    notes: list[str],
    codex: str | None,
    prompt: str,
    max_budget_usd: float,
    timeout_s: float,
    test_commands: tuple[str, ...],
    redaction: tuple[str, ...],
    echo: Callable[[str], None],
) -> AdapterResult:
    """Compile for Codex, narrow the sandbox, run `codex exec --json` and record what it did."""
    assert codex is not None
    base_env = dict(os.environ)
    compiled = codex_adapter.compile_agent(
        agent,
        body,
        skills,
        policy,
        ceiling=environment.autonomy_ceiling,
        model=resolved_model,
        out_dir=state_dir / "compiled" / run_id,
        roots=[environment.path, root],
        home=Path.home(),
        keep=[environment.path, environment.sandbox.tmp_dir],
        test_commands=test_commands,
    )
    extend_settings(
        environment.sandbox,
        deny_read=compiled.deny_read,
        allowed_domains=API_DOMAINS[codex_auth_mode(base_env)],
    )
    notes = [*notes, f"runtime codex, sandbox_mode {compiled.permissions.sandbox_mode}"]
    notes.append("unavailable: " + "; ".join(codex_adapter.UNAVAILABLE))
    if compiled.permissions.dropped:
        notes.append("dropped: " + "; ".join(compiled.permissions.dropped))
    state = agent_state(agent.triggers) or "IMPLEMENTATION"
    echo(status_line(store.transition(run_id, state, note=" | ".join(notes))))

    invocation = codex_adapter.build_invocation(
        compiled, codex=codex, sandbox=environment.sandbox, base_env=base_env, cwd=environment.path
    )
    output_model = next((OUTPUT_SCHEMAS[o] for o in agent.outputs if o in OUTPUT_SCHEMAS), None)
    stderr_path = environment.sandbox.tmp_dir / "codex.stderr"
    try:
        result, parser = codex_adapter.execute(
            invocation,
            prompt=prompt,
            cwd=environment.path,
            stderr_path=stderr_path,
            timeout_s=timeout_s,
            output_model=output_model,
            price=price,
            max_budget_usd=max_budget_usd,
            redaction=redaction,
        )
    finally:
        stderr_path.unlink(missing_ok=True)  # only the redacted tail is kept, in the result
    codex_adapter.record(store, run_id, parser, result)
    return result
