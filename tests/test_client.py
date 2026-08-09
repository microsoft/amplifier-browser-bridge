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

import socket

import pytest

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
