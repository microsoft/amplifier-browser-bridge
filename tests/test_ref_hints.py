"""Phase 4: label/input_type hints resolved from the hub's own remembered
`snapshot`/`wait_for` observations -- what makes click/type-based confirmation
gates fire BEFORE a command reaches the device, without requiring the caller
to supply `args["label"]` itself.

Two layers, deliberately (same pattern as test_policy.py):

1. **Unit tests** against `PolicyEngine.note_snapshot`/`note_ref`/
   `_resolve_ref_hint` directly.
2. **Hub-integration tests** using the same `FakeDeviceSocket` pattern as
   test_policy.py (routes through `Hub._ingest_result`, exactly what a real
   device `result` message does) -- these are the actual proof that a click
   gate fires end-to-end through the hub, not just via a synthetic hint
   supplied directly to `PolicyEngine.evaluate` in a unit test.
"""

from __future__ import annotations

import asyncio
from typing import Any

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.hub import Hub
from amplifier_browser_bridge.policy import PolicyEngine

# ---------------------------------------------------------------------------
# Shared test fixtures -- identical pattern to test_policy.py's FakeDeviceSocket
# ---------------------------------------------------------------------------


class FakeDeviceSocket:
    """Routes a canned result through `Hub._ingest_result` before resolving
    the pending future -- exactly what `Hub._handle_device_message`'s
    `result` branch does for a real device message. A fake that skipped this
    step would not actually prove anything about hub-side ref-hint wiring."""

    def __init__(self, hub: Hub, record: Any, canned_result: dict[str, Any] | None = None) -> None:
        self.hub = hub
        self.record = record
        self.sent: list[dict[str, Any]] = []
        self.canned_result = canned_result or {"ok": True, "result": {"stub": True}}
        self.overrides: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        pass

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
# Unit tests: PolicyEngine.note_snapshot / note_ref / _resolve_ref_hint
# ---------------------------------------------------------------------------


def test_note_snapshot_then_evaluate_resolves_label(tmp_path: Any) -> None:
    engine = PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))
    engine.note_snapshot(
        "d1",
        7,
        "https://example.com/",
        [{"ref": "e1", "name": "Delete account", "tag": "button"}],
    )
    decision = engine.evaluate(Target(device_id="d1", tab_id=7, ref="e1"), "click", {"ref": "e1"})
    assert decision.status == "gate"
    assert decision.category == "delete"


def test_explicit_label_wins_over_remembered_hint(tmp_path: Any) -> None:
    """Caller-supplied signal always takes precedence over the hub's own
    remembered hint -- the hint is a fallback, never an override."""
    engine = PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))
    engine.note_snapshot(
        "d1", 7, "https://example.com/", [{"ref": "e1", "name": "Delete account", "tag": "button"}]
    )
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=7, ref="e1"), "click", {"ref": "e1", "label": "just a normal thing"}
    )
    assert decision.status == "allow"


def test_note_ref_resolves_input_type_for_file_upload_gate(tmp_path: Any) -> None:
    engine = PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))
    engine.note_ref("d1", 3, "https://example.com/upload", "e5", label="Choose file", input_type="file")
    decision = engine.evaluate(Target(device_id="d1", tab_id=3, ref="e5"), "click", {"ref": "e5"})
    assert decision.status == "gate"
    assert decision.category == "file_upload"


def test_unknown_ref_has_no_hint_and_is_not_gated(tmp_path: Any) -> None:
    engine = PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))
    engine.note_snapshot("d1", 7, "https://example.com/", [{"ref": "e1", "name": "Delete", "tag": "button"}])
    decision = engine.evaluate(
        Target(device_id="d1", tab_id=7, ref="e_never_seen"), "click", {"ref": "e_never_seen"}
    )
    assert decision.status == "allow"


def test_stale_ref_after_navigation_is_not_trusted(tmp_path: Any) -> None:
    """The staleness guard: once the hub observes (via note_tab_url, fed by a
    navigate/read result) that a tab is now on a different URL than the one
    a ref's label was captured on, the label is discarded rather than
    trusted -- refs reset on navigation (injected.js), so a stale label could
    describe an element that no longer exists."""
    engine = PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))
    engine.note_snapshot(
        "d1", 7, "https://example.com/page-a", [{"ref": "e1", "name": "Delete", "tag": "button"}]
    )
    # Tab navigates elsewhere -- the hub observes a new URL for tab 7.
    engine.note_tab_url("d1", 7, "https://example.com/page-b")
    decision = engine.evaluate(Target(device_id="d1", tab_id=7, ref="e1"), "click", {"ref": "e1"})
    assert decision.status == "allow"  # stale hint discarded -- no signal, not gated


def test_ref_hint_valid_when_url_unchanged(tmp_path: Any) -> None:
    """Sanity check on the staleness comparison itself: an unchanged URL
    (the normal case -- note_tab_url observes the SAME url the snapshot was
    taken on) must not be treated as stale."""
    engine = PolicyEngine(AuditLog(tmp_path / "audit.jsonl"))
    engine.note_snapshot(
        "d1", 7, "https://example.com/page-a", [{"ref": "e1", "name": "Delete", "tag": "button"}]
    )
    engine.note_tab_url("d1", 7, "https://example.com/page-a")  # same url, e.g. from a `read`
    decision = engine.evaluate(Target(device_id="d1", tab_id=7, ref="e1"), "click", {"ref": "e1"})
    assert decision.status == "gate"


# ---------------------------------------------------------------------------
# Hub-integration proof: a click gate fires end-to-end, through a real
# hub-routed `snapshot` result -- not a synthetic hint handed to `evaluate`
# directly.
# ---------------------------------------------------------------------------


def test_hub_click_gate_fires_pre_action_from_real_snapshot_result(tmp_path: Any) -> None:
    """The end-to-end proof this phase is required to produce: a `snapshot`
    command result (carrying nodes, exactly as injected.js's snapshot()
    produces) flows through the real Hub._ingest_result path, and a
    SUBSEQUENT `click` naming that ref -- with NO label in the click's own
    args -- is gated. The device must never receive the click."""
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    fake_ws.canned_result = {
        "ok": True,
        "result": {
            "url": "https://shop.example.com/cart",
            "title": "Cart",
            "nodes": [
                {"ref": "e1", "role": "button", "name": "Place Order", "tag": "button"},
                {"ref": "e2", "role": "button", "name": "Continue shopping", "tag": "button"},
            ],
        },
    }

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        snap = await hub.send_command(Target(device_id="d1", tab_id=1), "snapshot", {})
        clicked = await hub.send_command(Target(device_id="d1", tab_id=1, ref="e1"), "click", {"ref": "e1"})
        return snap, clicked

    snap, clicked = asyncio.run(run())
    assert snap["ok"] is True
    assert clicked["status"] == "needs_confirmation"
    assert clicked["category"] == "purchase"
    # Only the snapshot reached the device -- the click was gated before dispatch.
    assert [env["command"] for env in fake_ws.sent] == ["snapshot"]


def test_hub_click_gate_does_not_fire_for_unrelated_ref(tmp_path: Any) -> None:
    """Same snapshot, different ref -- one whose label is not gate-worthy --
    must flow through untouched."""
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    fake_ws.canned_result = {
        "ok": True,
        "result": {
            "url": "https://shop.example.com/cart",
            "title": "Cart",
            "nodes": [
                {"ref": "e1", "role": "button", "name": "Place Order", "tag": "button"},
                {"ref": "e2", "role": "button", "name": "Continue shopping", "tag": "button"},
            ],
        },
    }

    async def run() -> dict[str, Any]:
        await hub.send_command(Target(device_id="d1", tab_id=1), "snapshot", {})
        return await hub.send_command(Target(device_id="d1", tab_id=1, ref="e2"), "click", {"ref": "e2"})

    clicked = asyncio.run(run())
    assert clicked["ok"] is True
    assert [env["command"] for env in fake_ws.sent] == ["snapshot", "click"]


def test_hub_wait_for_result_seeds_ref_hint_for_subsequent_click(tmp_path: Any) -> None:
    """The common `wait_for` -> `click` workflow (no full `snapshot` ever
    taken) still gets pre-action gating, because `wait_for`'s result
    (enriched by injected.js to carry url/tag/name/input_type) is fed into
    the same ref-hint cache via `Hub._ingest_result`."""
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    fake_ws.canned_result = {
        "ok": True,
        "result": {
            "ref": "e9",
            "url": "https://app.example.com/account",
            "tag": "button",
            "name": "Delete account",
        },
    }

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        waited = await hub.send_command(
            Target(device_id="d1", tab_id=1), "wait_for", {"selector": "#delete-btn"}
        )
        clicked = await hub.send_command(Target(device_id="d1", tab_id=1, ref="e9"), "click", {"ref": "e9"})
        return waited, clicked

    waited, clicked = asyncio.run(run())
    assert waited["ok"] is True
    assert clicked["status"] == "needs_confirmation"
    assert clicked["category"] == "delete"
    assert [env["command"] for env in fake_ws.sent] == ["wait_for"]


def test_hub_file_upload_gate_fires_from_snapshot_input_type(tmp_path: Any) -> None:
    """input_type flows from a real snapshot node -- proves the file_upload
    gate is reachable end-to-end without any caller ever passing
    args["input_type"] explicitly."""
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    fake_ws.canned_result = {
        "ok": True,
        "result": {
            "url": "https://app.example.com/upload",
            "title": "Upload",
            "nodes": [{"ref": "e1", "role": "textbox", "name": "", "tag": "input", "input_type": "file"}],
        },
    }

    async def run() -> dict[str, Any]:
        await hub.send_command(Target(device_id="d1", tab_id=1), "snapshot", {})
        return await hub.send_command(Target(device_id="d1", tab_id=1, ref="e1"), "click", {"ref": "e1"})

    clicked = asyncio.run(run())
    assert clicked["status"] == "needs_confirmation"
    assert clicked["category"] == "file_upload"
