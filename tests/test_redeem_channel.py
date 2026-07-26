"""Regression tests for the live self-attestation hole (review panel FAIL,
2026-07-26): "Mint any token with `redeem: 'out_of_band'` and call the confirm
handler from the agent's own process. It succeeds. That's not a gap to design
around later -- it's a working exploit right now, for every gate that declares
itself out-of-band."

`PendingConfirmation` had no `redeem` field and `_handle_agent_confirm` never
checked one -- a session declaring `redeem: "out_of_band"` (the name at the
time) was redeemable by the agent's own model via the exact same `/agent`
route as `redeem: "agent"`.

The fix: `redeem` is carried on the confirmation (`PendingConfirmation.redeem`,
set from `scope.redeem` at gate-fire time) and ENFORCED at redemption
(`PolicyEngine.consume_confirmation`'s `via` parameter, which is always
`"agent"` -- the only redemption route this codebase has, or ever will have).

RENAMED (2026-07-26, same day, following review of `docs/designs/
approval-channel-options.md`): `redeem: "out_of_band"` -> `redeem:
"unredeemable"`. The human-approval channel that name promised was cancelled
outright -- a live CDP experiment showed the strongest candidate (the
extension's own options page) could be driven by the very agent it needed to
exclude via `chrome.debugger`, and the simpler fix (a narrower session scope,
already built in `scope.py`) was available the whole time. `"unredeemable"` is
the honest name for what this value has always actually done: since no
out-of-band redemption channel exists -- and per that decision, none ever
will -- a confirmation declaring this mode cannot be redeemed at all, by any
route, ever. This is a permanent property, not a temporary gap. See
`docs/designs/approval-channel-options.md` and `docs/designs/
confirmation-gate.md` section 16 for the full reasoning.

This test suite proves the fail-closed behavior the rename now names honestly:
a `redeem: "unredeemable"` confirmation is refused through the agent channel,
unconditionally, every time.
"""

from __future__ import annotations

from typing import Any

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.hub import Hub
from amplifier_browser_bridge.policy import PolicyEngine, PolicyError
from amplifier_browser_bridge.scope import SessionScope

ELEVATE_LABEL = "Elevate bkrabach to Administrator"


def _engine(tmp_path: Any) -> PolicyEngine:
    return PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))


# ---------------------------------------------------------------------------
# THE DELIVERABLE: the FAIL, reproduced and proven closed.
# ---------------------------------------------------------------------------


def test_unredeemable_confirmation_cannot_be_redeemed_via_agent_channel(tmp_path: Any) -> None:
    """THE regression test for the review panel's FAIL. Reproduces the exploit
    verbatim: a session declares `redeem: "unredeemable"`, a gate fires under
    that session, and the agent tries to redeem its own token through the
    ONLY redemption route this codebase has (`consume_confirmation(..., via="agent")`,
    exactly what `Hub._handle_agent_confirm` calls for both an agent's own
    `confirm` message and a human running `abb confirm`). This must fail,
    loudly, every time -- not silently succeed."""
    engine = _engine(tmp_path)
    scope = SessionScope(
        session_id="sess-oob", write=("repos.opensource.microsoft.com",), redeem="unredeemable"
    )

    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e1"),
        "click",
        {"ref": "e1", "label": ELEVATE_LABEL, "page_url": "https://repos.opensource.microsoft.com/x"},
        scope=scope,
    )
    assert decision.status == "gate"
    assert decision.token is not None
    # The wire-level field must ALSO reflect the session's declared mode --
    # before this fix, PolicyDecision.redeem was NEVER set from scope.redeem
    # anywhere in evaluate(), so it silently stayed "agent" regardless of what
    # the session declared. Fixing enforcement without fixing this would still
    # tell the agent (and any caller inspecting the wire response) that
    # self-attestation was fine, which is its own instance of "a field nobody
    # checks is a claim about behavior that doesn't happen."
    assert decision.redeem == "unredeemable"

    # THE EXPLOIT: the agent tries to redeem its own unredeemable token through
    # the agent channel -- exactly `Hub._handle_agent_confirm`'s call shape.
    try:
        engine.consume_confirmation(decision.token, via="agent")
    except PolicyError as e:
        message = str(e)
    else:
        raise AssertionError(
            "SECURITY REGRESSION: an unredeemable-declared confirmation was "
            "redeemed via the agent channel -- the self-attestation hole is open."
        )

    # Fail closed AND say so clearly (task requirement): the error must name
    # what's actually true -- human approval is required and there is no such
    # channel, ever -- not a generic "denied".
    assert "human" in message.lower()
    assert "unredeemable" in message


def test_hub_agent_confirm_route_rejects_unredeemable_token_end_to_end(tmp_path: Any) -> None:
    """Same exploit, but driven through `Hub._handle_agent_confirm` itself --
    the exact code path both an agent's own `confirm` WebSocket message and a
    human running `abb confirm <token>` (cli.py) reach. Proves the fix holds
    at the boundary the exploit actually crosses, not just inside PolicyEngine."""
    hub = Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"))
    scope = hub.establish_session(write=["repos.opensource.microsoft.com"], redeem="unredeemable")
    record = hub.registry.get_or_create("d1")

    class _FakeWs:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_json(self, data: dict[str, Any], /) -> None:
            self.sent.append(data)
            fut = record.pending.get(data["id"])
            if fut is not None and not fut.done():
                fut.set_result({"ok": True, "id": data["id"], "result": {"ref": "e1"}})

    record.ws = _FakeWs()
    record.touch()

    import asyncio

    async def run() -> dict[str, Any]:
        gated = await hub.send_command(
            Target(device_id="d1", tab_id=1, ref="e1"),
            "click",
            {"ref": "e1", "label": ELEVATE_LABEL, "page_url": "https://repos.opensource.microsoft.com/x"},
            session_id=scope.session_id,
        )
        assert gated["status"] == "needs_confirmation"
        assert gated["redeem"] == "unredeemable"
        # The exploit: the agent calls the SAME confirm handler a human's
        # `abb confirm` CLI invocation reaches.
        return await hub._handle_agent_confirm({"confirmation_token": gated["confirmation_token"]})

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "unredeemable" in result["error"]


# ---------------------------------------------------------------------------
# Non-regression: the default (redeem="agent") path is completely unaffected.
# ---------------------------------------------------------------------------


def test_agent_channel_confirmation_still_works_as_before(tmp_path: Any) -> None:
    """Uses a NON-escalation category label (`delete`, not `permission_change`)
    -- FIX 3 (product review panel) forces `redeem="unredeemable"` for
    ESCALATION_CATEGORIES regardless of the session's own declared `redeem`,
    so `ELEVATE_LABEL` is no longer a valid "ordinary gate" fixture for this
    test's actual purpose (proving the wrong-channel FIX left the default
    redeem="agent" path unaffected for gates OUTSIDE that category). See
    `tests/test_escalation_category.py` for ELEVATE_LABEL's new behavior."""
    engine = _engine(tmp_path)
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e1"),
        "click",
        {"ref": "e1", "label": "Delete Repository", "page_url": "https://repos.opensource.microsoft.com/x"},
    )
    assert decision.status == "gate"
    assert decision.category == "delete"
    assert decision.redeem == "agent"
    assert decision.token is not None
    pending = engine.consume_confirmation(decision.token, via="agent")  # must NOT raise
    assert pending.used is True


# ---------------------------------------------------------------------------
# A wrong-channel attempt must not burn the token -- it isn't "used up," it's
# a standing refusal. It will refuse identically on every subsequent attempt,
# up to the token's normal expiry, since no channel this token would accept
# will ever exist (there is no future out-of-band channel to design around --
# see docs/designs/approval-channel-options.md's cancellation).
# ---------------------------------------------------------------------------


def test_wrong_channel_attempt_does_not_consume_the_token(tmp_path: Any) -> None:
    engine = _engine(tmp_path)
    scope = SessionScope(
        session_id="sess-oob2", write=("repos.opensource.microsoft.com",), redeem="unredeemable"
    )
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e1"),
        "click",
        {"ref": "e1", "label": ELEVATE_LABEL, "page_url": "https://repos.opensource.microsoft.com/x"},
        scope=scope,
    )
    assert decision.token is not None

    for _ in range(3):
        try:
            engine.consume_confirmation(decision.token, via="agent")
        except PolicyError:
            pass
        else:
            raise AssertionError("must keep refusing, not eventually succeed")

    # The token is still present, unused -- marking it "used" would claim a
    # redemption that never happened; it stays unused until it naturally
    # expires.
    pending = engine._confirmations.get(decision.token)
    assert pending is not None
    assert pending.used is False

    audit_text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "policy_confirmation_wrong_channel" in audit_text
