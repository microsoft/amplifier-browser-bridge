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
from .hub_location import read_hub_location, resolve_hub_location_file
from .netinfo import detect_tailscale_ip, is_loopback, is_wildcard_bind
from .service import describe_service

CheckStatus = Literal["ok", "fail", "skipped"]

# A2 fix (security review finding): `doctor` never reported anything about
# network exposure or Tailscale ACL posture, even though docs/THREAT_MODEL.md's
# original threat model rested entirely on "the tailnet is the boundary."
# Tailscale's own default policy is ALLOW-ALL WITHIN THE TAILNET unless an
# operator has hand-written a restrictive ACL -- for most users, that means
# the "tailnet boundary" is a no-op and the per-device token is the only
# real gate. See docs/tailscale-acl-example.json for a starting-point
# restrictive policy, and docs/THREAT_MODEL.md's rewritten threat-model section.
#
# Two forms of this fact (real-run maintainer feedback, 2026-08: this check
# printed ~400 characters of ACL explanation at what should be a success
# moment). `_ACL_DISCLOSURE` (full) is reserved for genuinely concerning
# states this check can detect -- a wildcard bind, an inconclusive loopback
# target, or auth disabled -- where the extra detail is actually load-bearing.
# The common case (a specific tailnet-looking host, auth enabled -- nothing
# here needs explaining) gets `_ACL_POINTER` instead: the same underlying
# fact, in one line, with a pointer to the rest rather than the rest itself.
_ACL_DISCLOSURE = (
    "Tailscale's default ACL allows every device on your tailnet to reach every port on "
    "every other device -- unless you've written a restrictive ACL of your own "
    "(https://login.tailscale.com/admin/acls), the per-device token is your real security "
    "boundary, not the tailnet. Starting-point ACL: docs/tailscale-acl-example.hujson."
)
_ACL_POINTER = "Your real security boundary is your Tailscale ACL (default: allow-all), not the tailnet -- docs/POLICY.md."


@dataclass
class DoctorCheck:
    name: str
    status: CheckStatus
    message: str
    # Optional expanded explanation, printed indented below `message` (see cli.py's
    # `_print_doctor_checks`). Split out (rather than folded into one long `message`
    # string) so the headline stays a single skimmable clause and the full honest
    # detail -- which some checks (network_exposure) have a real amount of --
    # doesn't force every check's line to wrap into a paragraph. Both fields are
    # always real information; nothing here is truncated or hidden, only laid out.
    detail: str | None = None

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


def _check_hub_location(hub_url: str) -> DoctorCheck:
    """Report what's persisted as the default hub location (hub_location.py) --
    visibility for the fix to the class of bug where a client fell back to a
    hardcoded loopback default with no way to see why. Always `ok`: this is
    informational, not a pass/fail check. `amplifier-browser-bridge init` and
    `amplifier-browser-bridge service install` are what write this file (at
    the moment each decides where the hub lives); re-run either one with an
    explicit `--hub-host`/`--host` to correct a stale value -- never by
    hand-editing the file.
    """
    file_path = resolve_hub_location_file()
    location = read_hub_location(file_path)
    if location is None:
        return DoctorCheck(
            "hub_location",
            "ok",
            f"no hub location persisted yet at {file_path} -- run `amplifier-browser-bridge init` "
            "or `amplifier-browser-bridge service install` to record one, so other commands (a "
            "bare `devices`, the MCP server, the Amplifier tool module) default to it too.",
        )
    persisted_url = location.to_agent_url()
    if persisted_url != hub_url:
        return DoctorCheck(
            "hub_location",
            "ok",
            f"persisted hub location is {persisted_url} (from {file_path}), but this doctor run is "
            f"checking {hub_url} -- an env var or --hub-url is overriding it, which is expected "
            "and always takes priority over the persisted value.",
        )
    return DoctorCheck("hub_location", "ok", f"persisted hub location: {persisted_url} (from {file_path})")


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

    # `message`: one skimmable clause -- what a glance at the checklist needs.
    # `detail`: the full honest explanation, printed indented below it (see
    # cli.py's `_print_doctor_checks`) -- nothing here is cut, only laid out so
    # "is this fine" and "here's exactly why, and what to double-check" don't
    # have to compete for the same line. See DoctorCheck's docstring.
    if is_wildcard_bind(host):
        message = f"targets a WILDCARD host ({host!r}) -- reachable from every interface if the hub matches."
    elif is_loopback(host):
        message = f"targets loopback ({host!r}) -- cannot prove the hub isn't ALSO bound wider."
    else:
        message = f"targets {host!r} -- confirm this is your tailnet IP, not something wider."

    wildcard = is_wildcard_bind(host)
    loopback = is_loopback(host)
    # An unusual bind (wildcard) or an inconclusive check (loopback) is where the
    # detected-IP cross-check is actually useful; skip it in the common case
    # (see this function's own note on `_ACL_POINTER` above).
    needs_full_detail = wildcard or loopback

    detail_lines: list[str] = []
    if wildcard:
        detail_lines.append(
            "if the running hub was actually started with --host matching this, it is reachable "
            "from EVERY network interface this machine has, not just the tailnet."
        )
    elif loopback:
        detail_lines.append(
            "this check can only see what host YOU pointed doctor at -- it cannot prove the "
            "running hub process isn't ALSO bound to a wider address (e.g. started with "
            "--host 0.0.0.0). Confirm separately how the hub you're diagnosing was actually started."
        )

    if needs_full_detail:
        if detected_tailscale_ip:
            detail_lines.append(f"this machine's own Tailscale IP: {detected_tailscale_ip}.")
        else:
            detail_lines.append(
                "could not detect a Tailscale IP on this machine (`tailscale ip -4` unavailable or failed)."
            )

    if not auth_enabled:
        message = "CRITICAL COMBINATION: auth is DISABLED and doctor " + message
        detail_lines.append(
            "auth is DISABLED (see token_store above). If this hub is reachable from anywhere "
            "beyond this machine, ANY device that can reach the port controls every connected "
            "browser, with no token check at all."
        )
        detail_lines.append(_ACL_DISCLOSURE)
    elif needs_full_detail:
        detail_lines.append(_ACL_DISCLOSURE)
    else:
        # The common, everything-is-fine case: one line, not a paragraph (real-run
        # maintainer feedback -- see this function's docstring note above).
        detail_lines.append(_ACL_POINTER)

    return DoctorCheck("network_exposure", "ok", message, detail="\n".join(detail_lines))


def _check_service_status(hub_url: str) -> DoctorCheck:
    """Tell "hub installed as a service but not running" apart from "hub genuinely
    misconfigured" -- without this, a stopped service and a broken hub look
    identical: `hub_reachable` just fails, with no hint which one it is.

    This check inspects the service on THE MACHINE RUNNING DOCTOR, via
    `service.describe_service()` (local systemctl/launchctl calls) -- it has no way
    to inspect a service on a DIFFERENT machine over the network. It is only treated
    as authoritative (able to make this a real `fail` that skips the downstream
    network checks below) when `hub_url`'s host looks like THIS machine -- loopback,
    or this machine's own detected Tailscale IP. Otherwise it's informational only,
    exactly like `_check_network_exposure`'s own honesty pattern: never assert
    something this check cannot actually see.
    """
    host = urlsplit(hub_url).hostname or ""
    detected_tailscale_ip = detect_tailscale_ip()
    is_local = is_loopback(host) or (detected_tailscale_ip is not None and detected_tailscale_ip == host)

    info = describe_service()
    if not info.supported:
        return DoctorCheck(
            "service_status",
            "ok",
            f"service management not available on this platform ({info.detail}). If the hub "
            "isn't already running some other way (foreground terminal, your own process "
            "manager), start it with `amplifier-browser-bridge hub ...`.",
        )
    if not info.installed:
        return DoctorCheck(
            "service_status",
            "ok",
            "no amplifier-browser-bridge service installed on this machine. If the hub isn't already "
            "running some other way, install one with `amplifier-browser-bridge service install` "
            "so it survives logout and reboot.",
        )
    if info.active:
        return DoctorCheck("service_status", "ok", f"service {info.detail}.")

    # installed but not active
    if is_local:
        return DoctorCheck(
            "service_status",
            "fail",
            f"service is {info.detail} -- the hub is NOT running. Start it with "
            "`amplifier-browser-bridge service start`, or see `amplifier-browser-bridge service status` "
            "for the failure reason.",
        )
    return DoctorCheck(
        "service_status",
        "ok",
        f"this machine's own service is {info.detail}, but --hub-url ({hub_url!r}) targets a "
        "DIFFERENT host, so this may not reflect the actual hub process -- inspect the service on "
        "the machine that's actually supposed to be running it.",
    )


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
    checks.append(_check_hub_location(hub_url))
    checks.append(_check_network_exposure(hub_url, store.auth_enabled))

    # Reachability is the GROUND TRUTH for whether the hub is up -- never the
    # locally-recorded systemd/launchd unit alone. Measured on a real DTU: a hub
    # demonstrably up and minting real pairing codes still got reported as
    # `service_status: fail` with hub_reachable/token_match/device_connected all
    # skipped, because the previous version of this function decided "broken"
    # from the local unit file BEFORE ever attempting the actual network round
    # trip. A unit file can go stale in ways this check cannot see from the
    # filesystem alone: a hub started manually outside the service manager, one
    # running under some other process manager entirely, or a unit written but
    # never loaded by a failed `service install` (see service.py's
    # `_systemd_install` -- that specific case is now rolled back at the
    # source, but older/partial state from before this fix, or from any other
    # service manager, is still possible). So the real network attempt always
    # runs FIRST; the local service record is corrected afterward if the hub
    # proves it's actually up, and is only trusted to explain (and skip
    # downstream on) a GENUINE network failure.
    service_check = _check_service_status(hub_url)
    locally_reported_stopped = service_check.status == "fail"

    client = HubClient(hub_url, token=token)
    try:
        devices = await client.list_devices()
    except HubError as e:
        message = str(e)
        if message.strip().lower() == "unauthorized":
            # The hub answered -- rejecting our token is proof of life, not
            # proof of absence. A stale "not active" unit record cannot
            # survive contact with a hub that just talked back.
            if locally_reported_stopped:
                service_check = _reachable_despite_stale_service_record(service_check, hub_url)
            checks.append(service_check)
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
            checks.append(service_check)
            if locally_reported_stopped:
                # Genuinely unreachable AND the local record agrees -- the
                # service check already named the actionable cause; a second,
                # less specific network failure for the same root cause adds
                # nothing.
                checks.append(DoctorCheck("hub_reachable", "skipped", "skipped (service not running)"))
                checks.append(DoctorCheck("token_match", "skipped", "skipped (service not running)"))
                checks.append(DoctorCheck("device_connected", "skipped", "skipped (service not running)"))
            else:
                checks.append(
                    DoctorCheck(
                        "hub_reachable",
                        "fail",
                        f"cannot reach hub at {hub_url}: {message}. Is `amplifier-browser-bridge hub` running? "
                        "Check the host/port and that you're on the same tailnet.",
                    )
                )
                checks.append(DoctorCheck("token_match", "skipped", "skipped (hub unreachable)"))
                checks.append(DoctorCheck("device_connected", "skipped", "skipped (hub unreachable)"))
        return checks
    except OSError as e:
        checks.append(service_check)
        if locally_reported_stopped:
            checks.append(DoctorCheck("hub_reachable", "skipped", "skipped (service not running)"))
            checks.append(DoctorCheck("token_match", "skipped", "skipped (service not running)"))
            checks.append(DoctorCheck("device_connected", "skipped", "skipped (service not running)"))
        else:
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

    # Reachable, and the token was accepted -- unambiguous proof of life. Correct
    # the service check if it disagreed; the network never lies about this.
    if locally_reported_stopped:
        service_check = _reachable_despite_stale_service_record(service_check, hub_url)
    checks.append(service_check)
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


def _reachable_despite_stale_service_record(service_check: DoctorCheck, hub_url: str) -> DoctorCheck:
    """Correct a `service_status: fail` verdict once the hub has PROVEN it's up.

    `_check_service_status` can only see THIS machine's own systemd/launchd
    record -- it has no way to know the hub is actually being served some
    other way (a manual/foreground run, a different process manager, or a
    unit left behind by a failed `service install`). A hub that just answered
    a real network request (even to reject a token) is ground truth; the
    local record is a hint that lost.
    """
    return DoctorCheck(
        "service_status",
        "ok",
        f"{service_check.message} -- however the hub IS reachable at {hub_url} right now, so it's being "
        "served some other way (a manual/foreground run, a different process manager, or a stale "
        "service record). Reachability is the ground truth here, not the local service record.",
        detail=service_check.detail,
    )


def all_ok(checks: list[DoctorCheck]) -> bool:
    return all(c.status != "fail" for c in checks)
