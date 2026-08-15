"""`browser_update_extension` -- the ONE agent-facing tool for the version-skew
story (Tier 1 + Tier 2, docs/PROTOCOL.md's "hello" section, skew.py, build_stamp.py).

## The design insight this implements

Detecting up front whether the connected browser's unpacked extension lives
on THIS machine (reachable by a hub-side restage) or a genuinely remote one
is unreliable -- a network mount, a symlinked staging directory, or a
same-machine dev setup can each look identical to "remote" from the hub's
side, and there is no API that tells the hub where Edge actually loaded a
given unpacked extension's files from.

So this module does not try. It always attempts the automatic path --
restage a fresh build (`setup.stage_extension`, the SAME idempotent function
`amplifier-browser-bridge init` already uses -- no second implementation),
then send the device a `reload` command -- and then VERIFIES the result by
re-reading the device's self-reported command set after it reconnects. If
the capability set actually changed, the automatic update genuinely reached
wherever this device's extension loads from (Tier 1: same machine, a shared
mount, whatever -- it worked, and we can prove it). If it did not change,
this hub's restage did not reach that browser's real load path (most likely
a different machine entirely) -- report that PLAINLY and hand back the
guided remote instructions instead of a false "done."

## Non-negotiables

- **Never report success without having verified the command set changed.**
  An unverified "probably worked" is the exact failure this feature exists
  to eliminate -- see docs/ISSUE_HANDLING.md's evidence-based testing
  discipline, applied here as a design constraint, not just a test
  requirement.
- **`chrome.runtime.reload()` drops the websocket.** The device must
  re-connect and re-`hello` before a post-reload command-set read means
  anything -- comparing against a STALE registry record (the pre-reload
  connection, not yet torn down) would silently "verify" nothing. This
  module polls `list_devices()` until it sees a NEW `connected_at` for the
  same `device_id` (see `registry.py`'s `to_summary()` -- `connected_at` is
  the moment THIS connection was established, distinct from `last_seen`,
  which a mere heartbeat on an already-live connection also bumps), with a
  real timeout -- never a bare sleep-and-hope.
- **The bootstrap limit is real and is not this tool's bug.** An extension
  too old to understand the `reload` command AT ALL (predates Phase 5's
  self-service reload, a much older vintage than "pre-Tier-0") cannot reload
  itself into a version that understands it -- there is no way around that
  one manual step, ever, for that one specific extension build. This module
  detects it (the `reload` command itself comes back `ok: false`) and
  reports it honestly as the reason the guided path is needed, rather than
  retrying or pretending it can route around a structural limit.
- **Already current means BOTH axes check out.** `skew.SkewReport.in_sync`
  answers "what can this device DO" (command-complete); `build_stamp.py`'s
  `BuildFreshness.current` answers "IS this device running the CURRENT
  build" (content-identical to what this hub would install). Both are
  computed server-side and attached to every `devices` entry -- this module
  never recomputes either itself; it reads what `Hub._devices_snapshot()`
  already sent. A device can be command-complete and still stale: a change
  that touches zero commands (a bug fix, a UI fix, a security fix -- e.g.
  commits 6175ce4/cc140c5 in this repo's own history) is invisible to
  `skew` alone. Proven live: pointing this tool at a real, genuinely-
  outdated browser that had already caught up on commands returned
  `already_current: true` before this fix -- false. See build_stamp.py's
  module docstring for the full incident.

## Why this doesn't try to be clever about WHERE the device is

The dogfooding motivation for this feature is a maintainer converting their
OWN setup to hub-on-Linux, Edge-on-a-different-machine, extension loaded
from a machine-local folder -- i.e. exactly the case where an automatic
restage can never reach the real load path. This module's guided output
(`_download_url`) is built to be followed by someone on THAT other machine:
it derives `http://<hub host>:<hub port>/setup/extension.zip` from the SAME
host the calling client is already using to reach the hub (whatever
`HubClient.url` resolved to -- see `hub_location.py` for how that's usually
a real Tailscale IP, never assumed to be `127.0.0.1`), and
`GET /setup/extension.zip` already builds a fresh zip on demand
(`extension_zip.py`) -- no separate packaging step for the guided path.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .addressing import Target
from .client import HubClient
from .extension_integrity import ExtensionIntegrityError
from .setup import ExtensionSourceNotFoundError, stage_extension

# How long to wait for the device to reconnect after `reload` before giving
# up and failing loud (rather than hanging indefinitely or silently
# "verifying" against a stale pre-reload connection). Generous: an MV3
# service worker restart plus a fresh WebSocket handshake is normally well
# under a few seconds, but a slow machine or a momentarily busy network
# shouldn't produce a false "never came back."
DEFAULT_RECONNECT_TIMEOUT_S = 30.0
DEFAULT_POLL_INTERVAL_S = 1.0

_GUIDED_INSTRUCTIONS = (
    "On the machine actually running this browser: open the download URL above (it builds a "
    "fresh copy from this hub's current source on every request -- no separate packaging step), "
    "unzip it over the existing extension folder (or to a fresh folder, then update "
    'edge://extensions/\'s "Load unpacked" path to point at it), then click the reload icon '
    "under the extension in edge://extensions/. Your Hub URL and token are not touched by this -- "
    "they live in the extension's chrome.storage.local, keyed to its install path, and survive an "
    'update untouched. See INSTALL.md\'s "Updating" section for the full detail.'
)


def _find_device(devices: list[dict[str, Any]], device_id: str) -> dict[str, Any] | None:
    for device in devices:
        if device.get("device_id") == device_id:
            return device
    return None


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"})


def _download_url(hub_agent_url: str) -> str:
    """Derive the hub's `GET /setup/extension.zip` URL from its `/agent`
    WebSocket URL -- same host, resolvable from wherever the CALLER of this
    tool is running (the same place that's already reaching the hub to ask
    this question).

    This can still yield a loopback URL, and that case is NOT benign here --
    see `_loopback_warning`."""
    parts = urlsplit(hub_agent_url)
    scheme = "https" if parts.scheme == "wss" else "http"
    return urlunsplit((scheme, parts.netloc, "/setup/extension.zip", "", ""))


def _loopback_warning(download_url: str) -> str | None:
    """The guided path exists precisely because the browser is on a DIFFERENT
    machine than this hub -- and a loopback download URL is unreachable from
    there. The hub's own default bind is `127.0.0.1` (see `cli.py`'s `hub
    --host` default), so this is the likely case, not the exotic one: handing
    the user `http://127.0.0.1:.../setup/extension.zip` to open on their
    laptop sends them to their laptop's own machine, where nothing is
    listening.

    Returning the URL with no warning would be a silent dead end, so this
    names the cause and the fix instead."""
    host = urlsplit(download_url).hostname
    if host is None or host.lower() not in _LOOPBACK_HOSTS:
        return None
    return (
        f"WARNING: this hub is reachable at {download_url!r}, which is a LOOPBACK address -- "
        "it resolves to whatever machine opens it, so it will NOT work from the machine running "
        "the browser. Restart the hub bound to an address that machine can reach (e.g. this "
        "machine's Tailscale IP: `tailscale ip -4`, then `amplifier-browser-bridge hub --host "
        "<that-ip>`), or copy the downloaded zip across by hand. Everything else below still "
        "applies."
    )


def _guided_result(
    client: HubClient, device_id: str, *, reason: str, error: str, before: dict[str, Any]
) -> dict[str, Any]:
    return {
        "ok": False,
        "device_id": device_id,
        "updated": False,
        "reason": reason,
        "error": error,
        "before_commands_count": (len(before["commands"]) if before.get("commands") is not None else None),
        "before_build_stamp": (before.get("build_freshness") or {}).get("device_stamp"),
        "guided": _guided_block(client.url),
    }


def _guided_block(hub_agent_url: str) -> dict[str, Any]:
    download_url = _download_url(hub_agent_url)
    block: dict[str, Any] = {
        "download_url": download_url,
        "instructions": _GUIDED_INSTRUCTIONS,
    }
    warning = _loopback_warning(download_url)
    if warning is not None:
        block["warning"] = warning
    return block


async def run_update_extension(
    client: HubClient,
    device_id: str,
    *,
    dest: str | Path | None = None,
    source: str | Path | None = None,
    reconnect_timeout_s: float = DEFAULT_RECONNECT_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> dict[str, Any]:
    """Verify-or-guide extension update for one device. See module docstring
    for the full design. Returns a compact summary dict ONLY -- device
    counts, booleans, short messages -- never file contents or anything
    unbounded (the exact failure class this repo just finished fixing for
    `browser_tabs`/`browser_archive`; this tool does not reintroduce it).

    `client` is an already-configured `HubClient` (the caller's own -- same
    pattern as `archive.run_archive`). `dest`/`source` are advanced overrides
    forwarded to `setup.stage_extension` unchanged; omit both for the
    ordinary case (this hub's own packaged/dev-checkout extension source,
    staged to its default directory).
    """
    devices = await client.list_devices()
    before = _find_device(devices, device_id)
    if before is None:
        return {
            "ok": False,
            "device_id": device_id,
            "error": f"unknown device_id: {device_id!r} -- call browser_devices first.",
        }

    skew_before = before.get("skew") or {}
    freshness_before = before.get("build_freshness") or {}
    # Already current means BOTH axes check out: command-complete (skew.py --
    # "what can this device DO") AND running the hub's current build
    # (build_stamp.py -- "IS this device running the CURRENT build"). A
    # device can be command-complete and still stale (a bug/UI/security fix
    # that touched zero commands -- see build_stamp.py's module docstring
    # for the real incident this closes); checking `skew` alone here used to
    # report exactly that case as `already_current: true`, which was false.
    if skew_before.get("in_sync") and freshness_before.get("current"):
        return {
            "ok": True,
            "device_id": device_id,
            "already_current": True,
            "updated": False,
            "message": (
                "this device already reports every command this hub knows AND its build stamp "
                "matches this hub's current build -- nothing to do."
            ),
            "commands_count": (len(before["commands"]) if before.get("commands") is not None else None),
            "build_stamp": freshness_before.get("device_stamp"),
        }

    if before.get("tier") != "live":
        return {
            "ok": False,
            "device_id": device_id,
            "updated": False,
            "reason": "device_not_live",
            "error": (
                f"device {device_id} is not currently connected (tier={before.get('tier')!r}) -- "
                "there is no live websocket to send a reload command over. Reconnect the browser, "
                "then retry."
            ),
        }

    try:
        staged_dir = await asyncio.to_thread(stage_extension, dest, source)
    except (ExtensionSourceNotFoundError, ExtensionIntegrityError) as e:
        return _guided_result(
            client,
            device_id,
            reason="restage_failed",
            error=f"could not stage a fresh extension build on this hub: {e}",
            before=before,
        )

    reload_result = await client.command(Target(device_id=device_id), "reload", {})
    if not reload_result.get("ok"):
        return _guided_result(
            client,
            device_id,
            reason="reload_unsupported",
            error=(
                "this device's extension did not acknowledge the reload command "
                f"({reload_result.get('error', 'unknown error')!r}). An extension has to already "
                "understand the `reload` command before it can reload itself into a version that "
                "does -- this is a one-time bootstrap limit for a build old enough to predate "
                "self-service reload entirely, not a bug in this tool. Update it manually once via "
                "the guided steps below; every subsequent update can use this tool."
            ),
            before=before,
        )

    before_connected_at = before.get("connected_at")
    deadline = time.monotonic() + reconnect_timeout_s
    after: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        devices = await client.list_devices()
        candidate = _find_device(devices, device_id)
        if (
            candidate is not None
            and candidate.get("connected")
            and candidate.get("connected_at") != before_connected_at
        ):
            after = candidate
            break
        await asyncio.sleep(poll_interval_s)

    if after is None:
        return {
            "ok": False,
            "device_id": device_id,
            "updated": False,
            "reason": "reconnect_timeout",
            "error": (
                f"sent reload, but device {device_id} did not reconnect within "
                f"{reconnect_timeout_s:.0f}s. chrome.runtime.reload() drops the websocket -- the "
                "device must re-hello before an update can be verified, and this hub never assumes "
                "success without that. Confirm the browser is still running, then retry -- or check "
                "browser_devices yourself once it reconnects."
            ),
        }

    before_commands = before.get("commands")
    after_commands = after.get("commands")
    before_stamp = freshness_before.get("device_stamp")
    after_stamp = (after.get("build_freshness") or {}).get("device_stamp")
    # Verified change on EITHER axis is proof the restage reached this
    # device's real extension files -- commands (the common case: a new/
    # removed command) OR build stamp (a bug/UI/security fix that changed
    # zero commands, e.g. commits 6175ce4/cc140c5). Requiring BOTH to change
    # would be wrong the other direction: a command-only change legitimately
    # leaves the build stamp as the only thing that moved, and vice versa.
    # Only when NEITHER changed is the automatic path genuinely unverifiable.
    if after_commands == before_commands and after_stamp == before_stamp:
        return _guided_result(
            client,
            device_id,
            reason="no_verified_change",
            error=(
                "reload succeeded and the device reconnected, but its reported command set AND "
                "build stamp are both UNCHANGED -- this hub's restage did not reach wherever this "
                "browser's unpacked extension actually loads its files from (most likely a "
                "different machine, or a custom path this hub does not know about). The automatic "
                "path cannot be verified, so it is not reported as success -- follow the guided "
                "steps below on the machine actually running this browser."
            ),
            before=before,
        )

    return {
        "ok": True,
        "device_id": device_id,
        "already_current": False,
        "updated": True,
        "message": (
            f"restaged {staged_dir} and reloaded the extension -- VERIFIED: its reported "
            + (
                "command set and build stamp both changed"
                if after_commands != before_commands and after_stamp != before_stamp
                else "command set changed"
                if after_commands != before_commands
                else "build stamp changed"
            )
            + " after reconnecting, so the automatic update genuinely reached this device's real "
            "extension files."
        ),
        "before_commands_count": len(before_commands) if before_commands is not None else None,
        "after_commands_count": len(after_commands) if after_commands is not None else None,
        "before_build_stamp": before_stamp,
        "after_build_stamp": after_stamp,
        "now_in_sync": (after.get("skew") or {}).get("in_sync"),
        "now_build_current": (after.get("build_freshness") or {}).get("current"),
    }


__all__ = ["DEFAULT_POLL_INTERVAL_S", "DEFAULT_RECONNECT_TIMEOUT_S", "run_update_extension"]
