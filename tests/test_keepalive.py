"""Tests for the hub's keepalive sweep (`Hub.keepalive_sweep`) -- the active
dead-socket DETECTION mechanism this fix adds.

Before this fix, a stale-but-open socket (the airplane-mode failure mode --
see tiers.py's module docstring) was only ever DISTRUSTED: `compute_tier`
demoted it out of `Tier.LIVE` once it had gone silent past
`LIVE_SILENCE_TIMEOUT_SECONDS`, but the socket itself stayed bound in the
registry indefinitely, and the documented hub-side keepalive ping
(docs/PROTOCOL.md) was never implemented. This test exercises the real
`/device` WebSocket route via an aiohttp `TestServer` (same technique
test_kill_switch.py uses) so the proactive close flows through
`_handle_device_ws`'s own (race-guarded -- see test_reconnect_race.py)
post-loop cleanup exactly as a real disconnect would.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient as AiohttpTestClient
from aiohttp.test_utils import TestServer

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.hub import Hub
from amplifier_browser_bridge.tiers import LIVE_SILENCE_TIMEOUT_SECONDS, Tier


def _hub(tmp_path: Path) -> Hub:
    return Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"))


def _audit_events(tmp_path: Path) -> list[str]:
    text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    return [json.loads(line)["event"] for line in text.splitlines() if line.strip()]


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.02) -> None:
    """Poll-don't-sleep: wait until `predicate()` is true or raise on timeout.

    The proactive close in `keepalive_sweep` only requests the close --
    the real `_handle_device_ws` coroutine that owns the connection notices
    and runs its own cleanup asynchronously, on its own schedule. A fixed
    sleep would be both slower than necessary and, occasionally, too short.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition never became true within {timeout}s")


@pytest.mark.asyncio
async def test_keepalive_sweep_closes_silent_connection_which_then_queues_commands(
    tmp_path: Path,
) -> None:
    """The actual detection fix: a connection that has gone silent past
    LIVE_SILENCE_TIMEOUT_SECONDS -- i.e. has stopped responding to the hub's
    own ping -- is proactively closed (not merely reclassified), unbinds
    through the exact same code path a real disconnect uses, and a command
    subsequently targeting that device queues instead of dispatching into
    dead air."""
    hub = _hub(tmp_path)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        device_ws = await client.ws_connect("/device")
        await device_ws.send_json({"v": 1, "id": "1", "type": "hello", "device_id": "d1", "token": None})

        record = hub.registry.get_or_create("d1")
        await _wait_until(lambda: record.connected)
        assert record.tier is Tier.LIVE

        # Simulate the failure mode this sweep exists to catch: the device
        # has gone silent well past the threshold, but nothing has told the
        # hub's socket it's gone (this test's fake "device" -- device_ws --
        # simply never replies to the ping the sweep is about to send,
        # exactly like a radio that's gone dark mid-connection).
        record.last_seen = datetime.now(UTC) - timedelta(seconds=LIVE_SILENCE_TIMEOUT_SECONDS + 1)

        closed = await hub.keepalive_sweep()
        assert closed == ["d1"]

        # `_handle_device_ws`'s own loop notices the close and runs its
        # (race-guarded) cleanup asynchronously -- poll for it.
        await _wait_until(lambda: not record.connected)
        assert record.tier is not Tier.LIVE
        assert record.tier is Tier.INTERMITTENT  # 61s silent, still well under the 150s dormant line

        result = await hub.send_command(Target(device_id="d1", tab_id=1), "tabs", {})
        assert result["status"] == "queued"
        assert len(record.queue) == 1

        events = _audit_events(tmp_path)
        assert "keepalive_timeout_closing" in events
        assert "device_disconnected" in events
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_keepalive_sweep_pings_a_recently_seen_device_without_closing_it(
    tmp_path: Path,
) -> None:
    """Sanity complement: a device heard from recently must be pinged, not
    closed -- the sweep must not tear down every connected device
    indiscriminately, only ones that have actually gone silent."""
    hub = _hub(tmp_path)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        device_ws = await client.ws_connect("/device")
        await device_ws.send_json({"v": 1, "id": "1", "type": "hello", "device_id": "d1", "token": None})
        record = hub.registry.get_or_create("d1")
        await _wait_until(lambda: record.connected)

        closed = await hub.keepalive_sweep()
        assert closed == []
        assert record.connected is True
        assert record.tier is Tier.LIVE

        ping = json.loads((await device_ws.receive()).data)
        assert ping["type"] == "ping"
    finally:
        await client.close()
