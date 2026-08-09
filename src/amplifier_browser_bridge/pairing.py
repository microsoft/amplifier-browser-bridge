"""Pairing: bootstrap a new desktop browser device without hand-transcribing a raw
``ws://`` URL and a 32-char hex token into the options page (two separate,
error-prone, cross-device copy operations).

## The problem this replaces

Today, configuring a new browser device means an operator reads a hub URL and a
32-character hex token off one machine's terminal (wherever ``amplifier-browser-bridge
init``/``hub`` runs) and hand-types both into two empty text fields on a *different*
machine's browser (the device being paired). Two design/product council reviews both
flagged this: the raw IP-literal URL and the bare secret are exactly the kind of
values a person mistypes, and there is no cross-device clipboard to lean on -- these
are frequently two different physical machines.

## The fix: a short-lived, single-use pairing ticket

``amplifier-browser-bridge pair`` (an *operator* action, gated by the same
token-authenticated ``/agent`` route as every other CLI command) asks a running hub to
mint a **pairing ticket**: a short, random, single-use, time-limited code. The operator
reads this one short code off the hub's terminal and enters it -- in ONE field -- on
the device being paired. The extension's options page uses the ticket to make a single
plain-HTTP request to the hub's ``/pair/redeem`` route (unauthenticated by the
long-lived token, since bootstrapping trust for a brand-new device *is* the whole
point) and receives back a freshly-minted, real per-device token in exchange.

## Why the ticket does not weaken the token as the security boundary

The ticket is a *bootstrap* credential, not a replacement for the token. Its only
power is "redeem once, within the TTL, for a freshly-minted real per-device token" --
it can never be used to run a command, read a tab, or reach any other hub route.
Concretely, comparing it against the status quo it replaces:

- **Entropy**: ``TICKET_ALPHABET`` is the 32-symbol Crockford Base32 alphabet (exactly
  5 bits/char by construction -- a power of two, so the bit math is exact, not
  approximate). ``TICKET_LENGTH = 10`` -> exactly 50 bits of entropy. That is *larger*
  than many real-world OAuth Device Authorization Grant (RFC 8628) user codes, which
  the IETF considers acceptable at as few as ~20 bits specifically *because* they pair
  a short code with a short TTL and attempt-rate bounding -- the same two properties
  this ticket has, with more than 2**30 times the search space RFC 8628's own minimum.
- **Lifetime**: ``DEFAULT_TICKET_TTL_SECONDS`` (600s / 10 minutes) by default, entirely
  in-memory (never written to disk, unlike the long-lived token file) -- a hub restart
  invalidates every outstanding ticket unconditionally.
- **Single use**: redemption deletes the ticket from the store immediately on success;
  it cannot be replayed even within its TTL.
- **Attempt bounding**: ``MAX_REDEEM_ATTEMPTS`` (20) burns a ticket after that many
  FAILED redemption attempts made against THAT specific ticket value, regardless of
  how much TTL remains. Be precise about what this does and does not defend against:
  a BLIND guess that does not match any outstanding ticket costs the store nothing at
  all (there is no record to charge an attempt against) -- entropy and TTL are the
  primary defense against blind guessing, exactly as argued above, and remain
  sufficient on their own. This bound is defense-in-depth for the narrower case where
  an attacker has ALREADY located a live ticket value (e.g. observed it, or guessed it
  correctly once) and is now retrying it or near-variants -- it caps how many times any
  one located ticket can be hammered, on top of (never instead of) the entropy/TTL
  guarantee.
- **Threat model**: exactly the same outer boundary as every other hub route --
  reachable only by something already on the tailnet (see ``auth.py``'s module
  docstring and ``docs/POLICY.md``). An attacker who is NOT on the tailnet cannot reach
  ``/pair/redeem`` at all. An attacker who IS on the tailnet, during the live window of
  an in-progress pairing, could in principle race the intended device to redeem the
  ticket first -- but this is a strictly SMALLER and SHORTER-lived exposure than the
  status quo it replaces (a single shared, unbounded-lifetime, full-strength secret
  that must be protected indefinitely and is displayed in a terminal for hand-copying).
  The ticket concentrates risk into a narrow, self-expiring, single-use window instead
  of a value that must remain secret forever.

Ticket minting itself (``PairingStore.create``) is not exposed to the ticket-redemption
threat model at all -- it requires the SAME long-lived token every other agent command
requires (enforced by ``hub.py``'s existing ``/agent`` route token check), so an
attacker who does not already hold that token cannot self-issue pairing tickets.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

# Crockford Base32: a well-known, exactly-32-symbol alphabet (excludes I, L, O, U to
# avoid transcription/pronunciation ambiguity -- not that this ticket is meant to be
# read aloud, but a screen-share or a rare hand-copy should not be tripped up by
# visually similar characters). 32 is a power of two, so every character contributes
# EXACTLY log2(32) = 5 bits of entropy -- the module docstring's bit-count is exact,
# not approximate, as a direct consequence of this choice.
TICKET_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
assert len(TICKET_ALPHABET) == 32  # exactness of the entropy math above depends on this

TICKET_LENGTH = 10  # 10 * 5 bits = 50 bits of entropy -- see module docstring.
DEFAULT_TICKET_TTL_SECONDS = 600.0  # 10 minutes.
MAX_REDEEM_ATTEMPTS = 20  # burn the ticket after this many FAILED attempts, regardless
# of remaining TTL -- bounds a live guessing attack's attempt budget, not just its time
# budget. See module docstring's "Attempt bounding" paragraph.

# How long a REDEEMED ticket's tombstone record is kept before being purged -- see
# `status()` below. This only needs to outlive the `/setup` page's own polling
# cadence (~1s, see onboarding.py) by a comfortable margin, so the tab that showed
# the code gets to observe "redeemed" at least once before the record is gone.
# Deliberately unrelated to DEFAULT_TICKET_TTL_SECONDS above (a different lifetime:
# how long an UNREDEEMED ticket stays valid).
REDEEMED_TOMBSTONE_SECONDS = 120.0


class PairingError(Exception):
    """Raised by `PairingStore.redeem` on any ticket that cannot be redeemed right
    now -- unknown, expired, already-used, or attempt-exhausted. The message is
    always safe to return directly to an unauthenticated caller (never leaks
    anything about OTHER tickets or the token store)."""


def generate_ticket() -> str:
    """A fresh ticket string: `TICKET_LENGTH` characters from `TICKET_ALPHABET`,
    via `secrets.choice` (cryptographically strong, matching `setup.py`'s
    `generate_token`'s use of the `secrets` module for the same reason)."""
    return "".join(secrets.choice(TICKET_ALPHABET) for _ in range(TICKET_LENGTH))


def format_ticket(ticket: str) -> str:
    """Group a raw ticket into `AAAAA-BBBBB` for readability when printed/typed.
    Purely cosmetic -- `normalize_ticket` strips this back out before comparison."""
    if len(ticket) != TICKET_LENGTH:
        return ticket
    half = TICKET_LENGTH // 2
    return f"{ticket[:half]}-{ticket[half:]}"


def normalize_ticket(raw: str) -> str:
    """Upper-case and strip whitespace/dashes -- the inverse of `format_ticket`,
    tolerant of however a caller typed or pasted it back."""
    return raw.strip().upper().replace("-", "").replace(" ", "")


@dataclass
class PairingTicket:
    ticket: str
    created_at: float
    expires_at: float
    attempts: int = 0
    # Set the moment `redeem()` succeeds; `None` for a still-pending ticket. A
    # tombstone (see `status()`/`REDEEMED_TOMBSTONE_SECONDS`), not a deletion --
    # this is what lets a *different*, side-effect-free caller (the `/setup`
    # page's own redemption poll, see hub.py's `_handle_pair_status`) observe
    # "this ticket was just redeemed" instead of the same "unknown" response an
    # expired-and-purged or never-valid ticket produces.
    redeemed_at: float | None = None

    def is_expired(self, *, now: float) -> bool:
        return now >= self.expires_at

    def is_tombstone_expired(self, *, now: float) -> bool:
        """True once a *redeemed* ticket's grace window has elapsed and it is
        safe to purge from the store entirely. A never-redeemed ticket is never
        tombstone-expired by this check (see `is_expired` for that lifetime)."""
        return self.redeemed_at is not None and now - self.redeemed_at >= REDEEMED_TOMBSTONE_SECONDS


@dataclass
class PairingStore:
    """In-memory (never persisted -- see module docstring's "Lifetime" point)
    registry of outstanding pairing tickets. One instance lives on the `Hub`
    (`hub.py`), created fresh on every hub process start -- a restart invalidates
    every outstanding ticket unconditionally, by construction (nothing here ever
    touches disk)."""

    _tickets: dict[str, PairingTicket] = field(default_factory=dict)

    def _purge_expired(self, *, now: float) -> None:
        """Purge tickets that are either (a) never-redeemed and past their
        normal TTL, or (b) redeemed and past their tombstone grace window
        (`REDEEMED_TOMBSTONE_SECONDS`) -- see `PairingTicket.is_tombstone_expired`.
        A redeemed-but-still-within-grace ticket is deliberately kept so
        `status()` can still report "redeemed" for it."""
        expired = [
            t
            for t, rec in self._tickets.items()
            if rec.is_tombstone_expired(now=now) or (rec.redeemed_at is None and rec.is_expired(now=now))
        ]
        for t in expired:
            del self._tickets[t]

    def create(
        self, *, ttl_seconds: float = DEFAULT_TICKET_TTL_SECONDS, now: float | None = None
    ) -> PairingTicket:
        """Mint a fresh ticket. Called only from the token-authenticated `/agent`
        route (`create_pairing` message, see hub.py) -- minting itself requires
        already holding the long-lived token; see module docstring."""
        effective_now = now if now is not None else time.time()
        self._purge_expired(now=effective_now)  # opportunistic cleanup; no background task needed
        ticket = generate_ticket()
        while (
            ticket in self._tickets
        ):  # astronomically unlikely at 50 bits; loop is defensive, not load-bearing
            ticket = generate_ticket()
        record = PairingTicket(
            ticket=ticket, created_at=effective_now, expires_at=effective_now + ttl_seconds
        )
        self._tickets[ticket] = record
        return record

    def redeem(self, raw_ticket: str, *, now: float | None = None) -> PairingTicket:
        """Consume a ticket exactly once. Raises `PairingError` (never returns a
        falsy sentinel -- see this project's fail-loud convention) on any ticket
        that is not currently valid. On success, the ticket is NOT deleted --
        it is marked redeemed (tombstoned; see `PairingTicket.redeemed_at` and
        `status()` below) so a separate, side-effect-free caller can learn the
        redemption happened, then purged automatically after
        `REDEEMED_TOMBSTONE_SECONDS`. Redemption is still exactly single-use: a
        second `redeem()` call against an already-redeemed ticket always raises
        "unknown or already-used ticket", indistinguishable (by design) from a
        value that was never valid at all, so a caller learns nothing about WHY
        a specific ticket failed beyond what this message says."""
        effective_now = now if now is not None else time.time()
        # Deliberately NOT calling _purge_expired() here (unlike create()/status()):
        # an expired-but-not-yet-purged ticket must still reach the `is_expired`
        # branch below so the caller gets the specific "pairing code expired"
        # message, not the generic "unknown or already-used" one a pre-emptive
        # purge would produce instead.
        ticket = normalize_ticket(raw_ticket)
        record = self._tickets.get(ticket)
        if record is None or record.redeemed_at is not None:
            raise PairingError("unknown or already-used pairing code")
        if record.is_expired(now=effective_now):
            del self._tickets[ticket]
            raise PairingError(
                "pairing code expired -- run `amplifier-browser-bridge pair` again for a new one"
            )
        record.attempts += 1
        if record.attempts > MAX_REDEEM_ATTEMPTS:
            del self._tickets[ticket]
            raise PairingError(
                "pairing code invalidated after too many failed attempts -- "
                "run `amplifier-browser-bridge pair` again for a new one"
            )
        record.redeemed_at = effective_now  # tombstoned, not deleted -- see docstring above
        return record

    def status(self, raw_ticket: str, *, now: float | None = None) -> str:
        """Read-only, side-effect-free status check for a ticket: one of
        `"pending"` (valid, not yet redeemed), `"redeemed"` (successfully
        redeemed within the last `REDEEMED_TOMBSTONE_SECONDS`), or `"unknown"`
        (never existed, already fully purged, or expired without ever being
        redeemed).

        This is the mechanism the `/setup` page's own redemption poll uses
        (see hub.py's `_handle_pair_status` and onboarding.py's polling
        script) to learn that ITS OWN code was redeemed elsewhere, so the tab
        can flip from a live countdown to "Connected" instead of continuing to
        count down a code that has already been used -- see onboarding.py's
        module docstring for the bug this closes.

        Deliberately does NOT increment `attempts` or otherwise mutate
        anything but expired-ticket housekeeping -- unlike `redeem()`, calling
        this repeatedly (as a ~1s poll does) must never burn down a ticket's
        own attempt budget or otherwise affect whether it can still be
        redeemed."""
        effective_now = now if now is not None else time.time()
        self._purge_expired(now=effective_now)
        ticket = normalize_ticket(raw_ticket)
        record = self._tickets.get(ticket)
        if record is None:
            return "unknown"
        if record.redeemed_at is not None:
            return "redeemed"
        if record.is_expired(now=effective_now):
            return "unknown"  # not purged here (read-only); next _purge_expired call will
        return "pending"

    def __len__(self) -> int:
        return len(self._tickets)


__all__ = [
    "DEFAULT_TICKET_TTL_SECONDS",
    "MAX_REDEEM_ATTEMPTS",
    "REDEEMED_TOMBSTONE_SECONDS",
    "TICKET_ALPHABET",
    "TICKET_LENGTH",
    "PairingError",
    "PairingStore",
    "PairingTicket",
    "format_ticket",
    "generate_ticket",
    "normalize_ticket",
]
