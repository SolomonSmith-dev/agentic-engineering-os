"""The three arms of the section 13 comparison, the per-task cap they share, and the probe before them.

    baseline-a   mini-swe-agent, one shot, on the strongest Claude model in the registry, inside srt
    baseline-b   one `claude -p` session: the implementer agent, the same model, no plan, reviewer or checks
    treatment    `agent-team run --workflow bug-fix`: implementer and reviewer on their registry classes

Each arm works on its own fresh copy of the task repository and is stopped at the
per-task cap: mini-swe-agent through `--cost-limit`, Claude Code through
`--max-budget-usd`, and the treatment through `CappedExecutor`, which hands each
agent call only what is left of the cell's cap and refuses the next call once
too little is left. A runtime checks its own cap between turns, so one turn can
overshoot it; the cell records the cost the runtime reported and flags it.

mini-swe-agent (verified from the 2.4.6 sdist and litellm 1.104.2, the pinned
`baseline` group): the `mini` command takes `--model`, `--task`, `--yolo`,
`--exit-immediately`, `--cost-limit`, `--output` and `--agent-class`; `default`
is the agent class that never prompts. It calls the model through litellm, which
reads `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`); a Claude Code login is not
something it can use, so without the key the arm refuses. It prices each call
from litellm's bundled cost map (`LITELLM_LOCAL_MODEL_COST_MAP=True`, so nothing
is fetched at import), stops once the cost reaches `--cost-limit`, and writes a
trajectory whose `info.model_stats.instance_cost` is the run's cost.
"""

import json
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_team.adapters.claude.invoke import ENV_ALLOWLIST, ClaudeVersionError, check_version
from agent_team.environments import AllocatorError, LockHeldError, srt_path
from agent_team.eval.tasks import LoadedTask, TaskError, git
from agent_team.eval.verify import eval_sandbox
from agent_team.orchestrator import DefaultExecutor, OrchestratorError, run_workflow
from agent_team.orchestrator.dag import DagNode
from agent_team.orchestrator.machine import NodeInput, NodeResult
from agent_team.routing import load_registry, resolve_model
from agent_team.run_agent import Canonical, RunAgentError, run_agent
from agent_team.runs.redact import redact_text
from agent_team.runs.store import RunStore, RunStoreError
from agent_team.schemas.adapter import AdapterResult
from agent_team.schemas.eval import ArmName, CellStatus

API_KEY_ENV = "ANTHROPIC_API_KEY"
MINI_VERSION = "2.4.6"  # the pin in pyproject.toml's `baseline` group

# The registry has no strength ranking; the baselines use the model behind this class (Opus 5.5).
BASELINE_CLASS = "high-reasoning"
EVAL_ENVIRONMENT = "eval"
WORKFLOW = "bug-fix"

# Below this, an agent call is not started: Claude Code's flag takes cents, and a call this small does nothing.
MIN_CALL_USD = 0.05

PROBE_BUDGET_USD = 0.10
PROBE_TIMEOUT_S = 300.0
PROBE_PROMPT = "Run `git show HEAD` first, then review that commit. Acceptance criterion AC-1: add(2, 3) returns 5."

# Baseline B's prompt: the implementer reads its state from the first line, as in the workflow.
SINGLE_SESSION_PROMPT = """Workflow single-session, state IMPLEMENTATION, attempt 1.

## Task

{instruction}

There is no plan, reviewer or orchestrator check in this session. Reproduce the problem, fix it,
run the tests, and commit everything you changed.
"""

LOG_TAIL_CHARS = 2000

Echo = Callable[[str], None]


@dataclass(frozen=True)
class ArmContext:
    """What every arm needs, resolved once per eval."""

    canonical_dir: Path  # the eval's copy, with the `eval` environment
    registry_path: Path
    srt: str
    baseline_model: str
    deny_read: list[Path]
    allowed_domains: list[str]
    timeout_s: float
    claude: str | None = None
    mini: str | None = None
    redaction: tuple[str, ...] = ()
    echo: Echo = print


@dataclass
class ArmOutcome:
    status: CellStatus
    tree: Path  # where the arm's work is: the checkout, or the run's worktree
    cost_usd: float | None
    model: str
    abort_reason: str | None = None
    error: str | None = None
    tokens: dict[str, int] | None = None
    tokens_note: str | None = None
    retries: int | None = None
    interventions: int | None = None
    run_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


ARM_DESCRIPTIONS: dict[ArmName, str] = {
    "baseline-a": f"mini-swe-agent {MINI_VERSION}, one shot",
    "baseline-b": "one claude -p session (the implementer agent), no team",
    "treatment": f"agent-team run --workflow {WORKFLOW}",
}


def which(name: str) -> str | None:
    return shutil.which(name)


def treatment_models(registry_path: Path) -> str:
    registry = load_registry(registry_path)
    implementer = resolve_model(registry, "strong-coding", runtime="claude").model
    reviewer = resolve_model(registry, "independent", runtime="claude").model
    return f"implementer {implementer}, reviewer {reviewer}"


def baseline_model(registry_path: Path, override: str | None) -> str:
    """The baselines' model: `override` (a registry id on the claude runtime) or the BASELINE_CLASS model."""
    registry = load_registry(registry_path)
    return resolve_model(registry, BASELINE_CLASS, runtime="claude", override=override).model


def refusal(arm: ArmName, *, claude: str | None, mini: str | None, environ: dict[str, str]) -> str | None:
    """Why `arm` cannot run on this machine, or None. Nothing is called that costs money."""
    if arm == "baseline-a":
        if mini is None:
            return "mini-swe-agent is not installed; run 'uv sync --group dev --group baseline' (the `mini` command)"
        if not environ.get(API_KEY_ENV):
            return (
                f"{API_KEY_ENV} is not set: mini-swe-agent calls the Anthropic API through litellm and cannot use "
                "a Claude Code login; set the key, or leave this arm out with --arms baseline-b,treatment"
            )
    elif claude is None:
        return "claude is not on PATH; run 'agent-team doctor'"
    else:
        try:
            check_version(claude)
        except (ClaudeVersionError, OSError, subprocess.SubprocessError) as exc:
            return str(exc)
    if srt_path() is None:
        return "srt is not installed; every arm and the verifier run inside it"
    return None


def _indent(echo: Echo) -> Echo:
    return lambda line: echo(f"    {line}")


def _agent_cost(repo: Path) -> tuple[float, list[str]]:
    """Every agent run's cost in this cell's run record, and the run ids: the cell's model spend."""
    try:
        store = RunStore.open(repo, create=False)
    except RunStoreError:
        return 0.0, []
    if store is None:
        return 0.0, []
    with store:
        runs = store.list_runs(1000)
    agents = [r for r in runs if r.workflow.startswith("agent:")]
    return sum(r.cost_usd for r in agents), [r.run_id for r in reversed(runs)]


def _sum_usage(results: list[AdapterResult]) -> tuple[dict[str, int] | None, str | None]:
    if not results:
        return None, "no agent call ran"
    if any(r.usage is None for r in results):
        return None, "an agent call reported no token counts"
    total: dict[str, int] = {}
    for result in results:
        for key, value in (result.usage or {}).items():
            total[key] = total.get(key, 0) + value
    return total, None


# --- baseline A: mini-swe-agent ------------------------------------------------------------


def _mini_env(tmp: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env[API_KEY_ENV] = os.environ[API_KEY_ENV]
    env.update({
        "MSWEA_CONFIGURED": "true",  # skip the interactive first-run setup
        "MSWEA_SILENT_STARTUP": "1",
        "MSWEA_GLOBAL_CONFIG_DIR": str(tmp / "mswea"),  # never the user's ~/.config/mini-swe-agent/.env
        "LITELLM_MODE": "PRODUCTION",  # litellm's DEV mode loads a .env file found above its own install
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",  # price from the pinned, bundled map; fetch nothing at import
        "PAGER": "cat",
    })  # fmt: skip
    return env


def read_trajectory(path: Path) -> dict[str, Any]:
    """Cost, calls, exit status and tokens from a mini-swe-agent trajectory file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    info = data.get("info") or {}
    stats = info.get("model_stats") or {}
    tokens: dict[str, int] = {}
    # unverified: the usage keys inside each response; litellm's ModelResponse dumps OpenAI-shaped
    # usage (prompt_tokens, completion_tokens), and nothing is counted when they are missing.
    for message in data.get("messages") or []:
        usage = (((message or {}).get("extra") or {}).get("response") or {}).get("usage") if isinstance(message, dict) else None
        if isinstance(usage, dict):
            for key in ("prompt_tokens", "completion_tokens"):
                if isinstance(usage.get(key), int):
                    tokens[key] = tokens.get(key, 0) + usage[key]
    return {
        "cost": float(stats["instance_cost"]) if isinstance(stats.get("instance_cost"), int | float) else None,
        "api_calls": stats.get("api_calls"),
        "exit_status": str(info.get("exit_status") or ""),
        "tokens": tokens or None,
    }


def run_baseline_a(loaded: LoadedTask, repo: Path, cell_dir: Path, cap: float, ctx: ArmContext) -> ArmOutcome:
    """mini-swe-agent in srt on the checkout itself: writes only there (never its .git) and in its temp dir."""
    assert ctx.mini is not None
    model = ctx.baseline_model
    tmp = cell_dir / "tmp-baseline-a"
    trajectory = tmp / "trajectory.json"
    sandbox = eval_sandbox(
        ctx.srt, cell_dir / "srt-baseline-a.json", tmp,
        allow_write=[repo], deny_write=[repo / ".git"], deny_read=ctx.deny_read, allowed_domains=ctx.allowed_domains,
    )  # fmt: skip
    argv = [
        ctx.mini, "--model", f"anthropic/{model}", "--task", loaded.task.instruction, "--yolo", "--exit-immediately",
        "--cost-limit", f"{cap:.2f}", "--output", str(trajectory), "--agent-class", "default",
    ]  # fmt: skip
    # unverified: mini-swe-agent and litellm running under srt (read-only HOME, the allowlist as the only
    # network) and the Anthropic API accepting `claude-opus-5-5` through litellm; never run against a model here.
    log = tmp / "mini.log"
    outcome = ArmOutcome(status="completed", tree=repo, cost_usd=None, model=f"{model} (through litellm)")
    with log.open("wb") as out:
        proc = subprocess.Popen(
            sandbox.wrap(argv), cwd=repo, env=sandbox.env(_mini_env(tmp)), stdin=subprocess.DEVNULL,
            stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
        )  # fmt: skip
        try:
            exit_code: int | None = proc.wait(timeout=ctx.timeout_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            exit_code = None
    patterns = (*ctx.redaction, re.escape(os.environ[API_KEY_ENV]))  # the key itself, whatever its shape
    tail = redact_text(log.read_bytes()[-LOG_TAIL_CHARS:].decode("utf-8", errors="replace"), patterns).strip()
    log.unlink(missing_ok=True)  # the transcript can hold anything a command printed, the API key included
    if not trajectory.is_file():
        outcome.status = "error"
        outcome.error = f"mini-swe-agent wrote no trajectory ({'timed out' if exit_code is None else f'exit {exit_code}'}): {tail[-500:]}"
        outcome.notes.append("cost unknown: no trajectory; the per-task cap is charged against the total")
        return outcome
    try:
        traj = read_trajectory(trajectory)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        outcome.status = "error"
        outcome.error = f"mini-swe-agent's trajectory is unreadable ({type(exc).__name__})"
        return outcome
    finally:
        trajectory.unlink(missing_ok=True)
    outcome.cost_usd = traj["cost"]
    outcome.tokens = traj["tokens"]
    outcome.tokens_note = None if traj["tokens"] else "mini-swe-agent's trajectory carried no token usage"
    outcome.retries = 0
    outcome.interventions = 0
    status = traj["exit_status"]
    outcome.notes.append(f"mini-swe-agent exit status {status or '?'}, {traj['api_calls']} model call(s)")
    if exit_code is None:
        outcome.status, outcome.abort_reason = "aborted", f"timeout: killed after {ctx.timeout_s:g} s"
    elif status == "LimitsExceeded":
        outcome.status = "aborted"
        outcome.abort_reason = f"per-task budget: mini-swe-agent stopped at its ${cap:.2f} cost limit (${traj['cost'] or 0:.4f})"
    elif status not in ("Submitted", ""):
        outcome.status, outcome.error = "error", f"mini-swe-agent ended with {status}: {tail[-500:]}"
    return outcome


# --- baseline B: one claude -p session -------------------------------------------------------


def run_baseline_b(loaded: LoadedTask, repo: Path, cell_dir: Path, cap: float, ctx: ArmContext) -> ArmOutcome:
    """The implementer agent, once, through `run_agent`: the same adapter, hook and srt as the team."""
    assert ctx.claude is not None
    task = loaded.task
    outcome = ArmOutcome(status="completed", tree=repo, cost_usd=None, model=ctx.baseline_model)
    try:
        record, result = run_agent(
            repo, ctx.canonical_dir, "implementer", SINGLE_SESSION_PROMPT.format(instruction=task.instruction),
            model=ctx.baseline_model, registry_path=ctx.registry_path, claude=ctx.claude, max_budget_usd=cap,
            timeout_s=ctx.timeout_s, test_commands=(task.test_command,) if task.test_command else (),
            environment_name=EVAL_ENVIRONMENT, state="IMPLEMENTATION", echo=_indent(ctx.echo),
        )  # fmt: skip
    except (RunAgentError, ClaudeVersionError, AllocatorError, LockHeldError, RunStoreError, OSError) as exc:
        outcome.status, outcome.error = "error", f"{type(exc).__name__}: {exc}"
        outcome.cost_usd, outcome.run_ids = _agent_cost(repo)
        return outcome
    outcome.cost_usd, outcome.run_ids = _agent_cost(repo)
    if record.worktree_path:
        outcome.tree = Path(record.worktree_path)
    outcome.tokens, outcome.tokens_note = _sum_usage([result])
    outcome.retries = 0
    outcome.interventions = 0
    outcome.notes.append(f"claude -p {result.status} in {result.num_turns or 0} turn(s), {result.tool_calls} tool call(s)")
    if result.status == "budget_exceeded":
        outcome.status = "aborted"
        outcome.abort_reason = f"per-task budget: claude stopped at --max-budget-usd {cap:.2f} (${outcome.cost_usd:.4f})"
    elif result.status == "timeout":
        outcome.status, outcome.abort_reason = "aborted", f"timeout: killed after {ctx.timeout_s:g} s"
    return outcome


# --- treatment: the bug-fix workflow ----------------------------------------------------------


@dataclass
class CappedExecutor:
    """Wraps the workflow's executor so the whole run, every agent call together, stays under one cap."""

    inner: DefaultExecutor
    cap_usd: float
    spent: float = 0.0
    aborted: str | None = None

    def unavailable(self, dag: Any) -> list[str]:
        return self.inner.unavailable(dag)

    def __call__(self, node: DagNode, given: NodeInput) -> NodeResult:
        if node.agent is not None:
            remaining = self.cap_usd - self.spent
            if remaining < MIN_CALL_USD:
                self.aborted = (
                    f"per-task budget: ${self.spent:.4f} of ${self.cap_usd:.2f} spent before {given.state} "
                    f"(attempt {given.attempt}); less than ${MIN_CALL_USD:.2f} left for agent {node.agent}"
                )
                return NodeResult(blocked=self.aborted)
            self.inner.max_budget_usd = remaining
        result = self.inner(node, given)
        self.spent += result.cost_usd
        return result


def run_treatment(loaded: LoadedTask, repo: Path, cell_dir: Path, cap: float, ctx: ArmContext) -> ArmOutcome:
    """The bug-fix workflow on the checkout, the cap shared by its agent calls; no PR is attempted (no gh)."""
    assert ctx.claude is not None
    task = loaded.task
    outcome = ArmOutcome(status="completed", tree=repo, cost_usd=None, model=treatment_models(ctx.registry_path))
    results: list[AdapterResult] = []
    built: list[CappedExecutor] = []

    def factory(canonical: Canonical) -> CappedExecutor:
        inner = DefaultExecutor(
            repo=repo, canonical_dir=ctx.canonical_dir, canonical=canonical, claude=ctx.claude, max_budget_usd=cap,
            timeout_s=ctx.timeout_s, registry_path=ctx.registry_path,
            test_commands=(task.test_command,) if task.test_command else (),
            gh=None,  # the task repository has no remote; the eval never opens a PR
            environment_name=EVAL_ENVIRONMENT, echo=_indent(ctx.echo), observe=lambda _name, r: results.append(r),
        )  # fmt: skip
        built.append(CappedExecutor(inner, cap))
        return built[-1]

    try:
        record = run_workflow(repo, ctx.canonical_dir, WORKFLOW, task.instruction, executor_factory=factory,
                              echo=_indent(ctx.echo))  # fmt: skip
    except (OrchestratorError, RunAgentError, ClaudeVersionError, AllocatorError, LockHeldError, RunStoreError,
            OSError) as exc:  # fmt: skip
        outcome.status, outcome.error = "error", f"{type(exc).__name__}: {exc}"
        outcome.cost_usd, outcome.run_ids = _agent_cost(repo)
        return outcome
    outcome.cost_usd, outcome.run_ids = _agent_cost(repo)
    if record.worktree_path:
        outcome.tree = Path(record.worktree_path)
    store = RunStore.open(repo, create=False)
    assert store is not None
    with store:
        transitions = store.transitions(record.run_id)
    outcome.retries = sum(1 for t in transitions if t.from_state == "CODE_REVIEW" and t.to_state == "IMPLEMENTATION")
    outcome.interventions = sum(1 for t in transitions if t.to_state == "RESUME")
    outcome.tokens, outcome.tokens_note = _sum_usage(results)
    outcome.notes.append(f"workflow {record.outcome} in {record.state} after {len(transitions)} transition(s)")
    capped = built[-1] if built else None
    over = [r for r in results if r.status == "budget_exceeded"]
    if capped is not None and capped.aborted:
        outcome.status, outcome.abort_reason = "aborted", capped.aborted
    elif over:
        outcome.status = "aborted"
        outcome.abort_reason = f"per-task budget: an agent call stopped at its share of the ${cap:.2f} cap"
    return outcome


ARM_RUNNERS: dict[ArmName, Callable[[LoadedTask, Path, Path, float, ArmContext], ArmOutcome]] = {
    "baseline-a": run_baseline_a,
    "baseline-b": run_baseline_b,
    "treatment": run_treatment,
}


# --- the probe: does a sandboxed Claude worker have a shell? -------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    passed: bool
    reason: str
    cost_usd: float
    run_id: str | None
    model: str


def probe(ctx: ArmContext, work: Path) -> ProbeResult:
    """The live reviewer test, in-process: the reviewer, on the cheap class, must run Bash inside srt.

    Same assertions as tests/test_live.py; catches a worker without a shell (the step 11
    cgroup failure) before the first paid cell. Spends at most PROBE_BUDGET_USD.
    """
    assert ctx.claude is not None
    registry = load_registry(ctx.registry_path)
    model = resolve_model(registry, "cheap", runtime="claude").model
    repo = work / "probe-repo"
    repo.mkdir(parents=True)
    git(repo, "init", "--quiet", "-b", "main")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=agent-team eval", "-c", "user.email=eval@agent-team.invalid", "commit", "--quiet", "-m", "add calc.add")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    with (repo / ".git" / "info" / "exclude").open("a", encoding="utf-8") as f:
        f.write("\n.agent-team/\n")
    try:
        record, result = run_agent(
            repo, ctx.canonical_dir, "independent-reviewer", PROBE_PROMPT, model=model, registry_path=ctx.registry_path,
            claude=ctx.claude, max_budget_usd=PROBE_BUDGET_USD, timeout_s=PROBE_TIMEOUT_S,
            environment_name=EVAL_ENVIRONMENT, echo=_indent(ctx.echo),
        )  # fmt: skip
    except (RunAgentError, ClaudeVersionError, AllocatorError, LockHeldError, RunStoreError, TaskError, OSError) as exc:
        cost, _ = _agent_cost(repo)
        return ProbeResult(False, f"{type(exc).__name__}: {exc}", cost, None, model)
    cost, _ = _agent_cost(repo)
    store = RunStore.open(repo, create=False)
    assert store is not None
    with store:
        calls = store.tool_calls(record.run_id)
        transitions = store.transitions(record.run_id)
    sandboxed = len(transitions) > 1 and "srt sandbox" in (transitions[1].note or "")
    bash = [c.result_status for c in calls if c.tool_name == "Bash"]
    if result.status != "succeeded":
        reason = f"the reviewer did not succeed ({result.status}: {result.reason or result.subtype or result.stderr_tail})"
    elif not sandboxed:
        reason = "the reviewer did not run inside srt"
    elif "ok" not in bash:
        reason = f"no Bash call succeeded inside srt (Bash results: {bash or 'none'}); the worker has no shell"
    else:
        return ProbeResult(True, f"Bash ran inside srt ({bash.count('ok')} call(s))", cost, record.run_id, model)
    return ProbeResult(False, reason, cost, record.run_id, model)
