"""Per-device command queue.

Deliberately factored out of DeviceRecord/Hub so it can be unit-tested in complete
isolation from websockets and asyncio: ordering and drain semantics are the load-bearing
contract for the "queued" tier behavior (design doc §5), and that contract should be
verifiable without spinning up a browser or a network connection.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .addressing import Target


@dataclass(frozen=True, slots=True)
class QueuedCommand:
    id: str
    target: Target
    command: str
    args: dict[str, Any] = field(default_factory=dict)
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class DeviceCommandQueue:
    """FIFO queue of commands waiting for a device to become reachable.

    Commands drain in the order they were enqueued -- never reordered, never
    dropped. `position()` gives a 1-based position so a queued response can tell
    the caller honestly how far back they are (design doc §5: queued state must
    be a real, inspectable state, never a hidden block).
    """

    def __init__(self) -> None:
        self._items: deque[QueuedCommand] = deque()

    def enqueue(self, cmd: QueuedCommand) -> int:
        """Add a command to the tail of the queue. Returns its 1-based position."""
        self._items.append(cmd)
        return len(self._items)

    def position(self, command_id: str) -> int | None:
        """1-based position of a still-queued command, or None if not present."""
        for i, item in enumerate(self._items, start=1):
            if item.id == command_id:
                return i
        return None

    def pop_next(self) -> QueuedCommand | None:
        """Remove and return the oldest queued command, or None if empty."""
        if not self._items:
            return None
        return self._items.popleft()

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def __iter__(self):
        return iter(self._items)
