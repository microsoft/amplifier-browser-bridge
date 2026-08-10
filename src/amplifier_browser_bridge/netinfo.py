"""Network-exposure helpers -- what a bind address actually exposes, and a
best-effort Tailscale IP lookup for a safe default.

This exists because of a real gap (security review finding, see docs/THREAT_MODEL.md
and docs/POLICY.md): `amplifier-browser-bridge hub` defaulted to `--host 0.0.0.0`, and
`amplifier-browser-bridge init` printed that default back as the recommended command. `0.0.0.0`
binds EVERY network interface the host has -- home Wi-Fi, hotel Wi-Fi, a
coffee-shop captive network, a corporate LAN -- not just the Tailscale tailnet
this project's threat model assumes. See `docs/designs/browser-bridge.md` and
docs/THREAT_MODEL.md's "Where the load-bearing boundary actually is" section.

Pure, side-effect-free except for `detect_tailscale_ip`'s one best-effort
subprocess call -- which never raises, and returns `None` (never a fake
address) on any failure. Nothing here is a security *enforcement* mechanism;
it exists to make an exposure choice loud and specific, and to pick a safer
default when one is available, not to firewall anything.
"""

from __future__ import annotations

import shutil
import subprocess

# Addresses that bind every interface (IPv4 and IPv6 wildcard forms). Any of
# these means "reachable from anywhere this machine's network stack can be
# reached from," not merely "reachable from the tailnet."
WILDCARD_HOSTS: frozenset[str] = frozenset({"0.0.0.0", "::", "0:0:0:0:0:0:0:0"})

LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


def is_wildcard_bind(host: str) -> bool:
    """True if `host` binds every network interface, not a specific one."""
    return host in WILDCARD_HOSTS


def is_loopback(host: str) -> bool:
    """True if `host` is a loopback-only address (unreachable from any other device,
    tailnet included)."""
    return host in LOOPBACK_HOSTS


def detect_tailscale_ip(timeout: float = 2.0) -> str | None:
    """Best-effort: this machine's own Tailscale IPv4 address, via the `tailscale`
    CLI (`tailscale ip -4`).

    Returns `None` -- never raises, never fabricates an address -- if the
    `tailscale` binary isn't on PATH, the command errors, or it times out.
    This is a convenience for picking a safe, still-cross-device-reachable
    default; no command in this project *requires* the `tailscale` CLI to be
    installed to function.
    """
    if shutil.which("tailscale") is None:
        return None
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[0] if lines else None


def wildcard_bind_warning(host: str, port: int) -> str:
    """Loud, specific text naming exactly what a wildcard bind exposes.

    Shared by `hub` (printed when it actually binds a wildcard address) and
    `init` (printed when the command it's about to recommend would bind one)
    so the two surfaces never describe the same address differently -- see
    docs/THREAT_MODEL.md and the A1 fix this module exists for.
    """
    return (
        f"WARNING: --host {host} binds port {port} on EVERY network interface this "
        "machine has right now -- home Wi-Fi, a hotel or airport captive network, a "
        "corporate LAN -- not only your Tailscale tailnet. Anyone who can reach this "
        "machine on that network can reach the hub at this port. If you only want "
        "tailnet reachability, bind this machine's own tailnet IP instead (see "
        "`tailscale ip -4`), or restrict inbound access to this port at the OS "
        "firewall to the tailnet interface only."
    )


__all__ = [
    "LOOPBACK_HOSTS",
    "WILDCARD_HOSTS",
    "detect_tailscale_ip",
    "is_loopback",
    "is_wildcard_bind",
    "wildcard_bind_warning",
]
