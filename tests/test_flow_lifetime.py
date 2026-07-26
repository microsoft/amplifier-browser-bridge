"""Regression tests for review panel finding F5 (tester-breaker), a real
bypass: "Get a human to approve one low-stakes action early in a long-lived
desktop session (measured: 142 min, zero gaps). If nothing defines when a
'flow' ends, that single confirmation is live for the rest of the session. At
minute 140, inject the actual dangerous action into the same still-open
flow. No new gate fires -- it's 'the same flow.'"

Before this fix, `FLOW_TTL_SECONDS` was a purely IDLE-gap bound: it reset on
every triggering observation (`note_effects`/`note_page_context`). A flow
that keeps getting touched by ordinary, continuing activity -- exactly the
shape of a multi-step enterprise wizard, which is also the measured
incident's own URL shape (`.../jit`, same-origin across every step, so
origin-change never clears it either) -- never actually timed out, no matter
how long the episode had been running.

The fix adds `FLOW_MAX_LIFETIME_SECONDS`: an ABSOLUTE cap measured from when
the flow episode STARTED (`started_at`), independent of how recently it was
last touched (`at`). These tests prove: (1) continuous refresh keeps a flow
alive past the idle TTL, as before: (2) the absolute cap still ends the
episode regardless: (3) hub-clock timestamps, not page-authored ones, decide
this, so a page cannot manufacture more elapsed time than has genuinely
passed.
"""

from __future__ import annotations

from typing import Any

import amplifier_browser_bridge.policy as policy_module
from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.effects import EffectsReport, ObservedRequest
from amplifier_browser_bridge.policy import FLOW_MAX_LIFETIME_SECONDS, FLOW_TTL_SECONDS, PolicyEngine

BLAND_LABEL = "Next"
FLOW_URL = "https://repos.opensource.microsoft.com/orgs/contoso/repos/sample-repo/jit"


def _engine(tmp_path: Any) -> PolicyEngine:
    return PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))


def _effect() -> EffectsReport:
    return EffectsReport(
        tier="webrequest",
        window_ms=1500,
        attribution="time_window",
        requests=(ObservedRequest(method="POST", url=f"{FLOW_URL}/step"),),
    )


class _FakeClock:
    """Deterministic replacement for `time.time()`, installed via monkeypatch
    on the shared `time` module object `policy.py` imports -- lets a test
    advance "wall clock" time in exact increments without a real sleep."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_flow_survives_continuous_refresh_up_to_absolute_cap(tmp_path: Any, monkeypatch: Any) -> None:
    """Sanity check: repeated triggering well inside both bounds keeps the
    flow elevated, same as before this fix -- the idle-gap behavior is
    unchanged."""
    clock = _FakeClock()
    monkeypatch.setattr(policy_module.time, "time", clock)
    engine = _engine(tmp_path)

    engine.note_effects("d1", 1, _effect(), FLOW_URL)
    for _ in range(5):
        clock.advance(FLOW_TTL_SECONDS - 100)  # always well under the idle TTL
        engine.note_effects("d1", 1, _effect(), FLOW_URL)

    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e1"),
        "click",
        {"ref": "e1", "label": BLAND_LABEL, "page_url": FLOW_URL},
    )
    assert decision.status == "gate"
    assert decision.confirm_scope == "flow"


def test_flow_expires_at_absolute_cap_despite_continuous_refresh(tmp_path: Any, monkeypatch: Any) -> None:
    """THE regression test for F5's exact exploit shape: a flow entered at
    minute 0, kept alive by ordinary, continuing activity every 10 minutes
    (every individual gap well under the 15-minute idle TTL, so the OLD
    idle-only mechanism would say "elevated" FOREVER, no matter how long the
    episode has run), must still end once FLOW_MAX_LIFETIME_SECONDS has
    elapsed since it STARTED -- proving continuous benign activity cannot
    hold a single early approval open indefinitely. The dangerous action
    itself (evaluated here) does NOT touch the flow -- exactly the "minute
    140: inject the actual dangerous action" moment, a plain evaluation with
    no accompanying triggering observation."""
    clock = _FakeClock()
    monkeypatch.setattr(policy_module.time, "time", clock)
    engine = _engine(tmp_path)

    # Minute 0: flow starts (e.g. the human's one low-stakes approval).
    engine.note_effects("d1", 1, _effect(), FLOW_URL)
    # Two more touches, 10 minutes apart -- each gap is WAY under the 15-min
    # idle TTL, so the pre-fix mechanism would never lapse this on its own.
    clock.advance(600.0)  # t=600
    engine.note_effects("d1", 1, _effect(), FLOW_URL)
    clock.advance(600.0)  # t=1200
    engine.note_effects("d1", 1, _effect(), FLOW_URL)

    # No further touch. Advance straight past the ABSOLUTE cap (1800s since
    # started_at=0) while still well under 900s since the last touch (1200)
    # -- i.e. the idle bound alone would NOT consider this expired.
    clock.advance(FLOW_MAX_LIFETIME_SECONDS - 1200 + 1)  # t = 1800 + 1 = 1801

    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e2"),
        "click",
        {"ref": "e2", "label": BLAND_LABEL, "page_url": FLOW_URL},
    )
    assert decision.status == "allow", (
        "SECURITY REGRESSION: a flow kept alive by continuous refresh past its "
        "absolute lifetime cap is STILL being honored -- F5's bypass is open."
    )
    assert decision.classification is not None
    flow_signals = [s for s in decision.classification.signals if s.channel == "flow"]
    assert flow_signals == []

    audit_text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "max_lifetime" in audit_text


def test_flow_restarts_fresh_episode_after_absolute_cap_expiry(tmp_path: Any, monkeypatch: Any) -> None:
    """A flow that is STILL actually consequential after the absolute cap
    re-enters elevation immediately on the very next trigger -- the cap ends
    one EPISODE, it doesn't disable flow elevation for the tab forever."""
    clock = _FakeClock()
    monkeypatch.setattr(policy_module.time, "time", clock)
    engine = _engine(tmp_path)

    engine.note_effects("d1", 1, _effect(), FLOW_URL)
    clock.advance(FLOW_MAX_LIFETIME_SECONDS + 1)  # past the absolute cap, single jump

    # Confirm the OLD episode is indeed gone.
    lapsed = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e3"),
        "click",
        {"ref": "e3", "label": BLAND_LABEL, "page_url": FLOW_URL},
    )
    assert lapsed.status == "allow"

    # A NEW triggering observation starts a fresh episode with its own new
    # started_at -- flow elevation is not permanently disabled for the tab.
    engine.note_effects("d1", 1, _effect(), FLOW_URL)
    fresh = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e4"),
        "click",
        {"ref": "e4", "label": BLAND_LABEL, "page_url": FLOW_URL},
    )
    assert fresh.status == "gate"
    assert fresh.confirm_scope == "flow"


def test_touch_flow_preserves_started_at_for_a_still_live_episode(tmp_path: Any, monkeypatch: Any) -> None:
    """The absolute cap is measured against `time.time()` (the hub's own
    clock), never anything derived from page-supplied data (label, url,
    title, headings). Directly asserts the mechanism: a page-context
    observation carrying attacker-controlled, alarming text ("Elevate to
    Administrator right now") touches the SAME still-live episode and
    refreshes `at` (the idle clock) but must NOT push `started_at` (the
    absolute-lifetime clock) forward -- a page cannot manufacture extra
    elapsed time for its own episode just by supplying more alarming text."""
    clock = _FakeClock()
    monkeypatch.setattr(policy_module.time, "time", clock)
    engine = _engine(tmp_path)

    engine.note_effects("d1", 1, _effect(), FLOW_URL)
    original_started_at = engine._flow_elevated[("d1", 1)]["started_at"]
    assert original_started_at == clock.now

    clock.advance(500.0)  # well within both the idle TTL and the absolute cap
    engine.note_page_context(
        "d1", 1, FLOW_URL, "Elevate to Administrator right now, definitely not expired", ["Grant access"]
    )
    touched = engine._flow_elevated[("d1", 1)]
    assert touched["at"] == clock.now  # idle clock DID refresh
    assert touched["started_at"] == original_started_at  # absolute clock did NOT
    assert touched["by"] == "page_context"  # the touch itself still registered
