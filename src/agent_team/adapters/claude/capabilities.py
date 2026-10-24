"""Canonical capabilities -> Claude Code tools and permission rules.

A capability absent from an agent's allow list does not exist for that agent:
the session runs in `dontAsk`, so any call no allow rule covers is denied.
Each capability also has a minimum autonomy level; a capability above the
run's ceiling is dropped and reported, never silently granted.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from agent_team.schemas.common import AUTONOMY_ORDER, Autonomy

# capability -> (minimum autonomy, permission allow rules)
CAPABILITY_RULES: dict[str, tuple[Autonomy, tuple[str, ...]]] = {
    "fs.read": ("A0", ("Read", "Glob", "Grep")),
    "git.read": (
        "A0",
        ("Bash(git status *)", "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)", "Bash(git blame *)"),
    ),
    "shell.read": ("A0", ("Bash(ls *)", "Bash(pwd)", "Bash(wc *)", "Bash(head *)", "Bash(tail *)")),
    "fs.write.workspace": ("A2", ("Edit", "Write")),
    "shell.execute.workspace": ("A2", ("Bash",)),
    "test.run": ("A2", ()),  # rules come from the repo's detected test commands
    "git.commit": ("A3", ("Bash(git add *)", "Bash(git commit *)")),
}

# Capabilities the orchestrator exercises itself, outside the worker: the agent never gets them.
ORCHESTRATOR_CAPABILITIES: tuple[str, ...] = ("git.write.branch", "github.pr.create", "github.pr.review")

# Commands denied in every compiled session, whatever the agent asks for. Each compiles to an
# exact rule and a prefix rule, because `Bash(git push *)` alone may not cover a bare `git push`.
DENIED_COMMANDS: tuple[str, ...] = (
    "git push",
    "git branch -d",
    "git branch -D",
    "git reset --hard",
    "git config",
)

ALWAYS_DENY: tuple[str, ...] = (
    "WebFetch",
    "WebSearch",
    *(rule for cmd in DENIED_COMMANDS for rule in (f"Bash({cmd})", f"Bash({cmd} *)")),
)

# Rules that a denied capability pattern adds to the deny list (defense in depth on top of dontAsk).
DENY_RULES: dict[str, tuple[str, ...]] = {
    "fs.write.workspace": ("Edit", "Write", "NotebookEdit"),
    "git.commit": ("Bash(git commit)", "Bash(git commit *)", "Bash(git add *)"),
    "shell.execute.workspace": (),
}


def matches(pattern: str, capability: str) -> bool:
    """`fs.write.*` matches `fs.write.workspace`; anything else matches exactly."""
    if pattern.endswith(".*"):
        return capability.startswith(pattern[:-1])
    return pattern == capability


def _denied(capability: str, deny: Iterable[str]) -> bool:
    return any(matches(p, capability) for p in deny)


def _tool_name(rule: str) -> str:
    return rule.split("(", 1)[0]


@dataclass(frozen=True)
class Permissions:
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    granted: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)  # "capability: reason"


def resolve(
    allow: Sequence[str],
    deny: Sequence[str],
    ceiling: Autonomy,
    *,
    test_commands: Sequence[str] = (),
) -> Permissions:
    """Permission rules for one agent at one autonomy ceiling."""
    allow_rules: list[str] = []
    granted: list[str] = []
    dropped: list[str] = []
    for capability in allow:
        if capability in ORCHESTRATOR_CAPABILITIES:
            dropped.append(f"{capability}: exercised by the orchestrator, not the agent")
            continue
        if capability not in CAPABILITY_RULES:
            dropped.append(f"{capability}: no Claude Code mapping")
            continue
        if _denied(capability, deny):
            dropped.append(f"{capability}: denied by policy")
            continue
        minimum, rules = CAPABILITY_RULES[capability]
        if AUTONOMY_ORDER[minimum] > AUTONOMY_ORDER[ceiling]:
            dropped.append(f"{capability}: needs {minimum}, ceiling is {ceiling}")
            continue
        if capability == "test.run":
            rules = tuple(r for cmd in test_commands for r in (f"Bash({cmd})", f"Bash({cmd} *)"))
            if not rules:
                dropped.append("test.run: no test command detected")
                continue
        granted.append(capability)
        allow_rules.extend(r for r in rules if r not in allow_rules)

    deny_rules = list(ALWAYS_DENY)
    for capability, rules in DENY_RULES.items():
        if capability not in granted:
            deny_rules.extend(r for r in rules if r not in deny_rules)
    tools = sorted({_tool_name(r) for r in allow_rules})
    return Permissions(allow=allow_rules, deny=deny_rules, tools=tools, granted=granted, dropped=dropped)
