"""`agent-team inspect`: the archaeologist at A0 builds repository memory (build-order step 9).

    refuse unless .agent-team/ is gitignored -> commands from config.yaml or proposal.yaml
      -> run_agent(repository-archaeologist)          the agent reads; it writes nothing
      -> after it succeeds, still in its run:
           verify each detected command inside srt   (or mark it unverified, with the reason)
           record each one as an `inspect.verify` tool call
           write .agent-team/memory/repo/*.yaml       (agent-team writes, never the agent)

Memory is one YAML file per concern plus `index.yaml`. The archaeologist's
output and every command's output are redacted through the policy before they
are written. Commands only the archaeologist proposed are listed as
unverified and never run: what runs comes from `init`'s deterministic
discovery, not from a model that has read untrusted repository content.
"""

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agent_team.bootstrap.configure import CONFIG_NAME, load_proposal
from agent_team.bootstrap.discovery import PROPOSAL_NAME, BootstrapError
from agent_team.memory.verify import (
    DEFAULT_COMMAND_TIMEOUT_S,
    Candidate,
    protect,
    record_verifications,
    sandbox_refusal,
    verify_commands,
)
from agent_team.run_agent import AgentRun, run_agent
from agent_team.runs.redact import redact_value
from agent_team.runs.render import safe
from agent_team.runs.store import STATE_DIR, check_state_dir_ignored, repo_root
from agent_team.schemas.adapter import AdapterResult, ArchitectureMap, RepositoryMemory
from agent_team.schemas.bootstrap import COMMAND_KINDS, Proposal, RepoConfig
from agent_team.schemas.memory import VERIFICATION_STATUSES, CommandVerification, MemoryIndex
from agent_team.schemas.runs import RunRecord

ARCHAEOLOGIST = "repository-archaeologist"
MEMORY_DIR = Path(STATE_DIR) / "memory" / "repo"
INDEX_NAME = "index.yaml"
CONCERNS: dict[str, str] = {
    "architecture": "architecture.yaml",
    "ownership": "ownership.yaml",
    "commands": "commands.yaml",
    "conventions": "conventions.yaml",
    "pitfalls": "pitfalls.yaml",
}
HEADER = "# agent-team repository memory, written by `agent-team inspect` (run {run_id}). Re-run inspect instead of editing.\n"

ARCHAEOLOGIST_ONLY = "proposed by the archaeologist, not detected by init; only init's commands are run"


class InspectError(RuntimeError):
    """inspect cannot proceed safely, or there is no memory to show; the message says what to fix."""


@dataclass
class InspectOutcome:
    record: RunRecord
    result: AdapterResult
    written: list[Path] = field(default_factory=list)


def memory_dir(root: Path) -> Path:
    """`.agent-team/memory/repo/` in `root`; refuses when any part of it is a symlink."""
    path = root / MEMORY_DIR
    if path.resolve() != path:
        raise InspectError(f"{MEMORY_DIR} in {root} is a symlink or sits under one; agent-team state must be a real directory")
    return path


def load_candidates(root: Path) -> tuple[list[Candidate], Proposal | None]:
    """init's commands: from the approved config.yaml when there is one, else from proposal.yaml.

    Also returns the proposal, when one is readable, for the archaeologist's prompt.
    """
    try:
        proposal: Proposal | None = load_proposal(root)
    except BootstrapError:
        proposal = None
    config_path = root / STATE_DIR / CONFIG_NAME
    if config_path.is_file():
        try:
            config = RepoConfig.model_validate(yaml.safe_load(config_path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
            raise InspectError(f"{STATE_DIR}/{CONFIG_NAME} is unreadable or invalid; re-run 'agent-team configure': {exc}") from exc
        found = [
            Candidate(kind, command, f"{CONFIG_NAME} ({'verified' if ok else 'unverified'} by init)", ok)
            for kind in COMMAND_KINDS
            for ok, commands in ((True, config.commands), (False, config.unverified_commands))
            for command in commands.get(kind, [])
        ]
        return found, proposal
    if proposal is None:
        raise InspectError(f"no {STATE_DIR}/{PROPOSAL_NAME} or {STATE_DIR}/{CONFIG_NAME} in {root}; run 'agent-team init' first")
    return [Candidate(c.kind, c.command, c.source, c.verified) for c in proposal.commands], proposal


def build_prompt(candidates: list[Candidate], proposal: Proposal | None) -> str:
    """The task for the archaeologist: what init found, and which commands agent-team will run."""
    lines = ["Inspect this repository and return its repository memory.", "", "## Detected commands", ""]
    if candidates:
        lines.append("agent-team runs these inside the sandbox after you finish; you do not run them.")
        lines += [f"- {c.kind}: `{c.command}` ({c.source})" for c in candidates]
    else:
        lines.append("init detected none; list what the repository declares, if anything.")
    if proposal is not None:
        lines += ["", "## Found by agent-team init", ""]
        lines.append("- CI: " + (", ".join(proposal.ci.files) or "none"))
        lines.append("- Instruction files: " + (", ".join(proposal.instruction_files) or "none"))
        lines.append(f"- CODEOWNERS: {proposal.codeowners or 'none'}")
        lines.append(f"- Default branch: {proposal.vcs.default_branch}")
    return "\n".join(lines) + "\n"


def merge_commands(verified: list[CommandVerification], memory: RepositoryMemory) -> list[CommandVerification]:
    """init's commands with their results, then any command only the archaeologist found, unverified."""
    known = {v.command for v in verified}
    extra = [
        CommandVerification(kind=c.kind, command=c.command, source=c.source, detected_by="archaeologist",
                            status="unverified", reason=ARCHAEOLOGIST_ONLY)  # fmt: skip
        for c in memory.commands
        if c.command not in known
    ]
    return [*verified, *extra]


def _write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _dump(run_id: str, data: Any) -> str:
    return HEADER.format(run_id=run_id) + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def write_memory(
    root: Path,
    *,
    run_id: str,
    base_commit: str,
    memory: RepositoryMemory,
    commands: list[CommandVerification],
    sandboxed: bool,
    redaction: tuple[str, ...],
) -> list[Path]:
    """Write one file per concern and the index under `.agent-team/memory/repo/`. Returns the paths."""
    directory = memory_dir(root)
    data = redact_value(memory.model_dump(mode="json"), redaction)
    contents: dict[str, Any] = {
        "architecture": data["architecture"],
        "ownership": {"ownership": data["ownership"]},
        "commands": {"commands": [c.model_dump(mode="json") for c in commands]},  # outputs are redacted already
        "conventions": {"conventions": data["conventions"]},
        "pitfalls": {"pitfalls": data["pitfalls"]},
    }
    counts = {s: n for s in VERIFICATION_STATUSES if (n := sum(c.status == s for c in commands))}
    index = MemoryIndex(
        run_id=run_id,
        generated_at=datetime.now(UTC).replace(microsecond=0),
        base_commit=base_commit,
        sandbox="srt" if sandboxed else "none",
        files=dict(CONCERNS),
        commands=counts,
    )
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for concern, name in CONCERNS.items():
        _write(directory / name, _dump(run_id, contents[concern]))
        written.append(directory / name)
    _write(directory / INDEX_NAME, _dump(run_id, index.model_dump(mode="json")))
    written.append(directory / INDEX_NAME)
    return written


def inspect_repo(
    repo: Path,
    canonical_dir: Path,
    *,
    claude: str,
    registry_path: Path | None = None,
    max_budget_usd: float,
    timeout_s: float,
    command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    echo: Callable[[str], None] = print,
) -> InspectOutcome:
    """Run the archaeologist, verify init's commands in its sandbox, and write repository memory."""
    root = repo_root(repo).resolve()
    check_state_dir_ignored(root)  # before reading or running anything
    if (root / STATE_DIR).is_symlink():
        raise InspectError(f"{STATE_DIR} in {root} is a symlink; agent-team state must be a real directory")
    memory_dir(root)
    candidates, proposal = load_candidates(root)
    written: list[Path] = []

    def after(run: AgentRun) -> None:
        memory = RepositoryMemory.model_validate(run.result.structured_output)
        redaction = tuple(r.pattern for r in run.policy.redaction)
        sandbox = run.environment.sandbox
        refusal = sandbox_refusal(sandbox)
        if refusal is None:
            protect(sandbox, run.policy, root=root, worktree=run.environment.path, home=Path.home())
        else:
            echo(f"commands unverified: {refusal}")
        verified = verify_commands(
            candidates,
            sandbox=sandbox,
            cwd=run.environment.path,
            timeout_s=command_timeout_s,
            redaction=redaction,
            base_env=dict(os.environ),
            echo=echo,
        )
        commands = merge_commands(verified, memory)
        record_verifications(run.store, run.run_id, commands)
        written.extend(
            write_memory(
                root,
                run_id=run.run_id,
                base_commit=run.environment.meta.base_commit,
                memory=memory,
                commands=commands,
                sandboxed=refusal is None,
                redaction=redaction,
            )
        )

    record, result = run_agent(
        root,
        canonical_dir,
        ARCHAEOLOGIST,
        build_prompt(candidates, proposal),
        registry_path=registry_path,
        claude=claude,
        max_budget_usd=max_budget_usd,
        timeout_s=timeout_s,
        after=after,
        echo=echo,
    )
    return InspectOutcome(record, result, written)


# Reading memory back: `agent-team inspect --summary`

_MARKS = {"passed": "✓", "failed": "✗", "timeout": "✗", "unverified": "-"}


def _load(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InspectError(f"{path.name} is missing from {path.parent}; re-run 'agent-team inspect'") from exc
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise InspectError(f"cannot read {path}: {str(exc).splitlines()[0]}") from exc


def render_memory_summary(repo: Path) -> str:
    """The architecture map and every command with its verification result, from the memory on disk."""
    root = repo_root(repo).resolve()
    directory = memory_dir(root)
    if not (directory / INDEX_NAME).is_file():
        raise InspectError(f"no repository memory in {root}; run 'agent-team inspect' first")
    try:
        index = MemoryIndex.model_validate(_load(directory / INDEX_NAME))
        architecture = ArchitectureMap.model_validate(_load(directory / index.files["architecture"]))
        commands = [CommandVerification.model_validate(c) for c in _load(directory / index.files["commands"])["commands"]]
    except (ValidationError, KeyError, TypeError) as exc:
        raise InspectError(f"repository memory in {directory} is invalid; re-run 'agent-team inspect': {exc}") from exc

    lines = [
        f"repository memory  {MEMORY_DIR}/  (run {index.run_id}, {index.generated_at:%Y-%m-%d %H:%M} UTC, "
        f"base {index.base_commit[:12]})",
        "",
        "map",
        f"  {safe(architecture.summary)}",
    ]
    width = max(len(safe(m.path)) for m in architecture.modules)
    for module in architecture.modules:
        lines.append(f"  {safe(module.path):<{width}}  {safe(module.purpose)}")
    lines += ["", "commands  " + ("(run inside srt)" if index.sandbox == "srt" else "(not run: no srt)")]
    if not commands:
        lines.append("  none detected")
    for c in commands:
        if c.status == "unverified":
            detail = c.reason or ""
        else:
            detail = f"exit {c.exit_code}" if c.exit_code is not None else (c.reason or "")
            detail += f", {(c.duration_ms or 0) / 1000:.1f}s"
        lines.append(f"  {_MARKS[c.status]} {c.status:<10} {safe(c.kind):<9} {safe(c.command)}  ({safe(detail)})")
    return "\n".join(lines)
