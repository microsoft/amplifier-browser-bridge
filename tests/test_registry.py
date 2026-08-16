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

    async def close(self) -> None:
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


# ---------------------------------------------------------------------------
# Tier 0 handshake -- self-reported command set + manifest version
# ---------------------------------------------------------------------------


def test_bind_without_commands_field_leaves_commands_none() -> None:
    """A pre-Tier-0 `hello` (every extension shipped before this feature)
    omits `commands` entirely -- this must leave `record.commands` as `None`,
    never an empty frozenset, so skew.py can tell "never reported" apart
    from "reported supporting nothing" (see registry.py's field docstring)."""
    registry = DeviceRegistry()
    record = registry.get_or_create("device-a")
    record.bind(_FakeWebSocket(), {"label": "edge-macos", "platform": "macOS", "capabilities": {}})
    assert record.commands is None
    assert record.manifest_version is None


def test_bind_with_commands_field_stores_a_frozenset() -> None:
    registry = DeviceRegistry()
    record = registry.get_or_create("device-a")
    record.bind(
        _FakeWebSocket(),
        {
            "label": "edge-macos",
            "platform": "macOS",
            "capabilities": {},
            "commands": ["snapshot", "click", "reload"],
            "manifest_version": "0.5.0",
        },
    )
    assert record.commands == frozenset({"snapshot", "click", "reload"})
    assert record.manifest_version == "0.5.0"


def test_to_summary_reports_commands_none_and_manifest_version_and_connected_at() -> None:
    registry = DeviceRegistry()
    record = registry.get_or_create("device-a")
    record.bind(_FakeWebSocket(), {"label": "edge-macos", "platform": "macOS", "capabilities": {}})
    summary = record.to_summary()
    assert summary["commands"] is None
    assert summary["manifest_version"] is None
    assert summary["connected_at"] is not None  # bound just now -- must be populated


def test_to_summary_reports_sorted_commands_list() -> None:
    registry = DeviceRegistry()
    record = registry.get_or_create("device-a")
    record.bind(
        _FakeWebSocket(),
        {
            "label": "edge-macos",
            "platform": "macOS",
            "capabilities": {},
            "commands": ["click", "reload", "snapshot"],
        },
    )
    summary = record.to_summary()
    assert summary["commands"] == ["click", "reload", "snapshot"]


# ---------------------------------------------------------------------------
# Build-freshness handshake -- self-reported content hash (build_stamp.py)
# ---------------------------------------------------------------------------


def test_bind_without_build_stamp_field_leaves_build_stamp_none() -> None:
    """A pre-stamp `hello` (every extension shipped before this feature)
    omits `build_stamp` entirely -- this must leave `record.build_stamp` as
    `None`, the same distinct 'never reported' state `commands` already
    carries (see registry.py's field docstring and build_stamp.py's
    'pre-stamp case')."""
    registry = DeviceRegistry()
    record = registry.get_or_create("device-a")
    record.bind(_FakeWebSocket(), {"label": "edge-macos", "platform": "macOS", "capabilities": {}})
    assert record.build_stamp is None


def test_bind_with_build_stamp_field_stores_it() -> None:
    registry = DeviceRegistry()
    record = registry.get_or_create("device-a")
    record.bind(
        _FakeWebSocket(),
        {"label": "edge-macos", "platform": "macOS", "capabilities": {}, "build_stamp": "a" * 64},
    )
    assert record.build_stamp == "a" * 64


def test_to_summary_reports_build_stamp_none_when_never_reported() -> None:
    registry = DeviceRegistry()
    record = registry.get_or_create("device-a")
    record.bind(_FakeWebSocket(), {"label": "edge-macos", "platform": "macOS", "capabilities": {}})
    assert record.to_summary()["build_stamp"] is None


def test_to_summary_reports_the_reported_build_stamp() -> None:
    registry = DeviceRegistry()
    record = registry.get_or_create("device-a")
    record.bind(
        _FakeWebSocket(),
        {"label": "edge-macos", "platform": "macOS", "capabilities": {}, "build_stamp": "b" * 64},
    )
    assert record.to_summary()["build_stamp"] == "b" * 64


def test_reconnecting_rebind_changes_connected_at() -> None:
    """A fresh `bind()` (e.g. a reload-triggered reconnect) must produce a NEW
    `connected_at` -- this is the exact signal `update_extension.py`'s
    reload-then-verify flow depends on to tell a genuine reconnect apart from
    an already-live connection that merely kept heartbeating."""
    registry = DeviceRegistry()
    record = registry.get_or_create("device-a")
    record.bind(_FakeWebSocket(), {})
    first_connected_at = record.connected_at
    assert first_connected_at is not None

    record.unbind()
    record.connected_at = first_connected_at - timedelta(seconds=1)  # force a measurable gap
    record.bind(_FakeWebSocket(), {})
    assert record.connected_at is not None
    assert record.connected_at != first_connected_at
