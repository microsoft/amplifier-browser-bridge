"""Phase 4: CDP escalation -- attach/detach state machine, automatic
escalation for trusted-input/hidden-capture, the CDP-unavailable failure
path, and soft-detach-on-idle.

Same `FakeDeviceSocket` pattern as test_policy.py/test_ref_hints.py: routes
canned results through `Hub._ingest_result` so attach/detach bookkeeping
(which lives entirely in that method -- see hub.py) actually fires, exactly
as it would for a real device connection.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.cdp import CdpRegistry, requires_cdp
from amplifier_browser_bridge.hub import Hub

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class FakeDeviceSocket:
    """Routes every canned result through `Hub._ingest_result` -- see
    test_policy.py's identical fixture for why this matters."""

    def __init__(self, hub: Hub, record: Any, canned_result: dict[str, Any] | None = None) -> None:
        self.hub = hub
        self.record = record
        self.sent: list[dict[str, Any]] = []
        self.canned_result = canned_result or {"ok": True, "result": {"stub": True}}
        # command -> canned result, so different commands (attach vs. the
        # real click) can be answered differently by one fake.
        self.by_command: dict[str, dict[str, Any]] = {}

    async def send_json(self, data: dict[str, Any], /) -> None:
        self.sent.append(data)
        fut = self.record.pending.get(data["id"])
        if fut is None or fut.done():
            return
        result = self.by_command.get(data["command"], self.canned_result)
        raw_env = {**result, "id": data["id"], "device_id": self.record.device_id}
        env = self.hub._ingest_result(self.record.device_id, data["id"], raw_env)
        fut.set_result(env)

    async def close(self) -> None:
        pass


def _hub(tmp_path: Any, **kwargs: Any) -> Hub:
    return Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"), **kwargs)


def _live_device(hub: Hub, device_id: str = "d1", *, debugger: bool = True) -> tuple[Any, FakeDeviceSocket]:
    record = hub.registry.get_or_create(device_id)
    record.capabilities = {"debugger": debugger, "scripting": True}
    fake_ws = FakeDeviceSocket(hub, record)
    record.ws = fake_ws
    record.touch()
    return record, fake_ws


# ---------------------------------------------------------------------------
# cdp.requires_cdp -- pure decision function
# ---------------------------------------------------------------------------


def test_requires_cdp_true_only_for_declared_intent() -> None:
    assert requires_cdp("click", {"trusted": True}) is True
    assert requires_cdp("type", {"trusted": True}) is True
    assert requires_cdp("key", {"trusted": True}) is True
    assert requires_cdp("screenshot", {"capture_hidden": True}) is True


def test_requires_cdp_false_by_default() -> None:
    assert requires_cdp("click", {}) is False
    assert requires_cdp("screenshot", {}) is False
    assert requires_cdp("click", {"trusted": False}) is False
    # A caller-supplied `_cdp` is NOT a recognized intent signal -- see
    # hub.py's send_command, which strips it before this is ever checked.
    assert requires_cdp("click", {"_cdp": True}) is False


def test_requires_cdp_accepts_string_and_int_true_forms() -> None:
    """Regression test for a real reported bug: `amplifier-browser-bridge cmd <target> screenshot
    --arg capture_hidden=true` sends the STRING "true" (the CLI's `cmd` escape
    hatch always parses --arg key=value as strings). Before args_bool.truthy()
    was wired in here, `requires_cdp` used a strict `is True` identity check,
    which silently returned False for a string -- the hub never escalated to
    CDP, and the device failed loud with "requires the target tab to already
    be active" despite the caller passing exactly the flag meant to prevent
    that."""
    assert requires_cdp("screenshot", {"capture_hidden": "true"}) is True
    assert requires_cdp("screenshot", {"capture_hidden": 1}) is True
    assert requires_cdp("click", {"trusted": "true"}) is True
    assert requires_cdp("type", {"trusted": "TRUE"}) is True
    assert requires_cdp("key", {"trusted": 1}) is True
    # And the false-ish string forms must still correctly resolve to False --
    # not "any string is truthy".
    assert requires_cdp("screenshot", {"capture_hidden": "false"}) is False


# ---------------------------------------------------------------------------
# cdp.CdpRegistry -- pure state machine
# ---------------------------------------------------------------------------


def test_cdp_registry_attach_detach_lifecycle() -> None:
    reg = CdpRegistry()
    assert reg.is_attached("d1", 1) is False
    reg.mark_attached("d1", 1)
    assert reg.is_attached("d1", 1) is True
    reg.mark_detached("d1", 1, reason="requested")
    assert reg.is_attached("d1", 1) is False
    assert reg.snapshot("d1")[1]["last_detach_reason"] == "requested"


def test_cdp_registry_idle_tabs() -> None:
    reg = CdpRegistry(idle_seconds=5.0)
    now = datetime.now(UTC)
    reg.mark_attached("d1", 1, now=now - timedelta(seconds=10))
    reg.touch("d1", 1, now=now - timedelta(seconds=10))
    reg.mark_attached("d1", 2, now=now)
    reg.touch("d1", 2, now=now)
    idle = reg.idle_tabs(now=now)
    assert ("d1", 1) in idle
    assert ("d1", 2) not in idle


def test_cdp_registry_independent_per_device_and_tab() -> None:
    """Same structural principle as DeviceRegistry: no global 'the attached
    tab' slot."""
    reg = CdpRegistry()
    reg.mark_attached("d1", 1)
    reg.mark_attached("d2", 1)
    reg.mark_detached("d1", 1)
    assert reg.is_attached("d1", 1) is False
    assert reg.is_attached("d2", 1) is True  # unaffected


# ---------------------------------------------------------------------------
# Hub: explicit attach/detach commands
# ---------------------------------------------------------------------------


def test_hub_explicit_attach_marks_state_and_audits(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    fake_ws.by_command["attach"] = {"ok": True, "result": {"tab_id": 5, "attached": True}}

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=5), "attach", {})

    result = asyncio.run(run())
    assert result["ok"] is True
    assert hub.cdp.is_attached("d1", 5) is True
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"event": "cdp_attached"' in line for line in lines)


def test_hub_explicit_detach_marks_state_and_audits(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    hub.cdp.mark_attached("d1", 5)
    fake_ws.by_command["detach"] = {"ok": True, "result": {"tab_id": 5, "attached": False}}

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=5), "detach", {})

    result = asyncio.run(run())
    assert result["ok"] is True
    assert hub.cdp.is_attached("d1", 5) is False


def test_hub_attach_failure_does_not_mark_attached(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    fake_ws.by_command["attach"] = {"ok": False, "error": "user cancelled the debugger banner"}

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=5), "attach", {})

    result = asyncio.run(run())
    assert result["ok"] is False
    assert hub.cdp.is_attached("d1", 5) is False


# ---------------------------------------------------------------------------
# Hub: automatic escalation for trusted input / hidden-capture screenshot
# ---------------------------------------------------------------------------


def test_hub_auto_attaches_for_trusted_click(tmp_path: Any) -> None:
    """A `click` with `trusted: True` must NOT be dispatched to the device
    until CDP is attached -- the hub attaches first (never speculatively --
    only because this specific command needs it), THEN sends the real click
    with the hub-asserted `_cdp` flag."""
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    fake_ws.by_command["attach"] = {"ok": True, "result": {"attached": True}}
    fake_ws.by_command["click"] = {"ok": True, "result": {"ref": "e1", "trusted": True}}

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1, ref="e1"), "click", {"ref": "e1", "trusted": True}
        )

    result = asyncio.run(run())
    assert result["ok"] is True
    sent_commands = [env["command"] for env in fake_ws.sent]
    assert sent_commands == ["attach", "click"]
    # The device-bound click carries the hub-asserted _cdp flag.
    click_env = next(env for env in fake_ws.sent if env["command"] == "click")
    assert click_env["args"]["_cdp"] is True
    assert hub.cdp.is_attached("d1", 1) is True


def test_hub_does_not_reattach_if_already_attached(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    hub.cdp.mark_attached("d1", 1)
    fake_ws.by_command["click"] = {"ok": True, "result": {"ref": "e1"}}

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1, ref="e1"), "click", {"ref": "e1", "trusted": True}
        )

    result = asyncio.run(run())
    assert result["ok"] is True
    assert [env["command"] for env in fake_ws.sent] == ["click"]  # no redundant attach


def test_hub_plain_click_never_escalates(tmp_path: Any) -> None:
    """A click WITHOUT trusted=True must behave exactly as before Phase 4 --
    no attach, no _cdp flag."""
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    fake_ws.by_command["click"] = {"ok": True, "result": {"ref": "e1"}}

    async def run() -> dict[str, Any]:
        return await hub.send_command(Target(device_id="d1", tab_id=1, ref="e1"), "click", {"ref": "e1"})

    result = asyncio.run(run())
    assert result["ok"] is True
    assert [env["command"] for env in fake_ws.sent] == ["click"]
    assert "_cdp" not in fake_ws.sent[0]["args"]
    assert hub.cdp.is_attached("d1", 1) is False


def test_caller_supplied_cdp_flag_is_stripped_and_ignored(tmp_path: Any) -> None:
    """Security/consistency hardening: a caller cannot bypass the capability
    check / attach bookkeeping by setting `_cdp` directly in its own args --
    only `trusted`/`capture_hidden` are recognized intent signals."""
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub, debugger=False)  # CDP unavailable
    fake_ws.by_command["click"] = {"ok": True, "result": {"ref": "e1"}}

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1, ref="e1"), "click", {"ref": "e1", "_cdp": True}
        )

    result = asyncio.run(run())
    # If `_cdp` were honored, this would either error (no debugger capability)
    # or attempt CDP; instead it must flow through completely normally.
    assert result["ok"] is True
    assert [env["command"] for env in fake_ws.sent] == ["click"]
    assert fake_ws.sent[0]["args"].get("_cdp") is not True


def test_hub_auto_attaches_for_hidden_screenshot(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    fake_ws.by_command["attach"] = {"ok": True, "result": {"attached": True}}
    fake_ws.by_command["screenshot"] = {
        "ok": True,
        "result": {"tab_id": 1, "format": "jpeg", "data_url_length": 12345, "via": "cdp"},
    }

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1), "screenshot", {"capture_hidden": True}
        )

    result = asyncio.run(run())
    assert result["ok"] is True
    assert result["result"]["via"] == "cdp"
    assert [env["command"] for env in fake_ws.sent] == ["attach", "screenshot"]


# ---------------------------------------------------------------------------
# CDP-unavailable failure path -- fails loud, never silently degrades
# ---------------------------------------------------------------------------


def test_hub_trusted_click_fails_loud_when_debugger_unavailable(tmp_path: Any) -> None:
    """The Android case: chrome.debugger genuinely absent. Must return a
    clear error and must NOT dispatch anything to the device (no silent
    fallback to the injection-only path the caller didn't ask for)."""
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub, debugger=False)

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1, ref="e1"), "click", {"ref": "e1", "trusted": True}
        )

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "capability unavailable" in result["error"].lower()
    assert fake_ws.sent == []  # nothing reached the device
    assert hub.cdp.is_attached("d1", 1) is False


def test_hub_hidden_screenshot_fails_loud_when_debugger_unavailable(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub, debugger=False)

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1), "screenshot", {"capture_hidden": True}
        )

    result = asyncio.run(run())
    assert result["ok"] is False
    assert "capability unavailable" in result["error"].lower()
    assert fake_ws.sent == []


def test_cdp_unavailable_error_is_retrievable_via_poll_when_command_was_queued(tmp_path: Any) -> None:
    """Correctness fix: a CDP-requiring command that was queued (device
    offline) and only reaches `_dispatch_live` on drain must still leave a
    retrievable result behind -- not silently vanish because it failed
    before ever reaching `_send_and_await`'s normal result-storing path."""
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    record.capabilities = {"debugger": False}  # never connected -- DORMANT tier

    async def enqueue() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=1, ref="e1"), "click", {"ref": "e1", "trusted": True}
        )

    queued = asyncio.run(enqueue())
    assert queued["status"] == "queued"
    command_id = queued["command_id"]

    # Bring the device online -- this triggers _drain_queue -> _dispatch_live
    # for the queued command, which must hit the same CDP-unavailable path.
    fake_ws = FakeDeviceSocket(hub, record)
    record.ws = fake_ws
    record.touch()

    async def drain() -> None:
        await hub._drain_queue(record)

    asyncio.run(drain())

    polled = hub._poll("d1", command_id)
    assert polled["ok"] is False
    assert "capability unavailable" in polled["error"].lower()
    assert fake_ws.sent == []  # the click itself never reached the device


# ---------------------------------------------------------------------------
# Unsolicited detach (event) -- Cancel on the banner / DevTools / crash
# ---------------------------------------------------------------------------


def test_unsolicited_cdp_detached_event_updates_state(tmp_path: Any) -> None:
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    hub.cdp.mark_attached("d1", 7)
    assert hub.cdp.is_attached("d1", 7) is True

    async def run() -> None:
        await hub._handle_device_message(
            fake_ws,
            "d1",
            {
                "v": 1,
                "id": "e1",
                "type": "event",
                "device_id": "d1",
                "event": "cdp_detached",
                "data": {"tab_id": 7, "reason": "canceled_by_user"},
            },
        )

    asyncio.run(run())
    assert hub.cdp.is_attached("d1", 7) is False
    assert hub.cdp.snapshot("d1")[7]["last_detach_reason"] == "canceled_by_user"


def test_recovers_by_reattaching_after_unsolicited_detach(tmp_path: Any) -> None:
    """After the hub learns (via the event) that CDP was detached out from
    under it, the NEXT trusted command must transparently re-attach rather
    than error out claiming CDP is still live."""
    hub = _hub(tmp_path)
    _record, fake_ws = _live_device(hub)
    hub.cdp.mark_attached("d1", 7)

    async def detach_event() -> None:
        await hub._handle_device_message(
            fake_ws,
            "d1",
            {
                "v": 1,
                "id": "e1",
                "type": "event",
                "device_id": "d1",
                "event": "cdp_detached",
                "data": {"tab_id": 7, "reason": "devtools_opened"},
            },
        )

    asyncio.run(detach_event())
    assert hub.cdp.is_attached("d1", 7) is False

    fake_ws.by_command["attach"] = {"ok": True, "result": {"attached": True}}
    fake_ws.by_command["click"] = {"ok": True, "result": {"ref": "e1"}}

    async def run() -> dict[str, Any]:
        return await hub.send_command(
            Target(device_id="d1", tab_id=7, ref="e1"), "click", {"ref": "e1", "trusted": True}
        )

    result = asyncio.run(run())
    assert result["ok"] is True
    assert [env["command"] for env in fake_ws.sent] == ["attach", "click"]
    assert hub.cdp.is_attached("d1", 7) is True


# ---------------------------------------------------------------------------
# Soft-detach on idle
# ---------------------------------------------------------------------------


def test_soft_detach_idle_tabs_detaches_only_past_threshold(tmp_path: Any) -> None:
    hub = _hub(tmp_path, cdp_idle_seconds=5.0)
    _record, fake_ws = _live_device(hub)
    fake_ws.by_command["detach"] = {"ok": True, "result": {"attached": False}}

    now = datetime.now(UTC)
    hub.cdp.mark_attached("d1", 1, now=now - timedelta(seconds=100))
    hub.cdp.touch("d1", 1, now=now - timedelta(seconds=100))  # idle 100s > 5s threshold
    hub.cdp.mark_attached("d1", 2, now=now)
    hub.cdp.touch("d1", 2, now=now)  # fresh -- not idle

    async def run() -> list[tuple[str, int]]:
        return await hub.soft_detach_idle_tabs(now=now)

    detached = asyncio.run(run())
    assert ("d1", 1) in detached
    assert ("d1", 2) not in detached
    assert hub.cdp.is_attached("d1", 1) is False
    assert hub.cdp.is_attached("d1", 2) is True


def test_soft_detach_is_audited_distinctly_from_requested_detach(tmp_path: Any) -> None:
    hub = _hub(tmp_path, cdp_idle_seconds=1.0)
    _record, fake_ws = _live_device(hub)
    fake_ws.by_command["detach"] = {"ok": True, "result": {"attached": False}}
    now = datetime.now(UTC)
    hub.cdp.mark_attached("d1", 1, now=now - timedelta(seconds=10))
    hub.cdp.touch("d1", 1, now=now - timedelta(seconds=10))

    asyncio.run(hub.soft_detach_idle_tabs(now=now))

    assert hub.cdp.snapshot("d1")[1]["last_detach_reason"] == "idle"
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"reason": "idle"' in line for line in lines)
