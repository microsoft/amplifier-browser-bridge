"""The hub: device registry, per-device command queue, routing/correlation, audit log.

Runs on the agent host, separate from any single agent process, for three reasons
(design doc §3.2), all load-bearing:

    1. Multiple agents can be talking to the same devices at once.
    2. The queue must outlive any one agent process -- mobile devices are reachable
       in windows, not continuously; a command issued now may execute in 90 seconds.
    3. Policy must live outside the model's reach (a later phase, but the hub is
       where it will live -- not in the extension, not in the agent).

Two WebSocket routes:

    /device -- extensions dial OUT to this. Device protocol (hello/heartbeat/result/event).
    /agent  -- CLI/lib clients connect to this, one request per short-lived connection
               (though the loop supports multiple requests per connection too).
               Agent protocol (list_devices/command/poll).

See docs/PROTOCOL.md for the full message catalogue.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import WSMsgType, web

from .addressing import Target, TargetError
from .audit import AuditLog
from .auth import TokenStore
from .protocol import COMMANDS, PROTOCOL_VERSION, new_id
from .queue import QueuedCommand
from .registry import DeviceRecord, DeviceRegistry
from .tiers import Tier

logger = logging.getLogger("amplifier_browser_bridge.hub")

DEFAULT_PORT = 8900
DEFAULT_COMMAND_TIMEOUT = 30.0


class Hub:
    """Owns all hub-side state. `build_app()` returns an aiohttp Application; nothing
    here depends on how it's actually served (test code can drive the Hub's methods
    directly without ever starting a real HTTP server)."""

    def __init__(
        self,
        token_store: TokenStore,
        audit_log: AuditLog,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> None:
        self.registry = DeviceRegistry()
        self.token_store = token_store
        self.audit = audit_log
        self.command_timeout = command_timeout
        if not token_store.auth_enabled:
            logger.warning(
                "No hub token configured (ABB_HUB_TOKEN / token file) -- running with "
                "auth DISABLED. Fine for local dev on a private tailnet; never ship this way."
            )

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/healthz", self._handle_healthz)
        app.router.add_get("/device", self._handle_device_ws)
        app.router.add_get("/agent", self._handle_agent_ws)
        return app

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    async def _handle_healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "devices": len(self.registry.all())})

    # ------------------------------------------------------------------
    # Device protocol (extension <-> hub)
    # ------------------------------------------------------------------

    async def _handle_device_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=None)  # app-level heartbeat, not transport-level
        await ws.prepare(request)
        device_id: str | None = None

        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                env = json.loads(msg.data)
            except json.JSONDecodeError:
                logger.warning("device sent non-JSON frame, ignoring")
                continue

            mtype = env.get("type")

            if mtype == "hello":
                candidate_id = env.get("device_id")
                if not candidate_id or not isinstance(candidate_id, str):
                    await ws.send_json(
                        {"type": "error", "id": env.get("id"), "error": "hello missing device_id"}
                    )
                    continue
                if not self.token_store.validate(env.get("token"), candidate_id):
                    await ws.send_json({"type": "error", "id": env.get("id"), "error": "unauthorized"})
                    await ws.close()
                    return ws
                device_id = candidate_id
                record = self.registry.get_or_create(device_id)
                record.bind(ws, env)
                self.audit.record(
                    "device_connected",
                    device_id=device_id,
                    label=record.label,
                    platform=record.platform,
                    capabilities=record.capabilities,
                )
                logger.info("device connected: %s (%s, %s)", device_id, record.label, record.platform)
                # Now that the device is live, drain anything that queued up while it was away.
                asyncio.create_task(self._drain_queue(record))

            elif mtype == "heartbeat":
                if device_id:
                    self.registry.get_or_create(device_id).touch()

            elif mtype == "result":
                if device_id:
                    record = self.registry.get_or_create(device_id)
                    record.touch()
                    cmd_id = env.get("id")
                    fut = record.pending.pop(cmd_id, None)
                    record.results[cmd_id] = env
                    self.audit.record(
                        "result_received", device_id=device_id, command_id=cmd_id, ok=env.get("ok")
                    )
                    if fut is not None and not fut.done():
                        fut.set_result(env)

            elif mtype == "event":
                if device_id:
                    self.registry.get_or_create(device_id).touch()
                    self.audit.record(
                        "device_event",
                        device_id=device_id,
                        device_event_name=env.get("event"),
                        data=env.get("data"),
                    )

            else:
                logger.warning("device sent unknown message type: %s", mtype)

        if device_id:
            record = self.registry.get(device_id)
            if record is not None:
                record.unbind()
            self.audit.record("device_disconnected", device_id=device_id)
            logger.info("device disconnected: %s", device_id)

        return ws

    async def _drain_queue(self, record: DeviceRecord) -> None:
        """Send queued commands in FIFO order now that the device is live. Stops
        (leaving the remainder queued) the moment the device disconnects again."""
        while record.connected:
            cmd = record.queue.pop_next()
            if cmd is None:
                return
            self.audit.record(
                "command_drained", device_id=record.device_id, command_id=cmd.id, command=cmd.command
            )
            try:
                await self._dispatch_live(record, cmd)
            except ConnectionError:
                return

    # ------------------------------------------------------------------
    # Agent protocol (CLI/lib <-> hub)
    # ------------------------------------------------------------------

    async def _handle_agent_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                req = json.loads(msg.data)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "id": None, "error": "invalid JSON"})
                continue

            rid = req.get("id") or new_id()
            if not self.token_store.validate(req.get("token")):
                await ws.send_json(
                    {"v": PROTOCOL_VERSION, "type": "error", "id": rid, "error": "unauthorized"}
                )
                continue

            rtype = req.get("type")
            try:
                if rtype == "list_devices":
                    await ws.send_json(
                        {
                            "v": PROTOCOL_VERSION,
                            "type": "devices",
                            "id": rid,
                            "devices": self.registry.snapshot(),
                        }
                    )
                elif rtype == "command":
                    result = await self._handle_agent_command(req)
                    await ws.send_json({"v": PROTOCOL_VERSION, "type": "result", "id": rid, **result})
                elif rtype == "poll":
                    result = self._poll(req.get("device_id", ""), req.get("command_id", ""))
                    await ws.send_json({"v": PROTOCOL_VERSION, "type": "result", "id": rid, **result})
                else:
                    await ws.send_json(
                        {
                            "v": PROTOCOL_VERSION,
                            "type": "error",
                            "id": rid,
                            "error": f"unknown request type: {rtype}",
                        }
                    )
            except Exception as exc:  # fail loud, but never crash the agent connection
                logger.exception("error handling agent request")
                await ws.send_json({"v": PROTOCOL_VERSION, "type": "error", "id": rid, "error": str(exc)})

        return ws

    async def _handle_agent_command(self, req: dict[str, Any]) -> dict[str, Any]:
        try:
            target = Target.from_dict(req.get("target") or {})
        except TargetError as e:
            return {"ok": False, "error": str(e)}
        command = req.get("command", "")
        args = req.get("args") or {}
        return await self.send_command(target, command, args)

    def _poll(self, device_id: str, command_id: str) -> dict[str, Any]:
        record = self.registry.get(device_id)
        if record is None:
            return {"ok": False, "error": f"unknown device: {device_id}"}
        if command_id in record.results:
            return record.results[command_id]
        if command_id in record.pending:
            return {"status": "pending"}
        position = record.queue.position(command_id)
        if position is not None:
            return {"status": "queued", "queue_position": position, "tier": record.tier.value}
        return {"ok": False, "error": f"unknown command_id: {command_id}"}

    # ------------------------------------------------------------------
    # Core dispatch logic -- used by both the agent route and tests directly
    # ------------------------------------------------------------------

    async def send_command(self, target: Target, command: str, args: dict[str, Any]) -> dict[str, Any]:
        if command not in COMMANDS:
            return {"ok": False, "error": f"unknown command: {command!r}. Valid: {sorted(COMMANDS)}"}

        record = self.registry.get(target.device_id)
        if record is None:
            return {"ok": False, "error": f"unknown device: {target.device_id!r}"}

        cmd = QueuedCommand(id=new_id(), target=target, command=command, args=args)

        if record.tier is Tier.LIVE:
            return await self._dispatch_live(record, cmd)

        position = record.queue.enqueue(cmd)
        self.audit.record(
            "command_queued",
            device_id=target.device_id,
            command_id=cmd.id,
            command=command,
            tier=record.tier.value,
            queue_position=position,
        )
        return {
            "status": "queued",
            "command_id": cmd.id,
            "tier": record.tier.value,
            "last_seen": record.last_seen.isoformat() if record.last_seen else None,
            "queue_position": position,
        }

    async def _dispatch_live(self, record: DeviceRecord, cmd: QueuedCommand) -> dict[str, Any]:
        assert record.ws is not None  # only called when record.tier is LIVE
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        record.pending[cmd.id] = fut

        env = {
            "v": PROTOCOL_VERSION,
            "id": cmd.id,
            "type": "command",
            "command": cmd.command,
            "target": cmd.target.to_dict(),
            "args": cmd.args,
        }
        self.audit.record(
            "command_sent",
            device_id=record.device_id,
            command_id=cmd.id,
            command=cmd.command,
            target=env["target"],
        )

        try:
            await record.ws.send_json(env)
        except ConnectionResetError as e:
            record.pending.pop(cmd.id, None)
            return {"ok": False, "error": f"device connection lost while sending command: {e}"}

        try:
            result_env = await asyncio.wait_for(fut, timeout=self.command_timeout)
        except TimeoutError:
            record.pending.pop(cmd.id, None)
            return {"ok": False, "error": f"timeout waiting {self.command_timeout}s for device result"}
        except ConnectionError as e:
            return {"ok": False, "error": str(e)}

        return {k: v for k, v in result_env.items() if k in ("ok", "result", "error")}
