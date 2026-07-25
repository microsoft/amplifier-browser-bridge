"""HubClient: the agent-side WebSocket client used by both the lib's public API and the CLI.

Each call opens a short-lived connection to the hub's `/agent` route, sends one request,
awaits the correlated response, and closes. This matches how the CLI is actually used (one
process invocation per command) while the hub-side route also happily supports a
longer-lived connection issuing many requests in sequence, for callers (like an MCP server,
in a later phase) that want to hold a session open.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets

from .addressing import Target
from .protocol import PROTOCOL_VERSION, new_id


class HubError(RuntimeError):
    """Raised when the hub returns an `error` message, or a request-level failure occurs."""


class HubClient:
    def __init__(self, url: str, token: str | None = None, timeout: float = 35.0) -> None:
        """`url` is the full hub agent endpoint, e.g. ws://100.124.126.19:8900/agent."""
        self.url = url
        self.token = token
        self.timeout = timeout

    async def _request(self, req: dict[str, Any]) -> dict[str, Any]:
        req = {**req, "token": self.token}
        async with websockets.connect(self.url, open_timeout=10) as ws:
            await ws.send(json.dumps(req))
            raw = await asyncio.wait_for(ws.recv(), timeout=self.timeout)
            resp: dict[str, Any] = json.loads(raw)
        if resp.get("type") == "error":
            raise HubError(resp.get("error", "unknown hub error"))
        return resp

    async def list_devices(self) -> list[dict[str, Any]]:
        resp = await self._request({"v": PROTOCOL_VERSION, "id": new_id(), "type": "list_devices"})
        return list(resp.get("devices", []))

    async def command(
        self, target: Target, command: str, args: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        resp = await self._request(
            {
                "v": PROTOCOL_VERSION,
                "id": new_id(),
                "type": "command",
                "command": command,
                "target": target.to_dict(),
                "args": args or {},
            }
        )
        return resp

    async def poll(self, device_id: str, command_id: str) -> dict[str, Any]:
        resp = await self._request(
            {
                "v": PROTOCOL_VERSION,
                "id": new_id(),
                "type": "poll",
                "device_id": device_id,
                "command_id": command_id,
            }
        )
        return resp
