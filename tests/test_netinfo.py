"""Tests for netinfo.py -- network-exposure helpers (A1/A2 fix support module).

`detect_tailscale_ip` shells out to the real `tailscale` CLI; these tests never
depend on Tailscale actually being installed -- they patch `shutil.which`/
`subprocess.run` so they're deterministic in any CI environment.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from amplifier_browser_bridge.netinfo import (
    detect_tailscale_ip,
    is_loopback,
    is_wildcard_bind,
    wildcard_bind_warning,
)


def test_is_wildcard_bind_recognizes_ipv4_and_ipv6_wildcards() -> None:
    assert is_wildcard_bind("0.0.0.0") is True
    assert is_wildcard_bind("::") is True


def test_is_wildcard_bind_rejects_specific_addresses() -> None:
    assert is_wildcard_bind("127.0.0.1") is False
    assert is_wildcard_bind("100.124.126.19") is False
    assert is_wildcard_bind("") is False


def test_is_loopback_recognizes_loopback_forms() -> None:
    assert is_loopback("127.0.0.1") is True
    assert is_loopback("localhost") is True
    assert is_loopback("::1") is True


def test_is_loopback_rejects_non_loopback() -> None:
    assert is_loopback("0.0.0.0") is False
    assert is_loopback("100.124.126.19") is False


def test_detect_tailscale_ip_returns_none_when_binary_missing() -> None:
    with patch("amplifier_browser_bridge.netinfo.shutil.which", return_value=None):
        assert detect_tailscale_ip() is None


def test_detect_tailscale_ip_returns_first_line_on_success() -> None:
    fake_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="100.124.126.19\n", stderr="")
    with (
        patch("amplifier_browser_bridge.netinfo.shutil.which", return_value="/usr/bin/tailscale"),
        patch("amplifier_browser_bridge.netinfo.subprocess.run", return_value=fake_result),
    ):
        assert detect_tailscale_ip() == "100.124.126.19"


def test_detect_tailscale_ip_returns_none_on_nonzero_exit() -> None:
    fake_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="not logged in")
    with (
        patch("amplifier_browser_bridge.netinfo.shutil.which", return_value="/usr/bin/tailscale"),
        patch("amplifier_browser_bridge.netinfo.subprocess.run", return_value=fake_result),
    ):
        assert detect_tailscale_ip() is None


def test_detect_tailscale_ip_returns_none_on_timeout() -> None:
    with (
        patch("amplifier_browser_bridge.netinfo.shutil.which", return_value="/usr/bin/tailscale"),
        patch(
            "amplifier_browser_bridge.netinfo.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tailscale", timeout=2.0),
        ),
    ):
        assert detect_tailscale_ip() is None


def test_detect_tailscale_ip_returns_none_on_oserror() -> None:
    with (
        patch("amplifier_browser_bridge.netinfo.shutil.which", return_value="/usr/bin/tailscale"),
        patch("amplifier_browser_bridge.netinfo.subprocess.run", side_effect=OSError("boom")),
    ):
        assert detect_tailscale_ip() is None


def test_wildcard_bind_warning_names_the_host_and_port_and_the_actual_exposure() -> None:
    text = wildcard_bind_warning("0.0.0.0", 8900)
    assert "0.0.0.0" in text
    assert "8900" in text
    assert "home Wi-Fi" in text
    assert "tailnet" in text
