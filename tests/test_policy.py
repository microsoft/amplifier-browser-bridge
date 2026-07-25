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
    from amplifier_browser_bridge.audit import AuditLog as _AL

    import tempfile

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
    record, fake_ws = _live_device(hub)
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


def test_hub_command_targeting_denied_tab_is_rejected(tmp_path: Any) -> None:
    """Full round trip: a `tabs` call observes a denylisted tab (and hides it),
    then a subsequent command explicitly targeting that same tab_id -- as a
    prompt-injected model might try, having somehow guessed or been told the
    tab_id despite never seeing it in a `tabs` listing -- is rejected before
    ever reaching the device."""
    hub = _hub(tmp_path)
    record, fake_ws = _live_device(hub)
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


def test_gate_purchase_via_checkout_url(tmp_path: Any) -> None:
    engine = _engine(tmp_path)
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


def test_gate_oauth_grant_requires_both_label_and_url(tmp_path: Any) -> None:
    engine = _engine(tmp_path)
    # Label alone ("Allow") is far too common a word (cookie banners, notification
    # prompts) -- must NOT gate without the URL signal too (combine="all").
    label_only = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e4"), "click", {"ref": "e4", "label": "Allow"}
    )
    assert label_only.status == "allow"

    both_signals = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e4"),
        "click",
        {"ref": "e4", "label": "Allow", "page_url": "https://github.com/login/oauth/authorize?client_id=x"},
    )
    assert both_signals.status == "gate"
    assert both_signals.category == "oauth_grant"


def test_gate_file_upload_via_input_type_hint(tmp_path: Any) -> None:
    engine = _engine(tmp_path)
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e5"), "click", {"ref": "e5", "input_type": "file"}
    )
    assert decision.status == "gate"
    assert decision.category == "file_upload"


def test_gate_account_creation_via_url(tmp_path: Any) -> None:
    engine = _engine(tmp_path)
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=1), "navigate", {"url": "https://app.example.com/signup"}
    )
    assert decision.status == "gate"
    assert decision.category == "account_creation"


def test_gate_permission_change_requires_both_signals(tmp_path: Any) -> None:
    engine = _engine(tmp_path)
    label_only = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e6"), "click", {"ref": "e6", "label": "Grant"}
    )
    assert label_only.status == "allow"

    both = engine.evaluate(
        Target(device_id="d1", tab_id=1, ref="e6"),
        "click",
        {"ref": "e6", "label": "Grant", "page_url": "https://app.example.com/settings/permissions"},
    )
    assert both.status == "gate"
    assert both.category == "permission_change"


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
    record, fake_ws = _live_device(hub)

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
    record, fake_ws = _live_device(hub)

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
    record, fake_ws = _live_device(hub)
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
    record, fake_ws = _live_device(hub)
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
    record, fake_ws = _live_device(hub)
    hub.engage_kill_switch()
    hub.disengage_kill_switch()

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=1), "read", {})

    result = asyncio.run(run())
    assert result["ok"] is True
    assert len(fake_ws.sent) == 1
