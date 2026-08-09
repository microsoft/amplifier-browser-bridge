"""Tests for pairing.py's ticket store -- entropy shape, single-use redemption,
expiry, and attempt-bounding. See pairing.py's module docstring for the full
entropy/lifetime/threat-model reasoning these tests pin down as behavior.
"""

from __future__ import annotations

import pytest

from amplifier_browser_bridge.pairing import (
    MAX_REDEEM_ATTEMPTS,
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
    assert len(store) == 0  # single-use: gone from the store immediately

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
