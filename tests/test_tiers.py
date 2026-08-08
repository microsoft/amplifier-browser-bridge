"""The three-tier connectivity model must behave exactly as documented in
docs/designs/browser-bridge.md §5 -- these thresholds are not arbitrary, they come
from measured dark-window data."""

from __future__ import annotations

from amplifier_browser_bridge.tiers import (
    INTERMITTENT_MAX_SECONDS,
    LIVE_SILENCE_TIMEOUT_SECONDS,
    Tier,
    compute_tier,
)


def test_freshly_connected_with_no_elapsed_data_is_live() -> None:
    assert compute_tier(connected=True, seconds_since_last_seen=None) is Tier.LIVE


def test_connected_and_recently_heard_from_is_live() -> None:
    assert compute_tier(connected=True, seconds_since_last_seen=0.0) is Tier.LIVE
    assert (
        compute_tier(connected=True, seconds_since_last_seen=LIVE_SILENCE_TIMEOUT_SECONDS - 0.01) is Tier.LIVE
    )


def test_connected_but_silent_past_the_liveness_timeout_is_not_live() -> None:
    """The bug this module fixes: a socket the hub still holds open (e.g. because
    the peer's radio was killed by airplane mode with no TCP FIN/RST) must NOT be
    trusted as live forever just because `connected` is true. Regression coverage
    for the exact field failure: `tier=live, connected=True, silent=604s`."""
    assert (
        compute_tier(connected=True, seconds_since_last_seen=LIVE_SILENCE_TIMEOUT_SECONDS)
        is Tier.INTERMITTENT
    )
    assert compute_tier(connected=True, seconds_since_last_seen=604.0) is Tier.INTERMITTENT
    assert compute_tier(connected=True, seconds_since_last_seen=99999.0) is Tier.INTERMITTENT


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
