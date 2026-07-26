"""Caller-declared session scope -- Candidate C from `docs/designs/confirmation-gate.md`.

Every signal `classify.py` scores is page-asserted (or, for `url`/`flow`, browser-asserted
but still post-hoc). Design doc section 2's lemma: the only pre-execution signal an
adversarial page cannot touch at all is one the CALLER declares through a channel the
page's content never enters. This module is that channel's data shape; `hub.py` is the
channel itself (a distinct `establish_session`/`narrow_scope` wire message, never the
`command` path a click/type/navigate travels).

## Why a prompt-injected model cannot use this module to widen its own grant

Two independent properties, both enforced here, both required together:

1. **`narrow()` only ever narrows.** `write`/`read` may shrink to a strict subset (or from
   `"*"` to any finite set) -- never grow, never return to `"*"`. `on_unknown` may only move
   `allow -> gate -> deny`; `redeem` only `agent -> out_of_band`; `unattended` only
   `False -> True`. Every one of these moves reduces what the session may do, never expands
   it. A model that calls `narrow_scope` with a widening request gets `ScopeError`, not a
   silently-clamped no-op -- fail loud (`CONTRIBUTING.md`).
2. **Once sealed, `narrow()` refuses ALL changes, including further narrowing.** `hub.py`
   calls `seal()` the first time a session's commands yield ANY page content back to the
   caller (a `read`/`snapshot`/`tabs` result -- see `Hub._maybe_seal_session`). This is the
   property that actually matters: a prompt-injected instruction can only exist inside page
   content the agent has already read, which means the session has already sealed by the
   time such an instruction could possibly reach the model. There is no sequence of calls,
   starting from an established session that has read anything, that gets a wider grant.

Establishing a BRAND NEW session (arbitrary initial `read`/`write`/... values, not subject to
narrow-only rules) is a *different* operation from narrowing an existing one, and lives on the
hub side (`Hub.establish_session`), not here -- the hub always mints a fresh `session_id` for
it and never accepts a caller-supplied one, so `establish_session` can never be replayed
against an existing (possibly sealed) session to reset its grant. See `hub.py`'s module
docstring section on sessions for the full wiring.

This module itself is pure data + validation -- no I/O, no hub/policy import (same discipline
`classify.py` holds itself to; see that module's docstring and this project's own quality bar
in `docs/designs/confirmation-gate.md` section 14.3: "classify.py has zero imports from hub,
policy, aiohttp, or any model SDK").

## A note on the origin string format

The design doc's own illustrative comment on `SessionScope.write` shows full origin URLs
(`"https://github.com"`). This implementation instead uses bare hostnames (e.g. `"github.com"`,
matching the codebase's *existing* convention: `policy.host_of()` returns a bare hostname, the
denylist's `host_matches_domain()` operates on bare hostnames, and `ActionDescriptor.origin`
(classify.py) is populated from that same bare hostname. Introducing a second, differently-shaped
string format (scheme-qualified origins) for this one field would mean two representations of
"the same kind of thing" in one codebase for no behavioral gain -- IMPLEMENTATION_PHILOSOPHY.md's
library-vs-custom-code judgment call applies here too: reuse the shape that already exists and is
already tested, rather than inventing a new one. `permits_write`/`permits_read` match using the
same subdomain-inclusive semantics as the denylist (a grant for `"github.com"` also covers
`"gist.github.com"`) -- this is a deliberate, documented deviation from the design doc's
cosmetic example, not a change in behavior it specifies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Origins = Literal["*"] | tuple[str, ...]

# The only fields `narrow()` (or a wire `narrow_scope` request) may touch. Kept as a
# module-level constant so `hub.py` can validate an incoming wire payload against the
# same set without duplicating the field list.
SCOPE_FIELDS: frozenset[str] = frozenset({"read", "write", "on_unknown", "redeem", "unattended"})

_ON_UNKNOWN_ORDER: tuple[str, ...] = ("allow", "gate", "deny")
_REDEEM_ORDER: tuple[str, ...] = ("agent", "out_of_band")


class ScopeError(ValueError):
    """Raised on any widening attempt, any change once sealed, or a malformed field.

    A single exception type for every rejection `narrow()` can produce -- callers (hub.py's
    wire handlers) only need to catch one thing to turn a rejected mutation into a
    `{"ok": false, "error": ...}` response.
    """


def _host_matches_domain(host: str, domain: str) -> bool:
    """Suffix-with-dot-boundary match -- deliberately duplicated from `policy.py`'s function
    of the same name (3 lines) rather than imported, so this module stays a pure, dependency-
    free leaf with no import of `policy`/`hub` (this module's own docstring, and the same
    discipline `classify.py` holds itself to). See IMPLEMENTATION_PHILOSOPHY.md's guidance on
    encoding a trivial, stable pattern as convention rather than a shared dependency between
    otherwise-isolated modules."""
    host = host.lower()
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)


def _validate_origin_tuple(field_name: str, value: Any) -> tuple[str, ...]:
    if value == "*":
        raise ScopeError(f"{field_name} may not be (re-)widened to '*'")
    if not isinstance(value, (list, tuple)):
        raise ScopeError(f"{field_name} must be '*' or a list/tuple of origin hostnames, got: {value!r}")
    result = tuple(value)
    for o in result:
        if not isinstance(o, str) or not o:
            raise ScopeError(f"{field_name} entries must be non-empty strings, got: {o!r}")
    return result


def _narrow_origins(field_name: str, current: Origins, new: Any) -> Origins:
    """`current` may be `"*"` (anything narrows it) or a tuple (only a STRICT subset
    narrows it further -- see this module's docstring point 1). Never returns `"*"`."""
    new_tuple = _validate_origin_tuple(field_name, new)
    if current == "*":
        return new_tuple  # "*" -> any finite set is always a narrowing.
    current_set, new_set = set(current), set(new_tuple)
    if not new_set < current_set:
        raise ScopeError(
            f"{field_name} may only narrow to a STRICT subset of the current grant "
            f"{sorted(current_set)!r}; got {sorted(new_set)!r}, which is not a strict subset"
        )
    return new_tuple


def _narrow_ordered(field_name: str, order: tuple[str, ...], current: str, new: Any) -> str:
    if new not in order:
        raise ScopeError(f"{field_name} must be one of {order}, got: {new!r}")
    if new == current:
        return new  # no-op is harmless for a simple ordered enum (unlike origin sets).
    if order.index(new) < order.index(current):
        raise ScopeError(
            f"{field_name} may only move toward more restrictive ({' -> '.join(order)}); "
            f"cannot go from {current!r} back to {new!r}"
        )
    return new


def _narrow_unattended(current: bool, new: Any) -> bool:
    if not isinstance(new, bool):
        raise ScopeError(f"unattended must be a bool, got: {new!r}")
    if new == current:
        return new
    if current is True and new is False:
        raise ScopeError("unattended may only move False -> True, never back to False")
    return new


@dataclass
class SessionScope:
    """A caller-declared, narrow-only constraint on what a session may DO.

    `read` defaults to `"*"` (the maintainer's own stance, design doc section 1: "I generally
    want it to be able to access what I access") -- declared here for forward-compatible
    symmetry with `write`, but see this module's docstring: only `write` is consulted by
    `PolicyEngine.evaluate` in this build pass (design doc section 12, step 5). `read` still
    participates fully in `narrow()`'s validation (so a caller declaring a read scope gets the
    same monotonic-narrowing guarantee), it is simply not yet enforced against any command --
    honestly documented, not silently dropped, the same "mechanism present, not yet a consumer"
    stance `addressing.py` takes with `profile_id`.
    """

    session_id: str
    read: Origins = "*"
    write: Origins = "*"
    on_unknown: Literal["allow", "gate", "deny"] = "allow"
    redeem: Literal["agent", "out_of_band"] = "agent"
    unattended: bool = False
    _sealed: bool = False

    @property
    def sealed(self) -> bool:
        return self._sealed

    def permits_write(self, origin: str | None) -> bool:
        """`origin` is a bare hostname (see this module's docstring on the origin string
        format) -- typically `ActionDescriptor.origin` / `PolicyEngine`'s own `host_of(...)`
        result, never anything the agent's request asserts about the target.

        `origin=None` (no browser-observed host at all for this tab) is DENIED whenever
        `write` is not `"*"` -- fail closed, the same direction `PolicyEngine`'s denylist
        takes when host resolution fails, since there is nothing to check the grant against.
        """
        if self.write == "*":
            return True
        if not origin:
            return False
        return any(_host_matches_domain(origin, allowed) for allowed in self.write)

    def permits_read(self, origin: str | None) -> bool:
        """Symmetric with `permits_write`. Not yet called from `PolicyEngine.evaluate` in
        this build pass -- see this class's docstring."""
        if self.read == "*":
            return True
        if not origin:
            return False
        return any(_host_matches_domain(origin, allowed) for allowed in self.read)

    def narrow(self, **kwargs: Any) -> None:
        """Apply a strictly-narrowing update. Raises `ScopeError` on any widening attempt,
        on an unknown field name, or on ANY change at all once `_sealed` is True.

        Validates every field in `kwargs` BEFORE mutating any of them, so a call that
        narrows three fields correctly and gets the fourth wrong leaves the scope entirely
        unchanged rather than partially updated.
        """
        if not kwargs:
            return
        unknown = set(kwargs) - SCOPE_FIELDS
        if unknown:
            raise ScopeError(f"unknown scope field(s): {sorted(unknown)}")
        if self._sealed:
            raise ScopeError(
                f"session {self.session_id!r} is sealed (it has already ingested page "
                "content) -- scope can no longer be changed at all, narrowing included"
            )
        updates: dict[str, Any] = {}
        if "write" in kwargs:
            updates["write"] = _narrow_origins("write", self.write, kwargs["write"])
        if "read" in kwargs:
            updates["read"] = _narrow_origins("read", self.read, kwargs["read"])
        if "on_unknown" in kwargs:
            updates["on_unknown"] = _narrow_ordered(
                "on_unknown", _ON_UNKNOWN_ORDER, self.on_unknown, kwargs["on_unknown"]
            )
        if "redeem" in kwargs:
            updates["redeem"] = _narrow_ordered("redeem", _REDEEM_ORDER, self.redeem, kwargs["redeem"])
        if "unattended" in kwargs:
            updates["unattended"] = _narrow_unattended(self.unattended, kwargs["unattended"])
        for key, value in updates.items():
            setattr(self, key, value)

    def seal(self) -> None:
        """Idempotent -- safe to call on every page-content-bearing result, not just the
        first (see `Hub._maybe_seal_session`, which checks `sealed` first purely to avoid a
        redundant audit event, not because a second `seal()` call would be unsafe)."""
        self._sealed = True

    def to_wire(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "read": list(self.read) if self.read != "*" else "*",
            "write": list(self.write) if self.write != "*" else "*",
            "on_unknown": self.on_unknown,
            "redeem": self.redeem,
            "unattended": self.unattended,
            "sealed": self._sealed,
        }

    @staticmethod
    def from_wire(session_id: str, payload: dict[str, Any]) -> SessionScope:
        """Build a BRAND NEW session's initial scope from an `establish_session` request.

        This is the one place arbitrary (non-narrowing) values are accepted -- by design,
        since it only ever constructs a scope for a `session_id` the hub itself just minted
        (see this module's docstring). Unlike `narrow()`, there is no "current" value to
        narrow relative to; instead this validates each field's *shape* only.
        """
        read = _validate_shape("read", payload.get("read", "*"))
        write = _validate_shape("write", payload.get("write", "*"))
        on_unknown = payload.get("on_unknown", "allow")
        if on_unknown not in _ON_UNKNOWN_ORDER:
            raise ScopeError(f"on_unknown must be one of {_ON_UNKNOWN_ORDER}, got: {on_unknown!r}")
        redeem = payload.get("redeem", "agent")
        if redeem not in _REDEEM_ORDER:
            raise ScopeError(f"redeem must be one of {_REDEEM_ORDER}, got: {redeem!r}")
        unattended = payload.get("unattended", False)
        if not isinstance(unattended, bool):
            raise ScopeError(f"unattended must be a bool, got: {unattended!r}")
        return SessionScope(
            session_id=session_id,
            read=read,
            write=write,
            on_unknown=on_unknown,
            redeem=redeem,
            unattended=unattended,
        )


def _validate_shape(field_name: str, value: Any) -> Origins:
    if value == "*":
        return "*"
    if not isinstance(value, (list, tuple)):
        raise ScopeError(f"{field_name} must be '*' or a list/tuple of origin hostnames, got: {value!r}")
    result = tuple(value)
    for o in result:
        if not isinstance(o, str) or not o:
            raise ScopeError(f"{field_name} entries must be non-empty strings, got: {o!r}")
    return result


__all__ = [
    "SCOPE_FIELDS",
    "ScopeError",
    "SessionScope",
]
