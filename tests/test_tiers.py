"""The three-tier connectivity model must behave exactly as documented in
docs/designs/browser-bridge.md §5 -- these thresholds are not arbitrary, they come
from measured dark-window data."""

from __future__ import annotations

from amplifier_browser_bridge.tiers import INTERMITTENT_MAX_SECONDS, Tier, compute_tier


def test_connected_is_always_live_regardless_of_elapsed_time() -> None:
    assert compute_tier(connected=True, seconds_since_last_seen=None) is Tier.LIVE
    assert compute_tier(connected=True, seconds_since_last_seen=99999) is Tier.LIVE


def test_never_seen_and_disconnected_is_dormant() -> None:
    assert compute_tier(connected=False, seconds_since_last_seen=None) is Tier.DORMANT


def test_recently_disconnected_is_intermittent() -> None:
    # Measured self-healing dark windows: 43-133s. A fresh disconnect is well inside that.
    assert compute_tier(connected=False, seconds_since_last_seen=0.0) is Tier.INTERMITTENT
    assert compute_tier(connected=False, seconds_since_last_seen=133.0) is Tier.INTERMITTENT


def test_long_disconnect_is_dormant() -> None:
    # Measured dormant dark window: 509s, zero self-recovery.
    assert compute_tier(connected=False, seconds_since_last_seen=509.0) is Tier.DORMANT


def test_threshold_boundary() -> None:
    just_under = INTERMITTENT_MAX_SECONDS - 0.01
    just_over = INTERMITTENT_MAX_SECONDS + 0.01
    assert compute_tier(connected=False, seconds_since_last_seen=just_under) is Tier.INTERMITTENT
    assert compute_tier(connected=False, seconds_since_last_seen=just_over) is Tier.DORMANT
