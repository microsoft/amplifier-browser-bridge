"""Tests for the `browser_setup` / `browser_setup_status` Amplifier tools.

Only the adapter layer is under test here (argument mapping, ToolResult shape) --
`auto_setup.run_auto_setup`'s own onboarding logic has its own dedicated test suite
at the repo root (`tests/test_auto_setup.py`), exercised against a real in-process
hub. Duplicating that here would be exactly the kind of parallel-implementation
risk this feature was built to avoid, so `run_auto_setup`/`run_doctor` are mocked
at this layer -- what's under test is that the tool calls them, maps inputs
correctly, and returns their result verbatim.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from amplifier_core import ToolResult

from amplifier_module_tool_browser_bridge import _build_tools


def _tool_by_name(name: str):
    tools = _build_tools()
    matches = [t for t in tools if t.name == name]
    assert len(matches) == 1, f"expected exactly one tool named {name!r}, found {len(matches)}"
    return matches[0]


@pytest.mark.asyncio
async def test_browser_setup_maps_inputs_to_run_auto_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_result = {"ok": True, "hub_reachable": True, "pairing": {"pair_url": "http://x/setup#pair=ABC"}}
    mock_run_auto_setup = AsyncMock(return_value=fake_result)
    monkeypatch.setattr("amplifier_module_tool_browser_bridge.run_auto_setup", mock_run_auto_setup)

    tool = _tool_by_name("browser_setup")
    result = await tool.execute(
        {
            "host": "100.1.2.3",
            "port": 9100,
            "install_service": False,
            "force_token": True,
            "wait_reachable_s": 3.0,
            "token_file": "/tmp/tokens.json",
            "dest": "/tmp/extension",
        }
    )

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.output == fake_result
    mock_run_auto_setup.assert_awaited_once_with(
        host="100.1.2.3",
        port=9100,
        token_file="/tmp/tokens.json",
        dest="/tmp/extension",
        install_service=False,
        force_token=True,
        wait_reachable_s=3.0,
    )


@pytest.mark.asyncio
async def test_browser_setup_uses_defaults_when_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    from amplifier_browser_bridge.auto_setup import DEFAULT_WAIT_REACHABLE_S
    from amplifier_browser_bridge.hub_location import DEFAULT_PORT

    mock_run_auto_setup = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("amplifier_module_tool_browser_bridge.run_auto_setup", mock_run_auto_setup)

    tool = _tool_by_name("browser_setup")
    await tool.execute({})

    mock_run_auto_setup.assert_awaited_once_with(
        host=None,
        port=DEFAULT_PORT,
        token_file=None,
        dest=None,
        install_service=True,
        force_token=False,
        wait_reachable_s=DEFAULT_WAIT_REACHABLE_S,
    )


@pytest.mark.asyncio
async def test_browser_setup_status_serializes_doctor_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    from amplifier_browser_bridge.doctor import DoctorCheck

    checks = [
        DoctorCheck("token_store", "ok", "auth enabled"),
        DoctorCheck("device_connected", "fail", "no device", detail="load the extension"),
    ]
    mock_run_doctor = AsyncMock(return_value=checks)
    monkeypatch.setattr("amplifier_module_tool_browser_bridge.run_doctor", mock_run_doctor)

    tool = _tool_by_name("browser_setup_status")
    result = await tool.execute({})

    assert result.success is True
    output = result.output
    assert isinstance(output, dict)
    assert output["ok"] is False  # one check failed
    assert output["checks"] == [
        {"name": "token_store", "status": "ok", "message": "auth enabled", "detail": None},
        {
            "name": "device_connected",
            "status": "fail",
            "message": "no device",
            "detail": "load the extension",
        },
    ]
    mock_run_doctor.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_setup_status_ok_true_when_all_checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    from amplifier_browser_bridge.doctor import DoctorCheck

    checks = [
        DoctorCheck("token_store", "ok", "auth enabled"),
        DoctorCheck("hub_reachable", "ok", "reachable"),
    ]
    monkeypatch.setattr("amplifier_module_tool_browser_bridge.run_doctor", AsyncMock(return_value=checks))

    tool = _tool_by_name("browser_setup_status")
    result = await tool.execute({})

    output: Any = result.output
    assert output["ok"] is True
