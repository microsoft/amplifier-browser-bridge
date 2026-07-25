"""The hub: device registry, per-device command queue, routing/correlation, audit log.

Runs on the agent host, separate from any single agent process, for three reasons
(design doc §3.2), all load-bearing:

    1. Multiple agents can be talking to the same devices at once.
    2. The queue must outlive any one agent process -- mobile devices are reachable
       in windows, not continuously; a command issued now may execute in 90 seconds.
    3. Policy must live outside the model's reach -- see policy.py, and the note
       below on `send_command` as the single choke point.

Two WebSocket routes:

    /device -- extensions dial OUT to this. Device protocol (hello/heartbeat/result/event).
    /agent  -- CLI/lib clients connect to this, one request per short-lived connection
               (though the loop supports multiple requests per connection too).
               Agent protocol (list_devices/command/poll/confirm).

See docs/PROTOCOL.md for the full message catalogue and docs/POLICY.md for the
policy engine's denylist/gate/kill-switch model.

## `send_command` is the single choke point for policy

`QueuedCommand` (queue.py) is constructed in exactly one place in this codebase:
inside `send_command`, and only *after* `PolicyEngine.evaluate` has returned an
"allow" decision. There is no other path to `_dispatch_live` or to a device's
`DeviceCommandQueue.enqueue` -- `_drain_queue` only ever redelivers commands that
already passed through `send_command` once, at enqueue time. A command cannot
reach a device without a policy decision having been made first; this is what
makes the capability-binding guarantee in design doc §6.2 structural rather than
a convention future contributors could accidentally route around.
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
from .policy import PolicyEngine, PolicyError
from .protocol import COMMANDS, PROTOCOL_VERSION, new_id
from .queue import QueuedCommand
from .registry import DeviceRecord, DeviceRegistry
from .tiers import Tier

logger = logging.getLogger("amplifier_browser_bridge.hub")

DEFAULT_PORT = 8900
DEFAULT_COMMAND_TIMEOUT = 30.0

# Commands whose successful device result carries a `url` field directly usable
# to update the policy engine's tab-host cache (see policy.py's "Observation
# intake" section). `tabs` is handled separately since its result is a *list* of
# per-tab entries rather than one url -- see `_ingest_result` below.
_URL_BEARING_RESULT_COMMANDS = frozenset({"navigate", "snapshot", "read"})


class Hub:
    """Owns all hub-side state. `build_app()` returns an aiohttp Application; nothing
    here depends on how it's actually served (test code can drive the Hub's methods
    directly without ever starting a real HTTP server)."""

    def __init__(
        self,
        token_store: TokenStore,
        audit_log: AuditLog,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
        policy: PolicyEngine | None = None,
    ) -> None:
        self.registry = DeviceRegistry()
        self.token_store = token_store
        self.audit = audit_log
        self.command_timeout = command_timeout
        self.policy = policy if policy is not None else PolicyEngine(audit_log)
        # command_id -> the QueuedCommand that was sent, kept only long enough to
        # know (a) whether a returning device result needs tabs-filtering / cache
        # updates, and (b) nothing more -- popped as soon as the result arrives.
        # Keyed globally by command_id (uuid4, unique across all devices) rather
        # than per-device, since that's what the device `result` handler has on
        # hand without needing to search every device's own bookkeeping.
        self._inflight: dict[str, QueuedCommand] = {}
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
                    env = self._ingest_result(device_id, cmd_id, env)
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

    def _ingest_result(self, device_id: str, cmd_id: str | None, env: dict[str, Any]) -> dict[str, Any]:
        """Feed a device's `result` envelope into the policy engine before it is
        stored or handed back to any agent -- the response-path half of the
        invisibility guarantee (design doc §6.2): a `tabs` result is filtered
        for denylisted hosts here, before it ever reaches `record.results` or a
        pending future, so both the immediate-dispatch and later-`poll` paths
        see the same sanitized data. Also feeds the policy engine's tab-host
        cache from `navigate`/`snapshot`/`read` results so the request-path
        denylist check (`PolicyEngine.evaluate`) has ground truth to check
        future commands against, even for tabs the agent never listed via `tabs`.
        """
        cmd = self._inflight.pop(cmd_id, None) if cmd_id else None
        if cmd is None or not env.get("ok"):
            return env

        result = env.get("result")
        if cmd.command == "tabs" and isinstance(result, list):
            filtered = self.policy.filter_tabs_result(device_id, result)
            return {**env, "result": filtered}

        if cmd.command in _URL_BEARING_RESULT_COMMANDS and isinstance(result, dict):
            url = result.get("url")
            tab_id = cmd.target.tab_id
            if isinstance(url, str) and tab_id is not None:
                self.policy.note_tab_url(device_id, tab_id, url)

        return env

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
                elif rtype == "confirm":
                    result = await self._handle_agent_confirm(req)
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

    async def _handle_agent_confirm(self, req: dict[str, Any]) -> dict[str, Any]:
        """Handle a `confirm` request: consume a single-use confirmation token
        (from a prior `needs_confirmation` response) and, if valid and unexpired,
        re-submit the original gated command with `skip_gate=True`. The denylist
        check still runs (a confirmation is not a denylist bypass) -- only the
        gate itself is skipped, since a human has already explicitly approved
        this exact action."""
        token = req.get("confirmation_token")
        if not token or not isinstance(token, str):
            return {"ok": False, "error": "confirm requires 'confirmation_token'"}
        try:
            pending = self.policy.consume_confirmation(token)
        except PolicyError as e:
            return {"ok": False, "error": str(e)}
        self.audit.record(
            "policy_confirmed",
            device_id=pending.target.device_id,
            tab_id=pending.target.tab_id,
            command=pending.command,
            category=pending.category,
            token=token,
        )
        return await self.send_command(pending.target, pending.command, pending.args, skip_gate=True)

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

    async def send_command(
        self, target: Target, command: str, args: dict[str, Any], *, skip_gate: bool = False
    ) -> dict[str, Any]:
        """The single choke point: every command, from every caller (the agent
        route, tests driving the Hub directly, and the post-confirmation
        re-dispatch in `_handle_agent_confirm`), passes through here. Nothing
        else in this module constructs a `QueuedCommand` or calls
        `_dispatch_live`/`queue.enqueue` -- see the module docstring.

        `skip_gate` is set only by `_handle_agent_confirm`, after a human has
        already explicitly approved this exact (target, command, args) via a
        single-use confirmation token. It skips the *gate* check only -- the
        denylist/kill-switch checks in `PolicyEngine.evaluate` always run.
        """
        if command not in COMMANDS:
            return {"ok": False, "error": f"unknown command: {command!r}. Valid: {sorted(COMMANDS)}"}

        decision = self.policy.evaluate(target, command, args, skip_gate=skip_gate)
        if decision.status == "deny":
            return {"ok": False, "error": decision.reason}
        if decision.status == "gate":
            return {
                "status": "needs_confirmation",
                "confirmation_token": decision.token,
                "category": decision.category,
                "detected": decision.detected,
            }

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
        # Remembered only so `_ingest_result` (called when the device's `result`
        # arrives) knows which command this was, for tabs-filtering / tab-host
        # cache updates -- see that method and the `_URL_BEARING_RESULT_COMMANDS`
        # note above. Popped there; also cleaned up below on every early-return
        # path so a dropped/timed-out command never leaks an entry.
        self._inflight[cmd.id] = cmd

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
            self._inflight.pop(cmd.id, None)
            return {"ok": False, "error": f"device connection lost while sending command: {e}"}

        try:
            result_env = await asyncio.wait_for(fut, timeout=self.command_timeout)
        except TimeoutError:
            record.pending.pop(cmd.id, None)
            self._inflight.pop(cmd.id, None)
            return {"ok": False, "error": f"timeout waiting {self.command_timeout}s for device result"}
        except ConnectionError as e:
            self._inflight.pop(cmd.id, None)
            return {"ok": False, "error": str(e)}

        return {k: v for k, v in result_env.items() if k in ("ok", "result", "error")}

    # ------------------------------------------------------------------
    # Kill switch -- design doc §6.2: "Revocation: disable the extension, or a
    # hub-level stop-all. Both immediate." The flag itself lives on the policy
    # engine (checked first thing in `PolicyEngine.evaluate`, so it denies any
    # new dispatch); draining already-queued commands lives here because it
    # needs the DeviceRegistry, which the policy engine deliberately does not
    # depend on (see policy.py's module docstring on why PolicyEngine only ever
    # trusts its own observations, not registry/agent state).
    # ------------------------------------------------------------------

    def engage_kill_switch(self) -> int:
        """Halt all future dispatch immediately, and reject (not silently drop)
        every command currently sitting in a per-device queue -- `poll()` on a
        rejected command_id returns a clear `ok: False` rather than leaving the
        caller to wonder why it never drained. Returns the number rejected.

        Does NOT recall a command already in flight to a device (already sent
        over the wire, awaiting that device's `result`) -- physically
        impossible to un-send a frame that already left the hub. See
        docs/POLICY.md for the honest accounting of what "immediate" covers.
        """
        self.policy.engage_kill_switch()
        rejected = 0
        for record in self.registry.all():
            while True:
                cmd = record.queue.pop_next()
                if cmd is None:
                    break
                self._inflight.pop(cmd.id, None)
                record.results[cmd.id] = {
                    "v": PROTOCOL_VERSION,
                    "id": cmd.id,
                    "type": "result",
                    "ok": False,
                    "error": "kill switch engaged: queued command rejected",
                }
                self.audit.record(
                    "kill_switch_rejected",
                    device_id=record.device_id,
                    command_id=cmd.id,
                    command=cmd.command,
                )
                rejected += 1
        self.audit.record("kill_switch_engaged", rejected=rejected)
        return rejected

    def disengage_kill_switch(self) -> None:
        self.policy.disengage_kill_switch()
        self.audit.record("kill_switch_disengaged")
