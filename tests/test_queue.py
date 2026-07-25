"""Queue ordering and drain semantics -- the load-bearing contract for the
'queued' tier behavior (design doc §5: commands must never be reordered or
silently dropped)."""

from __future__ import annotations

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.queue import DeviceCommandQueue, QueuedCommand


def _cmd(cmd_id: str, command: str = "snapshot") -> QueuedCommand:
    return QueuedCommand(id=cmd_id, target=Target(device_id="d1", tab_id=1), command=command)


def test_empty_queue() -> None:
    q = DeviceCommandQueue()
    assert len(q) == 0
    assert not q
    assert q.pop_next() is None
    assert q.position("nope") is None


def test_enqueue_returns_1_based_position() -> None:
    q = DeviceCommandQueue()
    assert q.enqueue(_cmd("a")) == 1
    assert q.enqueue(_cmd("b")) == 2
    assert q.enqueue(_cmd("c")) == 3
    assert len(q) == 3
    assert q


def test_position_tracks_queue_order() -> None:
    q = DeviceCommandQueue()
    q.enqueue(_cmd("a"))
    q.enqueue(_cmd("b"))
    q.enqueue(_cmd("c"))
    assert q.position("a") == 1
    assert q.position("b") == 2
    assert q.position("c") == 3
    assert q.position("missing") is None


def test_drain_is_strict_fifo() -> None:
    q = DeviceCommandQueue()
    ids = ["a", "b", "c", "d"]
    for i in ids:
        q.enqueue(_cmd(i))

    drained = []
    while True:
        item = q.pop_next()
        if item is None:
            break
        drained.append(item.id)

    assert drained == ids  # exact order preserved, nothing dropped
    assert len(q) == 0


def test_position_updates_after_partial_drain() -> None:
    q = DeviceCommandQueue()
    q.enqueue(_cmd("a"))
    q.enqueue(_cmd("b"))
    q.enqueue(_cmd("c"))

    popped = q.pop_next()
    assert popped is not None and popped.id == "a"

    # b is now first in line
    assert q.position("b") == 1
    assert q.position("c") == 2


def test_iteration_does_not_mutate_queue() -> None:
    q = DeviceCommandQueue()
    q.enqueue(_cmd("a"))
    q.enqueue(_cmd("b"))
    seen = [item.id for item in q]
    assert seen == ["a", "b"]
    assert len(q) == 2  # iterating must not drain
