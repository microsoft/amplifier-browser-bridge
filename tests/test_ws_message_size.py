"""Tests for the WebSocket message-size ceiling (protocol.py's
`MAX_WS_MESSAGE_BYTES`).

The bug: `HubClient._request`'s `websockets.connect()` call had no explicit
`max_size`, so it silently inherited that library's 1MB default; the hub's two
`web.WebSocketResponse()` routes (`/device`, `/agent`) had no explicit
`max_msg_size`, so they silently inherited aiohttp's 4MB default. Real-world
finding: archiving four real web pages at MHTML depth (L4 -- `Page.
captureSnapshot`) died with `websockets`' own "sent 1009 (message too big)
frame exceeds limit of 1048576 bytes" -- a real page's MHTML routinely exceeds
1MB, sometimes 4MB, for anything image/font-heavy. The earlier MHTML testing
that never hit this used 30-62KB local test decks, nowhere near either
default.

test_client.py already covers the CLIENT leg in isolation (a fake raw
`websockets` server). This module proves the fix holds across the FULL round
trip -- a real `Hub` (both WebSocketResponse routes) and the real `HubClient`
(client.py) -- with a payload that exceeds aiohttp's OLD 4MB default (so it
would have failed the hub's `/device` receive, the hub's `/agent` relay, or
the client's own receive, before this fix touched any of the three).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import websockets
from aiohttp.test_utils import TestServer

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.client import HubClient
from amplifier_browser_bridge.hub import Hub

# 5MB: exceeds aiohttp's OLD 4MB `max_msg_size` default (and `websockets`' OLD
# 1MB `max_size` default) but is comfortably under the new, explicit 64MiB
# `MAX_WS_MESSAGE_BYTES` ceiling both now use. A payload anywhere near the
# 30-62KB local test decks used during earlier MHTML testing could never
# have reproduced this regression.
_OVERSIZED_PAYLOAD = "z" * (5 * 1024 * 1024)


async def _wait_until(predicate: Any, *, timeout: float = 2.0, interval: float = 0.02) -> None:
    """Poll-don't-sleep (this project's convention -- see CONTRIBUTING.md)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition never became true within {timeout}s")


@pytest.mark.asyncio
async def test_oversized_device_result_survives_the_full_hub_round_trip(tmp_path: Path) -> None:
    """A real device connection sends back a 5MB `result` (bigger than
    aiohttp's old 4MB `max_msg_size` default) for an ordinary `tabs` command
    dispatched through a real `Hub` to a real `HubClient` -- exercising the
    hub's `/device` route (receiving the oversized result FROM the device),
    the hub's `/agent` route (relaying it TO the agent), and the client's own
    `websockets.connect` (receiving it), all in one pass with no leg
    downgraded to a fake/simplified stand-in.
    """
    hub = Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"))
    server = TestServer(hub.build_app())
    await server.start_server()
    assert server.port is not None
    device_url = f"ws://127.0.0.1:{server.port}/device"
    agent_url = f"ws://127.0.0.1:{server.port}/agent"

    device_id = "d1"
    record = hub.registry.get_or_create(device_id)

    async def run_device() -> None:
        async with websockets.connect(device_url) as dev_ws:
            await dev_ws.send(
                json.dumps({"v": 1, "id": "hello-1", "type": "hello", "device_id": device_id, "token": None})
            )
            async for raw in dev_ws:
                env = json.loads(raw)
                if env.get("type") == "command":
                    await dev_ws.send(
                        json.dumps(
                            {
                                "v": 1,
                                "id": env["id"],
                                "type": "result",
                                "ok": True,
                                "result": {"data": _OVERSIZED_PAYLOAD},
                            }
                        )
                    )
                    return

    device_task = asyncio.create_task(run_device())
    try:
        await _wait_until(lambda: record.connected)

        client = HubClient(agent_url)
        result = await client.command(Target(device_id=device_id, tab_id=1), "tabs", {})

        assert result["ok"] is True
        assert result["result"]["data"] == _OVERSIZED_PAYLOAD
        await device_task
    finally:
        if not device_task.done():
            device_task.cancel()
        await server.close()
