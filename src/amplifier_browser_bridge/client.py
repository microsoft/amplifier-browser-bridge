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
    # Buffer added on top of a caller's requested `args.timeout_s` for this
    # CLIENT's own websocket recv() -- must always be at least as generous as
    # whatever the HUB is willing to wait for its device round trip, or the
    # client gives up and disconnects before the hub's (still-legitimate)
    # answer ever arrives. Real-world finding: a `read` with args.timeout_s=45
    # (comfortably within hub.py's DEFAULT_COMMAND_TIMEOUT) still failed
    # client-side with a raw asyncio CancelledError/websockets recv() timeout,
    # because this class's own `timeout` (35.0 default, unrelated to the hub's
    # timeout) expired first. 10s covers connection setup + JSON round-trip
    # overhead beyond the hub's own wait.
    _TIMEOUT_BUFFER_S = 10.0

    def __init__(self, url: str, token: str | None = None, timeout: float = 35.0) -> None:
        """`url` is the full hub agent endpoint, e.g. ws://<your tailnet IP>:8900/agent.
        `timeout` is this client's OWN default wait for a hub response when no
        per-command override is known ahead of time (`list_devices`/`poll`, and
        `command()` calls that don't set `args["timeout_s"]`)."""
        self.url = url
        self.token = token
        self.timeout = timeout

    async def _request(self, req: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        effective_timeout = timeout if timeout is not None else self.timeout
        req = {**req, "token": self.token}
        # ping_interval=None: this class opens one connection per request, sends
        # exactly one message, awaits exactly one correlated reply, and closes
        # (module docstring) -- there is no long-lived session for a dead-peer
        # keepalive to protect. Real-world finding: with the library's default
        # ping_interval/ping_timeout (20s/20s), a legitimately long device round
        # trip (args.timeout_s raised past ~20s) got the CLIENT's own connection
        # closed out from under it by `websockets`' keepalive machinery
        # (`ConnectionClosedError: ... keepalive ping timeout`) well before
        # `effective_timeout` -- the hub was still working; the client silently
        # gave up first. `open_timeout` (connection establishment) is unaffected
        # and stays enforced.
        async with websockets.connect(self.url, open_timeout=10, ping_interval=None) as ws:
            await ws.send(json.dumps(req))
            raw = await asyncio.wait_for(ws.recv(), timeout=effective_timeout)
            resp: dict[str, Any] = json.loads(raw)
        if resp.get("type") == "error":
            raise HubError(resp.get("error", "unknown hub error"))
        return resp

    async def list_devices(self) -> list[dict[str, Any]]:
        resp = await self._request({"v": PROTOCOL_VERSION, "id": new_id(), "type": "list_devices"})
        return list(resp.get("devices", []))

    async def command(
        self,
        target: Target,
        command: str,
        args: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """`session_id`, if given, must come from a prior `establish_session()`
        call -- the hub enforces that session's declared write scope
        (`scope.py`, docs/designs/confirmation-gate.md section 11.2) against
        this command before it can reach the device. Omitting it keeps the
        existing fully-permissive default every caller that predates
        sessions already gets."""
        args = args or {}
        # If the caller asked the HUB to wait longer than this client's own
        # default (via args.timeout_s -- see hub.py/protocol.py's HUB_ONLY_ARGS),
        # this client's own recv() must wait at least that long too, or it
        # disconnects before the hub's answer can arrive. `timeout_s`'s own
        # validity (numeric, in-range) is the hub's job (`Hub._extract_timeout_override`);
        # this is a best-effort read of the same value purely to size the
        # CLIENT-side wait, so an invalid value here just falls back to
        # `self.timeout` and lets the hub return its own actionable error.
        client_timeout = self.timeout
        raw_timeout_s = args.get("timeout_s")
        if raw_timeout_s is not None:
            try:
                client_timeout = max(self.timeout, float(raw_timeout_s) + self._TIMEOUT_BUFFER_S)
            except (TypeError, ValueError):
                pass
        resp = await self._request(
            {
                "v": PROTOCOL_VERSION,
                "id": new_id(),
                "type": "command",
                "command": command,
                "target": target.to_dict(),
                "args": args,
            },
            timeout=client_timeout,
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

    async def confirm(self, confirmation_token: str) -> dict[str, Any]:
        """Redeem a single-use confirmation token from a prior
        `needs_confirmation` response (docs/designs/confirmation-gate.md, D2 --
        "a fired gate is a dead end without a redemption surface"). Sends the
        hub's existing `confirm` agent-route message (`Hub._handle_agent_confirm`,
        already wired at the wire level; this is the first CLI/lib-facing
        caller of it). `redeem: "agent"` (this method) is self-attestation --
        the caller (a human via `amplifier-browser-bridge confirm`, or an agent process explicitly
        deciding to proceed) makes a second, separately-audited decision.
        `redeem: "unredeemable"` sessions can never be confirmed through this
        method (or any other route in this system) -- there is no human-
        approval channel, by design; see docs/designs/approval-channel-options.md
        for the cancellation and its reasoning."""
        resp = await self._request(
            {
                "v": PROTOCOL_VERSION,
                "id": new_id(),
                "type": "confirm",
                "confirmation_token": confirmation_token,
            }
        )
        return resp

    async def establish_session(
        self,
        *,
        read: str | list[str] | tuple[str, ...] = "*",
        write: str | list[str] | tuple[str, ...] = "*",
        on_unknown: str = "allow",
        redeem: str = "agent",
        unattended: bool = False,
        allow_self_attested_escalation: bool = False,
    ) -> dict[str, Any]:
        """Create a brand-new session with a caller-declared write scope
        (docs/designs/confirmation-gate.md section 11.2, Candidate C). The
        hub ALWAYS mints a fresh `session_id` -- pass it as `session_id` to
        `command()` to enforce this scope, or to `narrow_scope()` to shrink
        it further. Returns `{"ok": True, "session_id": ..., "scope": {...}}`
        on success, or `{"ok": False, "error": ...}` if the declared values
        were malformed (e.g. `write` given as something other than `"*"` or a
        list of hostnames).

        `allow_self_attested_escalation` (FIX 3, product review panel)
        defaults to `False`: even when `write` covers the origin, an action
        classified into `classify.ESCALATION_CATEGORIES` (e.g.
        `permission_change` -- the measured incident's own category) is
        forced to `redeem="unredeemable"` unless this is explicitly set
        `True` here, at establishment. It cannot be turned on later via
        `narrow_scope` -- see `scope.py`'s docstring."""
        resp = await self._request(
            {
                "v": PROTOCOL_VERSION,
                "id": new_id(),
                "type": "establish_session",
                "read": list(read) if isinstance(read, (list, tuple)) else read,
                "write": list(write) if isinstance(write, (list, tuple)) else write,
                "on_unknown": on_unknown,
                "redeem": redeem,
                "unattended": unattended,
                "allow_self_attested_escalation": allow_self_attested_escalation,
            }
        )
        return resp

    async def narrow_scope(
        self,
        session_id: str,
        *,
        read: list[str] | tuple[str, ...] | None = None,
        write: list[str] | tuple[str, ...] | None = None,
        on_unknown: str | None = None,
        redeem: str | None = None,
        unattended: bool | None = None,
    ) -> dict[str, Any]:
        """Narrow an EXISTING session's scope -- never widens (scope.py's
        `SessionScope.narrow`). Only the fields explicitly passed here are
        touched; omitted fields (`None`, the default) are left exactly as
        they are. Fails with `{"ok": False, "error": ...}` naming the
        specific violation on any widening attempt, or if the session has
        already sealed (ingested page content) and can no longer change at
        all."""
        req: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "id": new_id(),
            "type": "narrow_scope",
            "session_id": session_id,
        }
        if read is not None:
            req["read"] = list(read)
        if write is not None:
            req["write"] = list(write)
        if on_unknown is not None:
            req["on_unknown"] = on_unknown
        if redeem is not None:
            req["redeem"] = redeem
        if unattended is not None:
            req["unattended"] = unattended
        resp = await self._request(req)
        return resp
