"""`amplifier-browser-bridge doctor` -- diagnose exactly which link in the setup chain is broken.

Checks run in dependency order and stop naming downstream checks as failures once an
upstream one has already failed (a hub that's unreachable makes "token match" and
"device connected" meaningless, not additionally broken) -- each check instead reports
`skipped` with a reason, so a stuck user sees ONE actionable thing to fix, not a wall of
undifferentiated red.

This is a thin, testable lib module (cli.py's `doctor` command only formats output --
see this project's "logic lives in the lib" convention, CONTRIBUTING.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from .auth import (
    TokenStore,
    extract_token_value,
    find_sibling_token_files,
    load_token_store,
    mask_token,
    resolve_token_file,
)
from .client import HubClient, HubError
from .netinfo import detect_tailscale_ip, is_loopback, is_wildcard_bind

CheckStatus = Literal["ok", "fail", "skipped"]

# A2 fix (security review finding): `doctor` never reported anything about
# network exposure or Tailscale ACL posture, even though SECURITY.md's
# original threat model rested entirely on "the tailnet is the boundary."
# Tailscale's own default policy is ALLOW-ALL WITHIN THE TAILNET unless an
# operator has hand-written a restrictive ACL -- for most users, that means
# the "tailnet boundary" is a no-op and the per-device token is the only
# real gate. See docs/tailscale-acl-example.json for a starting-point
# restrictive policy, and SECURITY.md's rewritten threat-model section.
_ACL_DISCLOSURE = (
    "Tailscale's DEFAULT policy allows every device on your tailnet to reach every other "
    "device on every port -- this is Tailscale's own default, not something this project "
    "configures. Unless you have written a restrictive ACL in your tailnet's admin console "
    "(https://login.tailscale.com/admin/acls), any device on your tailnet -- not just the "
    "ones you intend to use with this hub -- can reach it. A starting-point restrictive ACL "
    "is shipped at docs/tailscale-acl-example.hujson (scopes reachability of the hub's port to "
    "a tag you assign to your own devices). Until you apply something like it, the per-device "
    "token is the real gate, not the tailnet."
)


@dataclass
class DoctorCheck:
    name: str
    status: CheckStatus
    message: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _check_token_file_siblings(active_path: Path, store: TokenStore) -> DoctorCheck:
    """Detect a stray token-like file sitting beside the active token file, holding
    a different value than the one actually consulted -- see auth.py's
    `find_sibling_token_files`/`extract_token_value` docstrings for the exact
    failure mode this guards against (a hand-created or leftover file that was
    never read by any command, but that a user might reasonably have pasted into
    the extension's options page instead of the real token)."""
    siblings = find_sibling_token_files(active_path)
    divergent: list[tuple[Path, str]] = []
    for sibling in siblings:
        value = extract_token_value(sibling)
        if value is None:
            continue
        if store.default_token is None or value != store.default_token:
            divergent.append((sibling, value))

    if divergent:
        names = ", ".join(f"{p} (starts {mask_token(v)})" for p, v in divergent)
        return DoctorCheck(
            "token_file_siblings",
            "fail",
            f"found token-like file(s) beside {active_path} that are NOT read by amplifier-browser-bridge "
            f"init/hub/doctor and hold a different value: {names}. Only {active_path} "
            "(or $AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE) is consulted -- if one of these is what you pasted "
            "into the extension's options page, the hub will reject it. Delete the "
            "stray file or copy its value into the active token file.",
        )
    if siblings:
        return DoctorCheck(
            "token_file_siblings",
            "ok",
            f"other token-like file(s) found alongside {active_path} but they match "
            f"the active token: {', '.join(str(p) for p in siblings)}",
        )
    return DoctorCheck(
        "token_file_siblings", "ok", f"no other token-like files found alongside {active_path}"
    )


def _check_network_exposure(hub_url: str, auth_enabled: bool) -> DoctorCheck:
    """A2 fix (security review finding): report what the hub is (or could be)
    exposed to, and the Tailscale ACL disclosure -- neither existed before.

    This can only observe the host component of the URL THIS doctor
    invocation is pointed at, not necessarily every interface the actual
    running hub process bound (a hub started with `--host 0.0.0.0` is still
    reachable at `ws://127.0.0.1:.../agent` locally, so a doctor run against
    127.0.0.1 cannot, by itself, prove the hub isn't ALSO wildcard-bound).
    Says so explicitly rather than implying a guarantee this check cannot
    make.
    """
    host = urlsplit(hub_url).hostname or ""
    detected_tailscale_ip = detect_tailscale_ip()
    lines: list[str] = []

    if is_wildcard_bind(host):
        lines.append(
            f"this doctor invocation targets a WILDCARD host ({host!r}) -- if the running hub "
            "was actually started with --host matching this, it is reachable from EVERY network "
            "interface this machine has, not just the tailnet."
        )
    elif is_loopback(host):
        lines.append(
            f"this doctor invocation targets a loopback host ({host!r}). Note: this check can "
            "only see what host YOU pointed doctor at -- it cannot prove the running hub process "
            "isn't ALSO bound to a wider address (e.g. started with --host 0.0.0.0). Confirm "
            "separately how the hub you're diagnosing was actually started."
        )
    else:
        lines.append(
            f"this doctor invocation targets host {host!r} -- not a wildcard bind, but confirm "
            "this is the address you intend (your machine's own tailnet IP, not something wider)."
        )

    if detected_tailscale_ip:
        lines.append(f"this machine's own Tailscale IP: {detected_tailscale_ip}.")
    else:
        lines.append(
            "could not detect a Tailscale IP on this machine (`tailscale ip -4` unavailable or failed)."
        )

    if not auth_enabled:
        lines.append(
            "CRITICAL COMBINATION: auth is DISABLED (see token_store above). If this hub is "
            "reachable from anywhere beyond this machine, ANY device that can reach the port "
            "controls every connected browser, with no token check at all."
        )

    lines.append(_ACL_DISCLOSURE)
    return DoctorCheck("network_exposure", "ok", " ".join(lines))


async def run_doctor(
    hub_url: str,
    token: str | None,
    token_file: str | Path | None = None,
) -> list[DoctorCheck]:
    """Run every check and return them in order. Never raises -- every failure mode
    (including ones that would otherwise be an unhandled exception, e.g. a malformed
    hub_url) is captured as a `DoctorCheck` with `status="fail"` instead."""
    checks: list[DoctorCheck] = []

    file_path = resolve_token_file(token_file)
    store = load_token_store(token_file)
    if store.auth_enabled:
        checks.append(DoctorCheck("token_store", "ok", f"auth enabled; token file: {file_path}"))
    else:
        checks.append(
            DoctorCheck(
                "token_store",
                "ok",
                f"auth DISABLED (no token found at {file_path} or in AMPLIFIER_BROWSER_BRIDGE_HUB_TOKEN) -- fine for "
                "local dev on a private tailnet, run `amplifier-browser-bridge init` before sharing this hub with "
                "another device.",
            )
        )
    checks.append(_check_token_file_siblings(file_path, store))
    checks.append(_check_network_exposure(hub_url, store.auth_enabled))

    client = HubClient(hub_url, token=token)
    try:
        devices = await client.list_devices()
    except HubError as e:
        message = str(e)
        if message.strip().lower() == "unauthorized":
            checks.append(DoctorCheck("hub_reachable", "ok", f"hub reachable at {hub_url}"))
            checks.append(
                DoctorCheck(
                    "token_match",
                    "fail",
                    f"hub rejected the token ({message!r}). Confirm AMPLIFIER_BROWSER_BRIDGE_TOKEN/--token here matches "
                    f"the token in {file_path}, and matches what's pasted into the extension's "
                    "options page.",
                )
            )
            checks.append(DoctorCheck("device_connected", "skipped", "skipped (token mismatch)"))
        else:
            checks.append(
                DoctorCheck(
                    "hub_reachable",
                    "fail",
                    f"cannot reach hub at {hub_url}: {message}. Is `amplifier-browser-bridge hub` running? Check the "
                    "host/port and that you're on the same tailnet.",
                )
            )
            checks.append(DoctorCheck("token_match", "skipped", "skipped (hub unreachable)"))
            checks.append(DoctorCheck("device_connected", "skipped", "skipped (hub unreachable)"))
        return checks
    except OSError as e:
        checks.append(
            DoctorCheck(
                "hub_reachable",
                "fail",
                f"cannot reach hub at {hub_url}: {e}. Is `amplifier-browser-bridge hub` running? Check the host/port "
                "and that you're on the same tailnet.",
            )
        )
        checks.append(DoctorCheck("token_match", "skipped", "skipped (hub unreachable)"))
        checks.append(DoctorCheck("device_connected", "skipped", "skipped (hub unreachable)"))
        return checks

    checks.append(DoctorCheck("hub_reachable", "ok", f"hub reachable at {hub_url}"))
    checks.append(DoctorCheck("token_match", "ok", "token accepted by hub"))

    live = [d for d in devices if d.get("tier") == "live"]
    if not devices:
        checks.append(
            DoctorCheck(
                "device_connected",
                "fail",
                "no browser device has ever connected to this hub. Load the extension unpacked "
                "(edge://extensions -> Developer mode -> Load unpacked), click its toolbar icon, "
                "and set the Hub URL/token on the options page.",
            )
        )
    elif not live:
        tiers = {d.get("device_id"): d.get("tier") for d in devices}
        checks.append(
            DoctorCheck(
                "device_connected",
                "fail",
                f"device(s) known but not currently live: {tiers}. If this is a desktop device, "
                "it should reconnect within seconds -- check the extension's options page for a "
                "connection error. If it's mobile, this may be normal (see the tier model in "
                "README.md).",
            )
        )
    else:
        device_ids = [d.get("device_id") for d in live]
        checks.append(DoctorCheck("device_connected", "ok", f"{len(live)} device(s) live: {device_ids}"))

    return checks


def all_ok(checks: list[DoctorCheck]) -> bool:
    return all(c.status != "fail" for c in checks)
