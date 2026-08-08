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

## Bug fixed here: `connected` was trusted as proof of life, unconditionally

`connected` (registry.py's `DeviceRecord.connected`) means only "the hub still
holds an open aiohttp `WebSocketResponse` object for this device" -- it says
nothing about whether the underlying transport is actually alive. Airplane mode
(and similar abrupt radio-off events) kills the radio without a TCP FIN or RST:
the OS never tells the hub's socket the peer is gone, so `ws` never becomes
`None` and `connected` stays `True` indefinitely. Measured in the field: a
phone in airplane mode for 10+ minutes still reported `tier=live,
connected=True, silent=604s`, and a command dispatched into that state
timed out at 120s instead of queuing.

Prior to this fix, `compute_tier` returned `Tier.LIVE` the instant `connected`
was true and never consulted `seconds_since_last_seen` on that branch --
silence on an open-but-dead socket was invisible to the tier computation, so a
device that could never be observed to disconnect could never be classified
non-live, and therefore could never have a command queued for it (see
`Hub.send_command`'s `if record.tier is Tier.LIVE: dispatch else: enqueue`).
The queueing subsystem (queue.py) was correct and completely unreachable for
this failure mode.

The fix: `connected` is now necessary but not sufficient for `Tier.LIVE`. A
nominally-open socket that has gone silent longer than
`LIVE_SILENCE_TIMEOUT_SECONDS` (see below) is downgraded to `Tier.INTERMITTENT`
so new commands queue instead of dispatching into a socket that will never
answer. This is inference-only -- it does not close the stale socket or touch
connection lifecycle (see hub.py's module docstring for why active dead-socket
detection was deliberately deferred, not merely skipped).
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

# How long a *nominally still-connected* device may go without any inbound
# message (heartbeat, result, or event -- anything that calls `DeviceRecord.touch()`)
# before the hub stops trusting `connected` as proof of life.
#
# This is a DIFFERENT question from INTERMITTENT_MAX_SECONDS above, and deliberately
# a much smaller number: INTERMITTENT_MAX_SECONDS forgives silence AFTER a real
# disconnect, because a mobile radio genuinely may need up to ~133s to reconnect on
# its own (measured, see module docstring). This threshold instead asks "is an
# open socket still telling the truth?" -- and an open socket that is behaving
# normally has no comparable excuse to go quiet: the extension's heartbeat timer
# (`extension/background.js`, `HEARTBEAT_INTERVAL_MS = 15000`) fires every 15s
# independent of any in-flight command, and real soak testing measured 660
# heartbeats over 165 minutes (and, separately, 568 over 142 minutes) with a
# *maximum* observed gap of 15.1s and zero gaps beyond it (docs/designs/
# browser-bridge.md \u00a72; docs/designs/approval-channel-options.md R5). Extended
# silence on a socket the hub still considers open is therefore a strong,
# low-ambiguity signal that the transport died without a FIN/RST (exactly the
# airplane-mode failure mode above) -- not evidence of a battery-conserving nap.
#
# Set to 4x the measured healthy ceiling (15.1s): generous enough to absorb
# ordinary jitter or a slow GC pause without flapping a healthy connection, but
# tight enough to resolve a silently-dead socket in under a minute instead of
# the 604s+ observed in the field.
LIVE_SILENCE_TIMEOUT_SECONDS: float = 60.0


def compute_tier(connected: bool, seconds_since_last_seen: float | None) -> Tier:
    """Compute a device's current tier.

    `connected` is whether the hub currently holds an open websocket for this
    device. `seconds_since_last_seen` is elapsed time since the last hello,
    heartbeat, or result from that device (None if it has never connected).

    `connected` is necessary but NOT sufficient for `Tier.LIVE`: a socket the
    hub still holds open but hasn't heard from in over
    `LIVE_SILENCE_TIMEOUT_SECONDS` is demoted to `Tier.INTERMITTENT` (still
    holds a live-ish `ws`, but no longer trusted for immediate dispatch) rather
    than a device that has never connected at all (`Tier.DORMANT` below).
    """
    if connected:
        if seconds_since_last_seen is not None and seconds_since_last_seen >= LIVE_SILENCE_TIMEOUT_SECONDS:
            return Tier.INTERMITTENT
        return Tier.LIVE
    if seconds_since_last_seen is None:
        return Tier.DORMANT
    if seconds_since_last_seen < INTERMITTENT_MAX_SECONDS:
        return Tier.INTERMITTENT
    return Tier.DORMANT
