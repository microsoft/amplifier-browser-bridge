"""Device registry.

Keyed by device_id, one record per browser install that has ever said `hello`. This is
the structural fix for the reference implementation's `state = {"browser": None}` single
global slot, which silently overwrote itself on a second connection. Here, a second
device (or a second connection from the same device_id, e.g. after a crash) gets its own
independent record -- concurrent devices and concurrent agent clients are both native,
not bolted on.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from .queue import DeviceCommandQueue
from .tiers import Tier, compute_tier


@runtime_checkable
class DeviceConnection(Protocol):
    """The only things the registry/hub actually need from a live connection:
    the ability to send a JSON-serializable envelope, and the ability to close
    it. Deliberately NOT aiohttp's concrete `WebSocketResponse` type -- that
    would leak a transport-layer dependency into the registry, and it would
    make this class untestable without a real websocket. Any object with
    async `send_json`/`close` satisfies this (aiohttp's WebSocketResponse
    does; so does a test fake).

    `close` was added alongside the hub's keepalive sweep (`Hub.keepalive_sweep`,
    hub.py) -- the mechanism that proactively tears down a device connection
    that has gone silent past `LIVE_SILENCE_TIMEOUT_SECONDS`, rather than
    merely distrusting it as `tiers.py`'s `compute_tier` already did. See
    hub.py's module docstring."""

    async def send_json(self, data: Any, /) -> None: ...

    # Return type is deliberately `object`, not `None` -- aiohttp's real
    # `WebSocketResponse.close()` returns `bool`, and a Protocol method's
    # return type must be covariant with every real implementation's return
    # type. We never inspect the return value; `object` is simply the
    # widest type any implementation's return value is guaranteed to satisfy.
    async def close(self, /) -> object: ...


@dataclass
class DeviceRecord:
    device_id: str
    profile_id: str = ""
    label: str = "unknown"
    platform: str = "unknown"
    capabilities: dict[str, bool] = field(default_factory=dict)
    protocol_version: int | None = None
    # Tier 0 handshake (version-skew story, see skew.py's module docstring):
    # the actual set of commands this device's CURRENT build can execute,
    # self-reported in `hello.commands` (extension/background.js's
    # SUPPORTED_COMMANDS). `None` -- not an empty set -- means this device has
    # never reported one at all: every extension shipped before this feature,
    # including whatever is connected to a hub right now the moment it's
    # upgraded. That is a distinct, definitively-stale state (skew.py's
    # `SkewReport.known = False`), not "reports supporting zero commands."
    commands: frozenset[str] | None = None
    # Diagnostic color ONLY -- `chrome.runtime.getManifest().version`, hand-
    # maintained and already measured to drift in this exact repo
    # (pyproject.toml said 0.1.0, manifest.json said 0.4.0, same repo, same
    # day). Never consulted for skew detection -- see skew.py.
    manifest_version: str | None = None

    ws: DeviceConnection | None = None
    connected_at: datetime | None = None
    last_seen: datetime | None = None
    last_heartbeat_seq: int = 0

    # command_id -> future that resolves when a `result` for it arrives
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    # command_id -> stored result envelope, kept for `poll` after the fact
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    queue: DeviceCommandQueue = field(default_factory=DeviceCommandQueue)

    @property
    def connected(self) -> bool:
        return self.ws is not None

    @property
    def seconds_since_last_seen(self) -> float | None:
        """Elapsed time since the last hello, heartbeat, result, or event from
        this device (`None` if it has never connected). Exposed separately from
        `tier` so callers that need the raw elapsed value -- e.g. hub.py's
        timeout-diagnosis message -- don't have to recompute it by hand."""
        if self.last_seen is None:
            return None
        return (datetime.now(UTC) - self.last_seen).total_seconds()

    @property
    def tier(self) -> Tier:
        return compute_tier(self.connected, self.seconds_since_last_seen)

    def bind(self, ws: DeviceConnection, hello: dict[str, Any]) -> None:
        """Attach a live websocket connection, populated from a `hello` envelope."""
        self.ws = ws
        self.profile_id = str(hello.get("profile_id", self.profile_id))
        self.label = str(hello.get("label", self.label))
        self.platform = str(hello.get("platform", self.platform))
        self.capabilities = dict(hello.get("capabilities") or {})
        self.protocol_version = hello.get("protocol_version")
        # Tier 0 handshake -- see this class's `commands` field docstring and
        # skew.py's module docstring. `raw_commands` absent/not-a-list (every
        # extension shipped before this feature) leaves `self.commands` as
        # `None`, NOT an empty frozenset -- those are different facts
        # (`None` = "never reported one"; empty = "reported supporting
        # nothing", which no real build ever actually does).
        raw_commands = hello.get("commands")
        if isinstance(raw_commands, list):
            self.commands = frozenset(str(c) for c in raw_commands)
        manifest_version = hello.get("manifest_version")
        if isinstance(manifest_version, str):
            self.manifest_version = manifest_version
        now = datetime.now(UTC)
        self.connected_at = now
        self.last_seen = now

    def unbind(self) -> None:
        """Detach on disconnect. Identity/queue/results all survive -- only the live
        socket goes away. Outstanding `pending` futures are failed so callers awaiting
        a result don't hang forever on a dropped connection."""
        self.ws = None
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError(f"device {self.device_id} disconnected mid-command"))
        self.pending.clear()

    def touch(self) -> None:
        self.last_seen = datetime.now(UTC)

    def to_summary(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "profile_id": self.profile_id,
            "label": self.label,
            "platform": self.platform,
            "capabilities": self.capabilities,
            "protocol_version": self.protocol_version,
            # Tier 0 handshake -- `None` (not `[]`) means this device has
            # never reported a command set at all (skew.py's
            # `SkewReport.known = False`). See `hub.py`'s `_devices_snapshot`
            # for the "skew" key computed FROM this against the hub's own
            # vocabulary -- kept out of this class deliberately, the same way
            # `cdp` attach state is enriched by `Hub`, not `DeviceRecord`,
            # because both are comparisons against HUB-owned state.
            "commands": sorted(self.commands) if self.commands is not None else None,
            "manifest_version": self.manifest_version,
            "connected": self.connected,
            "tier": self.tier.value,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            # The moment THIS connection was established -- distinct from
            # `last_seen` (bumped by every heartbeat/result/event on an
            # ALREADY-live connection too). `update_extension.py`'s
            # reload-then-verify flow needs to tell "the device reconnected"
            # apart from "the device has just been quietly heartbeating the
            # whole time" -- only a NEW `connected_at` proves the former.
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
            "queue_length": len(self.queue),
        }


class DeviceRegistry:
    """Independent per-device state. No global "current device" slot anywhere."""

    def __init__(self) -> None:
        self._devices: dict[str, DeviceRecord] = {}

    def get(self, device_id: str) -> DeviceRecord | None:
        return self._devices.get(device_id)

    def get_or_create(self, device_id: str) -> DeviceRecord:
        record = self._devices.get(device_id)
        if record is None:
            record = DeviceRecord(device_id=device_id)
            self._devices[device_id] = record
        return record

    def all(self) -> list[DeviceRecord]:
        return list(self._devices.values())

    def snapshot(self) -> list[dict[str, Any]]:
        return [d.to_summary() for d in self._devices.values()]
