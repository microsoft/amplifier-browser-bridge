"""Tests for the A5 fix: omitting `session_id` on a STATE_CHANGING_COMMANDS
command used to silently run under the fully-permissive implicit write scope
with no indication in the response. Every such result now carries a
`scope_warning` field; a session-scoped call never sees it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.hub import SCOPE_UNSCOPED_WARNING, Hub


class FakeDeviceSocket:
    def __init__(self, record: Any, canned_result: dict[str, Any] | None = None) -> None:
        self.record = record
        self.canned_result = canned_result or {"ok": True, "result": {"stub": True}}

    async def send_json(self, data: dict[str, Any], /) -> None:
        fut = self.record.pending.get(data["id"])
        if fut is not None and not fut.done():
            fut.set_result({**self.canned_result, "id": data["id"]})


def _hub(tmp_path: Path) -> Hub:
    return Hub(token_store=TokenStore(), audit_log=AuditLog(tmp_path / "audit.jsonl"))


@pytest.mark.asyncio
async def test_state_changing_command_without_session_id_carries_scope_warning(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    record.ws = FakeDeviceSocket(record)
    record.touch()

    result = await hub.send_command(Target(device_id="d1", tab_id=1, ref="e1"), "click", {})

    assert result["ok"] is True
    assert result["scope_warning"] == SCOPE_UNSCOPED_WARNING


@pytest.mark.asyncio
async def test_state_changing_command_with_session_id_has_no_scope_warning(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    record.ws = FakeDeviceSocket(record)
    record.touch()
    scope = hub.establish_session(write="*")

    result = await hub.send_command(
        Target(device_id="d1", tab_id=1, ref="e1"), "click", {}, session_id=scope.session_id
    )

    assert result["ok"] is True
    assert "scope_warning" not in result


@pytest.mark.asyncio
async def test_read_only_command_never_carries_scope_warning_even_without_session(tmp_path: Path) -> None:
    """scope_warning is scoped to STATE_CHANGING_COMMANDS only -- reads/tabs/
    snapshot etc. were never subject to write-scope enforcement in the first
    place, so warning about it would be noise, not signal."""
    hub = _hub(tmp_path)
    record = hub.registry.get_or_create("d1")
    record.ws = FakeDeviceSocket(record)
    record.touch()

    result = await hub.send_command(Target(device_id="d1", tab_id=1), "read", {})

    assert result["ok"] is True
    assert "scope_warning" not in result


@pytest.mark.asyncio
async def test_queued_state_changing_command_without_session_id_carries_scope_warning(
    tmp_path: Path,
) -> None:
    hub = _hub(tmp_path)
    hub.registry.get_or_create("d1")  # never connected -- DORMANT, queues instead of dispatching

    result = await hub.send_command(Target(device_id="d1", tab_id=1, ref="e1"), "click", {})

    assert result["status"] == "queued"
    assert result["scope_warning"] == SCOPE_UNSCOPED_WARNING
