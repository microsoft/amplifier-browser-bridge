"""Tests for auto_setup.py -- the programmatic (non-interactive, non-blocking)
equivalent of `amplifier-browser-bridge init`'s building blocks, used by the
Amplifier tool module's `browser_setup` tool.

Load-bearing properties under test:

  - it reuses `init`'s own building blocks (`_resolve_hub_host`,
    `_wait_for_hub_reachable`, `_setup_pair_url`) rather than reimplementing them
    -- verified by observing their real, unmocked behavior (a genuinely closed
    port really is reported unreachable; an explicit host really does skip
    auto-detection) rather than only asserting against a mock.
  - it never blocks waiting for a browser to connect (no analogue of `init`'s
    `_watch_for_device_connection` exists here at all).
  - it never raises for an expected failure (service unsupported, hub not yet
    reachable) -- those are reported in the returned dict instead.
  - re-running it is idempotent: an existing token is reused, never rotated,
    unless `force_token=True` (same guarantee `setup.ensure_token_file` already
    has and this module must not weaken).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from amplifier_browser_bridge.audit import AuditLog
from amplifier_browser_bridge.auth import TokenStore
from amplifier_browser_bridge.auto_setup import run_auto_setup
from amplifier_browser_bridge.hub import Hub, serve_hub
from amplifier_browser_bridge.service import ServiceUnsupportedError
from amplifier_browser_bridge.setup import ensure_token_file


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_reports_not_reachable_when_no_hub_running(tmp_path: Path) -> None:
    """A genuinely closed port -- no mocking of reachability at all -- must be
    reported as `hub_reachable: False`, with `pairing: None` and an actionable
    warning, never a raised exception."""
    port = _free_port()  # bound-then-released: guaranteed nothing is listening

    result = asyncio.run(
        run_auto_setup(
            host="127.0.0.1",
            port=port,
            token_file=tmp_path / "tokens.json",
            dest=tmp_path / "extension",
            install_service=False,
            wait_reachable_s=0.3,
        )
    )

    assert result["ok"] is True
    assert result["hub_reachable"] is False
    assert result["pairing"] is None
    assert (tmp_path / "tokens.json").is_file()
    assert Path(result["staged_extension_dir"]).is_dir()
    assert any("not reachable" in w for w in result["warnings"])
    assert "manual_hub_command" in result and str(port) in result["manual_hub_command"]


def test_mints_pairing_link_when_hub_is_reachable(tmp_path: Path) -> None:
    """Full happy path against a REAL, in-process hub (not a mock): reachable,
    and a redeemable pairing link comes back."""
    token_path = tmp_path / "tokens.json"
    token_result = ensure_token_file(token_path)
    port = _free_port()

    hub = Hub(
        token_store=TokenStore(default_token=token_result.token), audit_log=AuditLog(tmp_path / "audit.jsonl")
    )
    app = hub.build_app()

    async def run() -> dict[str, Any]:
        bound: list[bool] = []
        task = asyncio.create_task(serve_hub(app, "127.0.0.1", port, on_bound=lambda: bound.append(True)))
        try:
            deadline = asyncio.get_event_loop().time() + 5.0
            while not bound and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.05)
            assert bound, "hub never bound"

            return await run_auto_setup(
                host="127.0.0.1",
                port=port,
                token_file=token_path,
                dest=tmp_path / "extension",
                install_service=False,
                wait_reachable_s=3.0,
            )
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    result = asyncio.run(run())

    assert result["ok"] is True
    assert result["hub_reachable"] is True
    assert result["warnings"] == []
    assert result["pairing"] is not None
    assert result["pairing"]["pair_url"].startswith(f"http://127.0.0.1:{port}/setup#pair=")
    assert result["pairing"]["code"].endswith(f"@127.0.0.1:{port}")


def test_reuses_existing_token_across_calls(tmp_path: Path) -> None:
    """The idempotency guarantee `setup.ensure_token_file` already has must survive
    being called through `run_auto_setup` -- a second call is not a token rotation."""
    token_path = tmp_path / "tokens.json"
    port = _free_port()

    first = asyncio.run(
        run_auto_setup(
            host="127.0.0.1",
            port=port,
            token_file=token_path,
            dest=tmp_path / "extension",
            install_service=False,
            wait_reachable_s=0.1,
        )
    )
    second = asyncio.run(
        run_auto_setup(
            host="127.0.0.1",
            port=port,
            token_file=token_path,
            dest=tmp_path / "extension",
            install_service=False,
            wait_reachable_s=0.1,
        )
    )

    assert first["token_created_new"] is True
    assert second["token_created_new"] is False
    # The token value itself isn't returned in the dict (never echo a secret back
    # into a chat transcript by default) -- token_file is the same path both times,
    # which is what `ensure_token_file`'s own idempotency contract guarantees.
    assert first["token_file"] == second["token_file"]


def test_degrades_honestly_when_service_install_is_unsupported(tmp_path: Path) -> None:
    """`ServiceUnsupportedError` must never propagate as an unhandled exception --
    it's reported in `service`/`warnings`, and the rest of the result (token,
    staged extension, manual fallback command) is still complete and usable."""
    port = _free_port()

    with patch(
        "amplifier_browser_bridge.auto_setup.service_install",
        side_effect=ServiceUnsupportedError("no systemctl on PATH"),
    ):
        result = asyncio.run(
            run_auto_setup(
                host="127.0.0.1",
                port=port,
                token_file=tmp_path / "tokens.json",
                dest=tmp_path / "extension",
                install_service=True,
                wait_reachable_s=0.1,
            )
        )

    assert result["ok"] is True
    assert result["service"]["attempted"] is True
    assert result["service"]["installed"] is False
    assert any("could not install the hub as a background service" in w for w in result["warnings"])
    assert result["manual_hub_command"]  # still a complete, usable fallback


def test_skips_service_install_when_requested(tmp_path: Path) -> None:
    """`install_service=False` must never call `service_install` at all -- e.g. a
    hub that's already managed some other way (a live production service) must
    never be touched."""
    port = _free_port()

    with patch("amplifier_browser_bridge.auto_setup.service_install") as mock_install:
        result = asyncio.run(
            run_auto_setup(
                host="127.0.0.1",
                port=port,
                token_file=tmp_path / "tokens.json",
                dest=tmp_path / "extension",
                install_service=False,
                wait_reachable_s=0.1,
            )
        )

    mock_install.assert_not_called()
    assert result["service"]["attempted"] is False


def test_omitting_host_defers_to_the_shared_resolve_hub_host(tmp_path: Path) -> None:
    """No host given must go through the SAME `cli._resolve_hub_host` `init` uses
    -- not a parallel host-detection implementation -- verified by controlling its
    one external dependency (`detect_tailscale_ip`) and observing the result."""
    port = _free_port()

    with patch("amplifier_browser_bridge.cli.detect_tailscale_ip", return_value="100.9.9.9"):
        result = asyncio.run(
            run_auto_setup(
                host=None,
                port=port,
                token_file=tmp_path / "tokens.json",
                dest=tmp_path / "extension",
                install_service=False,
                wait_reachable_s=0.1,
            )
        )

    assert result["hub_host"] == "100.9.9.9"
    assert result["host_detected_note"] is not None
    assert "auto-detected" in result["host_detected_note"]


def test_wildcard_host_is_flagged() -> None:
    port = _free_port()
    result = asyncio.run(
        run_auto_setup(host="0.0.0.0", port=port, install_service=False, wait_reachable_s=0.1)
    )
    assert result["wildcard_warning"] is not None
    assert "EVERY network interface" in result["wildcard_warning"]


def test_missing_extension_source_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMPLIFIER_BROWSER_BRIDGE_EXTENSION_SRC", str(tmp_path / "does-not-exist"))
    result = asyncio.run(
        run_auto_setup(
            host="127.0.0.1",
            port=_free_port(),
            token_file=tmp_path / "tokens.json",
            install_service=False,
            wait_reachable_s=0.1,
        )
    )
    assert result["ok"] is False
    assert "error" in result
