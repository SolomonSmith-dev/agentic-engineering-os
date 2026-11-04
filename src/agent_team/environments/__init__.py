"""agent_team.environments: worktree allocator, run lock and srt wrapper (build-order step 5)."""

from agent_team.environments.locks import LockEntry, LockHeldError, acquire, live_run_ids, read_table, release
from agent_team.environments.ports import NoFreeSlotError
from agent_team.environments.sandbox import UNSANDBOXED_CEILING, Sandbox, effective_ceiling, srt_path
from agent_team.environments.worktree import (
    AllocatorError,
    Environment,
    SweepResult,
    WorktreeMeta,
    attach,
    create,
    dispose,
    list_worktrees,
    sweep,
)

__all__ = [
    "UNSANDBOXED_CEILING",
    "AllocatorError",
    "Environment",
    "LockHeldError",
    "LockEntry",
    "NoFreeSlotError",
    "Sandbox",
    "SweepResult",
    "WorktreeMeta",
    "acquire",
    "attach",
    "create",
    "dispose",
    "effective_ceiling",
    "list_worktrees",
    "live_run_ids",
    "read_table",
    "release",
    "srt_path",
    "sweep",
]
