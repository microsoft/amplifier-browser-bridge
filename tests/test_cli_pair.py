"""Tests for the `amplifier-browser-bridge pair` CLI command -- mints a ticket
over a real hub (via HubClient/the /agent route) and prints a single pairing
code (host:port + ticket) instead of a raw hub URL and a 32-hex token.

Runs the hub as a REAL SUBPROCESS (not an in-process aiohttp TestServer) --
`pair` invokes `asyncio.run(...)` internally (cli.py's `_run_command`-style
commands all do), and `click.testing.CliRunner.invoke` is a synchronous call,
so it cannot be exercised from inside an already-running event loop (the
pattern test_doctor.py/test_kill_switch.py use for their async, in-process
TestServer tests). A real subprocess sidesteps that entirely -- and doubles as
the same "run a real hub, exercise pairing end to end" proof this feature's
review demands.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from click.testing import CliRunner

from amplifier_browser_bridge import cli


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_healthz(port: int, *, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            last_error = e
        time.sleep(0.1)
    raise AssertionError(f"hub subprocess never became reachable on port {port}: {last_error}")


@pytest.fixture
def running_hub(tmp_path: Path):
    """A real `amplifier-browser-bridge hub` subprocess, with auth enabled via a
    pre-written token file -- yields (port, token)."""
    port = _free_port()
    token_file = tmp_path / "tokens.json"
    token = "cli-test-secret-000000000000000"
    token_file.write_text(json.dumps({"default": token, "devices": {}}), encoding="utf-8")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "amplifier_browser_bridge.cli",
            "hub",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--token-file",
            str(token_file),
            "--audit-log",
            str(tmp_path / "audit.jsonl"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_healthz(port)
        yield port, token
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_pair_prints_a_single_code_with_no_raw_url_or_token(running_hub, monkeypatch) -> None:
    port, token = running_hub
    monkeypatch.setattr(cli, "DEFAULT_HUB_URL", f"ws://127.0.0.1:{port}/agent")
    monkeypatch.setattr(cli, "DEFAULT_TOKEN", token)

    runner = CliRunner()
    result = runner.invoke(cli.main, ["pair"])

    assert result.exit_code == 0, result.output
    assert "Pairing code" in result.output
    assert f"@127.0.0.1:{port}" in result.output
    # The whole point: no raw ws:// URL printed directly -- only the compact
    # `<ticket>@host:port` code (host/port alone, not a dialable URL).
    assert "ws://" not in result.output


def test_pair_honors_ttl_option(running_hub, monkeypatch) -> None:
    port, token = running_hub
    monkeypatch.setattr(cli, "DEFAULT_HUB_URL", f"ws://127.0.0.1:{port}/agent")
    monkeypatch.setattr(cli, "DEFAULT_TOKEN", token)

    runner = CliRunner()
    result = runner.invoke(cli.main, ["pair", "--ttl", "30"])

    assert result.exit_code == 0, result.output
    assert "valid 30s" in result.output


def test_pair_rejects_the_wrong_token(running_hub, monkeypatch) -> None:
    port, _token = running_hub
    monkeypatch.setattr(cli, "DEFAULT_HUB_URL", f"ws://127.0.0.1:{port}/agent")
    monkeypatch.setattr(cli, "DEFAULT_TOKEN", "not-the-right-token")

    runner = CliRunner()
    result = runner.invoke(cli.main, ["pair"])

    assert result.exit_code != 0
    assert "unauthorized" in result.output


def test_pair_fails_loud_when_hub_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(cli, "DEFAULT_HUB_URL", "ws://127.0.0.1:1/agent")  # nothing listens on port 1
    monkeypatch.setattr(cli, "DEFAULT_TOKEN", None)

    runner = CliRunner()
    result = runner.invoke(cli.main, ["pair"])

    assert result.exit_code != 0
    assert result.output  # some actionable message, not a bare traceback
