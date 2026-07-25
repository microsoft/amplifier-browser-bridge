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


def test_auth_per_device_override(tmp_path: Any) -> None:
    store = TokenStore(default_token="default-tok", device_tokens={"d1": "special-tok"})
    assert store.validate("special-tok", device_id="d1") is True
    assert store.validate("default-tok", device_id="d1") is False  # d1 has its own token
    assert store.validate("default-tok", device_id="d2") is True  # d2 falls back to default
