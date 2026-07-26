"""FIX 2 (product review priority, two independent councils converged here): the
regression test in `tests/test_gate_elevation.py` proves the *classifier/policy
layer* catches the measured incident when called directly (`PolicyEngine.evaluate`).
It does not prove the *front door* -- the real, end-to-end path an agent actually
uses (`Hub.send_command`, the single choke point documented in `hub.py`'s module
docstring) -- holds too.

Verbatim from the review panels:

    "You proved the escape hatch doesn't hold. You haven't shown me the front
    door holds either."
    "There's no line that says 'we replayed the actual Administrator-elevation
    click through the finished system and it stopped.'"

This file drives the FULL pipeline: a `snapshot` populates the hub's own
ref-label/page-context caches (`PolicyEngine.note_snapshot`/`note_page_context`,
fed from `Hub._ingest_result` -- exactly what a real device `result` message
does), then two `click` commands are sent through `Hub.send_command` with
*only a `ref`* in `args` -- the caller never supplies a `label`, mirroring how
an agent driving this system in practice would call it (it has a `ref` from a
snapshot, not necessarily a repeated label string). The hub must resolve the
label itself from its own cache and gate on it, or this proves nothing about
the real dispatch path.

## What's grounded vs. what's honestly unverified

`BLAND_LABEL` / `ELEVATE_LABEL` are the maintainer's own measured, verbatim
transcription of the incident (`docs/designs/confirmation-gate.md` section 1).
`FLOW_URL` / `PAGE_TITLE` are the OBSERVED URL shape and title template captured
read-only from a live, already-connected tab during this project's phase-5 work
(see `tests/test_gate_elevation.py`'s module docstring for the full provenance
of that observation) -- a neutral placeholder org/repo stands in for the real
names captured, since the shape is what's load-bearing, not the specific
repository identity.

Two facts remain genuinely UNVERIFIED, and this file does not claim them:
whether the "Next" click itself issues an observable non-GET request, and the
page's exact heading (`<h1>`/`<h2>`) structure. Both would require clicking
through the real privilege-escalation flow, which this task's own operating
instructions explicitly prohibited (read-only observation only, on the
maintainer's live, connected browser). Where a test below depends on either
fact, it is fabricated with an explicit `SYNTHETIC` label in the test name and
docstring -- consistent with `tests/test_gate_elevation.py`'s own convention --
never presented as an observed fact. See `docs/designs/confirmation-gate.md`
section 9 for the full, current honest-limits accounting these two gaps live
in.
"""

from __future__ import annotations

import asyncio
from typing import Any

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.hub import Hub
from amplifier_browser_bridge.policy import PolicyError

# Measured, verbatim (docs/designs/confirmation-gate.md section 1).
BLAND_LABEL = "Next"
ELEVATE_LABEL = "Elevate bkrabach to Administrator"

# OBSERVED (read-only snapshot of a live tab on the maintainer's real, already-
# connected browser -- see tests/test_gate_elevation.py's module docstring for
# the full provenance) URL shape and title template. A neutral placeholder
# org/repo stands in for the real names captured during that observation.
FLOW_URL = "https://repos.opensource.microsoft.com/orgs/contoso/repos/sample-repo/jit"
PAGE_TITLE = "contoso/sample-repo repository | Microsoft Open Source"


class _IngestingFakeDeviceSocket:
    """Stands in for the real device WebSocket, but -- unlike the plain
    `FakeDeviceSocket` fixtures elsewhere in this test suite -- routes every
    canned result through `Hub._ingest_result` first, exactly what
    `Hub._handle_device_message`'s real `result` branch does for an actual
    device message. This is what makes `note_snapshot`/`note_page_context`
    (fed from a `snapshot` result) and the seal-on-first-read session logic
    actually run, and is required for this file's whole point: proving the
    FULL pipeline, not a hand-constructed `PolicyEngine` call.

    `snapshot_result` answers any `snapshot` command. `click_results_by_ref`
    answers a `click` command by the `ref` the wire envelope actually names
    (read from `data["target"]["ref"]`, which `Target.to_dict()` includes
    whenever the target carries one) -- this lets the "Next" click and a
    later confirmed "Elevate" replay each get their own canned result,
    without conflating two different clicks under one command-name key.

    `dispatched` records every command that actually reached "the device" --
    the gated Elevate click must NOT appear here on its first attempt; only
    after confirmation does its re-dispatch show up.
    """

    def __init__(
        self,
        hub: Hub,
        record: Any,
        *,
        snapshot_result: dict[str, Any],
        click_results_by_ref: dict[str, dict[str, Any]],
    ) -> None:
        self.hub = hub
        self.record = record
        self.snapshot_result = snapshot_result
        self.click_results_by_ref = click_results_by_ref
        self.dispatched: list[tuple[str, str | None]] = []

    async def send_json(self, data: dict[str, Any], /) -> None:
        ref = (data.get("target") or {}).get("ref")
        self.dispatched.append((data["command"], ref))
        fut = self.record.pending.get(data["id"])
        if fut is None or fut.done():
            return
        if data["command"] == "snapshot":
            result = self.snapshot_result
        else:
            assert isinstance(ref, str), f"expected a ref-addressed command, got target: {data.get('target')}"
            result = self.click_results_by_ref[ref]
        raw_env = {**result, "id": data["id"], "device_id": self.record.device_id}
        env = self.hub._ingest_result(self.record.device_id, data["id"], raw_env)
        fut.set_result(env)


def _hub(tmp_path: Any) -> Hub:
    return Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"))


# ---------------------------------------------------------------------------
# The deliverable: the front door holds. Grounded ONLY in measured facts
# (labels, verbatim) and observed facts (URL shape, title template) -- no
# fabricated effects, no fabricated headings.
# ---------------------------------------------------------------------------


def test_incident_replay_gates_the_elevation_through_the_full_hub_pipeline(tmp_path: Any) -> None:
    """Replays the actual incident sequence -- snapshot, click "Next", click
    "Elevate bkrabach to Administrator" -- through `Hub.send_command`, the
    single choke point every real agent call travels through (not
    `PolicyEngine.evaluate` called directly). Proves:

    1. The hub resolves each click's label itself from its own snapshot cache
       (the caller supplies only `ref`, matching real agent usage) -- Phase 4's
       "label hints are wired" claim, exercised end-to-end rather than assumed.
    2. The bland "Next" click is allowed and reaches the device -- reproducing
       the incident's own (correct, not a defect) first half.
    3. The elevation click is gated by `Hub.send_command` BEFORE it ever
       reaches `_dispatch_live`/the device -- the front door holds. It never
       appears in `dispatched`.
    4. The gate decision is audited (`policy_gated`).
    """
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")

    snapshot_result: dict[str, Any] = {
        "ok": True,
        "result": {
            "url": FLOW_URL,
            "page_title": PAGE_TITLE,
            "headings": [],
            "nodes": [
                {"ref": "e1", "name": BLAND_LABEL, "tag": "button"},
                {"ref": "e2", "name": ELEVATE_LABEL, "tag": "button"},
            ],
        },
    }
    click_results_by_ref: dict[str, dict[str, Any]] = {
        # Honest about what's unverified: whether the real "Next" click issues
        # an observable non-GET request was never measured (clicking through
        # the flow was out of bounds for this task). No requests/navigations
        # are asserted here -- this is the honest "we don't know" wire shape,
        # not a claim that we verified no request occurred.
        "e1": {
            "ok": True,
            "result": {"ref": "e1", "tag": "button"},
            "effects": {"tier": "navigation", "window_ms": 1500, "attribution": "time_window"},
        },
    }
    fake_ws = _IngestingFakeDeviceSocket(
        hub, record, snapshot_result=snapshot_result, click_results_by_ref=click_results_by_ref
    )
    record.ws = fake_ws
    record.touch()

    async def run() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        snap = await hub.send_command(Target(device_id="d1", tab_id=1), "snapshot", {})
        next_click = await hub.send_command(
            Target(device_id="d1", tab_id=1, ref="e1"), "click", {"ref": "e1"}
        )
        elevate_click = await hub.send_command(
            Target(device_id="d1", tab_id=1, ref="e2"), "click", {"ref": "e2"}
        )
        return snap, next_click, elevate_click

    snap_result, next_result, elevate_result = asyncio.run(run())

    # --- snapshot reached the device normally ---
    assert snap_result["ok"] is True

    # --- the bland "Next" click: allowed, reached the device, no label
    #     supplied by the caller (proves the hub resolved it from its own
    #     cache -- there is no other source for a label here) ---
    assert next_result["ok"] is True
    assert ("click", "e1") in fake_ws.dispatched

    # --- the elevation click: gated, and NEVER reached the device ---
    assert elevate_result.get("status") == "needs_confirmation"
    assert elevate_result.get("confirmation_token")
    assert elevate_result.get("category") == "permission_change"
    classification = elevate_result.get("classification")
    assert classification is not None
    assert "permission_change" in classification["categories"]
    assert classification["score"] >= classification["threshold"]
    assert classification["advisory"] is True
    # This is the load-bearing assertion for "the front door holds": the
    # elevation click's (command, ref) pair never appears among what was
    # actually dispatched to "the device" -- Hub.send_command intercepted it
    # at the policy choke point, before _dispatch_live ever ran.
    assert ("click", "e2") not in fake_ws.dispatched
    assert fake_ws.dispatched == [("snapshot", None), ("click", "e1")]

    # --- audited ---
    audit_text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert '"policy_gated"' in audit_text
    assert ELEVATE_LABEL in audit_text or "permission_change" in audit_text


# ---------------------------------------------------------------------------
# Extends the above: the redemption path (D2) and attribution (D3) also work
# through the FULL hub, not just PolicyEngine directly (test_gate_elevation.py
# case 5 already proves the policy-layer half of this). The POST observed on
# the confirmed replay is explicitly SYNTHETIC -- whether the real elevation
# click issues one is unverified (see module docstring) -- so this test is
# named and documented accordingly, and must never be read as a claim about
# the real page's behavior.
# ---------------------------------------------------------------------------


def test_confirmed_elevation_replay_is_attributed_end_to_end_through_the_full_hub_SYNTHETIC(
    tmp_path: Any,
) -> None:
    """SYNTHETIC effects: the confirmed replay's canned device result reports
    a POST to `{FLOW_URL}/elevate`. This is a fabricated stand-in for an
    unverified real-world fact (see module docstring) -- it proves the
    attribution *mechanism* (effects parsed, attached to the result envelope,
    and audited) is wired correctly end-to-end through `Hub.send_command` ->
    `_handle_agent_confirm` -> `_ingest_result`, not that the real incident's
    click issued this exact request.

    Establishes a session with `allow_self_attested_escalation=True` (FIX 3,
    product review panel): ELEVATE_LABEL classifies as `permission_change`,
    which `PolicyEngine.evaluate` now forces to `redeem="unredeemable"`
    regardless of write scope UNLESS a session explicitly opts in. This
    test's purpose is D3 attribution, not the escalation lock itself --
    see `test_incident_replay_elevation_cannot_self_attest_even_when_the_origin_is_in_scope`
    below for that.
    """
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    scope = hub.establish_session(
        write=["repos.opensource.microsoft.com"], allow_self_attested_escalation=True
    )

    snapshot_result: dict[str, Any] = {
        "ok": True,
        "result": {
            "url": FLOW_URL,
            "page_title": PAGE_TITLE,
            "headings": [],
            "nodes": [{"ref": "e2", "name": ELEVATE_LABEL, "tag": "button"}],
        },
    }
    click_results_by_ref: dict[str, dict[str, Any]] = {
        # The confirmed re-dispatch's canned result -- SYNTHETIC (see docstring).
        "e2": {
            "ok": True,
            "result": {"ref": "e2", "tag": "button"},
            "effects": {
                "tier": "webrequest",
                "window_ms": 1500,
                "attribution": "time_window",
                "requests": [{"method": "POST", "url": f"{FLOW_URL}/elevate"}],
            },
        },
    }
    fake_ws = _IngestingFakeDeviceSocket(
        hub, record, snapshot_result=snapshot_result, click_results_by_ref=click_results_by_ref
    )
    record.ws = fake_ws
    record.touch()

    async def gate_and_confirm() -> tuple[dict[str, Any], list[tuple[str, str | None]], dict[str, Any]]:
        await hub.send_command(Target(device_id="d1", tab_id=1), "snapshot", {}, session_id=scope.session_id)
        gated = await hub.send_command(
            Target(device_id="d1", tab_id=1, ref="e2"), "click", {"ref": "e2"}, session_id=scope.session_id
        )
        # Snapshot dispatched state BETWEEN the gate and the confirm -- checking
        # fake_ws.dispatched only after both steps have run would always include
        # the post-confirm redispatch, hiding the "not yet dispatched" fact.
        dispatched_before_confirm = list(fake_ws.dispatched)
        confirmed = await hub._handle_agent_confirm({"confirmation_token": gated["confirmation_token"]})
        return gated, dispatched_before_confirm, confirmed

    gated_result, dispatched_before_confirm, confirmed_result = asyncio.run(gate_and_confirm())

    assert gated_result["status"] == "needs_confirmation"
    # Not dispatched to the device until confirmed.
    assert ("click", "e2") not in dispatched_before_confirm

    assert confirmed_result["ok"] is True
    # NOW it reached the device, exactly once, via the confirmed re-dispatch.
    assert fake_ws.dispatched.count(("click", "e2")) == 1

    effects = confirmed_result.get("effects")
    assert effects is not None
    assert effects["state_changing"] is True
    assert any(r["method"] == "POST" for r in effects["requests"])

    audit_text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert '"action_effects"' in audit_text
    assert '"policy_confirmed"' in audit_text


# ---------------------------------------------------------------------------
# FIX 3 (product review panel, the most concrete ask): "name and enforce an
# explicit deny-list of privilege/permission-escalating actions that no
# declared session scope can implicitly include." Ties directly to FIX 2 --
# this replays the SAME incident sequence through the SAME full Hub pipeline,
# but with a session whose write scope explicitly INCLUDES the flow's own
# origin (the realistic case: the maintainer's stated preference for broad
# access means github.com will almost always be in scope for a task that
# needs to be there at all). Proves the gap the panel named: "scope is the
# boundary" does not, by itself, prevent a recurrence, because self-
# attestation (redeem="agent", the system-wide default) is still available
# to whatever confirms the token -- UNLESS the session separately, explicitly
# opts in to `allow_self_attested_escalation`.
# ---------------------------------------------------------------------------


def test_incident_replay_elevation_cannot_self_attest_even_when_the_origin_is_in_scope(
    tmp_path: Any,
) -> None:
    """A session establishes write scope for the flow's own origin (the
    realistic shape -- the task legitimately needs to be on
    repos.opensource.microsoft.com) and does NOT opt into
    `allow_self_attested_escalation`. The elevation click still gates (scope
    inclusion never bypasses classification), but critically its
    confirmation is REFUSED through the ONLY redemption route this system
    has (`Hub._handle_agent_confirm`, reached identically by an agent's own
    `confirm` call and a human running `abb confirm`) -- proving that a
    broad, legitimate write scope does not implicitly grant the ability to
    self-attest an Administrator escalation on that same origin.
    """
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    # Realistic scope: broad enough to cover the task's legitimate origin,
    # but NOT opted into self-attested escalation.
    scope = hub.establish_session(write=["repos.opensource.microsoft.com"])

    snapshot_result: dict[str, Any] = {
        "ok": True,
        "result": {
            "url": FLOW_URL,
            "page_title": PAGE_TITLE,
            "headings": [],
            "nodes": [{"ref": "e2", "name": ELEVATE_LABEL, "tag": "button"}],
        },
    }
    fake_ws = _IngestingFakeDeviceSocket(
        hub, record, snapshot_result=snapshot_result, click_results_by_ref={}
    )
    record.ws = fake_ws
    record.touch()

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        await hub.send_command(Target(device_id="d1", tab_id=1), "snapshot", {}, session_id=scope.session_id)
        gated = await hub.send_command(
            Target(device_id="d1", tab_id=1, ref="e2"), "click", {"ref": "e2"}, session_id=scope.session_id
        )
        confirm_attempt = await hub._handle_agent_confirm({"confirmation_token": gated["confirmation_token"]})
        return gated, confirm_attempt

    gated_result, confirm_attempt_result = asyncio.run(run())

    # The origin IS in scope -- classification still runs and gates normally.
    assert gated_result["status"] == "needs_confirmation"
    assert gated_result["category"] == "permission_change"
    # The load-bearing assertion: even though the session's write scope
    # covers this exact origin, the wire-level redeem mode was forced to
    # "unredeemable" -- NOT the session's own "agent" default -- because
    # permission_change is an ESCALATION_CATEGORIES member and the session
    # never opted in.
    assert gated_result["redeem"] == "unredeemable"

    # The confirm attempt (the same route an agent's own confirm call, or a
    # human's `abb confirm`, would reach) is refused, never dispatched.
    assert confirm_attempt_result["ok"] is False
    assert "e2" not in [ref for _cmd, ref in fake_ws.dispatched if ref is not None]

    audit_text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert '"escalation_locked": true' in audit_text


def test_escalation_lock_lifted_only_by_explicit_opt_in(tmp_path: Any) -> None:
    """Symmetric proof: the SAME scope, SAME origin, SAME label -- the only
    difference is `allow_self_attested_escalation=True` declared explicitly
    at `establish_session` time. This is the only way to get the old
    (self-attestable) behavior back for this category -- confirming the
    lock is real and confirming the escape hatch is real and explicit, not
    a silent default."""
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    scope = hub.establish_session(
        write=["repos.opensource.microsoft.com"], allow_self_attested_escalation=True
    )
    fake_ws = _IngestingFakeDeviceSocket(
        hub,
        record,
        snapshot_result={
            "ok": True,
            "result": {
                "url": FLOW_URL,
                "page_title": PAGE_TITLE,
                "headings": [],
                "nodes": [{"ref": "e2", "name": ELEVATE_LABEL, "tag": "button"}],
            },
        },
        click_results_by_ref={"e2": {"ok": True, "result": {"ref": "e2", "tag": "button"}}},
    )
    record.ws = fake_ws
    record.touch()

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        await hub.send_command(Target(device_id="d1", tab_id=1), "snapshot", {}, session_id=scope.session_id)
        gated = await hub.send_command(
            Target(device_id="d1", tab_id=1, ref="e2"), "click", {"ref": "e2"}, session_id=scope.session_id
        )
        confirmed = await hub._handle_agent_confirm({"confirmation_token": gated["confirmation_token"]})
        return gated, confirmed

    gated_result, confirmed_result = asyncio.run(run())
    assert gated_result["redeem"] == "agent"
    assert confirmed_result["ok"] is True


def test_engine_level_wrong_channel_error_names_escalation_lock(tmp_path: Any) -> None:
    """Direct PolicyEngine-level check (mirrors tests/test_redeem_channel.py's
    style) that the refusal error is specific about WHY -- distinguishing
    "this session declared unredeemable itself" from "the escalation lock
    forced this," so an agent (or a human reading the audit log) is not left
    guessing which mechanism refused it."""
    from amplifier_browser_bridge.audit import AuditLog
    from amplifier_browser_bridge.policy import PolicyEngine
    from amplifier_browser_bridge.scope import SessionScope

    engine = PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))
    scope = SessionScope(session_id="sess-esc", write=("repos.opensource.microsoft.com",))
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e2"),
        "click",
        {"ref": "e2", "label": ELEVATE_LABEL, "page_url": FLOW_URL},
        scope=scope,
    )
    assert decision.status == "gate"
    assert decision.redeem == "unredeemable"
    assert decision.token is not None
    try:
        engine.consume_confirmation(decision.token, via="agent")
    except PolicyError as e:
        assert "unredeemable" in str(e)
    else:
        raise AssertionError("escalation-locked confirmation must not be redeemable via agent channel")
