"""Canonical capabilities -> a Codex sandbox mode, and what Codex cannot enforce.

Codex has no per-tool or per-command permission rules: `codex exec` gives the
model a shell and an edit tool, bounded by one `sandbox_mode`. So a capability
here does not become an allow rule; the set of granted capabilities picks the
narrowest sandbox mode that serves them (`read-only` unless something needs to
write). Capabilities above the run's ceiling, or denied by policy, are dropped
and reported, as in the Claude adapter.

What Codex lacks, or what this adapter does not use, is listed in
`UNAVAILABLE` and goes into the run record (architecture section 7: nothing is
faked).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from agent_team.adapters.claude.capabilities import ORCHESTRATOR_CAPABILITIES, matches
from agent_team.schemas.common import AUTONOMY_ORDER, Autonomy

SandboxMode = Literal["read-only", "workspace-write"]

# capability -> (minimum autonomy, whether it needs a writable workspace)
CAPABILITY_MODES: dict[str, tuple[Autonomy, bool]] = {
    "fs.read": ("A0", False),
    "git.read": ("A0", False),
    "shell.read": ("A0", False),
    "test.run": ("A2", False),  # named in the prompt; a test that writes fails under read-only
    "fs.write.workspace": ("A2", True),
    "shell.execute.workspace": ("A2", True),
    "git.commit": ("A3", True),
}

# Runtime capabilities (section 7 table) this adapter runs without, each with what stands in for it.
UNAVAILABLE: tuple[str, ...] = (
    "hooks: not used; protected paths are srt denyRead rules instead of a PreToolUse hook",
    "per-command rules: none in Codex; the sandbox mode bounds every shell command instead",
    "cost report: Codex reports tokens, not dollars; cost is estimated only from a registry price",
    "budget cap: no Codex flag; the timeout bounds the run and an estimate is checked after it",
    "fallback models: this adapter emits no fallback chain",
)


@dataclass(frozen=True)
class CodexPermissions:
    sandbox_mode: SandboxMode
    granted: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)  # "capability: reason"


def resolve(
    allow: Sequence[str],
    deny: Sequence[str],
    ceiling: Autonomy,
    *,
    test_commands: Sequence[str] = (),
) -> CodexPermissions:
    """The sandbox mode and granted capabilities for one agent at one autonomy ceiling."""
    granted: list[str] = []
    dropped: list[str] = []
    writes = False
    for capability in allow:
        if capability in ORCHESTRATOR_CAPABILITIES:
            dropped.append(f"{capability}: exercised by the orchestrator, not the agent")
            continue
        if capability not in CAPABILITY_MODES:
            dropped.append(f"{capability}: no Codex mapping")
            continue
        if any(matches(p, capability) for p in deny):
            dropped.append(f"{capability}: denied by policy")
            continue
        minimum, needs_write = CAPABILITY_MODES[capability]
        if AUTONOMY_ORDER[minimum] > AUTONOMY_ORDER[ceiling]:
            dropped.append(f"{capability}: needs {minimum}, ceiling is {ceiling}")
            continue
        if capability == "test.run" and not test_commands:
            dropped.append("test.run: no test command detected")
            continue
        granted.append(capability)
        writes = writes or needs_write
    return CodexPermissions(sandbox_mode="workspace-write" if writes else "read-only", granted=granted, dropped=dropped)
