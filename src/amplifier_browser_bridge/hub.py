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

## `_dispatch_live` is the single choke point for CDP escalation (Phase 4)

Symmetrically: `_dispatch_live` is the only place a command's wire envelope is
actually constructed and sent to a *live* device (`_send_and_await` does the
raw send-and-await; `_dispatch_live` wraps it). Every command reaches a device
through here -- both the immediate-dispatch path (`send_command`, when the
device is already `Tier.LIVE`) and the drained-later path (`_drain_queue`,
once a queued device reconnects) call `_dispatch_live`, never `_send_and_await`
directly. This is where `cdp.requires_cdp` is checked and, if the caller's
`args` genuinely need it (trusted input, hidden-tab capture -- see cdp.py),
CDP is auto-attached (never speculatively) before the real command is sent
with a hub-asserted `_cdp` flag the device honors. See `_ensure_cdp_attached`
and docs/PROTOCOL.md's CDP section.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import replace
from typing import Any

from aiohttp import WSMsgType, web

from .addressing import Target, TargetError
from .args_bool import truthy
from .audit import AuditLog
from .auth import TokenStore
from .cdp import DEFAULT_SOFT_DETACH_IDLE_SECONDS, CdpRegistry, requires_cdp
from .policy import PolicyEngine, PolicyError
from .protocol import COMMANDS, HUB_ONLY_ARGS, PROTOCOL_VERSION, new_id
from .queue import QueuedCommand
from .registry import DeviceConnection, DeviceRecord, DeviceRegistry
from .tiers import Tier

logger = logging.getLogger("amplifier_browser_bridge.hub")

DEFAULT_PORT = 8900
# Real-world finding (real Edge profile, 532 open tabs): `read` on a heavy SPA
# (repos.opensource.microsoft.com's Open Source Management Portal) timed out
# at the prior default of 30.0s even though the tab was awake and
# `status: "complete"` -- injection + shadow-DOM-piercing traversal on a large
# hydrated SPA genuinely needs more than 30s sometimes. 120s is a generous
# default for real-world pages while still bounding the worst case; a caller
# with an even heavier page can raise it further per-command via
# `args.timeout_s` (see protocol.py's HUB_ONLY_ARGS) up to MAX_COMMAND_TIMEOUT,
# or the hub operator can change the default via `abb hub --command-timeout`.
DEFAULT_COMMAND_TIMEOUT = 120.0
# Accepted range for a caller-supplied `args.timeout_s` override (see
# `Hub._extract_timeout_override`). Floor prevents a mistaken `0`/negative
# value from producing an unusable hair-trigger timeout; ceiling keeps a
# single command from being able to hang a caller indefinitely.
MIN_COMMAND_TIMEOUT = 1.0
MAX_COMMAND_TIMEOUT = 600.0
DEFAULT_SOFT_DETACH_SWEEP_INTERVAL_SECONDS = 30.0

# Commands whose successful device result carries a `url` field directly usable
# to update the policy engine's tab-host cache (see policy.py's "Observation
# intake" section). `tabs` is handled separately since its result is a *list* of
# per-tab entries rather than one url -- see `_ingest_result` below.
_URL_BEARING_RESULT_COMMANDS = frozenset({"navigate", "snapshot", "read"})


class _DeviceAuthError(Exception):
    """Raised internally by `_handle_device_message` on a bad `hello` token.
    Signals the caller (`_handle_device_ws`) to close the connection -- kept
    as an exception rather than a sentinel return value so the "keep looping"
    vs. "stop, close the socket" distinction can't be silently lost at a call
    site."""


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
        cdp_idle_seconds: float = DEFAULT_SOFT_DETACH_IDLE_SECONDS,
        soft_detach_sweep_interval: float = DEFAULT_SOFT_DETACH_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        self.registry = DeviceRegistry()
        self.token_store = token_store
        self.audit = audit_log
        self.command_timeout = command_timeout
        self.policy = policy if policy is not None else PolicyEngine(audit_log)
        # Per-(device, tab) CDP attach bookkeeping (Phase 4, design doc §7) --
        # see cdp.py and this module's "single choke point for CDP escalation"
        # docstring section above.
        self.cdp = CdpRegistry(idle_seconds=cdp_idle_seconds)
        self.soft_detach_sweep_interval = soft_detach_sweep_interval
        self._soft_detach_task: asyncio.Task[None] | None = None
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
        # Background soft-detach sweep (design doc §6.3/§7) -- started/stopped
        # via aiohttp's own app lifecycle so `cli.py`'s `hub` command needs no
        # changes to benefit from it. Tests that want deterministic control
        # call `soft_detach_idle_tabs()` directly instead of relying on this
        # loop's real sleep interval -- see tests/test_cdp.py.
        app.on_startup.append(self._start_soft_detach_task)
        app.on_cleanup.append(self._stop_soft_detach_task)
        return app

    async def _start_soft_detach_task(self, app: web.Application) -> None:
        self._soft_detach_task = asyncio.create_task(self.soft_detach_loop())

    async def _stop_soft_detach_task(self, app: web.Application) -> None:
        if self._soft_detach_task is not None:
            self._soft_detach_task.cancel()
            self._soft_detach_task = None

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
            try:
                device_id = await self._handle_device_message(ws, device_id, env)
            except _DeviceAuthError:
                await ws.close()
                return ws

        if device_id:
            record = self.registry.get(device_id)
            if record is not None:
                record.unbind()
            self.audit.record("device_disconnected", device_id=device_id)
            logger.info("device disconnected: %s", device_id)

        return ws

    async def _handle_device_message(
        self, ws: DeviceConnection, device_id: str | None, env: dict[str, Any]
    ) -> str | None:
        """Process one `/device`-route message and return the (possibly
        newly-established) device_id for this connection. Extracted from
        `_handle_device_ws` so this logic -- including `hello`,
        `capabilities_update`, and CDP-related `event`s -- can be exercised
        directly in tests without a real WebSocket (see tests/test_hub.py,
        tests/test_capabilities.py). Only needs `send_json` (the same
        `DeviceConnection` protocol `registry.py`'s `DeviceRecord.bind` uses)
        -- never the concrete `web.WebSocketResponse` -- so test doubles
        don't need to satisfy aiohttp's full type. Raises `_DeviceAuthError`
        on a bad `hello` token; the caller is responsible for closing the
        connection in that case."""
        mtype = env.get("type")

        if mtype == "hello":
            candidate_id = env.get("device_id")
            if not candidate_id or not isinstance(candidate_id, str):
                await ws.send_json({"type": "error", "id": env.get("id"), "error": "hello missing device_id"})
                return device_id
            if not self.token_store.validate(env.get("token"), candidate_id):
                await ws.send_json({"type": "error", "id": env.get("id"), "error": "unauthorized"})
                raise _DeviceAuthError()
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
            return device_id

        if mtype == "heartbeat":
            if device_id:
                self.registry.get_or_create(device_id).touch()
            return device_id

        if mtype == "result":
            if device_id:
                record = self.registry.get_or_create(device_id)
                record.touch()
                raw_cmd_id = env.get("id")
                if not isinstance(raw_cmd_id, str):
                    logger.warning("device sent a 'result' with no correlation id, ignoring")
                    return device_id
                cmd_id: str = raw_cmd_id
                env = self._ingest_result(device_id, cmd_id, env)
                fut = record.pending.pop(cmd_id, None)
                record.results[cmd_id] = env
                self.audit.record("result_received", device_id=device_id, command_id=cmd_id, ok=env.get("ok"))
                if fut is not None and not fut.done():
                    fut.set_result(env)
            return device_id

        if mtype == "event":
            if device_id:
                self.registry.get_or_create(device_id).touch()
                event_name = env.get("event")
                raw_data = env.get("data")
                data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
                # Unsolicited CDP detach (Cancel on the banner, DevTools
                # opened -- which force-detaches every session on the target
                # -- or the target crashed/was discarded). The hub did not
                # ask for this detach, so it must learn about it here rather
                # than from a `detach` command's own result -- see cdp.py's
                # module docstring and docs/POLICY.md's CDP section.
                if event_name == "cdp_detached":
                    tab_id = data.get("tab_id")
                    reason = data.get("reason")
                    if isinstance(tab_id, int):
                        self.cdp.mark_detached(
                            device_id,
                            tab_id,
                            reason=reason if isinstance(reason, str) else "unsolicited_detach",
                        )
                self.audit.record(
                    "device_event", device_id=device_id, device_event_name=event_name, data=data
                )
            return device_id

        if mtype == "capabilities_update":
            # Phase 1 finding: capture_visible_tab/scripting probes can
            # under-report `false` at `hello` time if no real tab existed yet
            # (design doc §2). The extension re-probes once a tab appears and
            # corrects the record here -- see background.js's
            # `maybeReprobe()`. Merge rather than replace: an update may
            # report a subset of keys.
            if device_id:
                record = self.registry.get_or_create(device_id)
                caps = env.get("capabilities")
                if isinstance(caps, dict):
                    merged = dict(record.capabilities)
                    for key, value in caps.items():
                        if isinstance(value, bool):
                            merged[key] = value
                    record.capabilities = merged
                    record.touch()
                    self.audit.record(
                        "capabilities_updated", device_id=device_id, capabilities=record.capabilities
                    )
            return device_id

        logger.warning("device sent unknown message type: %s", mtype)
        return device_id

    def _ingest_result(self, device_id: str, cmd_id: str | None, env: dict[str, Any]) -> dict[str, Any]:
        """Feed a device's `result` envelope into the policy engine and the
        CDP registry before it is stored or handed back to any agent -- the
        response-path half of the invisibility guarantee (design doc §6.2): a
        `tabs` result is filtered for denylisted hosts here, before it ever
        reaches `record.results` or a pending future, so both the
        immediate-dispatch and later-`poll` paths see the same sanitized
        data. Also feeds:

        - the policy engine's tab-host cache (`navigate`/`snapshot`/`read`),
          and its ref-label cache (`snapshot`/`wait_for` -- Phase 4, see
          policy.py's "Label hints are now wired") so gate detection has
          ground truth even for tabs/refs the agent never listed explicitly;
        - the CDP registry's attach state (`attach`/`detach` results -- Phase
          4, see cdp.py), handled first and unconditionally (even on
          failure), since an attach/detach's own success or failure IS the
          state transition, not a side observation of one.
        """
        cmd = self._inflight.pop(cmd_id, None) if cmd_id else None
        if cmd is None:
            return env

        if cmd.command == "attach":
            tab_id = cmd.target.tab_id
            if tab_id is not None:
                if env.get("ok"):
                    self.cdp.mark_attached(device_id, tab_id)
                    self.audit.record("cdp_attached", device_id=device_id, tab_id=tab_id)
                else:
                    self.audit.record(
                        "cdp_attach_failed", device_id=device_id, tab_id=tab_id, error=env.get("error")
                    )
            return env

        if cmd.command == "detach":
            tab_id = cmd.target.tab_id
            if tab_id is not None:
                if env.get("ok"):
                    reason = (
                        cmd.args.get("reason") if isinstance(cmd.args.get("reason"), str) else "requested"
                    )
                else:
                    reason = f"detach command failed: {env.get('error')}"
                self.cdp.mark_detached(device_id, tab_id, reason=reason)
                self.audit.record("cdp_detached", device_id=device_id, tab_id=tab_id, reason=reason)
            return env

        if not env.get("ok"):
            return env

        result = env.get("result")
        if cmd.command == "tabs" and isinstance(result, list):
            filtered = self.policy.filter_tabs_result(device_id, result)
            enriched = [
                {**tab, "cdp_attached": self.cdp.is_attached(device_id, tab["tab_id"])}
                if isinstance(tab, dict) and isinstance(tab.get("tab_id"), int)
                else tab
                for tab in filtered
            ]
            return {**env, "result": enriched}

        if cmd.command in _URL_BEARING_RESULT_COMMANDS and isinstance(result, dict):
            url = result.get("url")
            tab_id = cmd.target.tab_id
            if isinstance(url, str) and tab_id is not None:
                self.policy.note_tab_url(device_id, tab_id, url)
            if cmd.command == "snapshot":
                nodes = result.get("nodes")
                if isinstance(url, str) and tab_id is not None and isinstance(nodes, list):
                    self.policy.note_snapshot(device_id, tab_id, url, nodes)

        if cmd.command == "wait_for" and isinstance(result, dict):
            tab_id = cmd.target.tab_id
            ref = result.get("ref")
            url = result.get("url")
            if tab_id is not None and isinstance(ref, str) and isinstance(url, str):
                self.policy.note_ref(
                    device_id,
                    tab_id,
                    url,
                    ref,
                    label=result.get("name"),
                    tag=result.get("tag"),
                    input_type=result.get("input_type"),
                )

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
                            "devices": self._devices_snapshot(),
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

        # `_cdp` is a hub-internal wire signal (see cdp.py, `_dispatch_live`)
        # that authorizes the DEVICE to use CDP dispatch for this specific
        # command. It is set ONLY by the hub's own auto-escalation logic --
        # never accepted from a caller. Honoring a caller-supplied `_cdp`
        # would let an agent (or a prompt-injected one) request trusted-input
        # / hidden-capture dispatch without ever passing through the
        # capability check or attach bookkeeping in `_ensure_cdp_attached` --
        # the same capability-binding discipline policy.py applies to
        # denylisted targets applies here to CDP usage. `trusted` and
        # `capture_hidden` remain legitimate caller-facing intent args.
        if "_cdp" in args:
            args = {k: v for k, v in args.items() if k != "_cdp"}

        timeout_override, args, timeout_error = self._extract_timeout_override(args)
        if timeout_error is not None:
            return {"ok": False, "error": timeout_error}

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

        cmd = QueuedCommand(id=new_id(), target=target, command=command, args=args, timeout=timeout_override)

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

    @staticmethod
    def _extract_timeout_override(args: dict[str, Any]) -> tuple[float | None, dict[str, Any], str | None]:
        """Pop and validate `args.timeout_s` (see protocol.py's HUB_ONLY_ARGS).

        Returns `(timeout_override, remaining_args, error)`. `timeout_override`
        is `None` if the caller didn't supply one (meaning: use the hub's
        configured `command_timeout`). `error` is a human-readable message if
        the caller supplied a value that isn't usable -- fail loud rather than
        silently clamping or ignoring a bad value (design doc \u00a78).

        This never forwards `timeout_s` to the device: it is a hub-only
        knob (how long the HUB waits for a device reply), not something
        `injected.js`/`background.js` have any use for.
        """
        if "timeout_s" not in args:
            return None, args, None
        raw = args["timeout_s"]
        remaining = {k: v for k, v in args.items() if k not in HUB_ONLY_ARGS}
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None, remaining, f"args.timeout_s must be a number (seconds), got: {raw!r}"
        if not math.isfinite(value):
            return None, remaining, f"args.timeout_s must be a finite number, got: {raw!r}"
        if not (MIN_COMMAND_TIMEOUT <= value <= MAX_COMMAND_TIMEOUT):
            error = (
                f"args.timeout_s must be between {MIN_COMMAND_TIMEOUT} and {MAX_COMMAND_TIMEOUT} "
                f"seconds, got: {value}"
            )
            return None, remaining, error
        return value, remaining, None

    @staticmethod
    def _timeout_hint(command: str, args: dict[str, Any]) -> str:
        """A trailing sentence naming a DIFFERENT mechanism the caller may choose
        when a command's own device round trip times out -- discoverability, not
        decision-making (design doc's "Mechanism, not policy" section): this never
        retries the command differently or substitutes one mechanism for another
        itself, it only names what exists so the caller can pick.

        Returns an empty string for commands with no relevant alternative to name.
        """
        hint = ""
        if command in ("read", "snapshot"):
            if truthy(args.get("all_frames")):
                hint += (
                    " This request already used args.all_frames=true (every frame is instrumented "
                    "before any result returns, which is slower) -- if you know which frame you need "
                    "from a prior result's `frames` entry, args.frame_id=<id> targets just that one "
                    "frame and skips the rest."
                )
            else:
                hint += (
                    " If the content you need lives inside an embedded frame (e.g. a SharePoint/M365 "
                    "document viewer) rather than the top frame, args.all_frames=true gathers every frame "
                    "-- slower, so raise args.timeout_s alongside it."
                )
        if command in ("click", "type", "key"):
            hint += (
                " If the page ignores untrusted synthetic events, args.trusted=true escalates to "
                "CDP-backed isTrusted input (requires the debugger capability on this device)."
            )
        # Bug 3 (real-profile hardening): DOM injection/traversal on a heavy SPA is
        # viable-when-foreground, dead-when-background (measured: a heavy enterprise
        # SPA timed out at 170s backgrounded, ~2s once activated). Name every real
        # option -- never pick one automatically (design doc's "Mechanism, not
        # policy" section) -- for every DOM-injecting command this timeout applies to.
        page_world_commands = (
            "snapshot",
            "read",
            "click",
            "type",
            "key",
            "scroll",
            "back",
            "forward",
            "wait_for",
            "wait_text",
        )
        if command in page_world_commands and not truthy(args.get("activate")):
            if command in ("read", "snapshot"):
                hint += (
                    " If the target tab is not the active tab, DOM injection/traversal can be slow or "
                    "hang on a heavy page while backgrounded -- args.activate=true activates the tab "
                    "first (fast, exact DOM, but steals the human's focus), the agent-surface-only "
                    "vision_read (not a wire command) captures a screenshot and extracts text via a "
                    "vision model instead (no focus steal, costs a model call, produces no element "
                    "refs), or raise args.timeout_s."
                )
            else:
                hint += (
                    " If the target tab is not the active tab, DOM injection can be slow or hang on a "
                    "heavy page while backgrounded -- args.activate=true activates the tab first (fast, "
                    "exact DOM, but steals the human's focus), or raise args.timeout_s."
                )
        return hint

    async def _dispatch_live(self, record: DeviceRecord, cmd: QueuedCommand) -> dict[str, Any]:
        """The single choke point for CDP escalation (see this module's
        docstring). Called for both the immediate-dispatch path
        (`send_command`, device already live) and the drained-later path
        (`_drain_queue`, once a queued device reconnects) -- CDP escalation
        must apply identically to both, since a caller has no way to know
        (or control) which path their command will take."""
        assert record.ws is not None  # only called when record.tier is LIVE
        if requires_cdp(cmd.command, cmd.args):
            cdp_error = await self._ensure_cdp_attached(record, cmd.target.tab_id)
            if cdp_error is not None:
                # Never silently fall back to the injection-only path the
                # caller didn't ask for (design doc §8). Persist the result
                # exactly as if a device `result` had arrived, so a command
                # that was queued and only reaches this branch on drain still
                # behaves correctly under `poll()` -- see docs/PROTOCOL.md.
                self._inflight.pop(cmd.id, None)
                record.results[cmd.id] = {"v": PROTOCOL_VERSION, "id": cmd.id, "type": "result", **cdp_error}
                return cdp_error
            cmd = replace(cmd, args={**cmd.args, "_cdp": True})
        return await self._send_and_await(record, cmd)

    async def _ensure_cdp_attached(self, record: DeviceRecord, tab_id: int | None) -> dict[str, Any] | None:
        """Pre-flight for a CDP-requiring command. Returns `None` if CDP is
        (now) attached and the real command may proceed; otherwise an
        `{"ok": False, "error": ...}` dict to return in its place. Attaches
        on demand (never speculatively) by sending a real `attach` command
        and waiting for the device's result -- exactly the same wire
        round-trip an agent's own explicit `attach` command would produce."""
        if tab_id is None:
            return {"ok": False, "error": "CDP-requiring command needs an explicit tab_id in target"}
        if not record.capabilities.get("debugger"):
            return {
                "ok": False,
                "error": (
                    f"capability unavailable on this device ({record.label}): chrome.debugger/CDP is "
                    "not present here (e.g. Edge Android) -- cannot satisfy trusted input or "
                    "hidden-tab capture; refusing rather than silently falling back to the "
                    "injection-only path the caller didn't ask for. This device can still run "
                    "untrusted injected click/type/key (drop args.trusted) and capture the ACTIVE "
                    "tab only via chrome.tabs.captureVisibleTab (drop args.capture_hidden) -- use "
                    "those directly if isTrusted input or hidden/background-tab capture isn't "
                    "strictly required."
                ),
            }
        if self.cdp.is_attached(record.device_id, tab_id):
            self.cdp.touch(record.device_id, tab_id)
            return None

        attach_cmd = QueuedCommand(
            id=new_id(), target=Target(device_id=record.device_id, tab_id=tab_id), command="attach", args={}
        )
        result = await self._send_and_await(record, attach_cmd)
        if not result.get("ok"):
            return {"ok": False, "error": f"CDP auto-attach failed: {result.get('error', 'unknown error')}"}
        self.cdp.touch(record.device_id, tab_id)
        return None

    async def _send_and_await(self, record: DeviceRecord, cmd: QueuedCommand) -> dict[str, Any]:
        """Raw wire send + await the device's result future. No policy, no
        CDP escalation -- `_dispatch_live` is the only caller for real
        commands; tests may call this directly to bypass escalation."""
        assert record.ws is not None  # only called when record.tier is LIVE
        effective_timeout = cmd.timeout if cmd.timeout is not None else self.command_timeout
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
            result_env = await asyncio.wait_for(fut, timeout=effective_timeout)
        except TimeoutError:
            record.pending.pop(cmd.id, None)
            self._inflight.pop(cmd.id, None)
            # Actionable, not just "timeout" (real-world finding: a heavy SPA's
            # `read` needed longer than the prior fixed 30s even at
            # status:"complete" -- see DEFAULT_COMMAND_TIMEOUT's comment and
            # docs/PROTOCOL.md's "Command timeout" section). Discoverability,
            # not decision-making (design doc's "Mechanism, not policy"
            # section): _timeout_hint NAMES an alternative mechanism the
            # caller may choose -- it never retries or substitutes one for
            # another itself.
            return {
                "ok": False,
                "error": (
                    f"timeout waiting {effective_timeout}s for device result on command "
                    f"'{cmd.command}' (device={record.device_id}, tab_id={cmd.target.tab_id}). "
                    "The page may still be loading or a heavy SPA may still be hydrating. Raise "
                    f"the limit for just this command with args.timeout_s=<seconds> (CLI: "
                    f"--timeout <seconds>; MCP tools: timeout_s param), up to {MAX_COMMAND_TIMEOUT}s, "
                    "or raise the hub's own default with `abb hub --command-timeout <seconds>`."
                    f"{self._timeout_hint(cmd.command, cmd.args)}"
                ),
            }
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

    # ------------------------------------------------------------------
    # CDP soft-detach -- design doc §6.3/§7: "so the banner clears while the
    # human is just browsing." Idle tracking lives entirely on the hub side
    # (`CdpRegistry`), so this is testable without real sleeps -- see
    # tests/test_cdp.py, which shortens `cdp_idle_seconds` and calls
    # `soft_detach_idle_tabs()` directly rather than running `soft_detach_loop`.
    # ------------------------------------------------------------------

    async def soft_detach_idle_tabs(self, *, now: Any = None) -> list[tuple[str, int]]:
        """One sweep: detach every CDP-attached tab idle past the configured
        threshold. Returns the (device_id, tab_id) pairs actually detached.
        Safe to call repeatedly and safe to call directly in tests (with an
        injected `now`) for a deterministic proof without waiting out a real
        idle window."""
        detached: list[tuple[str, int]] = []
        for device_id, tab_id in self.cdp.idle_tabs(now=now):
            record = self.registry.get(device_id)
            if record is None or not record.connected:
                continue
            detach_cmd = QueuedCommand(
                id=new_id(),
                target=Target(device_id=device_id, tab_id=tab_id),
                command="detach",
                args={"reason": "idle"},
            )
            result = await self._send_and_await(record, detach_cmd)
            if result.get("ok"):
                detached.append((device_id, tab_id))
        return detached

    async def soft_detach_loop(self, *, interval_seconds: float | None = None) -> None:
        """Background task started by `build_app`'s `on_startup` hook --
        periodically sweeps for idle CDP sessions. Not used by tests, which
        call `soft_detach_idle_tabs()` directly instead of running a real
        timer loop."""
        interval = interval_seconds if interval_seconds is not None else self.soft_detach_sweep_interval
        while True:
            await asyncio.sleep(interval)
            try:
                await self.soft_detach_idle_tabs()
            except Exception:
                logger.exception("soft-detach sweep failed")

    # ------------------------------------------------------------------
    # Devices snapshot -- enriches DeviceRegistry.snapshot() with per-tab CDP
    # attach state (design doc §7: "report CDP attach state per tab so an
    # agent can reason about it"). Separate from DeviceRecord.to_summary()
    # (registry.py) because CDP state is owned by Hub (via CdpRegistry), not
    # by the registry -- see cdp.py's module docstring on why.
    # ------------------------------------------------------------------

    def _devices_snapshot(self) -> list[dict[str, Any]]:
        summaries = self.registry.snapshot()
        for summary in summaries:
            summary["cdp"] = self.cdp.snapshot(summary["device_id"])
        return summaries
