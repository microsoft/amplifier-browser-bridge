"""Tests for client.py -- HubClient's connection-failure handling.

The bug this closes: a raw `ConnectionRefusedError` (and friends -- DNS
failure, timeout, a websockets-level handshake failure) used to escape
`HubClient._request` as an unhandled Python exception. Every caller that
already did `except HubError` (nearly every command in cli.py, every tool in
mcp_server.py, the Amplifier tool module's `_HubTool.execute`) had no chance
to catch it, so it surfaced as a raw traceback -- exactly what the maintainer
hit running `amplifier-browser-bridge devices` against a hub on a different
host than the loopback default.

Fixed once, centrally, in `HubClient._request` -- every one of those existing
`except HubError` call sites gets the improvement automatically, with zero
per-call-site changes.
"""

from __future__ import annotations

import json
import socket
from typing import Any

import pytest
import websockets

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.client import HubClient, HubError, _describe_connection_failure


def test_describe_connection_failure_names_connection_refused() -> None:
    message = _describe_connection_failure("ws://100.1.2.3:8900/agent", ConnectionRefusedError())
    assert "ws://100.1.2.3:8900/agent" in message
    assert "connection refused" in message
    assert "amplifier-browser-bridge hub" in message
    assert "amplifier-browser-bridge doctor" in message


def test_describe_connection_failure_names_dns_failure() -> None:
    message = _describe_connection_failure(
        "ws://not-a-real-host.invalid:8900/agent", socket.gaierror("Name or service not known")
    )
    assert "could not resolve host" in message
    assert "Name or service not known" in message


def test_describe_connection_failure_names_timeout() -> None:
    message = _describe_connection_failure("ws://100.1.2.3:8900/agent", TimeoutError())
    assert "timed out" in message


@pytest.mark.asyncio
async def test_list_devices_raises_actionable_huberror_when_nothing_is_listening() -> None:
    """No server bound at all on this port -- a real ConnectionRefusedError,
    not a mock -- must surface as a HubError with an actionable message, never
    as a raw, unhandled ConnectionRefusedError traceback."""
    # Port 1 is a real, always-refused-or-permission-denied choice on any
    # normal host (privileged, never bound by a test) -- but to avoid relying
    # on that assumption across platforms, bind an ephemeral socket, close it
    # immediately, and use ITS port -- guaranteed refused since nothing is
    # listening there anymore.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    client = HubClient(f"ws://127.0.0.1:{port}/agent")
    with pytest.raises(HubError) as exc_info:
        await client.list_devices()

    message = str(exc_info.value)
    assert f"127.0.0.1:{port}" in message
    assert "amplifier-browser-bridge hub" in message


@pytest.mark.asyncio
async def test_command_raises_actionable_huberror_when_nothing_is_listening() -> None:
    """Same fix, via the `command()` path every tabs/snapshot/click/... CLI
    command and MCP tool actually uses (not just list_devices/devices)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    client = HubClient(f"ws://127.0.0.1:{port}/agent")
    with pytest.raises(HubError):
        await client.command(Target(device_id="dev-1"), "tabs", {})


@pytest.mark.asyncio
async def test_command_receives_a_result_larger_than_websockets_1mb_default() -> None:
    """Real-world finding (protocol.py's "WebSocket message-size ceiling"
    section): archiving four real web pages at MHTML depth (L4) died with
    `websockets`' own "sent 1009 (message too big) frame exceeds limit of
    1048576 bytes" -- `HubClient._request`'s `websockets.connect()` call had
    no explicit `max_size`, so it silently inherited that library's 1MB
    default while receiving the hub's relayed `mhtml` result. A real page's
    MHTML (stylesheets/fonts/images inlined by `Page.captureSnapshot`)
    routinely exceeds that; the earlier MHTML testing that never hit this
    only used 30-62KB local test decks, nowhere near the default.

    This test's payload (2MB) is deliberately well past that 1MB default --
    and well under the new, explicit `MAX_WS_MESSAGE_BYTES` (64MiB) ceiling
    `_request` now passes as `max_size` -- so it fails on the prior code
    (no `max_size` set, so 1MB) and passes on the fix. A small/local-deck-
    sized payload could never have reproduced this regression.
    """
    oversized_data = "x" * (2 * 1024 * 1024)  # 2MB: > websockets' 1MB default, < the new 64MiB cap

    async def handler(connection: Any) -> None:
        async for raw in connection:
            req = json.loads(raw)
            await connection.send(
                json.dumps(
                    {
                        "v": 1,
                        "id": req["id"],
                        "type": "result",
                        "ok": True,
                        "result": {"data": oversized_data},
                    }
                )
            )

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = HubClient(f"ws://127.0.0.1:{port}/agent")
        resp = await client.command(Target(device_id="dev-1"), "mhtml", {})

    assert resp["ok"] is True
    assert resp["result"]["data"] == oversized_data
