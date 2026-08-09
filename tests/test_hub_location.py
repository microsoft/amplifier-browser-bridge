"""Tests for hub_location.py -- persisted hub location + resolution precedence.

This is the fix for the class of bug where `init` resolved and printed a
tailnet host, but every OTHER client (`devices`, the MCP server, the tool
module) had no way to read that decision back and fell through to a
hardcoded `ws://127.0.0.1:8900/agent` -- wrong on the exact cross-device
setups this project exists for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amplifier_browser_bridge.hub_location import (
    DEFAULT_PORT,
    HubLocation,
    read_hub_location,
    resolve_hub_location_file,
    resolve_hub_url,
    write_hub_location,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with neither env var set -- a stray value from the
    real shell running these tests (or a prior test) must never leak in."""
    monkeypatch.delenv("AMPLIFIER_BROWSER_BRIDGE_HUB_URL", raising=False)
    monkeypatch.delenv("AMPLIFIER_BROWSER_BRIDGE_HUB_LOCATION_FILE", raising=False)


def test_read_hub_location_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert read_hub_location(tmp_path / "does-not-exist.json") is None


def test_read_hub_location_returns_none_on_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "hub_location.json"
    path.write_text("not json at all", encoding="utf-8")
    assert read_hub_location(path) is None


def test_read_hub_location_returns_none_when_fields_missing_or_wrong_type(tmp_path: Path) -> None:
    path = tmp_path / "hub_location.json"
    path.write_text(json.dumps({"host": "100.1.2.3"}), encoding="utf-8")  # no port
    assert read_hub_location(path) is None
    path.write_text(json.dumps({"host": "100.1.2.3", "port": "8900"}), encoding="utf-8")  # port not int
    assert read_hub_location(path) is None
    path.write_text(json.dumps({"host": 123, "port": 8900}), encoding="utf-8")  # host not str
    assert read_hub_location(path) is None


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "hub_location.json"  # parent dir does not exist yet
    write_hub_location("100.124.126.19", 8900, path=path)
    location = read_hub_location(path)
    assert location == HubLocation(host="100.124.126.19", port=8900)
    assert location is not None
    assert location.to_agent_url() == "ws://100.124.126.19:8900/agent"


def test_write_hub_location_is_best_effort_on_unwritable_path(tmp_path: Path) -> None:
    """A write failure (e.g. the parent path is actually a file, not a
    directory -- can't mkdir through it) must never raise; the caller's own
    resolved host/port is unaffected either way (module docstring)."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    path = blocker / "hub_location.json"  # parent ("blocker") is a file
    write_hub_location("100.1.2.3", 8900, path=path)  # must not raise
    assert read_hub_location(path) is None


def test_write_hub_location_overwrites_a_stale_value(tmp_path: Path) -> None:
    """Correcting a stale persisted value is exactly this: call it again with
    the right host -- never hand-editing the file."""
    path = tmp_path / "hub_location.json"
    write_hub_location("100.1.1.1", 8900, path=path)
    write_hub_location("100.2.2.2", 8901, path=path)
    assert read_hub_location(path) == HubLocation(host="100.2.2.2", port=8901)


def test_resolve_hub_location_file_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit.json"
    assert resolve_hub_location_file(explicit) == explicit

    env_path = tmp_path / "from-env.json"
    monkeypatch.setenv("AMPLIFIER_BROWSER_BRIDGE_HUB_LOCATION_FILE", str(env_path))
    assert resolve_hub_location_file() == env_path

    monkeypatch.delenv("AMPLIFIER_BROWSER_BRIDGE_HUB_LOCATION_FILE")
    assert resolve_hub_location_file().name == "hub_location.json"


# --- resolve_hub_url: the precedence that fixes the bug -------------------------


def test_resolve_hub_url_falls_back_to_loopback_when_nothing_persisted(tmp_path: Path) -> None:
    assert resolve_hub_url(path=tmp_path / "nope.json") == f"ws://127.0.0.1:{DEFAULT_PORT}/agent"


def test_resolve_hub_url_uses_persisted_location_when_present(tmp_path: Path) -> None:
    path = tmp_path / "hub_location.json"
    write_hub_location("100.124.126.19", 8900, path=path)
    assert resolve_hub_url(path=path) == "ws://100.124.126.19:8900/agent"


def test_resolve_hub_url_env_var_always_wins_over_persisted_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "hub_location.json"
    write_hub_location("100.124.126.19", 8900, path=path)
    monkeypatch.setenv("AMPLIFIER_BROWSER_BRIDGE_HUB_URL", "ws://10.0.0.5:9999/agent")
    assert resolve_hub_url(path=path) == "ws://10.0.0.5:9999/agent"


def test_resolve_hub_url_env_var_wins_even_when_nothing_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMPLIFIER_BROWSER_BRIDGE_HUB_URL", "ws://10.0.0.5:9999/agent")
    assert resolve_hub_url() == "ws://10.0.0.5:9999/agent"
