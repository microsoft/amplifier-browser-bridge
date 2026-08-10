"""Programmatic first-run setup for non-interactive callers (e.g. an Amplifier tool).

## Why this exists

`amplifier-browser-bridge init` (cli.py) is this project's original onboarding flow --
a real terminal, `[Y/n]` prompts, and an auto-advance loop that watches for a browser
to connect over several minutes. That flow assumes a human sitting at a TTY.

An Amplifier tool call is a different shape entirely: it returns ONE value, once, into
a chat transcript. There is no prompt to answer, and blocking a tool call for minutes
waiting on a browser to connect would make the calling agent (and the user watching
it) sit idle for no good reason -- the agent can simply call again later (or call
`browser_devices`/`doctor`) to check whether pairing completed.

## This is NOT a second implementation of `init`

See docs/ISSUE_HANDLING.md's "two-lists bug" -- a parallel reimplementation of
onboarding logic is exactly the mistake this project has repeatedly had to fix. This
module calls the SAME building blocks `init` itself calls, unchanged:

    - `setup.ensure_token_file` / `setup.stage_extension` -- token + extension staging
    - `cli._resolve_hub_host` -- the exact host-resolution decision `init` and
      `service install` already share (see that function's own docstring)
    - `service.service_install` / `service.describe_service` -- the same OS-service
      mechanism `init`'s guided flow offers interactively
    - `cli._wait_for_hub_reachable` -- the same bounded readiness poll `init` uses
      right after installing the service
    - `cli._setup_url` / `cli._setup_pair_url` -- the same onboarding-page URL shapes
      `init` prints and hands to the user

What this module does NOT do, by design: it never prompts, and it never runs `init`'s
multi-minute `_watch_for_device_connection` loop -- a caller that wants to know
whether pairing completed calls `doctor.run_doctor` (or the `browser_devices` tool)
separately, exactly as a human re-running `amplifier-browser-bridge doctor` would.

## Why `cli` is imported lazily

`cli.py` imports `hub.py`, which imports `aiohttp` -- and `click` for its own command
parsing. The Amplifier tool module (`amplifier_module_tool_browser_bridge`) is
deliberately kept a *thin* adapter that avoids that import graph at MOUNT time (see
that module's own docstring) so mounting the other 25+ tools stays fast regardless of
whether `browser_setup` is ever actually called. Importing `cli` inside
`run_auto_setup` (rather than at this module's top level) defers that cost to the one
call site that actually needs it.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import sys
import sysconfig
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import HubClient, HubError
from .extension_integrity import ExtensionIntegrityError
from .hub_location import DEFAULT_PORT, write_hub_location
from .netinfo import is_wildcard_bind, wildcard_bind_warning
from .pairing import DEFAULT_TICKET_TTL_SECONDS
from .service import (
    SERVICE_NAME,
    ServiceInfo,
    ServiceInstallError,
    ServiceUnsupportedError,
    describe_service,
    service_install,
)
from .setup import ExtensionSourceNotFoundError, TokenResult, ensure_token_file, stage_extension

# Same default readiness budget `init`'s own guided flow uses after installing the
# service (cli.py's `_SERVICE_READY_TIMEOUT_S`) -- named here too so a caller building
# the `browser_setup` tool's input schema can see/override it without reading cli.py.
DEFAULT_WAIT_REACHABLE_S = 8.0


def _resolve_cli_invocation() -> tuple[str, str | None]:
    """A `amplifier-browser-bridge` invocation the READER of a manual_*_command
    string can actually run -- not necessarily the process calling this tool. A
    bundle-only Amplifier install has no `amplifier-browser-bridge` console
    script on PATH at all (measured on a clean DTU container: `command -v
    amplifier-browser-bridge` -> exit 1, zero files found on the whole disk),
    so a manual command that just assumes the bare name is on the reader's
    PATH is exactly the same class of bug as the loopback hub URL and the
    missing HUB_URL on a printed `pair` command -- printed instruction that
    doesn't work. Fixed structurally here (one resolver, three call sites all
    derive from it) rather than by correcting the string in each of
    `manual_hub_command`/`manual_pair_command`/`manual_doctor_command`.

    Returns `(invocation, warning)`. `invocation` is always a single string
    that is verified runnable on THIS machine before being returned -- never a
    guess about where a console script "usually" lives:

      1. `shutil.which` -- already on PATH; shortest, most familiar command.
      2. This interpreter's own scripts directory (`sysconfig.get_path`) --
         where `uv tool install`/`pip install` puts the console script for
         the environment THIS code is actually running in, regardless of
         whether that directory happens to be on PATH. Derived at runtime,
         never a hardcoded layout (e.g. `~/.local/bin`), because the same
         Amplifier install might be a `uv tool install` venv, a plain venv, or
         something else entirely.
      3. `<this interpreter> -m amplifier_browser_bridge.cli` -- the one
         invocation guaranteed to work: this function only ever runs from
         inside `run_auto_setup`, AFTER `from .cli import ...` has already
         succeeded in this exact process (see module docstring's "why cli is
         imported lazily"), so this exact interpreter can always re-invoke
         that same module this way. `warning` is set only for this case,
         since it's the one that depends on being run on THIS machine, as the
         user this Amplifier installation runs as -- never assumed silently.
    """
    which = shutil.which(SERVICE_NAME)
    if which:
        return which, None

    scripts_dir = Path(sysconfig.get_path("scripts"))
    candidate = scripts_dir / SERVICE_NAME
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate), None

    fallback = shlex.join([sys.executable, "-m", "amplifier_browser_bridge.cli"])
    return fallback, (
        f"no `{SERVICE_NAME}` console script found on PATH or at {scripts_dir} -- the manual_* "
        f"commands below use `{fallback}` instead. This only works when run on THIS machine, as "
        "the same user this Amplifier installation runs as."
    )


@dataclass
class SetupResult:
    """Everything a caller needs to either continue (a redeemable pairing link) or
    self-serve the remaining manual step (Edge has no CLI/API for "load unpacked" --
    see `stage_extension`'s docstring). Always JSON-friendly via `to_dict()`.
    """

    token: str
    token_file: str
    token_created_new: bool
    staged_dir: str
    host: str
    port: int
    host_detected_note: str | None
    wildcard_warning: str | None
    service: dict[str, Any]
    hub_reachable: bool
    setup_url: str
    pairing: dict[str, Any] | None
    manual_hub_command: str
    manual_pair_command: str
    manual_doctor_command: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "token_file": self.token_file,
            "token_created_new": self.token_created_new,
            "staged_extension_dir": self.staged_dir,
            "hub_host": self.host,
            "hub_port": self.port,
            "host_detected_note": self.host_detected_note,
            "wildcard_warning": self.wildcard_warning,
            "service": self.service,
            "hub_reachable": self.hub_reachable,
            "setup_url": self.setup_url,
            "pairing": self.pairing,
            "manual_hub_command": self.manual_hub_command,
            "manual_pair_command": self.manual_pair_command,
            "manual_doctor_command": self.manual_doctor_command,
            "warnings": self.warnings,
        }


async def run_auto_setup(
    *,
    host: str | None = None,
    port: int = DEFAULT_PORT,
    token_file: str | Path | None = None,
    dest: str | Path | None = None,
    force_token: bool = False,
    install_service: bool = True,
    wait_reachable_s: float = DEFAULT_WAIT_REACHABLE_S,
    pairing_ttl_s: float = DEFAULT_TICKET_TTL_SECONDS,
) -> dict[str, Any]:
    """Programmatic equivalent of `init`'s non-prompting building blocks, safe to
    call from an async Amplifier tool's `execute()`.

    Never raises for any EXPECTED failure mode (unsupported service platform, hub
    not yet reachable, missing extension source) -- each is reported in the
    returned dict's `warnings` / `service` / `hub_reachable` fields instead, so the
    calling agent gets one complete, actionable answer rather than a stack trace.
    Only a genuinely unexpected filesystem error surfaces as `{"ok": False, ...}`.

    The synchronous building blocks this delegates to (`ensure_token_file`,
    `stage_extension`, `cli._resolve_hub_host`, `service_install`,
    `cli._wait_for_hub_reachable`, `describe_service`) each block on filesystem or
    subprocess I/O and are run via `asyncio.to_thread` so this coroutine never
    blocks the calling event loop -- they are called exactly as `init` calls them,
    not reimplemented.
    """
    # Deferred import -- see module docstring's "Why `cli` is imported lazily".
    from .cli import _resolve_hub_host, _setup_pair_url, _setup_url, _wait_for_hub_reachable

    warnings: list[str] = []

    try:
        token_result: TokenResult = await asyncio.to_thread(ensure_token_file, token_file, force=force_token)
    except OSError as e:
        return {"ok": False, "error": f"could not write token file: {e}"}

    try:
        staged_dir = await asyncio.to_thread(stage_extension, dest)
    except (ExtensionSourceNotFoundError, ExtensionIntegrityError) as e:
        return {"ok": False, "error": str(e)}

    resolved_host, detected_note = await asyncio.to_thread(_resolve_hub_host, host)
    # Persist the decision now, exactly as `init`/`service install` do -- see
    # hub_location.py's module docstring for why every other consumer (a bare
    # `devices`, the MCP server, THIS tool module's other 25 tools) depends on
    # this being recorded at the moment it's decided. Best-effort; never raises.
    write_hub_location(resolved_host, port)
    wildcard_warning = wildcard_bind_warning(resolved_host, port) if is_wildcard_bind(resolved_host) else None

    service_outcome: dict[str, Any]
    if install_service:
        try:
            info: ServiceInfo = await asyncio.to_thread(
                service_install, resolved_host, port, token_result.token_file
            )
            service_outcome = {
                "attempted": True,
                "installed": True,
                "platform": info.platform,
                "detail": info.detail,
            }
        except ServiceUnsupportedError as e:
            # Never a lost cause -- same honesty `init` gives: token and staged
            # extension above are unaffected; the manual foreground-hub command
            # below still works on every platform, including this one.
            service_outcome = {"attempted": True, "installed": False, "reason": str(e)}
            warnings.append(
                f"could not install the hub as a background service: {e} Your token and staged "
                "extension are unaffected -- start the hub directly instead (see "
                "manual_hub_command)."
            )
        except ServiceInstallError as e:
            # Measured, not theoretical (DTU, clean container, no user D-Bus
            # session): `service_install()` looked capable (systemctl/launchctl
            # present and probed usable) but the install itself failed --
            # `ServiceInstallError` is what `service_install()` now guarantees
            # every such failure arrives as, so this is the ONE place this
            # class of failure needs handling, not a growing list of
            # `subprocess.CalledProcessError`/`RuntimeError` except clauses
            # scattered across every caller. Degrades exactly like
            # ServiceUnsupportedError: token and staged extension are
            # unaffected, manual_hub_command still works.
            service_outcome = {"attempted": True, "installed": False, "reason": str(e)}
            warnings.append(
                f"the hub background service failed to install: {e} Your token and staged extension "
                "are unaffected -- start the hub directly instead (see manual_hub_command), or retry "
                "once the underlying issue is resolved."
            )
        except OSError as e:
            service_outcome = {"attempted": True, "installed": False, "reason": str(e)}
            warnings.append(f"service install failed: {e}. See manual_hub_command to run the hub directly.")
    else:
        existing = await asyncio.to_thread(describe_service)
        service_outcome = {
            "attempted": False,
            "installed": existing.installed,
            "active": existing.active,
            "detail": existing.detail,
        }

    reachable = await asyncio.to_thread(
        _wait_for_hub_reachable, resolved_host, port, token_result.token, timeout=wait_reachable_s
    )

    setup_url = _setup_url(resolved_host, port)
    # Resolved once, used by all three manual_*_command strings below -- see
    # `_resolve_cli_invocation`'s docstring for why this can't just be the bare
    # `amplifier-browser-bridge` name (measured: absent from PATH on a
    # bundle-only install).
    cli_invocation, cli_invocation_warning = _resolve_cli_invocation()
    if cli_invocation_warning:
        warnings.append(cli_invocation_warning)
    manual_hub_command = (
        f"AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE={token_result.token_file} {cli_invocation} "
        f"hub --host {resolved_host} --port {port}"
    )
    manual_pair_command = (
        f"AMPLIFIER_BROWSER_BRIDGE_TOKEN={token_result.token} "
        f"AMPLIFIER_BROWSER_BRIDGE_HUB_URL=ws://{resolved_host}:{port}/agent {cli_invocation} pair"
    )
    manual_doctor_command = (
        f"AMPLIFIER_BROWSER_BRIDGE_TOKEN={token_result.token} {cli_invocation} doctor "
        f"--hub-url ws://{resolved_host}:{port}/agent"
    )

    pairing: dict[str, Any] | None = None
    if not reachable:
        warnings.append(
            f"hub not reachable yet at ws://{resolved_host}:{port}/agent within {wait_reachable_s:.0f}s -- "
            "no pairing link minted. Start the hub (see service/manual_hub_command above), then call "
            "this again -- or run manual_pair_command yourself once it's up."
        )
    else:
        try:
            pairing_result = await HubClient(
                f"ws://{resolved_host}:{port}/agent", token=token_result.token
            ).create_pairing(ttl_seconds=pairing_ttl_s)
        except HubError as e:
            warnings.append(f"hub reachable, but minting a pairing code failed: {e}")
        else:
            if not pairing_result.get("ok"):
                warnings.append(
                    "hub reachable, but minting a pairing code failed: "
                    f"{pairing_result.get('error') or 'unknown error'}"
                )
            else:
                expires_at = time.time() + pairing_ttl_s
                code = f"{pairing_result['ticket']}@{resolved_host}:{port}"
                pair_url = _setup_pair_url(resolved_host, port, code, expires_at=expires_at)
                pairing = {"code": code, "pair_url": pair_url, "expires_in_s": int(pairing_ttl_s)}

    result = SetupResult(
        token=token_result.token,
        token_file=str(token_result.token_file),
        token_created_new=token_result.created_new,
        staged_dir=str(staged_dir),
        host=resolved_host,
        port=port,
        host_detected_note=detected_note,
        wildcard_warning=wildcard_warning,
        service=service_outcome,
        hub_reachable=reachable,
        setup_url=setup_url,
        pairing=pairing,
        manual_hub_command=manual_hub_command,
        manual_pair_command=manual_pair_command,
        manual_doctor_command=manual_doctor_command,
        warnings=warnings,
    )
    return result.to_dict()


__all__ = ["DEFAULT_WAIT_REACHABLE_S", "SetupResult", "run_auto_setup"]
