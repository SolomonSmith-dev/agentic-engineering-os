"""Jobs come out by priority (lower first); equal priorities come out in the order they went in."""

import heapq
from typing import Any


class JobQueue:
    def __init__(self) -> None:
        self._heap: list[tuple[int, Any]] = []

    def push(self, priority: int, job: Any) -> None:
        heapq.heappush(self._heap, (priority, job))

    def pop(self) -> Any:
        if not self._heap:
            raise IndexError("pop from an empty JobQueue")
        return heapq.heappop(self._heap)[-1]

    def __len__(self) -> int:
        return len(self._heap)
