"""The policy engine: denylist matching, tab invisibility (request + response
path), confirmation gates, the choke-point guarantee, and the kill switch.

Two layers of tests here, deliberately:

1. **Unit tests** against `PolicyEngine`/`Denylist`/`GateRule` directly -- fast,
   precise, no hub/websocket machinery involved.
2. **Hub-integration tests** using the same `FakeDeviceSocket` pattern as
   test_hub.py -- these are the actual proof that the choke point holds and that
   response-path filtering happens where the wire protocol really flows through
   (`Hub._handle_device_ws`'s `result` handling), not just in the policy engine
   in isolation.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.hub import Hub
from amplifier_browser_bridge.policy import (
    DEFAULT_CONFIRMATION_TTL_SECONDS,
    Denylist,
    PolicyEngine,
    PolicyError,
    host_matches_domain,
    host_of,
)

# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------


class FakeDeviceSocket:
    """Stands in for a live device connection, playing the extension's role.

    Unlike test_hub.py's simpler fake (which resolves the pending future
    directly), this one routes the canned result through `Hub._ingest_result`
    first -- exactly what `Hub._handle_device_ws`'s `result` branch does for a
    real device message. That is the ONE place tabs-filtering and tab-host-cache
    updates happen in production (see hub.py), so a test double that skipped it
    would not actually be proving anything about response-path filtering.
    """

    def __init__(self, hub: Hub, record: Any, canned_result: dict[str, Any] | None = None) -> None:
        self.hub = hub
        self.record = record
        self.sent: list[dict[str, Any]] = []
        self.canned_result = canned_result or {"ok": True, "result": {"stub": True}}
        # command_id -> override result, for tests that need different results
        # for different in-flight commands (e.g. two different `tabs` calls).
        self.overrides: dict[str, dict[str, Any]] = {}

    async def send_json(self, data: dict[str, Any], /) -> None:
        self.sent.append(data)
        fut = self.record.pending.get(data["id"])
        if fut is not None and not fut.done():
            result = self.overrides.get(data["id"], self.canned_result)
            raw_env = {**result, "id": data["id"], "device_id": self.record.device_id}
            env = self.hub._ingest_result(self.record.device_id, data["id"], raw_env)
            fut.set_result(env)


def _hub(tmp_path: Any) -> Hub:
    return Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"))


def _live_device(hub: Hub, device_id: str = "d1") -> tuple[Any, FakeDeviceSocket]:
    record = hub.registry.get_or_create(device_id)
    fake_ws = FakeDeviceSocket(hub, record)
    record.ws = fake_ws
    record.touch()
    return record, fake_ws


# ---------------------------------------------------------------------------
# Denylist matching -- host/domain, subdomain handling
# ---------------------------------------------------------------------------


def test_exact_domain_match() -> None:
    assert host_matches_domain("chase.com", "chase.com") is True


def test_subdomain_matches() -> None:
    assert host_matches_domain("sub.chase.com", "chase.com") is True
    assert host_matches_domain("www.chase.com", "chase.com") is True
    assert host_matches_domain("a.b.chase.com", "chase.com") is True


def test_similar_but_different_domain_does_not_match() -> None:
    """The substring-matching bug this suffix-with-dot-boundary check avoids:
    'chase.com' must not match as a naive substring of 'notchase.com'."""
    assert host_matches_domain("notchase.com", "chase.com") is False
    assert host_matches_domain("xchase.com", "chase.com") is False


def test_domain_as_suffix_of_unrelated_host_does_not_match() -> None:
    """'chase.com.evil.com' ends in '.evil.com', not '.chase.com' -- suffix
    matching must anchor from the right, not just check substring containment."""
    assert host_matches_domain("chase.com.evil.com", "chase.com") is False


def test_case_insensitive() -> None:
    assert host_matches_domain("Chase.COM", "chase.com") is True


def test_host_of_strips_scheme_path_port_and_lowercases() -> None:
    assert host_of("https://Accounts.Google.com:443/o/oauth2/auth?x=1") == "accounts.google.com"
    assert host_of(None) is None
    assert host_of("not a url \x00") is None or isinstance(host_of("not a url \x00"), (str, type(None)))


def test_denylist_default_categories_present() -> None:
    denylist = Denylist()
    assert "financial" in denylist.categories
    assert "healthcare" in denylist.categories
    assert "auth" in denylist.categories
    assert "password_managers" in denylist.categories


def test_denylist_match_returns_category_and_domain() -> None:
    denylist = Denylist()
    hit = denylist.match("secure.chase.com")
    assert hit is not None
    category, domain = hit
    assert category == "financial"
    assert domain == "chase.com"


def test_denylist_no_match_for_ordinary_site() -> None:
    denylist = Denylist()
    assert denylist.match("example.com") is None
    assert denylist.match(None) is None


def test_auth_denylist_does_not_leak_to_sibling_google_subdomains() -> None:
    """Bug 2 requirement 2, explicit regression: `accounts.google.com` is
    denylisted; `docs.google.com` and `mail.google.com` are ordinary content
    hosts on the SAME parent domain (google.com) and must stay visible unless
    separately, deliberately listed. Verifies host_matches_domain's
    suffix-with-dot-boundary semantics specifically for this pair, since it's
    the exact scenario named in the task."""
    denylist = Denylist()
    assert denylist.match("accounts.google.com") is not None
    assert denylist.match("docs.google.com") is None
    assert denylist.match("mail.google.com") is None
    # A genuine subdomain of the denylisted host itself must still match.
    assert denylist.match("sub.accounts.google.com") is not None


def test_denylist_load_from_file_replaces_defaults(tmp_path: Any) -> None:
    import json

    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        json.dumps({"denylist": {"custom": ["internal-hr.example.com"]}}), encoding="utf-8"
    )
    denylist = Denylist.load(policy_file)
    assert denylist.categories == {"custom": ["internal-hr.example.com"]}
    assert denylist.match("internal-hr.example.com") is not None
    # Replace semantics, not merge -- the default 'financial' category is gone.
    assert denylist.match("chase.com") is None


def test_denylist_load_missing_file_falls_back_to_defaults(tmp_path: Any) -> None:
    denylist = Denylist.load(tmp_path / "does-not-exist.json")
    assert denylist.match("chase.com") is not None


# ---------------------------------------------------------------------------
# Request-path invisibility: PolicyEngine.evaluate denies a targeted denied tab
# ---------------------------------------------------------------------------


def test_evaluate_denies_navigate_to_denylisted_host(tmp_path: Any) -> None:
    engine = PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=7), "navigate", {"url": "https://www.chase.com/login"}
    )
    assert decision.status == "deny"
    # Agent-facing reason is generic -- must not name the category or domain,
    # or the "invisible" guarantee is defeated via the error text itself.
    assert "chase" not in (decision.reason or "").lower()
    assert "financial" not in (decision.reason or "").lower()


def test_evaluate_denies_command_targeting_a_tab_with_cached_denied_host(tmp_path: Any) -> None:
    """The structural capability-binding case: the hub already observed (via a
    prior tabs/navigate/snapshot/read result) that tab 7 is on a denied host.
    A subsequent command naming tab 7 -- with NO url in its own args, i.e.
    exactly what a `click`/`type`/`read` command looks like on the wire -- must
    still be denied, because the hub checks its own recorded truth, not
    anything the caller asserts."""
    engine = PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))
    engine.note_tab_url("d1", 7, "https://accounts.google.com/signin/v2")
    decision = engine.evaluate(Target(device_id="d1", tab_id=7, ref="e3"), "click", {"ref": "e3"})
    assert decision.status == "deny"


def test_evaluate_allows_unobserved_tab_absent_denylist_signal(tmp_path: Any) -> None:
    """Documented, honest limitation (see policy.py module docstring): a tab_id
    the hub has never observed a URL for cannot be checked against the
    denylist. This is not a silent bypass of a *known* denial -- it is the
    inherent limit of a host-based denylist with no a-priori tab knowledge."""
    engine = PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))
    decision = engine.evaluate(Target(device_id="d1", tab_id=999), "read", {})
    assert decision.status == "allow"


def test_evaluate_allows_ordinary_site() -> None:
    import tempfile

    from amplifier_browser_bridge.audit import AuditLog as _AL

    with tempfile.TemporaryDirectory() as d:
        engine = PolicyEngine(_AL(f"{d}/audit.jsonl"))
        decision = engine.evaluate(
            Target(device_id="d1", tab_id=1), "navigate", {"url": "https://example.com"}
        )
        assert decision.status == "allow"


# ---------------------------------------------------------------------------
# Response-path invisibility: a `tabs` result is filtered before the agent sees it
# ---------------------------------------------------------------------------


def test_filter_tabs_result_removes_denylisted_entries(tmp_path: Any) -> None:
    engine = PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))
    tabs = [
        {"tab_id": 1, "window_id": 1, "url": "https://example.com/", "title": "Example"},
        {"tab_id": 2, "window_id": 1, "url": "https://www.chase.com/dashboard", "title": "Chase"},
        {"tab_id": 3, "window_id": 1, "url": "https://mail.google.com/mail/u/0/", "title": "Mail"},
    ]
    visible = engine.filter_tabs_result("d1", tabs)
    visible_ids = {t["tab_id"] for t in visible}
    assert visible_ids == {1, 3}
    assert 2 not in visible_ids


def test_filter_tabs_result_still_records_host_for_denied_tab(tmp_path: Any) -> None:
    """Denied tabs are hidden from the agent but the hub must still remember
    their host -- otherwise a later command addressing that tab_id directly
    (without ever seeing it in a `tabs` listing) would sail through unblocked."""
    engine = PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))
    tabs = [{"tab_id": 2, "window_id": 1, "url": "https://www.chase.com/dashboard", "title": "Chase"}]
    engine.filter_tabs_result("d1", tabs)
    decision = engine.evaluate(Target(device_id="d1", tab_id=2), "read", {})
    assert decision.status == "deny"


def test_hub_tabs_result_filtered_before_reaching_agent(tmp_path: Any) -> None:
    """The real proof: drive an actual Hub (no browser, no real socket) with a
    FakeDeviceSocket standing in for the device connection, and confirm that
    the envelope the AGENT receives back from `send_command("tabs")` already
    has the denylisted entry removed -- this exercises `Hub._ingest_result`,
    the exact code path a real device `result` message flows through."""
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    fake_ws.canned_result = {
        "ok": True,
        "result": [
            {"tab_id": 10, "window_id": 1, "url": "https://example.com/", "title": "Example"},
            {"tab_id": 11, "window_id": 1, "url": "https://mychart.com/portal", "title": "MyChart"},
        ],
    }

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1"), "tabs", {})

    result = asyncio.run(run())
    assert result["ok"] is True
    returned_ids = {t["tab_id"] for t in result["result"]}
    assert returned_ids == {10}
    assert 11 not in returned_ids


def test_filter_tabs_result_records_matched_domain_and_host_in_audit(tmp_path: Any) -> None:
    """Bug 2 requirement 3: the audit event for a hidden tab must record the
    matched rule (category + domain) and the actual host -- previously this
    event recorded `category` only, which is why the audit log could not
    explain what was hidden or why."""
    audit_path = tmp_path / "audit.jsonl"
    engine = PolicyEngine(AuditLog(audit_path))
    tabs = [{"tab_id": 2, "window_id": 1, "url": "https://secure.chase.com/dashboard", "title": "Chase"}]
    engine.filter_tabs_result("d1", tabs)

    lines = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    hidden = [rec for rec in lines if rec["event"] == "policy_tab_hidden"]
    assert len(hidden) == 1
    assert hidden[0]["category"] == "financial"
    assert hidden[0]["matched_domain"] == "chase.com"
    assert hidden[0]["host"] == "secure.chase.com"


# ---------------------------------------------------------------------------
# Bug 2 case study: discarded-tab exception for the `auth` category only
# ---------------------------------------------------------------------------
# A discarded tab has no live renderer -- see policy.py's `_tab_discarded`
# docstring for the full investigation (a real 531-tab profile had 49 tabs
# stuck displaying a login.microsoftonline.com OAuth-authorize URL, frozen
# mid-redirect while backgrounded/discarded; none were interactive login
# screens). The exception is narrow: `auth` category only, and only while the
# tab is known-discarded.


def test_filter_tabs_result_shows_discarded_auth_tab_instead_of_hiding(tmp_path: Any) -> None:
    audit_path = tmp_path / "audit.jsonl"
    engine = PolicyEngine(AuditLog(audit_path))
    tabs = [
        {
            "tab_id": 5,
            "window_id": 1,
            "url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=x",
            "title": "Sign in",
            "discarded": True,
        }
    ]
    visible = engine.filter_tabs_result("d1", tabs)
    assert {t["tab_id"] for t in visible} == {5}

    lines = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    shown_events = [rec for rec in lines if rec["event"] == "policy_tab_shown_despite_match"]
    assert len(shown_events) == 1
    assert shown_events[0]["category"] == "auth"
    assert shown_events[0]["reason"] == "discarded_auth_tab"
    # And it must NOT also appear as hidden.
    assert not [rec for rec in lines if rec["event"] == "policy_tab_hidden"]


def test_filter_tabs_result_still_hides_live_auth_tab(tmp_path: Any) -> None:
    """The exception must not weaken protection for a tab that IS live (not
    discarded) and genuinely on an identity-provider host -- e.g. the user
    actively looking at a real login form right now."""
    engine = PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))
    tabs = [
        {
            "tab_id": 6,
            "window_id": 1,
            "url": "https://accounts.google.com/signin/v2",
            "title": "Sign in",
            "discarded": False,
        }
    ]
    visible = engine.filter_tabs_result("d1", tabs)
    assert visible == []


def test_filter_tabs_result_does_not_extend_exception_to_financial_category(tmp_path: Any) -> None:
    """The discarded-tab exception is scoped to `auth` only -- a discarded
    financial-category tab must stay hidden. financial/healthcare/
    password_managers protect against revealing WHICH services the user
    uses at all, a concern that holds regardless of render state."""
    engine = PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))
    tabs = [
        {
            "tab_id": 7,
            "window_id": 1,
            "url": "https://www.chase.com/dashboard",
            "title": "Chase",
            "discarded": True,
        }
    ]
    visible = engine.filter_tabs_result("d1", tabs)
    assert visible == []


def test_evaluate_allows_command_targeting_discarded_auth_tab(tmp_path: Any) -> None:
    """Request-path symmetry: once a discarded auth-category tab has been
    observed (e.g. via a prior `tabs` call), a subsequent command explicitly
    targeting it is allowed through to dispatch -- NOT because the content is
    now considered safe, but because Bug 1's own discarded-tab check at the
    extension layer will still refuse to act on it without an explicit
    `wake=true`. The hub must not double-block ahead of that."""
    audit_path = tmp_path / "audit.jsonl"
    engine = PolicyEngine(AuditLog(audit_path))
    engine.filter_tabs_result(
        "d1",
        [
            {
                "tab_id": 9,
                "url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
                "discarded": True,
            }
        ],
    )
    decision = engine.evaluate(Target(device_id="d1", tab_id=9), "read", {"wake": True})
    assert decision.status == "allow"

    lines = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    allowed_events = [rec for rec in lines if rec["event"] == "policy_allowed_despite_match"]
    assert len(allowed_events) == 1
    assert allowed_events[0]["category"] == "auth"


def test_evaluate_still_denies_command_targeting_live_auth_tab(tmp_path: Any) -> None:
    """Symmetric negative case: a NON-discarded auth-category tab is still
    denied at the request path -- the exception never applies to a live tab."""
    engine = PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))
    engine.filter_tabs_result(
        "d1", [{"tab_id": 10, "url": "https://accounts.google.com/signin/v2", "discarded": False}]
    )
    decision = engine.evaluate(Target(device_id="d1", tab_id=10), "read", {})
    assert decision.status == "deny"


def test_hub_command_targeting_denied_tab_is_rejected(tmp_path: Any) -> None:
    """Full round trip: a `tabs` call observes a denylisted tab (and hides it),
    then a subsequent command explicitly targeting that same tab_id -- as a
    prompt-injected model might try, having somehow guessed or been told the
    tab_id despite never seeing it in a `tabs` listing -- is rejected before
    ever reaching the device."""
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    fake_ws.canned_result = {
        "ok": True,
        "result": [{"tab_id": 11, "window_id": 1, "url": "https://mychart.com/portal", "title": "MyChart"}],
    }

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        tabs_result = await hub.send_command(Target(device_id="d1"), "tabs", {})
        blocked = await hub.send_command(Target(device_id="d1", tab_id=11, ref="e1"), "click", {"ref": "e1"})
        return tabs_result, blocked

    tabs_result, blocked = asyncio.run(run())
    assert tabs_result["result"] == []  # the one tab was denylisted -- invisible
    assert blocked["ok"] is False
    # The device must never have received the click -- only the `tabs` command
    # should be in `fake_ws.sent`.
    assert [env["command"] for env in fake_ws.sent] == ["tabs"]


# ---------------------------------------------------------------------------
# Gate categories -- each canonical category fires given its documented signal
# ---------------------------------------------------------------------------


def _engine(tmp_path: Any) -> PolicyEngine:
    return PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))


def test_gate_purchase_via_label(tmp_path: Any) -> None:
    engine = _engine(tmp_path)
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e1"), "click", {"ref": "e1", "label": "Place Order"}
    )
    assert decision.status == "gate"
    assert decision.category == "purchase"
    assert decision.token


def test_purchase_checkout_url_alone_is_clear_not_gate_under_scoring(tmp_path: Any) -> None:
    """Post-confirmation-gate rewrite: `navigate` has no label channel, so a
    checkout URL match (weight 2, docs/designs/confirmation-gate.md section
    11.1's scoring table) alone no longer reaches threshold (3) on its own --
    this is the documented, intentional consequence of moving away from
    "any single channel gates" (the same conjunction-removal principle that
    motivated this whole redesign, applied honestly in the other direction
    too: a single weak signal must not gate just because it used to).
    Corroborated by a second signal (a form_cross_origin hint, or flow
    elevation), it still gates -- see the next test."""
    engine = _engine(tmp_path)
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1), "navigate", {"url": "https://shop.example.com/cart/checkout"}
    )
    assert decision.status == "allow"
    assert decision.classification is not None
    assert decision.classification.status == "clear"
    assert "purchase" in decision.classification.categories


def test_purchase_checkout_url_plus_flow_elevation_gates(tmp_path: Any) -> None:
    """Preserves the original protective intent by a different mechanism
    (docs/designs/confirmation-gate.md section 8's migration pattern, applied
    here too): a checkout-URL navigate inside a tab already flow-elevated by
    an observed effect reaches threshold (2 + 3 = 5) and gates."""
    from amplifier_browser_bridge.effects import EffectsReport, ObservedRequest

    engine = _engine(tmp_path)
    engine.note_effects(
        "d1",
        1,
        EffectsReport(
            tier="webrequest",
            window_ms=1500,
            attribution="time_window",
            requests=(ObservedRequest(method="POST", url="https://shop.example.com/cart/add"),),
        ),
        "https://shop.example.com/cart",
    )
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1), "navigate", {"url": "https://shop.example.com/cart/checkout"}
    )
    assert decision.status == "gate"
    assert decision.category == "purchase"


def test_gate_send(tmp_path: Any) -> None:
    engine = _engine(tmp_path)
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e2"), "click", {"ref": "e2", "label": "Send"}
    )
    assert decision.status == "gate"
    assert decision.category == "send"


def test_gate_delete(tmp_path: Any) -> None:
    engine = _engine(tmp_path)
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e3"), "click", {"ref": "e3", "label": "Delete account data"}
    )
    assert decision.status == "gate"
    assert decision.category == "delete"


def test_lone_allow_label_does_not_gate(tmp_path: Any) -> None:
    """Replacement for test_gate_oauth_grant_requires_both_label_and_url
    (docs/designs/confirmation-gate.md section 8's migration table): the old
    test encoded `combine="all"` -- the very conjunction bug that let
    "Elevate bkrabach to Administrator" through, because it required BOTH
    signals unconditionally, no matter how strong either one was alone. The
    new mechanism preserves the same protective intent (a bare "Allow" --
    cookie banner, notification prompt -- must not gate) via scoring instead
    of a hardcoded conjunction: "allow" is a single weak `oauth_consent`
    family term (weight 1, below threshold), so it does not gate alone --
    but it is no longer STRUCTURALLY incapable of gating alone the way the
    old rule was; two family terms together, or one term plus flow
    elevation, reaches threshold with no URL at all (see
    test_lone_allow_label_does_not_gate's sibling assertions below and
    test_permission_change_weak_label_alone_does_not_gate's flow-elevation
    test for the general pattern)."""
    engine = _engine(tmp_path)
    # Label alone ("Allow") is one weak family term -- does not gate.
    label_only = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e4"), "click", {"ref": "e4", "label": "Allow"}
    )
    assert label_only.status == "allow"
    assert label_only.classification is not None
    assert label_only.classification.status == "clear"

    # "Allow" (weight 1) + an OAuth authorize URL (weight 2) reaches
    # threshold (3) -- same practical outcome as the old combine="all" rule,
    # reached by summing independent evidence rather than hardcoding a
    # two-signal requirement that the measured incident's own label
    # ("Elevate bkrabach to Administrator") could never satisfy.
    both_signals = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e4"),
        "click",
        {"ref": "e4", "label": "Allow", "page_url": "https://github.com/login/oauth/authorize?client_id=x"},
    )
    assert both_signals.status == "gate"
    assert both_signals.classification is not None
    assert "oauth_grant" in both_signals.classification.categories


def test_gate_file_upload_via_input_type_hint(tmp_path: Any) -> None:
    engine = _engine(tmp_path)
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e5"), "click", {"ref": "e5", "input_type": "file"}
    )
    assert decision.status == "gate"
    assert decision.category == "file_upload"


def test_signup_url_alone_is_clear_not_gate_under_scoring(tmp_path: Any) -> None:
    """See test_purchase_checkout_url_alone_is_clear_not_gate_under_scoring's
    docstring -- same scoring-table consequence for account_creation."""
    engine = _engine(tmp_path)
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1), "navigate", {"url": "https://app.example.com/signup"}
    )
    assert decision.status == "allow"
    assert decision.classification is not None
    assert "account_creation" in decision.classification.categories


def test_signup_url_plus_flow_elevation_gates(tmp_path: Any) -> None:
    from amplifier_browser_bridge.effects import EffectsReport, ObservedRequest

    engine = _engine(tmp_path)
    engine.note_effects(
        "d1",
        1,
        EffectsReport(
            tier="webrequest",
            window_ms=1500,
            attribution="time_window",
            requests=(ObservedRequest(method="POST", url="https://app.example.com/onboarding/start"),),
        ),
        "https://app.example.com/onboarding",
    )
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1), "navigate", {"url": "https://app.example.com/signup"}
    )
    assert decision.status == "gate"
    assert decision.category == "account_creation"


def test_permission_change_weak_label_alone_does_not_gate(tmp_path: Any) -> None:
    """Replacement for test_gate_permission_change_requires_both_signals
    (docs/designs/confirmation-gate.md section 8's migration table -- this is
    the exact rule that failed to catch "Elevate bkrabach to Administrator"
    because `combine="all"` required a URL match too, and the JIT elevation
    flow's URL never matched `/settings/permissions`). "Access" is a single
    weak `privilege`-family term (weight 1) -- below threshold alone,
    preserving the original protective intent (a lone weak word should not
    gate) via scoring rather than a hardcoded two-signal requirement."""
    engine = _engine(tmp_path)
    weak_label = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e6"), "click", {"ref": "e6", "label": "Access"}
    )
    assert weak_label.status == "allow"
    assert weak_label.classification is not None
    assert weak_label.classification.status == "clear"

    both = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e6"),
        "click",
        {"ref": "e6", "label": "Access", "page_url": "https://app.example.com/settings/permissions"},
    )
    assert both.status == "gate"
    assert both.classification is not None
    assert "permission_change" in both.classification.categories

    # And -- the actual bug fix -- the measured label alone, with NEITHER a
    # matching URL NOR any conjunction requirement, now gates on its own:
    # two `privilege`-family terms ("elevate", "administrator") co-occurring
    # scores 3, at threshold.
    measured = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e7"),
        "click",
        {"ref": "e7", "label": "Elevate bkrabach to Administrator"},
    )
    assert measured.status == "gate"
    assert measured.classification is not None
    assert "permission_change" in measured.classification.categories


def test_ordinary_click_with_no_signal_is_not_gated(tmp_path: Any) -> None:
    """Honest limitation, proven directly: a click with no label/url signal at
    all cannot be classified, so it passes through un-gated. This is the
    documented gap, not a bug -- see policy.py's 'Honest limits' section."""
    engine = _engine(tmp_path)
    decision = engine.evaluate(Target(device_id="d1", tab_id=1, ref="e9"), "click", {"ref": "e9"})
    assert decision.status == "allow"


def test_ordinary_verb_like_publish_is_not_over_gated_outside_click(tmp_path: Any) -> None:
    """Gate rules are scoped to specific commands -- 'read' is never gated no
    matter what label-shaped text might theoretically appear in its args."""
    engine = _engine(tmp_path)
    decision = engine.evaluate(Target(device_id="d1", tab_id=1), "read", {"label": "Delete"})
    assert decision.status == "allow"


# ---------------------------------------------------------------------------
# Confirmation tokens -- single-use + expiry
# ---------------------------------------------------------------------------


def test_confirmation_token_consumed_once(tmp_path: Any) -> None:
    engine = _engine(tmp_path)
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e1"), "click", {"ref": "e1", "label": "Delete"}
    )
    assert decision.token is not None
    pending = engine.consume_confirmation(decision.token)
    assert pending.command == "click"
    assert pending.category == "delete"

    try:
        engine.consume_confirmation(decision.token)
        raised = False
    except PolicyError:
        raised = True
    assert raised, "second consume of the same token must raise PolicyError"


def test_confirmation_token_unknown_raises() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        engine = PolicyEngine(AuditLog(f"{d}/audit.jsonl"))
        try:
            engine.consume_confirmation("not-a-real-token")
            raised = False
        except PolicyError:
            raised = True
        assert raised


def test_confirmation_token_expires(tmp_path: Any) -> None:
    engine = PolicyEngine(AuditLog(tmp_path / "audit.jsonl"), confirmation_ttl=0.05)
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e1"), "click", {"ref": "e1", "label": "Delete"}
    )
    assert decision.token is not None
    time.sleep(0.1)
    try:
        engine.consume_confirmation(decision.token)
        raised = False
    except PolicyError as e:
        raised = True
        assert "expired" in str(e).lower()
    assert raised


def test_default_confirmation_ttl_is_positive_and_bounded() -> None:
    # Sanity check on the shipped default -- long enough to be usable in a
    # human-in-the-loop turn, short enough not to be a permanent bypass.
    assert 0 < DEFAULT_CONFIRMATION_TTL_SECONDS <= 3600


# ---------------------------------------------------------------------------
# Full gate -> confirm -> dispatch round trip through Hub
# ---------------------------------------------------------------------------


def test_hub_gated_action_does_not_reach_device_until_confirmed(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)

    async def run() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        gated = await hub.send_command(
            Target(device_id="d1", tab_id=1, ref="e1"), "click", {"ref": "e1", "label": "Delete"}
        )
        # Not yet dispatched -- the device must have received nothing.
        pre_confirm_sent = list(fake_ws.sent)

        confirmed = await hub._handle_agent_confirm({"confirmation_token": gated["confirmation_token"]})
        return gated, confirmed, {"pre_confirm_sent": pre_confirm_sent}

    gated, confirmed, extra = asyncio.run(run())

    assert gated["status"] == "needs_confirmation"
    assert gated["category"] == "delete"
    assert extra["pre_confirm_sent"] == []  # nothing reached the device pre-confirmation

    assert confirmed["ok"] is True  # now dispatched and fulfilled by the fake device
    assert len(fake_ws.sent) == 1
    assert fake_ws.sent[0]["command"] == "click"


def test_hub_confirm_with_bad_token_is_rejected(tmp_path: Any) -> None:
    hub = _hub(tmp_path)

    async def run() -> dict[str, Any]:
        return await hub._handle_agent_confirm({"confirmation_token": "bogus"})

    result = asyncio.run(run())
    assert result["ok"] is False


def test_hub_confirm_does_not_bypass_denylist(tmp_path: Any) -> None:
    """A confirmation token only skips the GATE, never the denylist. If the
    target became denylisted between gating and confirming (edge case, but the
    principle matters): the confirm must still be refused."""
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        gated = await hub.send_command(
            Target(device_id="d1", tab_id=1, ref="e1"), "click", {"ref": "e1", "label": "Delete"}
        )
        # Simulate the hub having since observed this exact tab is on a denied host.
        hub.policy.note_tab_url("d1", 1, "https://www.chase.com/accounts")
        confirmed = await hub._handle_agent_confirm({"confirmation_token": gated["confirmation_token"]})
        return gated, confirmed

    gated, confirmed = asyncio.run(run())
    assert gated["status"] == "needs_confirmation"
    assert confirmed["ok"] is False
    assert fake_ws.sent == []  # never reached the device


# ---------------------------------------------------------------------------
# Choke-point guarantee: a command cannot dispatch without a policy decision
# ---------------------------------------------------------------------------


def test_choke_point_deny_prevents_any_device_traffic(tmp_path: Any) -> None:
    """If policy denies, the device must receive literally nothing -- no
    QueuedCommand is ever constructed for a denied target (see hub.py's
    send_command docstring: it is the only constructor call site, and it is
    guarded by the policy check above it)."""
    hub = _hub(tmp_path)
    record, fake_ws = _live_device(hub)
    hub.policy.note_tab_url("d1", 5, "https://www.chase.com/wire-transfer")

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=5, ref="e1"), "click", {"ref": "e1"})

    result = asyncio.run(run())
    assert result["ok"] is False
    assert fake_ws.sent == []
    # Also true of the queued (non-live) path: denial happens before enqueue.
    assert len(record.queue) == 0


def test_choke_point_gate_prevents_enqueue_on_offline_device(tmp_path: Any) -> None:
    """A gated command targeting an offline (queued-tier) device must not sit
    in the device queue pre-approved -- it must not be queued at all until
    confirmed. This proves the policy check happens before both the immediate
    dispatch branch AND the enqueue branch in send_command."""
    hub = _hub(tmp_path)
    hub.registry.get_or_create("d1")  # never bound -- offline/dormant

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1, ref="e1"), "click", {"ref": "e1", "label": "Delete"}
        )

    result = asyncio.run(run())
    assert result["status"] == "needs_confirmation"
    record = hub.registry.get("d1")
    assert record is not None
    assert len(record.queue) == 0  # nothing was queued -- the gate fired first


def test_choke_point_unaffected_commands_still_flow_normally(tmp_path: Any) -> None:
    """Regression guard: ordinary allowed commands must be completely
    unaffected by the policy layer's presence."""
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    fake_ws.canned_result = {"ok": True, "result": {"title": "Example"}}

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=7), "read", {})

    result = asyncio.run(run())
    assert result == {"ok": True, "result": {"title": "Example"}}


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def test_kill_switch_halts_new_dispatch(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    hub.engage_kill_switch()

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=1), "read", {})

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "kill switch" in result["error"].lower()
    assert fake_ws.sent == []


def test_kill_switch_rejects_already_queued_commands(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    hub.registry.get_or_create("d1")  # offline -- queued tier

    async def enqueue() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=1), "read", {})

    queued_result = asyncio.run(enqueue())
    assert queued_result["status"] == "queued"
    command_id = queued_result["command_id"]

    rejected_count = hub.engage_kill_switch()
    assert rejected_count == 1

    record = hub.registry.get("d1")
    assert record is not None
    assert len(record.queue) == 0  # drained
    stored = record.results[command_id]
    assert stored["ok"] is False
    assert "kill switch" in stored["error"].lower()


def test_kill_switch_disengage_restores_dispatch(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    hub.engage_kill_switch()
    hub.disengage_kill_switch()

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=1), "read", {})

    result = asyncio.run(run())
    assert result["ok"] is True
    assert len(fake_ws.sent) == 1
