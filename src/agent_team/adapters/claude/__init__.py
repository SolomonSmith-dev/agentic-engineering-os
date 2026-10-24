"""agent_team.adapters.claude: compile canonical agents for Claude Code and run them headless (build-order step 6)."""

from agent_team.adapters.claude.capabilities import Permissions, resolve
from agent_team.adapters.claude.compile import CompiledAgent, compile_agent
from agent_team.adapters.claude.invoke import (
    ClaudeVersionError,
    Invocation,
    StreamParser,
    build_invocation,
    check_version,
    execute,
    normalize,
    record,
)

__all__ = [
    "ClaudeVersionError",
    "CompiledAgent",
    "Invocation",
    "Permissions",
    "StreamParser",
    "build_invocation",
    "check_version",
    "compile_agent",
    "execute",
    "normalize",
    "record",
    "resolve",
]
