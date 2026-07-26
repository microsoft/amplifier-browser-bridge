"""`abb doctor` -- diagnose exactly which link in the setup chain is broken.

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

from .auth import DEFAULT_TOKEN_FILE, load_token_store
from .client import HubClient, HubError

CheckStatus = Literal["ok", "fail", "skipped"]


@dataclass
class DoctorCheck:
    name: str
    status: CheckStatus
    message: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"


async def run_doctor(
    hub_url: str,
    token: str | None,
    token_file: str | Path | None = None,
) -> list[DoctorCheck]:
    """Run every check and return them in order. Never raises -- every failure mode
    (including ones that would otherwise be an unhandled exception, e.g. a malformed
    hub_url) is captured as a `DoctorCheck` with `status="fail"` instead."""
    checks: list[DoctorCheck] = []

    file_path = Path(token_file or DEFAULT_TOKEN_FILE).expanduser()
    store = load_token_store(token_file)
    if store.auth_enabled:
        checks.append(DoctorCheck("token_store", "ok", f"auth enabled; token file: {file_path}"))
    else:
        checks.append(
            DoctorCheck(
                "token_store",
                "ok",
                f"auth DISABLED (no token found at {file_path} or in ABB_HUB_TOKEN) -- fine for "
                "local dev on a private tailnet, run `abb init` before sharing this hub with "
                "another device.",
            )
        )

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
                    f"hub rejected the token ({message!r}). Confirm ABB_TOKEN/--token here matches "
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
                    f"cannot reach hub at {hub_url}: {message}. Is `abb hub` running? Check the "
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
                f"cannot reach hub at {hub_url}: {e}. Is `abb hub` running? Check the host/port "
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
