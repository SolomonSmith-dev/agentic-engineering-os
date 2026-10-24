"""Port offset per worktree: slot 0 is the main checkout, worktrees take the lowest free slot >= 1.

A worktree's dev servers add AGENT_TEAM_PORT_OFFSET (slot * block_size) to their
usual ports, so two worktrees never fight over :3000.
"""

from collections.abc import Iterable

MAX_SLOTS = 100
# Highest offset handed out, so a base port up to ~5000 plus the offset stays under 65536.
MAX_OFFSET = 60000


class NoFreeSlotError(RuntimeError):
    """Every port slot is taken; sweep stale worktrees first."""


def slot_limit(block_size: int) -> int:
    return min(MAX_SLOTS, MAX_OFFSET // block_size)


def next_slot(taken: Iterable[int], block_size: int) -> int:
    used = set(taken)
    limit = slot_limit(block_size)
    for slot in range(1, limit + 1):
        if slot not in used:
            return slot
    raise NoFreeSlotError(f"all {limit} port slots are taken; run 'agent-team env sweep'")


def offset(slot: int, block_size: int) -> int:
    return slot * block_size
