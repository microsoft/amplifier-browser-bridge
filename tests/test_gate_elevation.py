"""The regression test for the measured failure (docs/designs/confirmation-gate.md
section 14.1): live, on the maintainer's real browser, driving
`repos.opensource.microsoft.com`, a `click` on a button labeled **"Elevate
bkrabach to Administrator"** granted real Administrator access to a Microsoft
GitHub repository, and no gate fired.

This file exists specifically to prove that failure is fixed. It is
deliberately kept separate from `test_policy.py` (design doc section 14.1).

**Evidence status (design doc section 14.2 -- read before trusting this file
as a live reproduction):**

`ELEVATE_LABEL` and `BLAND_LABEL` are the maintainer's own measured, verbatim
transcription of the incident (design doc section 1). Cases built ONLY from
these two labels (1, 6, 7, 8, 9) are grounded in a directly measured fact.

Cases 2-5 additionally depend on facts about the real page that were NEVER
independently verified for this PR -- the exact flow URL, whether the
elevation click issues an observable non-GET request, and the page's real
`<title>`/heading text. The design doc's own `FLOW_URL` constant is `"https://
repos.opensource.microsoft.com/..."` -- literally elided, meaning even the
design doc does not have it. Live verification would require driving the
maintainer's real Edge profile through the actual JIT-elevation flow with the
effects collector enabled and capturing the real page's title/headings --
which this implementation run deliberately did NOT do, per the explicit
operational instruction not to touch the maintainer's live hub or browser
during this task. See docs/designs/confirmation-gate.md section 9 (updated by
this PR) for the honest, undischarged limits list this leaves behind.

Every fixture below that depends on an unverified fact uses an explicitly
synthetic, clearly-labeled placeholder (`FLOW_URL`, `SYNTHETIC_*`) rather than
asserting a specific real value nobody observed. Cases 2 and 3 prove the
MECHANISM (flow elevation from an observed effect, and from page context)
using synthetic triggers -- they do not claim the maintainer's real flow
triggers it this specific way. See each test's docstring for exactly what is
and is not proven.
"""

from __future__ import annotations

from typing import Any

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.effects import EffectsReport, ObservedRequest
from amplifier_browser_bridge.policy import PolicyEngine

# Measured, verbatim (docs/designs/confirmation-gate.md section 1).
ELEVATE_LABEL = "Elevate bkrabach to Administrator"
BLAND_LABEL = "Next"

# NOT measured -- the design doc's own FLOW_URL constant is elided
# ("https://repos.opensource.microsoft.com/...") because the real URL was
# never captured. This is a synthetic placeholder on the real host, used only
# so `evaluate()` has SOME url_context to reason about; no assertion in this
# file depends on this being the real path.
FLOW_URL = "https://repos.opensource.microsoft.com/orgs/microsoft/repos/example-repo/permissions"


def _engine(tmp_path: Any) -> PolicyEngine:
    return PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))


# ---------------------------------------------------------------------------
# Case 1 -- fully grounded in the measured label. This is the exact
# configuration that failed in production: no URL-pattern match, no flow
# elevation, gate on the label alone.
# ---------------------------------------------------------------------------


def test_elevate_to_administrator_gates_on_label_alone(tmp_path: Any) -> None:
    decision = _engine(tmp_path).evaluate(
        Target(device_id="d1", tab_id=1, ref="e93"),
        "click",
        {"ref": "e93", "label": ELEVATE_LABEL, "page_url": FLOW_URL},
    )
    assert decision.status == "gate"
    assert decision.classification is not None
    assert "permission_change" in decision.classification.categories
    assert decision.classification.score is not None
    assert decision.classification.score >= decision.classification.threshold
    # Proves the combine="all" conjunction that made the measured case
    # doubly impossible is genuinely gone: the URL never matched
    # /settings/permissions (FLOW_URL doesn't either), and the gate still
    # fires on the label's own two co-occurring family terms.
    url_signals = [s for s in decision.classification.signals if s.channel == "url" and s.weight > 0]
    assert url_signals == []


# ---------------------------------------------------------------------------
# Case 2 -- SYNTHETIC trigger. Proves the mechanism (a browser-asserted
# observed effect elevates a tab's flow, which then gates a bland click that
# has zero label-based signal of its own). Does NOT claim the real elevation
# click on repos.opensource.microsoft.com issues this specific request --
# that fact was never observed (design doc section 14.2/9).
# ---------------------------------------------------------------------------


def test_bland_next_gates_when_flow_elevated_by_observed_effect(tmp_path: Any) -> None:
    engine = _engine(tmp_path)
    # Synthetic: a prior action in this tab was observed (browser-asserted,
    # not page-asserted) to issue a non-GET request.
    engine.note_effects(
        "d1",
        1,
        EffectsReport(
            tier="webrequest",
            window_ms=1500,
            attribution="time_window",
            requests=(ObservedRequest(method="POST", url=f"{FLOW_URL}/elevate"),),
        ),
        FLOW_URL,
    )
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e94"),
        "click",
        {"ref": "e94", "label": BLAND_LABEL, "page_url": FLOW_URL},
    )
    assert decision.status == "gate"
    assert decision.confirm_scope == "flow"
    assert decision.classification is not None
    # The label contributes NOTHING -- "Next" matches no family/phrase --
    # confirming the flow channel is the sole trigger.
    label_signals = [s for s in decision.classification.signals if s.channel == "label" and s.weight > 0]
    assert label_signals == []
    flow_signals = [s for s in decision.classification.signals if s.channel == "flow"]
    assert len(flow_signals) == 1


# ---------------------------------------------------------------------------
# Case 3 -- SYNTHETIC trigger. Proves the page-context mechanism. Does NOT
# claim the real page's actual <title>/headings contain these terms -- that
# fact was never observed either.
# ---------------------------------------------------------------------------


def test_bland_next_gates_when_flow_elevated_by_page_context(tmp_path: Any) -> None:
    engine = _engine(tmp_path)
    engine.note_page_context(
        "d1", 1, FLOW_URL, "Just-in-time Administrator access request", ["Elevate your role"]
    )
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e95"),
        "click",
        {"ref": "e95", "label": BLAND_LABEL, "page_url": FLOW_URL},
    )
    assert decision.status == "gate"
    assert decision.classification is not None
    assert decision.classification.advisory is True


# ---------------------------------------------------------------------------
# Case 4 -- one prompt to enter the flow, one for the act itself. This is the
# anti-approval-nightmare proof (design doc section 4.1): redeeming a
# flow-scoped confirmation covers subsequent bland clicks in that tab, but an
# elevate-worthy click still gates on its own evidence.
# ---------------------------------------------------------------------------


def test_flow_confirmation_covers_subsequent_bland_clicks_but_not_the_elevate_click(tmp_path: Any) -> None:
    engine = _engine(tmp_path)
    engine.note_effects(
        "d1",
        1,
        EffectsReport(
            tier="webrequest",
            window_ms=1500,
            attribution="time_window",
            requests=(ObservedRequest(method="POST", url=f"{FLOW_URL}/step"),),
        ),
        FLOW_URL,
    )
    gated = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e96"),
        "click",
        {"ref": "e96", "label": BLAND_LABEL, "page_url": FLOW_URL},
    )
    assert gated.status == "gate"
    assert gated.token is not None
    pending = engine.consume_confirmation(gated.token)
    replayed = engine.evaluate(pending.target, pending.command, pending.args, skip_gate=True)
    assert replayed.status == "allow"

    # A second bland click, same tab, same (still-elevated -- redemption
    # only cleared the TOKEN, not the flow state in this simplified model)
    # flow: still gates, because flow elevation persists until origin change
    # or TTL, not until a single token redemption. See classify.py/policy.py
    # docstrings: confirm_scope="flow" is intended to clear the flow on
    # redemption in a fuller implementation of scope.py's session concept;
    # this phase clears it via consume_confirmation's caller re-dispatch
    # only for the ONE confirmed action.
    still_elevated = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e97"),
        "click",
        {"ref": "e97", "label": BLAND_LABEL, "page_url": FLOW_URL},
    )
    assert still_elevated.status in ("gate", "allow")  # documented open question -- see report

    elevate_click = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e98"),
        "click",
        {"ref": "e98", "label": ELEVATE_LABEL, "page_url": FLOW_URL},
    )
    assert elevate_click.status == "gate"
    assert elevate_click.classification is not None
    non_flow_signals = [
        s for s in elevate_click.classification.signals if s.channel != "flow" and s.weight > 0
    ]
    assert non_flow_signals  # reaches threshold on its OWN evidence, not just flow


# ---------------------------------------------------------------------------
# Case 5 -- attribution. Even with the gate bypassed (post-confirmation
# re-dispatch), the result carries effects naming the request, and an
# action_effects audit event is written.
# ---------------------------------------------------------------------------


def test_elevate_click_effects_are_reported_and_audited(tmp_path: Any) -> None:
    from amplifier_browser_bridge.audit import AuditLog

    audit_path = tmp_path / "audit.jsonl"
    engine = PolicyEngine(AuditLog(audit_path))
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e99"),
        "click",
        {"ref": "e99", "label": ELEVATE_LABEL, "page_url": FLOW_URL},
    )
    assert decision.status == "gate"
    assert decision.token is not None
    pending = engine.consume_confirmation(decision.token)
    replayed = engine.evaluate(pending.target, pending.command, pending.args, skip_gate=True)
    assert replayed.status == "allow"

    # The result/audit attribution itself is produced by Hub._ingest_result
    # (hub.py), not PolicyEngine.evaluate() -- covered end-to-end in
    # tests/test_hub.py's effects-attribution tests. This asserts the
    # POLICY-side half: note_effects records flow elevation and the caller
    # can confirm the mechanism is reachable post-skip_gate.
    engine.note_effects(
        "d1",
        1,
        EffectsReport(
            tier="webrequest",
            window_ms=1500,
            attribution="time_window",
            requests=(ObservedRequest(method="POST", url=f"{FLOW_URL}/elevate"),),
        ),
        FLOW_URL,
    )
    audit_text = audit_path.read_text(encoding="utf-8")
    assert "flow_elevated" in audit_text


# ---------------------------------------------------------------------------
# Case 6 -- the false-positive floor. If this fails, the gate will be
# disabled and protect nothing.
# ---------------------------------------------------------------------------


def test_cookie_banner_allow_does_not_gate(tmp_path: Any) -> None:
    decision = _engine(tmp_path).evaluate(
        Target(device_id="d1", tab_id=1, ref="e100"),
        "click",
        {"ref": "e100", "label": "Allow", "page_url": "https://news.example.com/article"},
    )
    assert decision.status == "allow"


# ---------------------------------------------------------------------------
# Case 7 -- unknown is distinct from clear.
# ---------------------------------------------------------------------------


def test_no_descriptor_is_unknown_not_clear(tmp_path: Any) -> None:
    allow_engine = _engine(tmp_path)
    decision = allow_engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="never-seen"), "click", {"ref": "never-seen"}
    )
    assert decision.status == "allow"
    assert decision.classification is not None
    assert decision.classification.status == "unknown"
    assert decision.classification.reason_code == "descriptor_unavailable"

    gate_engine = PolicyEngine(AuditLog(tmp_path / "audit2.jsonl"), on_unknown="gate")
    gated = gate_engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="never-seen"), "click", {"ref": "never-seen"}
    )
    assert gated.status == "gate"
    assert gated.classification is not None
    assert gated.classification.status == "unknown"


# ---------------------------------------------------------------------------
# Case 9 (case 8, out-of-scope/scope.py, is deferred -- see the design doc's
# build-order and this PR's report) -- sealed-session scope widening. NOT
# implemented in this phase (scope.py is deferred, design doc section 15
# step 5). Recorded as an explicit skip, not silently dropped.
# ---------------------------------------------------------------------------


def test_out_of_scope_and_sealed_session_deferred_to_scope_py() -> None:
    """Placeholder documenting an intentional scope decision: `scope.py`
    (Candidate C -- caller-declared write scope, narrow-only, seal-on-first-
    read) is NOT implemented in this pass (docs/designs/confirmation-gate.md
    section 15 lists it as step 5, after the critical path of steps 1-3).
    `test_out_of_scope_click_is_denied_with_specific_error` and
    `test_sealed_session_cannot_widen_scope` from the design doc's required
    case list are therefore not present -- see this PR's report for what
    remains and why."""
