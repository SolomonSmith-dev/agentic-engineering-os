"""agent_team.environments: worktree allocator, run lock and srt wrapper (build-order step 5)."""

from agent_team.environments.lock import LockHeldError, LockInfo, acquire, live_run_id, read_lock, release
from agent_team.environments.ports import NoFreeSlotError
from agent_team.environments.sandbox import UNSANDBOXED_CEILING, Sandbox, effective_ceiling, srt_path
from agent_team.environments.worktree import (
    AllocatorError,
    Environment,
    SweepResult,
    WorktreeMeta,
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
    "LockInfo",
    "NoFreeSlotError",
    "Sandbox",
    "SweepResult",
    "WorktreeMeta",
    "acquire",
    "create",
    "dispose",
    "effective_ceiling",
    "list_worktrees",
    "live_run_id",
    "read_lock",
    "release",
    "srt_path",
    "sweep",
]
