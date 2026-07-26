"""Hub dispatch logic, exercised directly against Hub methods -- no real network
socket involved. A FakeDeviceSocket stands in for aiohttp's WebSocketResponse and
plays the extension's role: when the hub "sends" a command, the fake immediately
resolves it with a canned result, exactly as a real device's `result` message would
arrive asynchronously over the wire.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from typing import Any

import pytest

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.hub import Hub, HubBindError, serve_hub
from amplifier_browser_bridge.tiers import Tier


class FakeDeviceSocket:
    """Simulates a live device connection. `sent` records every envelope the hub
    would have sent over the wire; `send_json` immediately fulfils the
    corresponding pending future on the record, as if the device replied instantly.
    """

    def __init__(self, record: Any, canned_result: dict[str, Any] | None = None) -> None:
        self.record = record
        self.sent: list[dict[str, Any]] = []
        self.canned_result = canned_result or {"ok": True, "result": {"stub": True}}

    async def send_json(self, data: dict[str, Any], /) -> None:
        self.sent.append(data)
        fut = self.record.pending.get(data["id"])
        if fut is not None and not fut.done():
            fut.set_result({**self.canned_result, "id": data["id"]})


def _hub(tmp_path: Any) -> Hub:
    return Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"))


def test_unknown_device_fails_loud(tmp_path: Any) -> None:
    hub = _hub(tmp_path)

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="ghost", tab_id=1), "snapshot", {})

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "unknown device" in result["error"]


def test_unknown_command_fails_loud(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    record.ws = FakeDeviceSocket(record)
    record.touch()

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=1), "not_a_real_command", {})

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "unknown command" in result["error"]


def test_live_device_executes_immediately(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    fake_ws = FakeDeviceSocket(record, canned_result={"ok": True, "result": {"title": "Example"}})
    record.ws = fake_ws
    record.touch()
    assert record.tier is Tier.LIVE

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=7), "read", {})

    result = asyncio.run(run())
    assert result == {"ok": True, "result": {"title": "Example"}}
    assert len(fake_ws.sent) == 1
    assert fake_ws.sent[0]["command"] == "read"
    assert fake_ws.sent[0]["target"] == {"device_id": "d1", "tab_id": 7}


def test_offline_device_queues_and_returns_immediately(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    hub.registry.get_or_create("d1")  # never bound -- never connected, so DORMANT

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=1), "snapshot", {})

    result = asyncio.run(run())
    assert result["status"] == "queued"
    assert result["tier"] == Tier.DORMANT.value
    assert result["queue_position"] == 1
    assert "command_id" in result


def test_two_tabs_on_one_device_do_not_cross_contaminate(tmp_path: Any) -> None:
    """The addressing proof, at the hub layer: two commands targeting different
    tab_ids on the same device must be dispatched with distinct target payloads."""
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    fake_ws = FakeDeviceSocket(record)
    record.ws = fake_ws
    record.touch()

    async def run() -> None:
        await hub.send_command(Target(device_id="d1", tab_id=10), "read", {})
        await hub.send_command(Target(device_id="d1", tab_id=11), "read", {})

    asyncio.run(run())
    targets_sent = [env["target"] for env in fake_ws.sent]
    assert targets_sent == [{"device_id": "d1", "tab_id": 10}, {"device_id": "d1", "tab_id": 11}]
    assert targets_sent[0] != targets_sent[1]


def test_establish_session_mints_a_fresh_id_never_caller_supplied(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    scope = hub.establish_session(write=["github.com"])
    assert scope.session_id
    # The wire handler ignores any session_id the caller might try to pass --
    # SCOPE_FIELDS never includes it, so a smuggled one is simply dropped.
    result = hub._handle_establish_session({"session_id": "attacker-chosen", "write": ["evil.com"]})
    assert result["ok"] is True
    assert result["session_id"] != "attacker-chosen"


def test_command_with_session_id_enforces_write_scope(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    record.ws = FakeDeviceSocket(record)
    record.touch()
    scope = hub.establish_session(write=["github.com"])

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1),
            "navigate",
            {"url": "https://repos.opensource.microsoft.com/foo"},
            session_id=scope.session_id,
        )

    result = asyncio.run(run())
    assert result["ok"] is False
    assert result["reason_code"] == "out_of_scope"
    assert "github.com" in result["error"]


def test_command_with_in_scope_origin_reaches_the_device(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    fake_ws = FakeDeviceSocket(record, canned_result={"ok": True, "result": {"ref": "e1"}})
    record.ws = fake_ws
    record.touch()
    scope = hub.establish_session(write=["github.com"])

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1),
            "navigate",
            {"url": "https://github.com/foo"},
            session_id=scope.session_id,
        )

    result = asyncio.run(run())
    assert result["ok"] is True
    assert len(fake_ws.sent) == 1


def test_unknown_session_id_fails_loud_rather_than_falling_back_permissive(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    record.ws = FakeDeviceSocket(record)
    record.touch()

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1), "click", {"ref": "e1"}, session_id="never-established"
        )

    result = asyncio.run(run())
    assert result["ok"] is False
    assert result["reason_code"] == "unknown_session"


def test_narrow_scope_wire_handler_rejects_widening(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    scope = hub.establish_session(write=["github.com"])
    result = hub._handle_narrow_scope({"session_id": scope.session_id, "write": ["github.com", "evil.com"]})
    assert result["ok"] is False
    assert "strict subset" in result["error"]
    assert scope.write == ("github.com",)


class _IngestingFakeDeviceSocket:
    """Unlike the plain `FakeDeviceSocket` above (which resolves the pending
    future directly, bypassing `Hub._ingest_result`), this fake routes the
    canned result through `_ingest_result` first -- exactly what
    `Hub._handle_device_message`'s `result` branch does for a REAL device
    message (same pattern as `tests/test_ref_hints.py`'s fake). Required
    here because seal-on-first-read (`Hub._maybe_seal_session`) is invoked
    from inside `_ingest_result`, not from the raw future-resolution path."""

    def __init__(self, hub: Hub, record: Any, canned_result: dict[str, Any]) -> None:
        self.hub = hub
        self.record = record
        self.canned_result = canned_result

    async def send_json(self, data: dict[str, Any], /) -> None:
        fut = self.record.pending.get(data["id"])
        if fut is not None and not fut.done():
            raw_env = {**self.canned_result, "id": data["id"]}
            env = self.hub._ingest_result(self.record.device_id, data["id"], raw_env)
            fut.set_result(env)


def test_read_snapshot_seals_the_session_and_blocks_further_narrowing(tmp_path: Any) -> None:
    """The load-bearing anti-injection property, exercised end-to-end through
    the hub: a `snapshot` result reaching the caller seals the session, and
    every subsequent narrow_scope call for it -- even a further-narrowing one
    -- is rejected outright."""
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    fake_ws = _IngestingFakeDeviceSocket(
        hub, record, canned_result={"ok": True, "result": {"url": "https://github.com/x", "nodes": []}}
    )
    record.ws = fake_ws
    record.touch()
    scope = hub.establish_session(write=["github.com", "contoso.com"])
    assert not scope.sealed

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1), "snapshot", {}, session_id=scope.session_id
        )

    result = asyncio.run(run())
    assert result["ok"] is True
    assert scope.sealed

    narrowed = hub._handle_narrow_scope({"session_id": scope.session_id, "write": ["github.com"]})
    assert narrowed["ok"] is False
    assert "sealed" in narrowed["error"]
    assert scope.write == ("github.com", "contoso.com")  # unchanged


def test_session_scope_survives_device_disconnect_and_reconnect(tmp_path: Any) -> None:
    """A session is hub-process state, not device-connection state -- mobile
    devices drop and re-attach by design (the three-tier connectivity model),
    and a scope that evaporated on reconnect would defeat its own purpose."""
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    record.ws = FakeDeviceSocket(record)
    record.touch()
    scope = hub.establish_session(write=["github.com"])

    # Simulate a disconnect (unbind, as `_handle_device_ws` does on socket
    # close) followed by a reconnect (a fresh `hello`/bind).
    record.unbind()
    assert record.tier is not Tier.LIVE
    new_ws = FakeDeviceSocket(record)
    record.bind(new_ws, {"device_id": "d1"})
    record.touch()
    assert record.tier is Tier.LIVE

    # The session established before the reconnect is still there, and still
    # enforces its original scope.
    assert hub._sessions[scope.session_id] is scope

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1),
            "navigate",
            {"url": "https://repos.opensource.microsoft.com/foo"},
            session_id=scope.session_id,
        )

    result = asyncio.run(run())
    assert result["ok"] is False
    assert result["reason_code"] == "out_of_scope"


def test_confirm_replay_is_rechecked_against_the_original_sessions_scope(tmp_path: Any) -> None:
    """Scope enforcement runs BEFORE skip_gate (design doc section 12) -- a
    gate fired under one session's scope must be re-checked against that
    SAME scope on redemption, not dispatched scope-free.

    `allow_self_attested_escalation=True` is declared explicitly here (FIX 3,
    product review panel): the elevate label classifies as `permission_change`,
    an ESCALATION_CATEGORIES member, which is now forced to
    `redeem="unredeemable"` regardless of write scope UNLESS a session opts in
    -- this test's purpose is the scope-recheck-on-redeem property, not the
    escalation lock itself (see `tests/test_escalation_category.py` for that).
    """
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    fake_ws = FakeDeviceSocket(record, canned_result={"ok": True, "result": {"ref": "e1"}})
    record.ws = fake_ws
    record.touch()
    # In scope for github.com; the classifier fires on the elevate label
    # regardless (label-alone gate, case 1's exact configuration).
    scope = hub.establish_session(write=["github.com"], allow_self_attested_escalation=True)

    async def gate() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1, ref="e1"),
            "click",
            {"ref": "e1", "label": "Elevate bkrabach to Administrator", "page_url": "https://github.com/x"},
            session_id=scope.session_id,
        )

    gated = asyncio.run(gate())
    assert gated["status"] == "needs_confirmation"
    token = gated["confirmation_token"]

    async def confirm() -> dict[str, Any]:
        return await hub._handle_agent_confirm({"confirmation_token": token})

    confirmed = asyncio.run(confirm())
    # In-scope origin -> the confirmed replay reaches the device.
    assert confirmed["ok"] is True


# ---------------------------------------------------------------------------
# F6 (review panel): session sealing had no named serialization point -- two
# commands dispatched before the first response lands could both evaluate
# against pre-seal scope. `send_command` now acquires a per-session_id
# asyncio.Lock across the full evaluate-through-dispatch span, so a second
# command sharing a session cannot begin `PolicyEngine.evaluate` until the
# first command's full round trip -- including seal-on-first-read -- has
# completed.
# ---------------------------------------------------------------------------


class _SlowIngestingFakeDeviceSocket:
    """Like `_IngestingFakeDeviceSocket` above, but delays resolving the
    pending future until `release_event` is set -- lets a test hold command A
    "in flight" while command B is dispatched concurrently, to prove ordering
    rather than hoping a race resolves favorably."""

    def __init__(
        self, hub: Hub, record: Any, canned_result: dict[str, Any], release_event: asyncio.Event
    ) -> None:
        self.hub = hub
        self.record = record
        self.canned_result = canned_result
        self.release_event = release_event

    async def send_json(self, data: dict[str, Any], /) -> None:
        await self.release_event.wait()
        fut = self.record.pending.get(data["id"])
        if fut is not None and not fut.done():
            raw_env = {**self.canned_result, "id": data["id"]}
            env = self.hub._ingest_result(self.record.device_id, data["id"], raw_env)
            fut.set_result(env)


def test_concurrent_commands_on_same_session_serialize_through_the_session_lock(tmp_path: Any) -> None:
    """Two commands sharing a session_id, dispatched concurrently (command A,
    a `snapshot` that will seal the session, held in flight; command B, a
    state-changing `navigate` to an origin that would be OUT of a narrower
    scope) must not have B's `evaluate()` run while A's response -- and its
    seal -- is still pending. Proven by starting both concurrently, releasing
    A first, and asserting A's seal is visible by the time B's evaluation
    happens (B is dispatched only after A completes in this test, matching
    what the lock enforces: no interleaving of the evaluate-through-dispatch
    span for a shared session)."""
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    release_a = asyncio.Event()
    fake_ws = _SlowIngestingFakeDeviceSocket(
        hub,
        record,
        canned_result={"ok": True, "result": {"url": "https://github.com/x", "nodes": []}},
        release_event=release_a,
    )
    record.ws = fake_ws
    record.touch()
    scope = hub.establish_session(write=["github.com", "contoso.com"])
    assert not scope.sealed

    order: list[str] = []

    async def command_a() -> dict[str, Any]:
        result = await hub.send_command(
            Target(device_id="d1", tab_id=1), "snapshot", {}, session_id=scope.session_id
        )
        order.append("a_done")
        return result

    async def command_b() -> dict[str, Any]:
        # Give command_a's send_command a chance to acquire the session lock
        # first (it starts first below), then attempt to enter the lock too --
        # this call must BLOCK until command_a releases it.
        await asyncio.sleep(0)
        result = await hub.send_command(
            Target(device_id="d1", tab_id=1),
            "navigate",
            {"url": "https://github.com/x"},
            session_id=scope.session_id,
        )
        order.append("b_done")
        return result

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        task_a = asyncio.create_task(command_a())
        task_b = asyncio.create_task(command_b())
        # Let both tasks actually start and block (task_b waiting on the lock,
        # task_a waiting on release_a) before releasing A.
        await asyncio.sleep(0.01)
        assert not scope.sealed  # neither has completed yet -- A is still in flight
        release_a.set()
        return await task_a, await task_b

    result_a, result_b = asyncio.run(run())
    assert result_a["ok"] is True
    assert scope.sealed  # A's seal-on-first-read ran before B's evaluate could
    assert result_b["ok"] is True  # B is in-scope (github.com), so it proceeds
    # A's full round trip (including seal) completed before B's completed --
    # the lock forces this ordering rather than leaving it to chance.
    assert order == ["a_done", "b_done"]


def test_auth_rejects_wrong_token(tmp_path: Any) -> None:
    store = TokenStore(default_token="secret")
    assert store.validate("secret") is True
    assert store.validate("wrong") is False
    assert store.validate(None) is False


def test_auth_disabled_when_unconfigured(tmp_path: Any) -> None:
    store = TokenStore()
    assert store.auth_enabled is False
    assert store.validate(None) is True
    assert store.validate("anything") is True


# ---------------------------------------------------------------------------
# Gap 2: configurable per-command device-round-trip timeout (real-world finding
# -- a heavy SPA's `read` timed out at the prior fixed 30s default even at
# status:"complete"; see hub.py's DEFAULT_COMMAND_TIMEOUT / MIN/MAX_COMMAND_TIMEOUT
# and protocol.py's HUB_ONLY_ARGS).
# ---------------------------------------------------------------------------


class _NeverRespondingSocket:
    """A device connection that accepts the send but never resolves the
    pending future -- simulates a device that is connected (LIVE) but whose
    command genuinely never returns, so the hub's own wait is what expires."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any], /) -> None:
        self.sent.append(data)
        # Deliberately never resolves record.pending[data["id"]].


def test_timeout_s_override_fires_before_the_hub_default(tmp_path: Any) -> None:
    """A caller-supplied args.timeout_s shorter than the hub's default must be
    the one that actually expires -- proves the override reaches asyncio.wait_for,
    not just gets validated and ignored."""
    hub = Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"), command_timeout=120.0)
    record = hub.registry.get_or_create("d1")
    record.ws = _NeverRespondingSocket()
    record.touch()
    assert record.tier is Tier.LIVE

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=1), "read", {"timeout_s": 1.0})

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "timeout waiting 1.0s" in result["error"]
    assert "'read'" in result["error"]
    assert "args.timeout_s" in result["error"]  # actionable: names how to raise it


def test_timeout_s_rejects_out_of_range_values(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    record.ws = FakeDeviceSocket(record)
    record.touch()

    async def run(value: Any) -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=1), "read", {"timeout_s": value})

    too_small = asyncio.run(run(0.0))
    assert too_small["ok"] is False
    assert "args.timeout_s" in too_small["error"]

    too_large = asyncio.run(run(99999.0))
    assert too_large["ok"] is False
    assert "args.timeout_s" in too_large["error"]

    not_a_number = asyncio.run(run("soon"))
    assert not_a_number["ok"] is False
    assert "args.timeout_s" in not_a_number["error"]


def test_timeout_s_never_reaches_the_device_wire(tmp_path: Any) -> None:
    """timeout_s is a hub-only knob (protocol.py's HUB_ONLY_ARGS) -- it must never
    show up in the envelope actually sent to the device."""
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    fake_ws = FakeDeviceSocket(record)
    record.ws = fake_ws
    record.touch()

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1), "read", {"timeout_s": 5.0, "wake": True}
        )

    result = asyncio.run(run())
    assert result["ok"] is True
    assert len(fake_ws.sent) == 1
    assert "timeout_s" not in fake_ws.sent[0]["args"]
    assert fake_ws.sent[0]["args"] == {"wake": True}  # every OTHER arg still passes through


def test_default_timeout_used_when_no_override_given(tmp_path: Any) -> None:
    """Without args.timeout_s, a command still uses the hub's configured default
    (proves the override plumbing doesn't silently break the no-override path)."""
    hub = Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"), command_timeout=0.05)
    record = hub.registry.get_or_create("d1")
    record.ws = _NeverRespondingSocket()
    record.touch()

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=1), "read", {})

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "timeout waiting 0.05s" in result["error"]


def test_queued_command_preserves_timeout_override_until_drained(tmp_path: Any) -> None:
    """A command that queues on a non-live device must not silently revert to the
    hub default once the device reconnects and the command drains."""
    hub = Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"), command_timeout=120.0)
    hub.registry.get_or_create("d1")  # never bound -- DORMANT

    async def enqueue() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=1), "read", {"timeout_s": 5.0})

    queued = asyncio.run(enqueue())
    assert queued["status"] == "queued"

    record = hub.registry.get_or_create("d1")
    assert len(record.queue) == 1
    stored_cmd = next(iter(record.queue))
    assert stored_cmd.timeout == 5.0


# ---------------------------------------------------------------------------
# Task 3: discoverable alternatives named in error text (never automatically
# retried/escalated -- see docs/designs/browser-bridge.md's "Mechanism, not
# policy" section).
# ---------------------------------------------------------------------------


def test_read_timeout_names_all_frames_as_an_alternative_when_not_already_set(tmp_path: Any) -> None:
    hub = Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"), command_timeout=120.0)
    record = hub.registry.get_or_create("d1")
    record.ws = _NeverRespondingSocket()
    record.touch()

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=1), "read", {"timeout_s": 1.0})

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "args.all_frames=true" in result["error"]


def test_read_timeout_with_all_frames_already_set_names_frame_id_instead(tmp_path: Any) -> None:
    hub = Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"), command_timeout=120.0)
    record = hub.registry.get_or_create("d1")
    record.ws = _NeverRespondingSocket()
    record.touch()

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1), "read", {"timeout_s": 1.0, "all_frames": True}
        )

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "args.frame_id" in result["error"]
    # Doesn't re-suggest the option the caller already used.
    assert "args.all_frames=true gathers" not in result["error"]


def test_click_timeout_names_trusted_as_an_alternative(tmp_path: Any) -> None:
    hub = Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"), command_timeout=120.0)
    record = hub.registry.get_or_create("d1")
    record.ws = _NeverRespondingSocket()
    record.touch()

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=1, ref="e1"), "click", {"timeout_s": 1.0})

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "args.trusted=true" in result["error"]


def test_navigate_timeout_names_no_alternative(tmp_path: Any) -> None:
    """A command with no relevant alternative gets no hint appended -- the
    hint function must not invent one just to say something."""
    hub = Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"), command_timeout=120.0)
    record = hub.registry.get_or_create("d1")
    record.ws = _NeverRespondingSocket()
    record.touch()

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1), "navigate", {"timeout_s": 1.0, "url": "x"}
        )

    result = asyncio.run(run())
    assert result["ok"] is False
    assert result["error"].rstrip().endswith("--command-timeout <seconds>`.")


# ---------------------------------------------------------------------------
# Bug 3 (real-profile hardening): a DOM-injecting command timing out on a
# non-active tab names ALL real alternatives -- activate, vision_read (for
# read/snapshot only), and raising timeout_s -- never picks one automatically.
# ---------------------------------------------------------------------------


def test_snapshot_timeout_names_activate_and_vision_read_when_not_already_activated(tmp_path: Any) -> None:
    hub = Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"), command_timeout=120.0)
    record = hub.registry.get_or_create("d1")
    record.ws = _NeverRespondingSocket()
    record.touch()

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=1), "snapshot", {"timeout_s": 1.0})

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "args.activate=true" in result["error"]
    assert "vision_read" in result["error"]
    assert "args.timeout_s" in result["error"]


def test_snapshot_timeout_does_not_resuggest_activate_when_already_set(tmp_path: Any) -> None:
    hub = Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"), command_timeout=120.0)
    record = hub.registry.get_or_create("d1")
    record.ws = _NeverRespondingSocket()
    record.touch()

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1), "snapshot", {"timeout_s": 1.0, "activate": True}
        )

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "args.activate=true activates" not in result["error"]


def test_click_timeout_names_activate_alongside_trusted(tmp_path: Any) -> None:
    hub = Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"), command_timeout=120.0)
    record = hub.registry.get_or_create("d1")
    record.ws = _NeverRespondingSocket()
    record.touch()

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=1, ref="e1"), "click", {"timeout_s": 1.0})

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "args.trusted=true" in result["error"]
    assert "args.activate=true" in result["error"]
    # click/type/key don't get the vision_read mention -- that's a read/snapshot-only alternative.
    assert "vision_read" not in result["error"]


def test_auth_per_device_override(tmp_path: Any) -> None:
    store = TokenStore(default_token="default-tok", device_tokens={"d1": "special-tok"})
    assert store.validate("special-tok", device_id="d1") is True
    assert store.validate("default-tok", device_id="d1") is False  # d1 has its own token
    assert store.validate("default-tok", device_id="d2") is True  # d2 falls back to default


# ----------------------------------------------------------------------
# serve_hub -- Bug B regression: bind THEN announce, and turn a bind failure
# (e.g. address already in use) into a clean HubBindError, never a raw OSError
# with a traceback.
# ----------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_serve_hub_binds_before_calling_on_bound(tmp_path: Any) -> None:
    """The core regression for Bug B: `on_bound` (where the CLI's "listening"
    banner gets printed) must fire only AFTER the socket is actually bound and
    accepting connections -- never before."""
    hub = _hub(tmp_path)
    app = hub.build_app()
    port = _free_port()
    bound_calls: list[bool] = []

    async def run() -> None:
        task = asyncio.create_task(
            serve_hub(app, "127.0.0.1", port, on_bound=lambda: bound_calls.append(True))
        )
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if bound_calls:
                    break
                await asyncio.sleep(0.05)
            assert bound_calls, "on_bound was never called"

            # The port must genuinely be accepting connections BY THE TIME
            # on_bound fired -- not just "we called TCPSite.start() and hoped".
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.settimeout(2.0)
                client.connect(("127.0.0.1", port))
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(run())


def test_serve_hub_raises_hub_bind_error_on_port_already_in_use(tmp_path: Any) -> None:
    """Second regression for Bug B: binding to a port already held by another
    listener must raise a clean `HubBindError` (never a bare OSError/traceback),
    and `on_bound` must NEVER be called -- nothing may claim success for a bind
    that didn't happen."""
    hub = _hub(tmp_path)
    app = hub.build_app()
    port = _free_port()
    bound_calls: list[bool] = []

    async def run() -> None:
        # Occupy the port first, exactly like "a hub is already running there".
        occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupier.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupier.bind(("127.0.0.1", port))
        occupier.listen(1)
        try:
            with pytest.raises(HubBindError) as exc_info:
                await serve_hub(app, "127.0.0.1", port, on_bound=lambda: bound_calls.append(True))
            assert str(port) in str(exc_info.value)
            assert "already in use" in str(exc_info.value)
        finally:
            occupier.close()

    asyncio.run(run())
    assert bound_calls == []
