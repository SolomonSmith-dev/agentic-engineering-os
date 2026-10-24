"""`agent-team configure`: approved proposal -> config.yaml and runtime files (build-order step 8).

Configure plans every write first, shows the plan as a unified diff, and writes
only the files in that diff, only inside the repository it is pointed at:

    .agent-team/config.yaml     the approved proposal, reduced to what runs read
    CLAUDE.md, AGENTS.md        extended with one marked block, never replaced
    .claude/agents/<name>.md    each canonical agent compiled to a native subagent

An existing instruction file keeps every byte outside the block; re-running
updates the block in place and changes nothing when nothing changed. A path
that is a symlink, or sits under one, is left untouched, so a `.claude` linked
to `~/.claude` is never written. `.claude/settings.json` is never written:
runs reach Claude Code's settings only through compiled flags (step 6).
"""

import difflib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from agent_team.adapters.claude.compile import compile_agent
from agent_team.bootstrap.discovery import PROPOSAL_NAME, BootstrapError, check_state_file_ignored
from agent_team.routing import RegistryError, default_registry_path, load_registry, resolve_model
from agent_team.run_agent import RunAgentError, load_canonical, requested_autonomy
from agent_team.runs.store import STATE_DIR, repo_root
from agent_team.schemas.bootstrap import COMMAND_KINDS, Proposal, RepoConfig

CONFIG_NAME = "config.yaml"
RUNTIME = "claude"

BEGIN_MARKER = "<!-- agent-team:begin -->"
END_MARKER = "<!-- agent-team:end -->"
AGENT_MARKER = "<!-- agent-team:managed (written by agent-team configure; re-run it instead of editing this file) -->"

# A CLAUDE.md line that imports AGENTS.md, so the block in AGENTS.md already reaches Claude Code.
_IMPORTS_AGENTS_MD = re.compile(r"^\s*@(?:\./)?AGENTS\.md\s*$", re.MULTILINE)


@dataclass(frozen=True)
class PlannedWrite:
    path: str  # relative to the repository root
    before: str | None  # None: the file does not exist yet
    after: str

    def diff(self) -> str:
        lines = difflib.unified_diff(
            (self.before or "").splitlines(keepends=True),
            self.after.splitlines(keepends=True),
            fromfile=f"a/{self.path}" if self.before is not None else "/dev/null",
            tofile=f"b/{self.path}",
        )
        return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


@dataclass
class Plan:
    root: Path
    config: RepoConfig
    writes: list[PlannedWrite] = field(default_factory=list)  # only files whose content changes
    notes: list[str] = field(default_factory=list)

    def diff(self) -> str:
        return "".join(w.diff() for w in self.writes)


def load_proposal(root: Path) -> Proposal:
    path = root / STATE_DIR / PROPOSAL_NAME
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BootstrapError(f"no {STATE_DIR}/{PROPOSAL_NAME} in {root}; run 'agent-team init' first") from exc
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BootstrapError(f"cannot read {path}: {str(exc).splitlines()[0]}") from exc
    try:
        return Proposal.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "<root>"
        raise BootstrapError(f"{STATE_DIR}/{PROPOSAL_NAME} is invalid: {where}: {first['msg']}") from exc


def build_config(proposal: Proposal) -> RepoConfig:
    commands: dict[str, list[str]] = {}
    unverified: dict[str, list[str]] = {}
    for command in proposal.commands:
        (commands if command.verified else unverified).setdefault(command.kind, []).append(command.command)
    ask = proposal.ask
    return RepoConfig(
        default_branch=proposal.vcs.default_branch,
        protected_branches=proposal.vcs.protected_branches,
        runtimes=ask.runtimes,
        privacy_classes=ask.privacy_classes,
        autonomy_ceiling=ask.autonomy_ceiling,
        draft_prs=ask.draft_prs,
        commands={k: commands[k] for k in COMMAND_KINDS if k in commands},
        unverified_commands={k: unverified[k] for k in COMMAND_KINDS if k in unverified},
    )


def render_block(config: RepoConfig) -> str:
    """The marked block added to instruction files. Facts from the approved proposal, nothing else."""
    protected = config.protected_branches
    shown = ", ".join(f"`{b}`" for b in protected) if isinstance(protected, list) and protected else "unknown"
    pr_rule = (
        "work on a branch and open a draft pull request" if config.draft_prs
        else "work on a branch and leave it for a human; do not open pull requests"
    )  # fmt: skip
    lines = [
        BEGIN_MARKER,
        "## agent-team",
        "",
        "Managed by `agent-team configure` from `.agent-team/proposal.yaml`. Edit the proposal and re-run",
        "`agent-team configure` instead of editing this block; text outside it is never changed.",
        "",
        f"- Default branch `{config.default_branch}`; protected branches: {shown}.",
        f"- Never push to the default branch or a protected branch: {pr_rule}.",
        f"- Autonomy ceiling {config.autonomy_ceiling}.",
        "- `.agent-team/` holds agent-team runtime state and is gitignored; never commit it.",
        "- Never read, print or commit secrets (`.env*`, key files).",
    ]
    if config.commands:
        lines.append("- Commands (tool found by `agent-team init`; nothing was run to check them):")
        lines += [f"  - {kind}: `{cmd}`" for kind, cmds in config.commands.items() for cmd in cmds]
    else:
        lines.append("- No verified build, test, lint or type-check commands.")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def extend_with_block(existing: str | None, block: str) -> str:
    """Append the block, or replace the one already there. Raises on a malformed or repeated block."""
    if existing is None or existing == "":
        return block
    lines = existing.splitlines(keepends=True)
    begins = [i for i, line in enumerate(lines) if line.strip() == BEGIN_MARKER]
    ends = [i for i, line in enumerate(lines) if line.strip() == END_MARKER]
    if not begins and not ends:
        separator = "\n" if existing.endswith("\n") else "\n\n"
        return existing + separator + block
    if len(begins) == 1 and len(ends) == 1 and begins[0] < ends[0]:
        return "".join(lines[: begins[0]]) + block + "".join(lines[ends[0] + 1 :])
    raise BootstrapError(
        f"found {len(begins)} '{BEGIN_MARKER}' and {len(ends)} '{END_MARKER}' line(s); "
        "leave exactly one pair, in order, or none"
    )


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _unlinked(root: Path, rel: str) -> bool:
    """True when no component of `rel` under `root` is a symlink, so the write lands where the diff says."""
    return (root / rel).resolve() == root / rel


def _instruction_targets(root: Path, config: RepoConfig, notes: list[str]) -> list[str]:
    """CLAUDE.md for the Claude runtime, AGENTS.md when it exists. Symlinks are filtered later, unread."""
    claude_md, agents_md = root / "CLAUDE.md", root / "AGENTS.md"
    targets: list[str] = []
    if agents_md.exists() or agents_md.is_symlink():
        targets.append("AGENTS.md")
    if RUNTIME in config.runtimes:
        same_file = claude_md.exists() and agents_md.exists() and claude_md.resolve() == agents_md.resolve()
        text = _read(claude_md) if _unlinked(root, "CLAUDE.md") else None
        if same_file or (text is not None and agents_md.exists() and _IMPORTS_AGENTS_MD.search(text)):
            notes.append("CLAUDE.md reaches AGENTS.md; the block goes in AGENTS.md only")
        else:
            targets.append("CLAUDE.md")
    return targets


def _compile_agents(
    root: Path, config: RepoConfig, canonical_dir: Path, registry_path: Path | None, notes: list[str]
) -> dict[str, str]:
    """Native subagent files for every canonical agent: relative path -> content."""
    try:
        canonical = load_canonical(canonical_dir)
        registry = load_registry(registry_path or default_registry_path(canonical_dir))
    except (RunAgentError, RegistryError) as exc:
        raise BootstrapError(f"{exc}; pass --canonical (and --registry) to point at agent-team's canonical files") from exc
    policy = canonical.one("policy", "default").spec
    allowed = [c for c in config.privacy_classes if policy.allowed_privacy_classes is None or c in policy.allowed_privacy_classes]
    files: dict[str, str] = {}
    for doc in canonical.docs:
        if doc.kind != "agent" or doc.spec is None:
            continue
        agent = doc.spec
        try:
            resolved = resolve_model(registry, agent.model_class, runtime=RUNTIME, allowed_privacy=allowed)
        except RegistryError as exc:
            notes.append(f"agent {agent.name} not compiled: {exc}")
            continue
        policy_ceiling = policy.autonomy_ceiling.get(agent.name, policy.default_autonomy)
        ceiling = requested_autonomy(requested_autonomy(agent.max_autonomy, policy_ceiling), config.autonomy_ceiling)
        skills = [(d.spec, d.body) for d in (canonical.one("skill", s) for s in agent.skills)]
        # compile_agent also writes settings.json and policy.json; those stay in a temp dir outside the repo.
        with tempfile.TemporaryDirectory(prefix="agent-team-configure-") as tmp:
            compiled = compile_agent(
                agent,
                doc.body,
                skills,
                policy,
                ceiling=ceiling,
                model=resolved.model,
                fallback_models=resolved.fallbacks,
                out_dir=Path(tmp),
                roots=[root],
                home=Path.home(),
                test_commands=tuple(config.commands.get("test", [])),
                sandboxed=False,  # these files load in the developer's own Claude Code, which srt does not wrap
            )
            text = compiled.native_md.read_text(encoding="utf-8")
        frontmatter, fence, prompt = text.partition("\n---\n")  # split at the closing fence
        files[f".claude/agents/{agent.name}.md"] = frontmatter + fence + "\n" + AGENT_MARKER + "\n" + prompt
    return files


def plan_configure(
    path: Path,
    canonical_dir: Path,
    *,
    registry_path: Path | None = None,
    require_approved: bool = True,
) -> Plan:
    """Work out every write configure would make. Reads only; writes nothing."""
    root = repo_root(path).resolve()
    check_state_file_ignored(root, CONFIG_NAME)
    proposal = load_proposal(root)
    if require_approved and not proposal.ask.approved:
        raise BootstrapError(
            f"{STATE_DIR}/{PROPOSAL_NAME} is not approved; review it, set 'ask.approved: true', and re-run "
            "(--dry-run shows the diff without approval)"
        )
    config = build_config(proposal)
    plan = Plan(root=root, config=config)
    for runtime in config.runtimes:
        if runtime != RUNTIME:
            plan.notes.append(f"runtime {runtime}: no adapter yet; nothing compiled for it")
    for command in proposal.commands:
        if not command.verified:
            plan.notes.append(f"✗ {command.kind} `{command.command}` left out of config.yaml: {command.check}")

    def linked(rel: str) -> bool:
        # Checked before any read, so a path linked into ~/.claude or another repo is never even opened.
        if _unlinked(root, rel):
            return False
        plan.notes.append(f"{rel} is a symlink or sits under one; left untouched")
        return True

    block = render_block(config)
    wanted: dict[str, str] = {}
    for rel in _instruction_targets(root, config, plan.notes):
        if not linked(rel):
            wanted[rel] = extend_with_block(_read(root / rel), block)

    if RUNTIME in config.runtimes:
        for rel, content in _compile_agents(root, config, canonical_dir, registry_path, plan.notes).items():
            if linked(rel):
                continue
            existing = _read(root / rel)
            if existing is not None and AGENT_MARKER not in existing:
                plan.notes.append(f"{rel} exists and was not written by agent-team; left untouched")
                continue
            wanted[rel] = content
        github_agents = root / ".github" / "agents"
        if github_agents.is_dir() and _unlinked(root, ".github/agents"):
            names = {Path(rel).stem for rel in wanted if rel.startswith(".claude/agents/")}
            for other in sorted(github_agents.iterdir()):
                if other.name.split(".")[0] in names:
                    plan.notes.append(f".github/agents/{other.name} defines the same agent for Copilot; left untouched")

    config = config.model_copy(
        update={
            "instruction_files": [r for r in wanted if not r.startswith(".claude/")],
            "agents": [r for r in wanted if r.startswith(".claude/")],
        }
    )
    plan.config = config
    header = "# agent-team config, written by `agent-team configure` from the approved proposal.yaml.\n"
    wanted[f"{STATE_DIR}/{CONFIG_NAME}"] = header + yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)

    for rel, after in wanted.items():
        if not _unlinked(root, rel):
            raise BootstrapError(f"{rel} is a symlink or sits under one; agent-team state must be a real directory")
        before = _read(root / rel)
        if before != after:
            plan.writes.append(PlannedWrite(path=rel, before=before, after=after))
    return plan


def apply_plan(plan: Plan) -> list[Path]:
    """Write exactly the planned files. Refuses if any changed since the plan was made."""
    for write in plan.writes:
        if not _unlinked(plan.root, write.path) or _read(plan.root / write.path) != write.before:
            raise BootstrapError(f"{write.path} changed since the diff was shown; re-run configure")
    written: list[Path] = []
    for write in plan.writes:
        target = plan.root / write.path
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(write.after)
            os.chmod(tmp, target.stat().st_mode & 0o777 if target.exists() else 0o644)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        written.append(target)
    return written
