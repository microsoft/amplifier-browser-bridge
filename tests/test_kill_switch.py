"""Tests for the A4 fix: the kill switch is now reachable over the wire
protocol (`kill_switch_engage`/`kill_switch_disengage`/`kill_switch_status`),
not library-API-only (`Hub.engage_kill_switch()` called directly in-process).

Exercised at the `Hub._handle_kill_switch_*` level (same technique test_hub.py
uses -- no real network socket needed to prove the wiring), plus one
end-to-end pass through the real `/agent` WebSocket route via an aiohttp
TestServer, matching test_doctor.py's precedent for exercising the actual
wire protocol rather than only the internal handler.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient as AiohttpTestClient
from aiohttp.test_utils import TestServer

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.hub import Hub


class FakeDeviceSocket:
    def __init__(self, record: Any, canned_result: dict[str, Any] | None = None) -> None:
        self.record = record
        self.sent: list[dict[str, Any]] = []
        self.canned_result = canned_result or {"ok": True, "result": {"stub": True}}

    async def send_json(self, data: dict[str, Any], /) -> None:
        self.sent.append(data)
        fut = self.record.pending.get(data["id"])
        if fut is not None and not fut.done():
            fut.set_result({**self.canned_result, "id": data["id"]})


def _hub(tmp_path: Path) -> Hub:
    return Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"))


def test_kill_switch_status_reports_disengaged_by_default(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    result = hub._handle_kill_switch_status()
    assert result == {"ok": True, "kill_switch_active": False}


def test_kill_switch_engage_reports_active_and_rejected_count(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    result = hub._handle_kill_switch_engage({})
    assert result["ok"] is True
    assert result["kill_switch_active"] is True
    assert result["rejected_queued_commands"] == 0
    assert hub._handle_kill_switch_status() == {"ok": True, "kill_switch_active": True}


def test_kill_switch_disengage_restores_normal_dispatch(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    hub._handle_kill_switch_engage({})
    result = hub._handle_kill_switch_disengage({})
    assert result == {"ok": True, "kill_switch_active": False}
    assert hub._handle_kill_switch_status() == {"ok": True, "kill_switch_active": False}


@pytest.mark.asyncio
async def test_engaged_kill_switch_denies_new_commands(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    record.ws = FakeDeviceSocket(record)
    record.touch()
    hub._handle_kill_switch_engage({})

    result = await hub.send_command(Target(device_id="d1", tab_id=1), "snapshot", {})

    assert result["ok"] is False
    assert "kill switch engaged" in result["error"]


@pytest.mark.asyncio
async def test_engaged_kill_switch_rejects_queued_commands(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    hub.registry.get_or_create("d1")  # never connected -- DORMANT, commands queue

    queued = await hub.send_command(Target(device_id="d1", tab_id=1), "snapshot", {})
    assert queued["status"] == "queued"

    result = hub._handle_kill_switch_engage({})
    assert result["rejected_queued_commands"] == 1

    polled = hub._poll("d1", queued["command_id"])
    assert polled["ok"] is False
    assert "kill switch" in polled["error"]


@pytest.mark.asyncio
async def test_kill_switch_reachable_over_the_real_agent_websocket_route(tmp_path: Path) -> None:
    """The actual A4 regression: these three message types must be handled by
    the real `/agent` WebSocket route (`Hub._handle_agent_ws`), not just
    callable as internal methods -- this is what makes them reachable by the
    shipped CLI/lib, not merely by an embedding app with direct Hub access."""
    hub = Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"))
    server = TestServer(hub.build_app())
    client = AiohttpTestClient(server)
    await client.start_server()
    try:
        ws = await client.ws_connect("/agent")
        await ws.send_json({"v": 1, "id": "1", "type": "kill_switch_engage"})
        resp = json.loads((await ws.receive()).data)
        assert resp["ok"] is True
        assert resp["kill_switch_active"] is True

        await ws.send_json({"v": 1, "id": "2", "type": "kill_switch_status"})
        resp = json.loads((await ws.receive()).data)
        assert resp["kill_switch_active"] is True

        await ws.send_json({"v": 1, "id": "3", "type": "kill_switch_disengage"})
        resp = json.loads((await ws.receive()).data)
        assert resp["ok"] is True
        assert resp["kill_switch_active"] is False
        await ws.close()
    finally:
        await client.close()
