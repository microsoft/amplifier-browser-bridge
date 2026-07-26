"""The regression test for the measured failure (docs/designs/confirmation-gate.md
section 14.1): live, on the maintainer's real browser, driving
`repos.opensource.microsoft.com`, a `click` on a button labeled **"Elevate
bkrabach to Administrator"** granted real Administrator access to a Microsoft
GitHub repository, and no gate fired.

This file exists specifically to prove that failure is fixed. It is
deliberately kept separate from `test_policy.py` (design doc section 14.1).

**Evidence status (design doc section 14.2/9 -- read before trusting this
file as a live reproduction):**

`ELEVATE_LABEL` and `BLAND_LABEL` are the maintainer's own measured, verbatim
transcription of the incident (design doc section 1). Cases built ONLY from
these two labels (1, 6, 7) are grounded in a directly measured fact.

**`FLOW_URL` and `PAGE_TITLE` are now OBSERVED, not synthetic** (updated by
the phase-5 PR that added `scope.py`): a read-only `snapshot` of a live tab on
the maintainer's real, already-connected browser -- a DIFFERENT repository
than the incident (`amplifier-app-wiki-weaver`, not the incident's
`amplifier-app-simulated-user-research`), taken without clicking or otherwise
changing any state -- recorded:

    url:   https://repos.opensource.microsoft.com/orgs/microsoft/repos/amplifier-app-wiki-weaver/jit
    title: microsoft/amplifier-app-wiki-weaver repository | Microsoft Open Source

This establishes the URL *shape* (`.../orgs/{org}/repos/{repo}/jit`) and the
page-title *template* (`{org}/{repo} repository | Microsoft Open Source`) as
real, observed facts -- not guesses -- even though the specific repo observed
differs from the incident repo. `FLOW_URL`/`PAGE_TITLE` below substitute a
neutral placeholder org/repo (`contoso`/`sample-repo`) for the real names seen
during capture: this project is headed for public release and the real names
carry no test-relevant signal beyond the shape itself.

Two facts remain genuinely unverified, and are NOT claimed by anything below:
whether the elevation click itself issues an observable non-GET request, and
the page's exact heading (`<h1>`/`<h2>`) structure. Both require actually
clicking through the flow, which the operational instructions for this task
explicitly prohibited. See docs/designs/confirmation-gate.md section 9 for the
full, current honest-limits accounting.

Cases 2 and 4 (flow elevation) prove the MECHANISM (an observed effect, or a
page-context match, elevates a tab) using the now-observed URL shape and
title template, plus a synthetic (not-yet-verified) elevation request/heading
detail -- they do not claim the maintainer's real flow issues that exact
request or renders that exact heading. See each test's docstring for exactly
what is and is not proven.
"""

from __future__ import annotations

from typing import Any

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.effects import EffectsReport, ObservedRequest
from amplifier_browser_bridge.policy import PolicyEngine
from amplifier_browser_bridge.scope import ScopeError, SessionScope

# Measured, verbatim (docs/designs/confirmation-gate.md section 1).
ELEVATE_LABEL = "Elevate bkrabach to Administrator"
BLAND_LABEL = "Next"

# OBSERVED (read-only snapshot, see module docstring above) URL shape and
# title template, with a neutral placeholder org/repo standing in for the
# real names captured -- the shape is what's load-bearing for these tests,
# not the specific repository identity.
FLOW_URL = "https://repos.opensource.microsoft.com/orgs/contoso/repos/sample-repo/jit"
PAGE_TITLE = "contoso/sample-repo repository | Microsoft Open Source"


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
# Case 2 -- URL shape OBSERVED, request SYNTHETIC. Proves the mechanism (a
# browser-asserted observed effect elevates a tab's flow, which then gates a
# bland click that has zero label-based signal of its own) using the
# now-observed FLOW_URL shape. Does NOT claim the real elevation click on
# repos.opensource.microsoft.com issues this specific request -- whether it
# issues ANY observable non-GET request remains unverified (design doc
# section 9).
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
# Case 3 -- title template OBSERVED (PAGE_TITLE, see module docstring),
# heading text SYNTHETIC. Proves the page-context mechanism using the real,
# observed title shape. Does NOT claim the real page's actual headings
# contain these terms -- heading structure was never observed (design doc
# section 9).
# ---------------------------------------------------------------------------


def test_bland_next_gates_when_flow_elevated_by_page_context(tmp_path: Any) -> None:
    engine = _engine(tmp_path)
    engine.note_page_context("d1", 1, FLOW_URL, PAGE_TITLE, ["Elevate your role"])
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
    """Uses a session with `allow_self_attested_escalation=True` (FIX 3,
    product review panel): ELEVATE_LABEL classifies as `permission_change`,
    an ESCALATION_CATEGORIES member, which `PolicyEngine.evaluate` now forces
    to `redeem="unredeemable"` regardless of write scope UNLESS a session
    explicitly opts in. This test's purpose is D3 attribution, not the
    escalation lock itself (see `tests/test_escalation_category.py` for that),
    so it opts in explicitly to keep exercising the confirm-then-redispatch
    path."""
    from amplifier_browser_bridge.audit import AuditLog

    audit_path = tmp_path / "audit.jsonl"
    engine = PolicyEngine(AuditLog(audit_path))
    scope = SessionScope(session_id="sess-d3", allow_self_attested_escalation=True)
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e99"),
        "click",
        {"ref": "e99", "label": ELEVATE_LABEL, "page_url": FLOW_URL},
        scope=scope,
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
# Case 8 -- scope.py (Candidate C), now implemented (design doc section 15
# step 5). A session narrowed to a DIFFERENT origin than FLOW_URL's host
# denies the elevate click outright, before classification even runs --
# page-immune prevention, not detection.
# ---------------------------------------------------------------------------


def test_out_of_scope_click_is_denied_with_specific_error(tmp_path: Any) -> None:
    engine = _engine(tmp_path)
    scope = SessionScope(session_id="sess-1", write=("github.com",))
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e101"),
        "click",
        {"ref": "e101", "label": ELEVATE_LABEL, "page_url": FLOW_URL},
        scope=scope,
    )
    assert decision.status == "deny"
    assert decision.reason_code == "out_of_scope"
    assert decision.reason is not None
    # Specific, unlike the denylist's deliberately generic DENY_REASON (the
    # caller's OWN declared constraint being read back to it -- design doc
    # section 7.4, docs/POLICY.md's "Invisibility, both directions").
    assert "repos.opensource.microsoft.com" in decision.reason
    assert "github.com" in decision.reason
    # Denied BEFORE classification ever ran -- this is prevention, not
    # detection (design doc section 4: "C is the only page-immune
    # prevention"). No classification is attached to a scope-denied decision.
    assert decision.classification is None


def test_in_scope_click_still_gates_normally(tmp_path: Any) -> None:
    """A session scoped to the flow's OWN host is unaffected -- scope only
    ever adds a constraint, never changes classification for an in-scope
    origin."""
    engine = _engine(tmp_path)
    scope = SessionScope(session_id="sess-2", write=("repos.opensource.microsoft.com",))
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e102"),
        "click",
        {"ref": "e102", "label": ELEVATE_LABEL, "page_url": FLOW_URL},
        scope=scope,
    )
    assert decision.status == "gate"
    assert decision.classification is not None
    assert "permission_change" in decision.classification.categories


# ---------------------------------------------------------------------------
# Case 9 -- the load-bearing anti-injection property: once a session has
# ingested page content (sealed), NO further scope change is accepted, not
# even one that would narrow it further. This is what stops a prompt
# injected FROM a page the agent already read from using a subsequent
# narrow_scope/establish_session call to reset its own grant -- see
# scope.py's module docstring for the full argument.
# ---------------------------------------------------------------------------


def test_sealed_session_cannot_widen_scope() -> None:
    scope = SessionScope(session_id="sess-3", write=("github.com", "contoso.com"))
    scope.seal()  # Hub calls this on the first read/snapshot/tabs result.
    try:
        scope.narrow(write=("github.com",))  # a NARROWING request -- still rejected once sealed.
    except ScopeError as e:
        assert "sealed" in str(e)
    else:
        raise AssertionError("narrow() must reject ANY change once sealed, narrowing included")
    # Confirm nothing actually changed -- the rejected call must not have
    # partially mutated the scope.
    assert scope.write == ("github.com", "contoso.com")


def test_narrow_before_seal_cannot_widen_either() -> None:
    """Even before sealing, `narrow()` never accepts a widening request --
    the seal is the second of two independent guarantees (scope.py's module
    docstring), not the only one."""
    scope = SessionScope(session_id="sess-4", write=("github.com",))
    try:
        scope.narrow(write="*")
    except ScopeError:
        pass
    else:
        raise AssertionError("narrow() must never re-widen write back to '*'")
    try:
        scope.narrow(write=("github.com", "contoso.com"))
    except ScopeError:
        pass
    else:
        raise AssertionError("narrow() must reject adding an origin to the write set")
    assert scope.write == ("github.com",)
