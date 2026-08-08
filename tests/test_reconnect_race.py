"""Test for the connection-identity race in `_handle_device_ws`'s exit
handler.

The bug: on disconnect, the handler called `record.unbind()` unconditionally,
without checking that the connection being torn down was still the one the
record held. If a stale connection's belated close is processed AFTER a
legitimate reconnect has already re-bound the record, that unbind wipes out
the live connection.

This was rare when closes only ever happened as the OS reported them. The
keepalive sweep (`Hub.keepalive_sweep`, see test_keepalive.py) actively closes
silent connections, which exercises this exit path far more often -- so this
race had to be fixed before that sweep could ship safely. This test exercises
the real `/device` WebSocket route via an aiohttp `TestServer` (same
technique test_kill_switch.py uses), because the race lives specifically in
`_handle_device_ws`'s own connection-identity check on its exit path --
something a fake socket bypassing the real route can't exercise.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient as AiohttpTestClient
from aiohttp.test_utils import TestServer

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.hub import Hub
from amplifier_browser_bridge.tiers import Tier


def _hub(tmp_path: Path) -> Hub:
    return Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"))


def _audit_events(tmp_path: Path) -> list[str]:
    text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    return [json.loads(line)["event"] for line in text.splitlines() if line.strip()]


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.02) -> None:
    """Poll-don't-sleep: wait until `predicate()` is true or raise on timeout."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition never became true within {timeout}s")


@pytest.mark.asyncio
async def test_stale_connections_belated_close_does_not_unbind_a_newer_reconnect(
    tmp_path: Path,
) -> None:
    """Connection A binds "d1", then connection B reconnects as "d1" --
    rebinding the SAME record -- before A's own underlying socket has
    actually closed. This is exactly what a real device reconnecting
    slightly ahead of its old TCP connection's teardown looks like, and is
    also exactly what the keepalive sweep now produces routinely (close A;
    A's own reconnect logic dials in as B before A's close is fully
    processed server-side). When A's connection finally closes, its belated
    cleanup must not unbind the record out from under B's live connection."""
    hub = _hub(tmp_path)
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    ws_a = None
    ws_b = None
    try:
        record = hub.registry.get_or_create("d1")

        ws_a = await client.ws_connect("/device")
        await ws_a.send_json({"v": 1, "id": "1", "type": "hello", "device_id": "d1", "token": None})
        await _wait_until(lambda: record.connected)

        ws_b = await client.ws_connect("/device")
        await ws_b.send_json({"v": 1, "id": "2", "type": "hello", "device_id": "d1", "token": None})
        # B's hello rebinds the SAME record. A's connection is now stale,
        # but its server-side `_handle_device_ws` coroutine hasn't noticed
        # anything -- its socket hasn't closed yet.
        await _wait_until(lambda: record.connected)

        # A's stale connection finally closes -- belatedly, after B already
        # replaced it. Without the `record.ws is ws` guard, this unbinds the
        # record and wipes out B's live connection.
        await ws_a.close()
        await _wait_until(lambda: "stale_connection_ignored" in _audit_events(tmp_path))

        # The record must still show a live connection -- B's -- not None.
        assert record.connected is True
        assert record.tier is Tier.LIVE

        # Strongest proof available: B's connection is still genuinely
        # usable for real command dispatch, not merely "connected" in name.
        async def _reply_as_device_b() -> None:
            msg = await ws_b.receive()
            env = json.loads(msg.data)
            await ws_b.send_json(
                {
                    "v": 1,
                    "id": env["id"],
                    "type": "result",
                    "device_id": "d1",
                    "ok": True,
                    "result": {"tabs": []},
                }
            )

        reply_task = asyncio.create_task(_reply_as_device_b())
        result = await hub.send_command(Target(device_id="d1", tab_id=1), "tabs", {})
        await reply_task
        assert result == {"ok": True, "result": {"tabs": []}}

        events = _audit_events(tmp_path)
        assert "stale_connection_ignored" in events
        assert events.count("device_disconnected") == 0  # never unbound by the stale close
    finally:
        if ws_b is not None:
            await ws_b.close()
        await client.close()
