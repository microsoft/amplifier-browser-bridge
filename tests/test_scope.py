"""Unit tests for `scope.py` (Candidate C -- caller-declared write scope).

Pure, dependency-free module (no hub/policy/aiohttp imports -- see scope.py's
module docstring and its own "zero imports" quality bar, matching
classify.py's precedent). These tests exercise `SessionScope` in complete
isolation, with no `PolicyEngine`/`Hub` involved -- the hub-level integration
(session establishment wire messages, seal-on-read, reconnect survival) is
covered in `tests/test_hub.py`, and the policy-level integration (a scope
denying an actual gated click) is covered in `tests/test_gate_elevation.py`.
"""

from __future__ import annotations

import pytest

from amplifier_browser_bridge.scope import ScopeError, SessionScope


def test_default_scope_is_fully_permissive() -> None:
    scope = SessionScope(session_id="s1")
    assert scope.read == "*"
    assert scope.write == "*"
    assert scope.permits_write("anything.example.com")
    assert scope.permits_write(None)


def test_permits_write_matches_subdomains_like_the_denylist() -> None:
    scope = SessionScope(session_id="s1", write=("github.com",))
    assert scope.permits_write("github.com")
    assert scope.permits_write("gist.github.com")
    assert not scope.permits_write("notgithub.com")
    assert not scope.permits_write("github.com.evil.com")


def test_permits_write_denies_none_origin_when_not_wildcard() -> None:
    """Fail closed: no browser-observed host at all is denied whenever write
    isn't '*' -- there's nothing to check the grant against."""
    scope = SessionScope(session_id="s1", write=("github.com",))
    assert not scope.permits_write(None)
    assert not scope.permits_write("")


def test_narrow_from_wildcard_to_tuple() -> None:
    scope = SessionScope(session_id="s1")
    scope.narrow(write=("github.com", "contoso.com"))
    assert scope.write == ("github.com", "contoso.com")


def test_narrow_write_rejects_widening_back_to_wildcard() -> None:
    scope = SessionScope(session_id="s1", write=("github.com",))
    with pytest.raises(ScopeError):
        scope.narrow(write="*")
    assert scope.write == ("github.com",)  # unchanged


def test_narrow_write_rejects_adding_an_origin() -> None:
    scope = SessionScope(session_id="s1", write=("github.com",))
    with pytest.raises(ScopeError):
        scope.narrow(write=("github.com", "contoso.com"))
    assert scope.write == ("github.com",)


def test_narrow_write_rejects_a_disjoint_set() -> None:
    scope = SessionScope(session_id="s1", write=("github.com",))
    with pytest.raises(ScopeError):
        scope.narrow(write=("contoso.com",))


def test_narrow_write_accepts_strict_subset() -> None:
    scope = SessionScope(session_id="s1", write=("github.com", "contoso.com"))
    scope.narrow(write=("github.com",))
    assert scope.write == ("github.com",)


def test_narrow_write_to_empty_tuple_is_the_ultimate_narrowing() -> None:
    scope = SessionScope(session_id="s1", write=("github.com",))
    scope.narrow(write=())
    assert scope.write == ()
    assert not scope.permits_write("github.com")


def test_narrow_on_unknown_moves_forward_only() -> None:
    scope = SessionScope(session_id="s1")
    scope.narrow(on_unknown="gate")
    assert scope.on_unknown == "gate"
    scope.narrow(on_unknown="deny")
    assert scope.on_unknown == "deny"
    with pytest.raises(ScopeError):
        scope.narrow(on_unknown="allow")


def test_narrow_on_unknown_can_skip_directly_to_deny() -> None:
    scope = SessionScope(session_id="s1")
    scope.narrow(on_unknown="deny")
    assert scope.on_unknown == "deny"


def test_narrow_redeem_moves_forward_only() -> None:
    scope = SessionScope(session_id="s1")
    scope.narrow(redeem="out_of_band")
    assert scope.redeem == "out_of_band"
    with pytest.raises(ScopeError):
        scope.narrow(redeem="agent")


def test_narrow_unattended_moves_forward_only() -> None:
    scope = SessionScope(session_id="s1")
    scope.narrow(unattended=True)
    assert scope.unattended is True
    with pytest.raises(ScopeError):
        scope.narrow(unattended=False)


def test_narrow_no_op_on_ordered_fields_is_tolerated() -> None:
    """Re-declaring the SAME value for on_unknown/redeem/unattended is
    harmless (unlike write/read, which require a literal 'strict subset')."""
    scope = SessionScope(session_id="s1", on_unknown="gate", redeem="out_of_band", unattended=True)
    scope.narrow(on_unknown="gate", redeem="out_of_band", unattended=True)
    assert scope.on_unknown == "gate"
    assert scope.redeem == "out_of_band"
    assert scope.unattended is True


def test_narrow_rejects_unknown_field() -> None:
    scope = SessionScope(session_id="s1")
    with pytest.raises(ScopeError):
        scope.narrow(bogus_field="x")  # type: ignore[call-arg]


def test_narrow_validates_atomically_before_mutating_anything() -> None:
    """Three fields narrow correctly; the fourth is a widening attempt -- the
    WHOLE call must fail, and none of the three valid ones may apply either."""
    scope = SessionScope(session_id="s1", write=("github.com", "contoso.com"), on_unknown="allow")
    with pytest.raises(ScopeError):
        scope.narrow(write=("github.com",), on_unknown="gate", redeem="out_of_band", unattended="not-a-bool")
    assert scope.write == ("github.com", "contoso.com")
    assert scope.on_unknown == "allow"
    assert scope.redeem == "agent"


def test_seal_blocks_every_subsequent_change_narrowing_included() -> None:
    scope = SessionScope(session_id="s1", write=("github.com", "contoso.com"))
    assert not scope.sealed
    scope.seal()
    assert scope.sealed
    with pytest.raises(ScopeError, match="sealed"):
        scope.narrow(write=("github.com",))
    with pytest.raises(ScopeError, match="sealed"):
        scope.narrow(on_unknown="deny")
    assert scope.write == ("github.com", "contoso.com")


def test_seal_is_idempotent() -> None:
    scope = SessionScope(session_id="s1")
    scope.seal()
    scope.seal()  # must not raise
    assert scope.sealed


def test_narrow_empty_kwargs_is_a_no_op_even_when_sealed() -> None:
    scope = SessionScope(session_id="s1", write=("github.com",))
    scope.seal()
    scope.narrow()  # no fields -> nothing to reject
    assert scope.write == ("github.com",)


def test_from_wire_builds_arbitrary_initial_scope() -> None:
    """Unlike narrow(), from_wire (establish_session's construction path)
    accepts ANY well-shaped initial values -- there is no 'current' state to
    narrow relative to yet."""
    scope = SessionScope.from_wire(
        "s1", {"write": ["github.com"], "on_unknown": "deny", "redeem": "out_of_band", "unattended": True}
    )
    assert scope.session_id == "s1"
    assert scope.write == ("github.com",)
    assert scope.on_unknown == "deny"
    assert scope.redeem == "out_of_band"
    assert scope.unattended is True


def test_from_wire_rejects_malformed_fields() -> None:
    with pytest.raises(ScopeError):
        SessionScope.from_wire("s1", {"on_unknown": "not-a-real-value"})
    with pytest.raises(ScopeError):
        SessionScope.from_wire("s1", {"write": "not-a-list-or-star"})
    with pytest.raises(ScopeError):
        SessionScope.from_wire("s1", {"unattended": "yes"})


def test_to_wire_round_trips_the_shape() -> None:
    scope = SessionScope(session_id="s1", write=("github.com",), read="*")
    wire = scope.to_wire()
    assert wire == {
        "session_id": "s1",
        "read": "*",
        "write": ["github.com"],
        "on_unknown": "allow",
        "redeem": "agent",
        "unattended": False,
        "sealed": False,
    }
