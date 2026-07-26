"""Hub dispatch logic, exercised directly against Hub methods -- no real network
socket involved. A FakeDeviceSocket stands in for aiohttp's WebSocketResponse and
plays the extension's role: when the hub "sends" a command, the fake immediately
resolves it with a canned result, exactly as a real device's `result` message would
arrive asynchronously over the wire.
"""

from __future__ import annotations

import asyncio
from typing import Any

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.hub import Hub
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
