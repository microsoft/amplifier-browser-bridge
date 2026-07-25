"""Independent per-device state -- the structural fix for the reference
implementation's single `state = {"browser": None}` slot, which silently overwrote
itself on a second connection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from amplifier_browser_bridge.registry import DeviceRegistry
from amplifier_browser_bridge.tiers import Tier


class _FakeWebSocket:
    """Stands in for aiohttp.web.WebSocketResponse -- registry/DeviceRecord never
    need anything from it except identity (is it set or not) and, structurally,
    an async send_json (the DeviceConnection protocol -- see registry.py)."""

    async def send_json(self, data: object, /) -> None:
        pass


def test_two_devices_do_not_clobber_each_other() -> None:
    registry = DeviceRegistry()
    a = registry.get_or_create("device-a")
    b = registry.get_or_create("device-b")

    a.bind(_FakeWebSocket(), {"label": "edge-macos", "platform": "macOS", "capabilities": {"windows": True}})
    b.bind(
        _FakeWebSocket(), {"label": "edge-android", "platform": "Android", "capabilities": {"windows": False}}
    )

    # Each device's own record, fetched independently, reflects only its own hello --
    # never information leaking from the other connection.
    assert registry.get("device-a") is a
    assert registry.get("device-b") is b
    assert a.label == "edge-macos"
    assert b.label == "edge-android"
    assert a.capabilities != b.capabilities
    assert a.connected and b.connected


def test_get_or_create_is_stable_across_calls() -> None:
    registry = DeviceRegistry()
    first = registry.get_or_create("device-a")
    second = registry.get_or_create("device-a")
    assert first is second


def test_unknown_device_get_returns_none() -> None:
    registry = DeviceRegistry()
    assert registry.get("nope") is None


def test_unbind_disconnects_but_preserves_identity_and_queue() -> None:
    registry = DeviceRegistry()
    record = registry.get_or_create("device-a")
    record.bind(_FakeWebSocket(), {"label": "edge-macos", "platform": "macOS", "capabilities": {}})
    assert record.tier is Tier.LIVE

    record.unbind()
    assert not record.connected
    # Identity survives a disconnect -- only the socket goes away (design doc §5:
    # "the service worker never dies -- only the socket does -- queued state and
    # identity survive blackouts intact").
    assert record.label == "edge-macos"


def test_disconnected_recent_device_is_intermittent() -> None:
    registry = DeviceRegistry()
    record = registry.get_or_create("device-a")
    record.bind(_FakeWebSocket(), {})
    record.unbind()
    record.last_seen = datetime.now(UTC) - timedelta(seconds=10)
    assert record.tier is Tier.INTERMITTENT


def test_disconnected_stale_device_is_dormant() -> None:
    registry = DeviceRegistry()
    record = registry.get_or_create("device-a")
    record.bind(_FakeWebSocket(), {})
    record.unbind()
    record.last_seen = datetime.now(UTC) - timedelta(seconds=600)
    assert record.tier is Tier.DORMANT


def test_snapshot_includes_all_devices_independently() -> None:
    registry = DeviceRegistry()
    registry.get_or_create("device-a").bind(_FakeWebSocket(), {"label": "a"})
    registry.get_or_create("device-b").bind(_FakeWebSocket(), {"label": "b"})
    summaries = {s["device_id"]: s for s in registry.snapshot()}
    assert set(summaries) == {"device-a", "device-b"}
    assert summaries["device-a"]["label"] == "a"
    assert summaries["device-b"]["label"] == "b"
