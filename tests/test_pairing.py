"""Tests for pairing.py's ticket store -- entropy shape, single-use redemption,
expiry, and attempt-bounding. See pairing.py's module docstring for the full
entropy/lifetime/threat-model reasoning these tests pin down as behavior.
"""

from __future__ import annotations

import pytest

from amplifier_browser_bridge.pairing import (
    MAX_REDEEM_ATTEMPTS,
    REDEEMED_TOMBSTONE_SECONDS,
    TICKET_ALPHABET,
    TICKET_LENGTH,
    PairingError,
    PairingStore,
    format_ticket,
    generate_ticket,
    normalize_ticket,
)


def test_ticket_alphabet_is_exactly_32_symbols() -> None:
    """The module docstring's "50 bits of entropy" claim depends on this being
    an exact power of two -- pin it down so a future edit can't silently drift
    the bit-count claim out of sync with the actual alphabet."""
    assert len(TICKET_ALPHABET) == 32
    assert len(set(TICKET_ALPHABET)) == 32  # no duplicate symbols


def test_generate_ticket_has_expected_length_and_alphabet() -> None:
    ticket = generate_ticket()
    assert len(ticket) == TICKET_LENGTH
    assert all(ch in TICKET_ALPHABET for ch in ticket)


def test_generate_ticket_is_random_across_calls() -> None:
    tickets = {generate_ticket() for _ in range(50)}
    assert len(tickets) == 50  # collision at 50 bits across 50 draws is not a real risk


def test_format_and_normalize_ticket_round_trip() -> None:
    ticket = generate_ticket()
    formatted = format_ticket(ticket)
    assert "-" in formatted
    assert normalize_ticket(formatted) == ticket
    # Tolerant of lowercase and stray whitespace, however a caller typed it back.
    assert normalize_ticket(f"  {formatted.lower()}  ") == ticket


def test_create_returns_a_ticket_registered_in_the_store() -> None:
    store = PairingStore()
    record = store.create(now=1000.0)
    assert len(store) == 1
    assert record.ticket in normalize_ticket(record.ticket)  # already normalized form
    assert record.expires_at > record.created_at


def test_redeem_succeeds_exactly_once() -> None:
    store = PairingStore()
    record = store.create(now=1000.0)

    redeemed = store.redeem(record.ticket, now=1001.0)
    assert redeemed.ticket == record.ticket
    # NOT deleted immediately -- tombstoned (kept, marked redeemed) so status()
    # can still report "redeemed" within the grace window. See test_status_*.
    assert len(store) == 1
    assert store._tickets[record.ticket].redeemed_at == 1001.0  # type: ignore[attr-defined]

    with pytest.raises(PairingError, match="unknown or already-used"):
        store.redeem(record.ticket, now=1002.0)


def test_redeem_accepts_the_cosmetically_formatted_and_lowercased_form() -> None:
    store = PairingStore()
    record = store.create(now=1000.0)
    typed = format_ticket(record.ticket).lower()

    redeemed = store.redeem(typed, now=1001.0)
    assert redeemed.ticket == record.ticket


def test_redeem_rejects_unknown_ticket() -> None:
    store = PairingStore()
    with pytest.raises(PairingError, match="unknown or already-used"):
        store.redeem("NOTATICKET1", now=1000.0)


def test_redeem_rejects_expired_ticket() -> None:
    store = PairingStore()
    record = store.create(ttl_seconds=60.0, now=1000.0)

    with pytest.raises(PairingError, match="expired"):
        store.redeem(record.ticket, now=1061.0)  # 1 second past expiry

    assert len(store) == 0  # expired ticket is purged on the failed attempt


def test_redeem_is_invalidated_after_max_failed_attempts() -> None:
    """Defense-in-depth for a LOCATED ticket (see pairing.py's module docstring
    "Attempt bounding" paragraph -- this is explicitly not the primary defense
    against blind guessing, which is entropy + TTL). `redeem()` is single-use, so
    exercising `attempts` reaching the bound on one still-valid record requires
    seeding that record's counter directly rather than via repeated public
    `redeem()` calls (each success deletes the record) -- the honest way to pin
    this specific mechanism down without inventing a second, weaker public API
    purely for testability.
    """
    store = PairingStore()
    record = store.create(ttl_seconds=600.0, now=1000.0)
    ticket = record.ticket
    store._tickets[ticket].attempts = MAX_REDEEM_ATTEMPTS  # type: ignore[attr-defined]

    with pytest.raises(PairingError, match="too many failed attempts"):
        store.redeem(ticket, now=1001.0)
    assert len(store) == 0  # burned, not merely still-pending


def test_create_purges_expired_tickets_opportunistically() -> None:
    store = PairingStore()
    store.create(ttl_seconds=1.0, now=1000.0)  # will be expired by the time of the next create()
    assert len(store) == 1

    store.create(ttl_seconds=600.0, now=2000.0)  # far past the first ticket's expiry
    assert len(store) == 1  # the expired one was purged; only the fresh one remains


# --- status() -- read-only redemption polling (onboarding.py's /setup fix) -----


def test_status_is_pending_for_a_fresh_ticket() -> None:
    store = PairingStore()
    record = store.create(now=1000.0)
    assert store.status(record.ticket, now=1000.5) == "pending"


def test_status_is_unknown_for_a_ticket_that_never_existed() -> None:
    store = PairingStore()
    assert store.status("NOTATICKET1", now=1000.0) == "unknown"


def test_status_flips_to_redeemed_after_redeem_succeeds() -> None:
    store = PairingStore()
    record = store.create(now=1000.0)
    store.redeem(record.ticket, now=1001.0)
    assert store.status(record.ticket, now=1001.5) == "redeemed"


def test_status_never_mutates_attempts_or_burns_the_ticket() -> None:
    """The core regression this exists to prevent: a ~1s poll must never count
    against MAX_REDEEM_ATTEMPTS, or a long enough wait would invalidate a
    ticket the user never even tried to redeem."""
    store = PairingStore()
    record = store.create(now=1000.0)
    for i in range(MAX_REDEEM_ATTEMPTS + 5):
        assert store.status(record.ticket, now=1000.0 + i) == "pending"
    # Still fully redeemable after all that polling (well within the ticket's TTL).
    redeemed = store.redeem(record.ticket, now=1030.0)
    assert redeemed.ticket == record.ticket


def test_status_is_unknown_once_the_tombstone_grace_window_elapses() -> None:
    store = PairingStore()
    record = store.create(now=1000.0)
    store.redeem(record.ticket, now=1001.0)
    assert store.status(record.ticket, now=1001.0 + REDEEMED_TOMBSTONE_SECONDS - 1) == "redeemed"
    assert store.status(record.ticket, now=1001.0 + REDEEMED_TOMBSTONE_SECONDS + 1) == "unknown"


def test_status_is_unknown_for_an_expired_never_redeemed_ticket_without_purging_it_here() -> None:
    """status() is read-only -- it must not purge an expired-but-unredeemed
    record itself (that stays redeem()/create()'s job); it just reports
    "unknown" honestly either way."""
    store = PairingStore()
    record = store.create(ttl_seconds=10.0, now=1000.0)
    assert store.status(record.ticket, now=1500.0) == "unknown"


def test_redeem_a_second_time_after_tombstoning_still_fails_the_same_way() -> None:
    """A tombstoned (not yet purged) ticket must still behave, from redeem()'s
    perspective, exactly like a ticket that was never valid -- the existing
    single-use guarantee must not weaken just because the record now lingers
    for status()'s benefit."""
    store = PairingStore()
    record = store.create(now=1000.0)
    store.redeem(record.ticket, now=1001.0)
    with pytest.raises(PairingError, match="unknown or already-used"):
        store.redeem(record.ticket, now=1002.0)  # tombstone still present, not yet purged
