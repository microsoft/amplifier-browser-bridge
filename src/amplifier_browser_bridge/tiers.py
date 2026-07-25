"""The three-tier connectivity model -- a first-class concept, not bolted on.

Measured dark-window data (see docs/designs/browser-bridge.md §2, §5):

    - Desktop MV3 service worker: 660 heartbeats / 165 min / zero gaps.
    - Android, screen off, WITH battery-optimization exemption: 5 dark windows of
      43-133s each, self-reconnecting in <2s every time.
    - Android, screen off, default settings (no exemption): 509s dark, zero
      reconnects observed -- effectively unreachable until the device is touched.

This module turns that data into a simple, honest heuristic: tier is a function of
"are we connected right now" and "how long has it been since we last heard from this
device." It is deliberately NOT a platform-specific classifier -- we don't reliably
know a device's platform-level power state from the hub side, only its connection
behavior. That's an intentional simplification: document the heuristic, don't
pretend to more precision than the hub can actually observe.
"""

from __future__ import annotations

from enum import Enum


class Tier(str, Enum):
    LIVE = "live"
    INTERMITTENT = "intermittent"
    DORMANT = "dormant"


# Padded above the measured intermittent dark-window ceiling (133s) so that a
# device mid-reconnect-cycle isn't misclassified as dormant. Below this
# threshold, a disconnected device is assumed to be about to reconnect on its
# own; a command is queued and expected to drain soon.
INTERMITTENT_MAX_SECONDS: float = 150.0


def compute_tier(connected: bool, seconds_since_last_seen: float | None) -> Tier:
    """Compute a device's current tier.

    `connected` is whether the hub currently holds an open websocket for this
    device. `seconds_since_last_seen` is elapsed time since the last hello,
    heartbeat, or result from that device (None if it has never connected).
    """
    if connected:
        return Tier.LIVE
    if seconds_since_last_seen is None:
        return Tier.DORMANT
    if seconds_since_last_seen < INTERMITTENT_MAX_SECONDS:
        return Tier.INTERMITTENT
    return Tier.DORMANT
