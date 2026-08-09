"""amplifier-browser-bridge -- thin CLI adapter over the amplifier_browser_bridge library.

All logic lives in the lib (hub.py, client.py, addressing.py, ...). This module only
parses argv, builds a Target/HubClient, prints JSON, and translates library exceptions
into click.ClickException. Nothing here should ever need a unit test of its own --
if it does, that logic belongs in the lib instead.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import click

from .addressing import TargetError, parse_target
from .audit import AuditLog
from .auth import (
    extract_token_value,
    find_sibling_token_files,
    load_token_store,
    mask_token,
    resolve_token_file,
)
from .client import HubClient, HubError
from .clipboard import copy_to_clipboard
from .doctor import DoctorCheck, run_doctor
from .extension_integrity import ExtensionIntegrityError
from .hub import DEFAULT_COMMAND_TIMEOUT, DEFAULT_PORT, Hub, HubBindError, serve_hub
from .netinfo import detect_tailscale_ip, is_wildcard_bind, wildcard_bind_warning
from .pairing import DEFAULT_TICKET_TTL_SECONDS
from .policy import Denylist, host_of
from .protocol import COMMANDS
from .service import (
    SERVICE_NAME,
    ServiceUnsupportedError,
    describe_service,
    service_install,
    service_logs,
    service_restart,
    service_start,
    service_status,
    service_stop,
    service_uninstall,
)
from .setup import (
    DEFAULT_STAGE_DIR,
    ExtensionSourceNotFoundError,
    TokenResult,
    ensure_token_file,
    stage_extension,
)
from .vision import VisionConfigError, VisionError
from .vision_read import vision_read as _vision_read

DEFAULT_HUB_URL = os.environ.get("AMPLIFIER_BROWSER_BRIDGE_HUB_URL", "ws://127.0.0.1:8900/agent")
DEFAULT_TOKEN = os.environ.get("AMPLIFIER_BROWSER_BRIDGE_TOKEN")


def _client() -> HubClient:
    return HubClient(DEFAULT_HUB_URL, token=DEFAULT_TOKEN)


def _print(obj: Any) -> None:
    click.echo(json.dumps(obj, indent=2, default=str))


def _run_command(
    target_str: str,
    command: str,
    args: dict[str, Any],
    *,
    timeout: float | None = None,
    session_id: str | None = None,
) -> None:
    try:
        target = parse_target(target_str)
    except TargetError as e:
        raise click.ClickException(str(e)) from e
    if target.ref and "ref" not in args:
        args = {**args, "ref": target.ref}
    if timeout is not None:
        args = {**args, "timeout_s": timeout}
    try:
        result = asyncio.run(_client().command(target, command, args, session_id=session_id))
    except HubError as e:
        raise click.ClickException(str(e)) from e
    _print(result)


# Shared --session option for the state-changing commands (click/type/navigate,
# and the generic `cmd` escape hatch, which also reaches `key`) -- enforces
# that session's declared write scope (scope.py, docs/designs/confirmation-gate.md
# section 11.2). Not on read-only commands (snapshot/read/tabs/...): scope
# enforcement only ever applies to STATE_CHANGING_COMMANDS (see policy.py).
SESSION_OPTION = click.option(
    "--session",
    "session_id",
    default=None,
    help=(
        "Session id from a prior `amplifier-browser-bridge session-establish` call. If given, the hub enforces "
        "that session's declared write scope against this command before it reaches the "
        "device. Omit for the existing, fully-permissive default."
    ),
)


# Shared --timeout option for every subcommand that ends up in `_run_command` --
# one flag name, one meaning (the hub's device-round-trip wait for THIS
# command only), everywhere. Distinct from `--timeout-ms` on wait-for/wait-text,
# which is an in-page polling deadline, not a wire-level timeout -- see
# docs/PROTOCOL.md's "Command timeout" section for how the two interact (a
# wait-for/wait-text timeout-ms longer than the default --timeout will need
# --timeout raised too, or the hub will give up on the round trip before the
# page-side wait finishes).
TIMEOUT_OPTION = click.option(
    "--timeout",
    type=float,
    default=None,
    help=(
        "Override the hub's device-round-trip wait (seconds) for this command only. "
        "Default: the hub's configured command-timeout (see `amplifier-browser-bridge hub --command-timeout`, "
        "120s out of the box). Real heavy SPAs have been observed needing more than the "
        "old fixed 30s default even once the tab reports status=complete."
    ),
)


@click.group()
def main() -> None:
    """amplifier-browser-bridge -- Amplifier Browser Bridge CLI.

    Target strings address a command: `device_id`, `device_id/tab_id`, or
    `device_id/window_id/tab_id`, optionally with a trailing `#ref`.
    Configure the hub via AMPLIFIER_BROWSER_BRIDGE_HUB_URL (default ws://127.0.0.1:8900/agent) and
    AMPLIFIER_BROWSER_BRIDGE_TOKEN (if the hub has auth enabled).
    """


@main.command()
def devices() -> None:
    """List known devices, their tier, capabilities, and last-seen time."""
    try:
        result = asyncio.run(_client().list_devices())
    except HubError as e:
        raise click.ClickException(str(e)) from e
    _print(result)


@main.command()
@click.argument("target")
@TIMEOUT_OPTION
def tabs(target: str, timeout: float | None) -> None:
    """List tabs for a device (optionally scoped to a window)."""
    _run_command(target, "tabs", {}, timeout=timeout)


ACTIVATE_OPTION = click.option(
    "--activate",
    is_flag=True,
    default=False,
    help=(
        "Activate (foreground) the tab before running this command. Never automatic -- steals "
        "the human's focus, same co-working-etiquette exception as tab-activate -- but DOM "
        "injection/traversal on a heavy page is measured to be dramatically faster (or to "
        "outright time out) while backgrounded. Result reports 'activated': true when this "
        "actually changed the tab's active state."
    ),
)


@main.command()
@click.argument("target")
@click.option(
    "--wake",
    is_flag=True,
    default=False,
    help=(
        "If the target tab is discarded (Edge unloaded it to reclaim memory), reload it and "
        "retry instead of failing loud. Destroys in-page state (unsaved form data, scroll "
        "position, ephemeral JS state) -- opt-in only; the result reports 'woke': true."
    ),
)
@ACTIVATE_OPTION
@TIMEOUT_OPTION
def snapshot(target: str, wake: bool, activate: bool, timeout: float | None) -> None:
    """Accessibility-style snapshot of a tab: stable element refs for click/type.

    Refs are frame-qualified (e.g. "f0.e12") -- see docs/PROTOCOL.md's "Frames" section.
    Each node also carries a `generation` -- refs are only valid from the MOST RECENT
    snapshot; a ref from a superseded snapshot fails loud on click/type/key rather than
    silently resolving (see docs/PROTOCOL.md's "Snapshot generations" section).
    """
    args: dict[str, Any] = {}
    if wake:
        args["wake"] = True
    if activate:
        args["activate"] = True
    _run_command(target, "snapshot", args, timeout=timeout)


@main.command()
@click.argument("target")
@click.option(
    "--wake",
    is_flag=True,
    default=False,
    help=(
        "If the target tab is discarded (Edge unloaded it to reclaim memory), reload it and "
        "retry instead of failing loud. Destroys in-page state (unsaved form data, scroll "
        "position, ephemeral JS state) -- opt-in only; the result reports 'woke': true."
    ),
)
@ACTIVATE_OPTION
@TIMEOUT_OPTION
def read(target: str, wake: bool, activate: bool, timeout: float | None) -> None:
    """Read the visible text of a tab, gathered across all frames -- see
    docs/PROTOCOL.md's "Frames" section for the combine strategy."""
    args: dict[str, Any] = {}
    if wake:
        args["wake"] = True
    if activate:
        args["activate"] = True
    _run_command(target, "read", args, timeout=timeout)


@main.command(name="click")
@click.argument("target")
@click.argument("ref")
@SESSION_OPTION
@TIMEOUT_OPTION
def click_cmd(target: str, ref: str, session_id: str | None, timeout: float | None) -> None:
    """Click an element by ref (from a prior snapshot). A frame-qualified ref
    (e.g. "f3.e7") routes the click to that exact frame."""
    _run_command(target, "click", {"ref": ref}, timeout=timeout, session_id=session_id)


@main.command(name="type")
@click.argument("target")
@click.argument("ref")
@click.argument("text")
@SESSION_OPTION
@TIMEOUT_OPTION
def type_cmd(target: str, ref: str, text: str, session_id: str | None, timeout: float | None) -> None:
    """Type text into an element by ref."""
    _run_command(target, "type", {"ref": ref, "text": text}, timeout=timeout, session_id=session_id)


@main.command()
@click.argument("target")
@click.argument("url")
@SESSION_OPTION
@TIMEOUT_OPTION
def navigate(target: str, url: str, session_id: str | None, timeout: float | None) -> None:
    """Navigate a tab to a URL."""
    _run_command(target, "navigate", {"url": url}, timeout=timeout, session_id=session_id)


@main.command(name="tab-open")
@click.argument("device")
@click.argument("url", required=False, default="about:blank")
@click.option(
    "--active/--background",
    default=False,
    help="Open as the active tab (default: background -- co-working etiquette).",
)
@TIMEOUT_OPTION
def tab_open(device: str, url: str, active: bool, timeout: float | None) -> None:
    """Open a new tab on a device. Target is device-only; no tab exists yet to address."""
    _run_command(device, "tab_open", {"url": url, "active": active}, timeout=timeout)


@main.group(name="kill-switch")
def kill_switch() -> None:
    """Hub-level stop-all (docs/POLICY.md section 5): halts new dispatch and
    rejects every queued command immediately. Does NOT recall a command
    already in flight to a device -- see `Hub.engage_kill_switch`'s docstring.

    A4 fix (security review finding): README's consent table always listed
    the kill switch as an available control, but no operator running the
    shipped CLI, and no agent over the wire protocol, had any way to reach
    it -- it was reachable only by an embedding app calling
    `Hub.engage_kill_switch()` directly in-process. These subcommands are
    the fix: same `/agent` route, same token check, as every other command.
    """


@kill_switch.command(name="engage")
def kill_switch_engage() -> None:
    """Halt all future dispatch and reject every currently-queued command."""
    try:
        result = asyncio.run(_client().kill_switch_engage())
    except HubError as e:
        raise click.ClickException(str(e)) from e
    _print(result)


@kill_switch.command(name="disengage")
def kill_switch_disengage() -> None:
    """Restore normal dispatch after `kill-switch engage`."""
    try:
        result = asyncio.run(_client().kill_switch_disengage())
    except HubError as e:
        raise click.ClickException(str(e)) from e
    _print(result)


@kill_switch.command(name="status")
def kill_switch_status() -> None:
    """Report whether the kill switch is currently engaged, without changing it."""
    try:
        result = asyncio.run(_client().kill_switch_status())
    except HubError as e:
        raise click.ClickException(str(e)) from e
    _print(result)


@main.command()
@click.argument("confirmation_token")
def confirm(confirmation_token: str) -> None:
    """Redeem a confirmation token from a prior `needs_confirmation` response
    (docs/designs/confirmation-gate.md, D2).

    HONEST LABEL (docs/designs/approval-channel-options.md section 4,
    Candidate A): this is a HOST-LOCAL operator convenience, not a human-
    approval channel. It reaches the exact same hub route
    (`Hub._handle_agent_confirm`) that an agent's own `confirm` call reaches
    -- running this command is out-of-band with respect to the *protocol*,
    not with respect to the *host*. An agent with a shell on this machine can
    run `amplifier-browser-bridge confirm <token>` itself; this command grants no protection
    against that.

    It can ONLY redeem a confirmation whose session declared `redeem:
    "agent"` (the default). A confirmation whose session declared `redeem:
    "unredeemable"` is structurally refused here -- the hub enforces this at
    `PolicyEngine.consume_confirmation`, not merely by convention -- because
    there is no human-approval channel in this system TODAY, by deliberate
    current decision (a channel was considered and explicitly cancelled for
    now, after a live experiment showed the strongest candidate could be
    driven by the very agent it needed to exclude -- see docs/designs/
    approval-channel-options.md section 0 for the decision and what would
    reopen it). If you see that refusal, it is working as intended: this
    command is not, and must never be treated as, a substitute for real
    human-in-the-loop approval. There is no such substitute in this system
    right now -- if an action must not happen unattended, the way to prevent
    it today is to not grant it in the session's write scope
    (`amplifier-browser-bridge session-establish --write ...`), not to rely on a gate firing.
    """
    try:
        result = asyncio.run(_client().confirm(confirmation_token))
    except HubError as e:
        raise click.ClickException(str(e)) from e
    _print(result)


@main.command(name="tab-close")
@click.argument("target")
@TIMEOUT_OPTION
def tab_close(target: str, timeout: float | None) -> None:
    """Close a tab."""
    _run_command(target, "tab_close", {}, timeout=timeout)


@main.command(name="tab-activate")
@click.argument("target")
@TIMEOUT_OPTION
def tab_activate(target: str, timeout: float | None) -> None:
    """Bring a tab to the foreground. Use sparingly -- co-working etiquette favors
    acting on background tabs without stealing focus wherever a command allows it."""
    _run_command(target, "tab_activate", {}, timeout=timeout)


@main.command()
@click.argument("target")
@click.option(
    "--capture-hidden",
    is_flag=True,
    default=False,
    help=(
        "Capture a tab that is NOT the active tab of a focused window (auto-escalates to CDP; "
        "requires the debugger capability on this device). Without this, only the active tab "
        "of a focused window can be captured -- see docs/PROTOCOL.md's CDP section."
    ),
)
@click.option(
    "--frame-id",
    type=int,
    default=None,
    help=(
        "Crop the capture to a specific frame's on-screen region (from a prior read/snapshot's "
        "`frames` entries) rather than the whole tab -- e.g. a document viewer embedded in an "
        "iframe. Requires --capture-hidden."
    ),
)
@click.option(
    "--multi-page",
    is_flag=True,
    default=False,
    help=(
        "Scroll and capture repeatedly until the scrollable region's end is reached or "
        "--max-pages is hit -- for multi-page content (e.g. a document viewer) that doesn't "
        "fit in one viewport. Returns a `pages` array plus honest `capped`/`stopped_reason`."
    ),
)
@click.option(
    "--max-pages", type=int, default=None, help="Cap on pages for --multi-page (default 10, hard cap 50)."
)
@click.option(
    "--scroll-selector",
    default=None,
    help="CSS selector of the scrollable container to page through (default: the document itself).",
)
@click.option("--page-delay-ms", type=int, default=None, help="Settle delay between scroll and capture (ms).")
@click.option(
    "--out",
    "out_path",
    default=None,
    help=(
        "Save the captured image(s) to this path (PNG/JPEG bytes written as-is). For "
        "--multi-page, each page is written alongside with a -N suffix before the extension. "
        "Default: saves to a temp file and prints the path anyway -- screenshot bytes are never "
        "dumped raw to stdout."
    ),
)
@TIMEOUT_OPTION
def screenshot(
    target: str,
    capture_hidden: bool,
    frame_id: int | None,
    multi_page: bool,
    max_pages: int | None,
    scroll_selector: str | None,
    page_delay_ms: int | None,
    out_path: str | None,
    timeout: float | None,
) -> None:
    """Screenshot a tab -- returns pixels (base64 + a saved file path), no model call.

    Distinct from `vision-read`, which additionally calls a vision model to extract TEXT
    from the captured pixels -- this command never does that; it only ever returns the image.
    """
    args: dict[str, Any] = {}
    if capture_hidden:
        args["capture_hidden"] = True
    if frame_id is not None:
        args["frame_id"] = frame_id
    if multi_page:
        args["multi_page"] = True
        args["max_pages"] = max_pages if max_pages is not None else 10
    if scroll_selector is not None:
        args["scroll_selector"] = scroll_selector
    if page_delay_ms is not None:
        args["page_delay_ms"] = page_delay_ms

    try:
        parsed_target = parse_target(target)
    except TargetError as e:
        raise click.ClickException(str(e)) from e
    if timeout is not None:
        args = {**args, "timeout_s": timeout}
    try:
        result = asyncio.run(_client().command(parsed_target, "screenshot", args))
    except HubError as e:
        raise click.ClickException(str(e)) from e

    if result.get("ok") and isinstance(result.get("result"), dict):
        result["result"] = _save_screenshot_bytes(result["result"], out_path)
    _print(result)


def _save_screenshot_bytes(result: dict[str, Any], out_path: str | None) -> dict[str, Any]:
    """Decode `base64`/`pages[].base64` in a screenshot result and write the bytes to
    disk, replacing the (large, not worth printing raw) base64 field(s) with a
    `saved_path`/`pages[].saved_path` -- screenshot bytes are always written to a file,
    never dumped to stdout as a giant base64 blob."""
    stem = (
        Path(out_path)
        if out_path
        else Path(
            f"{os.environ.get('TMPDIR', '/tmp')}/amplifier-browser-bridge-screenshot-{int(time.time() * 1000)}"
        )
    )
    ext = ".jpg" if result.get("format", "jpeg") == "jpeg" else f".{result.get('format', 'jpeg')}"

    if "pages" in result:
        pages_out = []
        for page in result["pages"]:
            page_path = (
                stem.with_name(f"{stem.stem}-{page['index']}{ext}")
                if not out_path
                else Path(f"{out_path}-{page['index']}{ext}")
            )
            page_path.write_bytes(base64.b64decode(page["base64"]))
            pages_out.append(
                {k: v for k, v in page.items() if k != "base64"} | {"saved_path": str(page_path)}
            )
        return {**result, "pages": pages_out}

    if "base64" in result:
        path = stem if out_path else stem.with_suffix(ext)
        path.write_bytes(base64.b64decode(result["base64"]))
        return {k: v for k, v in result.items() if k != "base64"} | {"saved_path": str(path)}

    return result


@main.command(name="vision-read")
@click.argument("target")
@click.argument("prompt", required=False, default=None)
@click.option(
    "--frame-id",
    type=int,
    default=None,
    help="Crop the capture to a specific frame's on-screen region before extracting text.",
)
@click.option(
    "--multi-page", is_flag=True, default=False, help="Scroll and capture multiple pages before extracting."
)
@click.option(
    "--max-pages", type=int, default=None, help="Cap on pages for --multi-page (default 10, hard cap 50)."
)
@click.option(
    "--scroll-selector", default=None, help="CSS selector of the scrollable container to page through."
)
@click.option("--page-delay-ms", type=int, default=None, help="Settle delay between scroll and capture (ms).")
@click.option(
    "--capture-hidden/--no-capture-hidden",
    default=True,
    show_default=True,
    help="Capture a non-active tab via CDP (default: on -- this command exists specifically for that case).",
)
@TIMEOUT_OPTION
def vision_read_cmd(
    target: str,
    prompt: str | None,
    frame_id: int | None,
    multi_page: bool,
    max_pages: int | None,
    scroll_selector: str | None,
    page_delay_ms: int | None,
    capture_hidden: bool,
    timeout: float | None,
) -> None:
    """Capture pixels and extract TEXT from them via a vision-capable LLM (a real model call).

    Distinct from `screenshot` (pixels only, no model call) -- use this when the content you
    need was never in the DOM as text (e.g. a canvas-rendered document viewer) and you want
    text back, not an image. Requires a vision provider configured via environment variable
    (ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY, or AMPLIFIER_BROWSER_BRIDGE_VISION_PROVIDER to pin one) --
    fails loud with setup instructions if none is configured.
    """
    try:
        parsed_target = parse_target(target)
    except TargetError as e:
        raise click.ClickException(str(e)) from e
    try:
        result = asyncio.run(
            _vision_read(
                _client(),
                parsed_target,
                prompt=prompt,
                frame_id=frame_id,
                multi_page=multi_page,
                max_pages=max_pages,
                scroll_selector=scroll_selector,
                page_delay_ms=page_delay_ms,
                capture_hidden=capture_hidden,
                timeout_s=timeout,
            )
        )
    except (HubError, VisionConfigError, VisionError) as e:
        raise click.ClickException(str(e)) from e
    _print(result)


@main.command(name="wait-for")
@click.argument("target")
@click.argument("selector")
@click.option("--timeout-ms", default=10000, show_default=True)
@TIMEOUT_OPTION
def wait_for(target: str, selector: str, timeout_ms: int, timeout: float | None) -> None:
    """Poll (don't sleep) until a CSS selector matches, or time out.

    Note: --timeout-ms is the in-page polling deadline; --timeout (seconds) is the
    hub's device-round-trip wait and must be at least as long, or the hub will give
    up on the round trip before the page-side wait finishes -- see docs/PROTOCOL.md.
    """
    _run_command(target, "wait_for", {"selector": selector, "timeout_ms": timeout_ms}, timeout=timeout)


@main.command(name="wait-text")
@click.argument("target")
@click.argument("text")
@click.option("--timeout-ms", default=10000, show_default=True)
@TIMEOUT_OPTION
def wait_text(target: str, text: str, timeout_ms: int, timeout: float | None) -> None:
    """Poll (don't sleep) until visible text contains a substring, or time out.

    Note: --timeout-ms is the in-page polling deadline; --timeout (seconds) is the
    hub's device-round-trip wait and must be at least as long, or the hub will give
    up on the round trip before the page-side wait finishes -- see docs/PROTOCOL.md.
    """
    _run_command(target, "wait_text", {"text": text, "timeout_ms": timeout_ms}, timeout=timeout)


@main.command(name="cmd")
@click.argument("target")
@click.argument("command")
@click.option("--arg", "raw_args", multiple=True, help="key=value, repeatable")
@SESSION_OPTION
@TIMEOUT_OPTION
def cmd(
    target: str, command: str, raw_args: tuple[str, ...], session_id: str | None, timeout: float | None
) -> None:
    """Escape hatch: run any vocabulary command with free-form args."""
    if command not in COMMANDS:
        raise click.ClickException(f"unknown command: {command}. Valid: {sorted(COMMANDS)}")
    args: dict[str, Any] = {}
    for kv in raw_args:
        if "=" not in kv:
            raise click.ClickException(f"--arg must be key=value, got: {kv}")
        k, v = kv.split("=", 1)
        args[k] = v
    _run_command(target, command, args, timeout=timeout, session_id=session_id)


@main.command(name="session-establish")
@click.option("--read", default="*", show_default=True, help="Comma-separated read-scope hostnames, or '*'.")
@click.option(
    "--write",
    default="*",
    show_default=True,
    help=(
        "Comma-separated write-scope hostnames (subdomain-inclusive, e.g. 'github.com' also "
        "covers 'gist.github.com'), or '*' for unrestricted."
    ),
)
@click.option(
    "--on-unknown", type=click.Choice(["allow", "gate", "deny"]), default="allow", show_default=True
)
@click.option("--redeem", type=click.Choice(["agent", "unredeemable"]), default="agent", show_default=True)
@click.option("--unattended", is_flag=True, default=False)
@click.option(
    "--allow-self-attested-escalation",
    is_flag=True,
    default=False,
    help=(
        "FIX 3 (product review panel): by default, an action classified into a privilege/"
        "permission-escalation category (e.g. permission_change) is forced to "
        "redeem='unredeemable' regardless of write scope -- write scope alone never implies "
        "'and may self-attest its own escalations.' Pass this flag to opt back into the old "
        "self-attestable behavior for this session. Cannot be turned on later via "
        "session-narrow -- see docs/POLICY.md."
    ),
)
def session_establish(
    read: str,
    write: str,
    on_unknown: str,
    redeem: str,
    unattended: bool,
    allow_self_attested_escalation: bool,
) -> None:
    """Create a new session with a caller-declared write scope
    (docs/designs/confirmation-gate.md, Candidate C). Prints the new
    session_id -- pass it via --session on click/type/navigate/cmd to
    enforce this scope, or as the first argument to `amplifier-browser-bridge session-narrow` to
    shrink it further later.

    The hub ALWAYS mints a fresh session_id and never accepts one you
    supply -- this is what stops re-running this command from silently
    resetting an EXISTING session's scope back to broad. To change an
    existing session, use `amplifier-browser-bridge session-narrow` instead, which can only ever
    narrow, never widen.
    """
    read_scope = "*" if read == "*" else [o.strip() for o in read.split(",") if o.strip()]
    write_scope = "*" if write == "*" else [o.strip() for o in write.split(",") if o.strip()]
    try:
        result = asyncio.run(
            _client().establish_session(
                read=read_scope,
                write=write_scope,
                on_unknown=on_unknown,
                redeem=redeem,
                unattended=unattended,
            )
        )
    except HubError as e:
        raise click.ClickException(str(e)) from e
    _print(result)


@main.command(name="session-narrow")
@click.argument("session_id")
@click.option("--read", default=None, help="Comma-separated origins to narrow READ scope to.")
@click.option("--write", default=None, help="Comma-separated origins to narrow WRITE scope to.")
@click.option("--on-unknown", type=click.Choice(["allow", "gate", "deny"]), default=None)
@click.option("--redeem", type=click.Choice(["agent", "unredeemable"]), default=None)
@click.option(
    "--unattended", is_flag=True, default=False, help="Set unattended=true (one-way: False -> True only)."
)
@click.option(
    "--deny-self-attested-escalation",
    is_flag=True,
    default=False,
    help=(
        "Narrow allow_self_attested_escalation True -> False (one-way -- FIX 3, product "
        "review panel). It can never be turned back on for this session."
    ),
)
def session_narrow(
    session_id: str,
    read: str | None,
    write: str | None,
    on_unknown: str | None,
    redeem: str | None,
    unattended: bool,
    deny_self_attested_escalation: bool,
) -> None:
    """Narrow an EXISTING session's scope -- NEVER widens
    (docs/designs/confirmation-gate.md section 11.2): write/read may only
    shrink to a strict subset of the current grant, on_unknown may only
    move allow -> gate -> deny, redeem only agent -> unredeemable, unattended
    only False -> True. Only the options you pass are touched.

    Once the session has ingested any page content (a read/snapshot/tabs
    result), the hub SEALS it and every subsequent call to this command --
    including this one -- is rejected outright, no matter how narrow the
    request. This is the property that makes the scope page-immune: by the
    time a page-injected instruction could exist, the session that read it
    has already sealed.
    """
    kwargs: dict[str, Any] = {}
    if write is not None:
        kwargs["write"] = [o.strip() for o in write.split(",") if o.strip()]
    if read is not None:
        kwargs["read"] = [o.strip() for o in read.split(",") if o.strip()]
    if on_unknown is not None:
        kwargs["on_unknown"] = on_unknown
    if redeem is not None:
        kwargs["redeem"] = redeem
    if unattended:
        kwargs["unattended"] = True
    try:
        result = asyncio.run(_client().narrow_scope(session_id, **kwargs))
    except HubError as e:
        raise click.ClickException(str(e)) from e
    _print(result)


@main.command(name="policy-explain")
@click.argument("url_or_host")
def policy_explain(url_or_host: str) -> None:
    """Test a URL (or bare hostname) against the CURRENT denylist, without touching the hub.

    Answers "would this be hidden, and why?" directly -- see docs/POLICY.md. Loads the
    denylist the same way the hub does (AMPLIFIER_BROWSER_BRIDGE_POLICY_FILE / conventional path / built-in
    defaults), so this reflects exactly what a running hub would decide.
    """
    denylist = Denylist.load()
    host = host_of(url_or_host) or url_or_host.strip().lower()
    hit = denylist.match(host)
    if hit is None:
        _print({"input": url_or_host, "host": host, "hidden": False})
        return
    category, matched_domain = hit
    _print(
        {
            "input": url_or_host,
            "host": host,
            "hidden": True,
            "category": category,
            "matched_domain": matched_domain,
        }
    )


@main.command(name="policy-summary")
@click.option(
    "--audit-log",
    default=None,
    help="Path to the JSONL audit log (default: $AMPLIFIER_BROWSER_BRIDGE_AUDIT_LOG or ./amplifier-browser-bridge-audit.jsonl).",
)
def policy_summary(audit_log: str | None) -> None:
    """Summarize denylist activity from the audit log: counts by category and by matched
    rule (category:domain), without requiring the user to parse JSONL by hand (see
    docs/POLICY.md). Covers both `policy_tab_hidden` (tabs listing) and `policy_denied`
    (a command explicitly targeting a denylisted tab) events.
    """
    path = Path(
        audit_log
        or os.environ.get("AMPLIFIER_BROWSER_BRIDGE_AUDIT_LOG", "./amplifier-browser-bridge-audit.jsonl")
    ).expanduser()
    if not path.is_file():
        raise click.ClickException(f"audit log not found: {path}")

    by_category: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    hidden_count = 0
    denied_count = 0
    shown_despite_match_count = 0

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = rec.get("event")
        category = rec.get("category")
        matched_domain = rec.get("matched_domain")
        if event == "policy_tab_hidden":
            hidden_count += 1
        elif event == "policy_denied":
            denied_count += 1
        elif event == "policy_tab_shown_despite_match":
            shown_despite_match_count += 1
        else:
            continue
        if isinstance(category, str):
            by_category[category] += 1
        if isinstance(category, str) and isinstance(matched_domain, str):
            by_rule[f"{category}:{matched_domain}"] += 1

    _print(
        {
            "audit_log": str(path),
            "hidden_tab_events": hidden_count,
            "denied_command_events": denied_count,
            "shown_despite_match_events": shown_despite_match_count,
            "by_category": dict(by_category.most_common()),
            "by_rule": dict(by_rule.most_common()),
        }
    )


@main.command(name="gate-summary")
@click.option(
    "--audit-log",
    default=None,
    help="Path to the JSONL audit log (default: $AMPLIFIER_BROWSER_BRIDGE_AUDIT_LOG or ./amplifier-browser-bridge-audit.jsonl).",
)
def gate_summary(audit_log: str | None) -> None:
    """Summarize CONFIRMATION GATE activity from the audit log (FIX 4, product review panel).

    The panel's FAIL: "'a gate that fires often will be disabled' -- presented as the deciding
    rationale for cancelling Phase 6, with zero cited firing-rate or disablement data. ... That
    same rigor is never applied to the security outcome." This is the instrument: a cheap,
    read-only summary over the existing audit log (no metrics pipeline, no new storage), so the
    next person deciding whether the gate is too noisy or too quiet has a number instead of an
    aphorism.

    Reports: how often the gate fires overall and per category, how often a fired gate is
    redeemed vs. left to expire (abandoned) vs. refused via the wrong channel, how often the NEW
    escalation lock (FIX 3, docs/POLICY.md section 3.1) is what forced a gate unredeemable, and
    how often a command was denied outright by session write scope. Covers `policy_gated`,
    `policy_confirmed`, `policy_confirmation_expired`, `policy_confirmation_wrong_channel`, and
    `policy_scope_denied` events -- see audit.py's module docstring for the full event table.
    """
    path = Path(
        audit_log
        or os.environ.get("AMPLIFIER_BROWSER_BRIDGE_AUDIT_LOG", "./amplifier-browser-bridge-audit.jsonl")
    ).expanduser()
    if not path.is_file():
        raise click.ClickException(f"audit log not found: {path}")

    gated_by_category: Counter[str] = Counter()
    gated_total = 0
    escalation_locked_count = 0
    confirmed_count = 0
    expired_count = 0
    wrong_channel_count = 0
    scope_denied_count = 0
    unclassified_count = 0

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = rec.get("event")
        if event == "policy_gated":
            gated_total += 1
            category = rec.get("category")
            gated_by_category[category if isinstance(category, str) else "(none)"] += 1
            if rec.get("escalation_locked") is True:
                escalation_locked_count += 1
        elif event == "policy_confirmed":
            confirmed_count += 1
        elif event == "policy_confirmation_expired":
            expired_count += 1
        elif event == "policy_confirmation_wrong_channel":
            wrong_channel_count += 1
        elif event == "policy_scope_denied":
            scope_denied_count += 1
        elif event == "policy_unclassified":
            unclassified_count += 1

    # "Abandoned" = fired but neither confirmed nor yet counted as expired --
    # still-pending at the moment this summary was run (a token not yet past
    # its TTL). Not double-counted against expired_count, which only counts
    # tokens the hub has ALREADY swept.
    outstanding = gated_total - confirmed_count - expired_count - wrong_channel_count
    outstanding = max(outstanding, 0)

    _print(
        {
            "audit_log": str(path),
            "gate_fired_total": gated_total,
            "gate_fired_by_category": dict(gated_by_category.most_common()),
            "escalation_locked_count": escalation_locked_count,
            "redeemed_count": confirmed_count,
            "expired_unredeemed_count": expired_count,
            "wrong_channel_refused_count": wrong_channel_count,
            "outstanding_or_abandoned_count": outstanding,
            "scope_denied_count": scope_denied_count,
            "unclassified_count": unclassified_count,
        }
    )


@main.command(name="fetch-bytes")
@click.argument("device")
@click.argument("url")
@click.option("--max-bytes", type=int, default=None, help="Override the default byte-size cap (bytes).")
@TIMEOUT_OPTION
def fetch_bytes(device: str, url: str, max_bytes: int | None, timeout: float | None) -> None:
    """Fetch a URL from the EXTENSION's own context, credentials included -- rides the user's
    real authenticated session (cookies) for the target origin, no tab required. This is how
    you retrieve a linked file (.docx/.pdf/binary) using the user's existing login. Returns
    base64 bytes, content-type, and byte length; refuses (naming the limit) past --max-bytes.

    If the target blocks extension-context requests (some CDNs/hotlink protection check the
    request's Referer/Origin), try grab-image instead -- it fetches from the page's own
    script context and carries the page's real Referer.
    """
    args: dict[str, Any] = {"url": url}
    if max_bytes is not None:
        args["max_bytes"] = max_bytes
    _run_command(device, "fetch_bytes", args, timeout=timeout)


@main.command(name="grab-image")
@click.argument("target")
@click.argument("url")
@click.option("--max-bytes", type=int, default=None, help="Override the default byte-size cap (bytes).")
@TIMEOUT_OPTION
def grab_image(target: str, url: str, max_bytes: int | None, timeout: float | None) -> None:
    """Fetch a URL from the PAGE's own main-world script context (not the extension's) --
    the request carries the page's real Referer and cookie context, defeating hotlink
    protection an extension-context fetch (fetch-bytes) would trip. Requires a tab in TARGET
    (the page whose script context does the fetching). Returns base64 bytes, content-type,
    and byte length; refuses (naming the limit) past --max-bytes.
    """
    args: dict[str, Any] = {"url": url}
    if max_bytes is not None:
        args["max_bytes"] = max_bytes
    _run_command(target, "grab_image", args, timeout=timeout)


@main.command(name="downloads-list")
@click.argument("device")
@click.option("--limit", type=int, default=20, show_default=True)
@TIMEOUT_OPTION
def downloads_list(device: str, limit: int, timeout: float | None) -> None:
    """List recent downloads on a device (chrome.downloads.search), plus max_download_id --
    the highest download id chrome currently knows about. Call this BEFORE an action that
    triggers a native/indirect download (e.g. clicking a page's own Download control) and
    pass its max_download_id as wait-download's --since-id, so the new download is
    identified without ever mistaking one the human started themselves for the agent's own.
    """
    _run_command(device, "downloads_list", {"limit": limit}, timeout=timeout)


@main.command()
@click.argument("device")
@click.argument("url")
@click.option("--filename", default=None, help="Suggested filename for the download.")
@TIMEOUT_OPTION
def download(device: str, url: str, filename: str | None, timeout: float | None) -> None:
    """Trigger a download of a URL directly (chrome.downloads.download). Returns a
    download_id you already know precisely, since this command started the download itself
    -- pass it to wait-download's --download-id to poll for completion."""
    args: dict[str, Any] = {"url": url}
    if filename is not None:
        args["filename"] = filename
    _run_command(device, "download", args, timeout=timeout)


@main.command(name="wait-download")
@click.argument("device")
@click.option("--download-id", type=int, default=None, help="Wait for this specific download id to complete.")
@click.option(
    "--since-id",
    type=int,
    default=None,
    help=(
        "Baseline max_download_id (from downloads-list) -- wait for a NEW completed download "
        "with a higher id, so a download the human started themselves is never claimed."
    ),
)
@click.option("--pattern", default=None, help="Optional regex to match the completed download's filename.")
@click.option("--timeout-ms", type=int, default=30000, show_default=True)
@TIMEOUT_OPTION
def wait_download(
    device: str,
    download_id: int | None,
    since_id: int | None,
    pattern: str | None,
    timeout_ms: int,
    timeout: float | None,
) -> None:
    """Poll (don't sleep) for a completed download -- either a specific --download-id (from a
    prior `download` call), or a NEW download after --since-id (a baseline from
    downloads-list), optionally narrowed by --pattern. Exactly one of --download-id/--since-id
    is required.

    Note: --timeout-ms is the in-page-style polling deadline for the download itself;
    --timeout (seconds) is the hub's device-round-trip wait and must be at least as long, or
    the hub gives up on the round trip before the poll finishes -- see docs/PROTOCOL.md.
    """
    if download_id is None and since_id is None:
        raise click.ClickException("wait-download requires --download-id or --since-id")
    args: dict[str, Any] = {"timeout_ms": timeout_ms}
    if download_id is not None:
        args["download_id"] = download_id
    if since_id is not None:
        args["since_id"] = since_id
    if pattern is not None:
        args["pattern"] = pattern
    _run_command(device, "wait_download", args, timeout=timeout)


@main.command()
@click.argument("device")
def reload(device: str) -> None:
    """Reload the extension on a device (chrome.runtime.reload()).

    Self-service for unpacked-extension iteration: after updating the files under
    extension/ on the device's machine, this picks up the change without a manual
    click in edge://extensions. Note: the very first deployment of this command
    itself still requires one manual reload -- an extension has to already be
    running code that understands the `reload` command before it can reload itself
    into a version that understands it.
    """
    _run_command(device, "reload", {})


@main.command()
@click.option(
    "--ttl",
    type=int,
    default=600,
    show_default=True,
    help="Pairing code lifetime, in seconds, before it expires unredeemed (single-use regardless).",
)
def pair(ttl: int) -> None:
    """Mint a short-lived pairing code for a new browser device -- replaces hand-
    transcribing a raw hub URL + 32-hex token into the extension's options page
    (docs/PROTOCOL.md's Pairing section).

    Requires a hub already running and reachable at AMPLIFIER_BROWSER_BRIDGE_HUB_URL /
    --hub-url's default (start one with `amplifier-browser-bridge hub` or
    `amplifier-browser-bridge service install` first) -- this talks to it exactly
    like every other command, over the same token-authenticated /agent route, so
    it requires the same AMPLIFIER_BROWSER_BRIDGE_TOKEN this CLI already needs for
    `devices`/`doctor`/etc.
    """
    try:
        result = asyncio.run(_client().create_pairing(ttl_seconds=float(ttl)))
    except HubError as e:
        raise click.ClickException(str(e)) from e
    except (OSError, TimeoutError) as e:
        # Unlike a rejected token (HubError, above -- the hub answered, just said
        # no), this is "nothing answered at all" -- name that distinction rather
        # than letting a raw connection exception escape as an unhandled traceback.
        raise click.ClickException(
            f"could not reach hub at {DEFAULT_HUB_URL}: {e}. Is `amplifier-browser-bridge hub` "
            "(or the service) running?"
        ) from e
    if not result.get("ok"):
        raise click.ClickException(result.get("error") or "pairing request failed")

    ticket = result["ticket"]
    host_port = urlsplit(DEFAULT_HUB_URL).netloc  # same host:port this command just talked to over /agent
    host, _, port_str = host_port.rpartition(":")
    code = f"{ticket}@{host_port}"
    expires_at = time.time() + ttl

    # Lead with the LINK, not the bare code (originality-critic / coherence-guardian
    # review finding): the fragment-carrying link is the one idea that could not have
    # come from a template -- it already carries this code, so following it never
    # sends anyone back to a terminal for anything. It works whether or not the
    # extension is installed yet. The bare code below remains for the one case the
    # link can't cover on its own: pasting directly into an ALREADY-OPEN Settings page.
    #
    # Auto-copies to the terminal's clipboard via OSC 52 (works over SSH -- see
    # clipboard.py) before printing either form, so following the link or pasting
    # the code never requires selecting terminal text by hand -- maintainer
    # feedback: "if we HAVE to copy and paste ... can't we auto put it into the
    # user's clipboard". The plain-text printout right below is the fallback for
    # any terminal that doesn't support OSC 52.
    have_link = bool(host and port_str.isdigit())
    if have_link:
        link = _setup_pair_url(host, int(port_str), code, expires_at=expires_at)
        copied = copy_to_clipboard(link)
        expires_str = time.strftime("%H:%M:%S", time.localtime(expires_at))
        suffix = ", copied to clipboard" if copied else ""
        click.echo(f"Open on the browser being paired (valid {ttl}s, expires {expires_str}{suffix}):")
        click.echo("")
        click.echo(f"    {link}")
        click.echo("")
    click.echo('Settings already open? Paste this code under "Enter a code by hand" -> Pair:')
    click.echo("")
    if not have_link:
        copy_to_clipboard(code)
    click.echo(f"    {code}")
    click.echo("")
    if not result.get("persisted", True):
        click.echo("")
        click.echo(
            "NOTE: this hub has no token file to persist a minted device token into -- the "
            "token this code produces will only live in this hub process's memory and will "
            "need to be re-paired after the next hub restart."
        )


def _warn_divergent_token_siblings(active_token_file: Path, active_token: str) -> None:
    """Print a loud warning if a file that looks like ANOTHER token store sits next
    to the one `amplifier-browser-bridge init` just wrote/reused, holding a different value.

    This is the concrete failure mode that made "Reusing existing hub token" a lie
    in practice: the message was true of the file `amplifier-browser-bridge init` actually uses, while an
    unrelated file (hand-created, or left over from before this project settled on
    `tokens.json`) sat right beside it, never consulted, holding a token a user might
    reasonably have pasted into the extension instead of the real one. Surfacing
    this HERE -- at the point of first friction -- catches it before a confusing
    auth failure three commands later (`amplifier-browser-bridge doctor` repeats this same check).
    """
    divergent = [
        (path, value)
        for path in find_sibling_token_files(active_token_file)
        for value in [extract_token_value(path)]
        if value is not None and value != active_token
    ]
    if not divergent:
        return
    click.echo("")
    click.echo(
        "WARNING: found other token-like file(s) that amplifier-browser-bridge does NOT read, holding a "
        "DIFFERENT value than the token above:"
    )
    for path, value in divergent:
        click.echo(f"  {path}  (starts {mask_token(value)})")
    click.echo(
        f"Only the token from {active_token_file} (printed below) is valid -- pasting one of "
        "the files above into the extension's options page will make the hub reject it."
    )


_HUB_HOST_HELP = (
    "Host to bind/print for the hub. Default: auto-detect this machine's Tailscale IP "
    "(`tailscale ip -4`) -- reachable from the tailnet, reachable from nowhere else. Falls "
    "back to 127.0.0.1 (loopback only -- NOT reachable from another device) if Tailscale "
    "isn't detected. Passing a wildcard address (0.0.0.0, ::) binds every network interface "
    "this machine has, not just the tailnet -- printed with a loud, specific warning if you "
    "do (security review finding: this used to be the silent default)."
)


def _resolve_hub_host(explicit_host: str | None) -> tuple[str, str | None]:
    """Resolve the host to bind/print for the hub, and an optional human-readable note
    explaining how it was resolved.

    Shared by `init` and `service install` so the two can never silently disagree on
    what "the safe, still cross-device-reachable default" means (A1 fix, security
    review finding -- see `init`'s original comment for the incident this replaced):

    \b
    1. an explicit host always wins (loudly warned by the caller if it's a wildcard)
    2. else, auto-detect this machine's own Tailscale IP (netinfo.py) -- reachable
       from the tailnet, reachable from nowhere else
    3. else, fall back to 127.0.0.1 (safe, but NOT cross-device -- say so loudly,
       since cross-device operation is this project's whole point)
    """
    if explicit_host is not None:
        return explicit_host, None
    tailnet_ip = detect_tailscale_ip()
    if tailnet_ip is not None:
        return tailnet_ip, f"(auto-detected this machine's Tailscale IP via `tailscale ip -4`: {tailnet_ip})"
    return (
        "127.0.0.1",
        (
            "(could not detect a Tailscale IP -- `tailscale ip -4` is unavailable or failed -- "
            "defaulting to 127.0.0.1, which is NOT reachable from another device; for cross-device "
            "use, re-run with --host <this machine's tailnet IP>)"
        ),
    )


def _stdin_is_interactive() -> bool:
    """True if both stdin and stdout are attached to a real terminal.

    Factored into its own function (rather than inlined `sys.stdin.isatty()`) so
    tests can monkeypatch it: `click.testing.CliRunner` always wraps stdin/stdout in
    non-tty streams, so without this seam the interactive branch of `init` below
    would be unreachable from any test.
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        # Some non-standard stdin/stdout replacements (frozen apps, certain
        # subprocess pipes) raise instead of returning False -- treat that the
        # same as "not a terminal" rather than letting init crash over it.
        return False


def _resolve_interactivity(*, yes: bool, non_interactive: bool) -> bool:
    """Whether `init` should run its guided, prompting flow.

    \b
    - `--non-interactive` always wins: never prompt, never take the new
      side-effecting actions (service install) -- exactly the pre-existing,
      print-only `init` behavior. This is also what a caller gets automatically
      whenever stdin/stdout aren't a real terminal (CI, scripts, digital twins,
      an editor's integrated terminal running this non-interactively) -- an
      interactive prompt with nothing attached to answer it would otherwise hang
      forever, which is a regression `init` must never introduce.
    - `--yes` forces the guided flow to run even without a real terminal (for a
      script that explicitly wants the automation, e.g. "install the service for
      me"), but never blocks on a prompt -- see `init`'s docstring for exactly
      what it automates vs. what it still prints as a manual step.
    - Otherwise: guided flow if and only if this looks like a real terminal.
    """
    if non_interactive:
        return False
    return yes or _stdin_is_interactive()


def _setup_url(host: str, port: int) -> str:
    """The onboarding page URL -- see hub.py's `GET /setup` route
    (onboarding.py). Single home for this string so every printed
    instruction (`init`'s guided and non-interactive flows, `pair`) can never
    drift apart on its shape the way three separate "select: <path>" print
    sites already had before this fix."""
    return f"http://{host}:{port}/setup"


def _setup_pair_url(host: str, port: int, code: str, *, expires_at: float | None = None) -> str:
    """The onboarding page URL with a pairing code embedded in the URL
    FRAGMENT (`#pair=...`), never a query parameter -- browsers never send a
    URL fragment to a server, so this code never touches the hub's own
    access logs or a Referer header the way a query string would. See
    onboarding.py's module docstring and hub.py's "Onboarding" section for
    the full reasoning.

    `expires_at` (a Unix-epoch-seconds float, optional) is carried alongside
    the code as `&exp=<int>` in that SAME fragment -- same never-sent-to-
    server property applies -- so the setup page can render a live countdown
    (human-advocate review finding: the ticket's real, short TTL had no
    visible countdown anywhere). Omitted entirely when not given, so an older
    caller/link shape is unaffected."""
    url = f"{_setup_url(host, port)}#pair={code}"
    if expires_at is not None:
        url += f"&exp={int(expires_at)}"
    return url


def _print_remaining_steps(
    *,
    token_result: TokenResult,
    staged_dir: Path,
    resolved_host: str,
    hub_port: int,
    detected_note: str | None,
) -> None:
    """The full manual-steps block -- service/foreground hub, load extension,
    pair (or configure manually), confirm with doctor.

    This is the SINGLE place this text is written. Every caller that needs to
    hand the user "the exact remaining steps" -- the classic non-interactive
    `init`, and every bail-out point in the guided flow below (declined the
    service offer, service unsupported on this platform, user stops partway
    through pairing) -- calls this same function, so the printed hub/doctor
    commands can never drift out of agreement with each other the way they did
    before the A1 fix (see `_resolve_hub_host`'s docstring for that incident).
    """
    click.echo("Remaining steps (manual -- Edge has no CLI for these):")
    click.echo("")
    click.echo("  1. Start the hub as a background service (recommended -- survives logout and reboot):")
    click.echo(f"       amplifier-browser-bridge service install --host {resolved_host} --port {hub_port}")
    if detected_note:
        click.echo(f"       {detected_note}")
    click.echo("")
    click.echo("     Or run it directly in this terminal instead (stops when the terminal closes):")
    click.echo(
        f"       AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE={token_result.token_file} amplifier-browser-bridge hub --host {resolved_host} --port {hub_port}"
    )
    click.echo("")
    click.echo("  2. Load the extension:")
    click.echo("     On the browser being paired (any device on your tailnet -- open this URL")
    click.echo("     THERE, not necessarily on this machine), once the hub from step 1 is running:")
    click.echo(f"       http://{resolved_host}:{hub_port}/setup")
    click.echo("     That page downloads the extension, and walks through unzipping it and")
    click.echo("     'Load unpacked' -- Edge has no CLI for that step, on any platform.")
    click.echo("")
    click.echo("     If Edge is on THIS SAME machine, you can skip the download and point")
    click.echo("     'Load unpacked' straight at the already-unzipped staged copy instead:")
    click.echo(f"       edge://extensions -> enable Developer mode -> Load unpacked -> select: {staged_dir}")
    click.echo("")
    click.echo("  3. Configure it -- once the hub from step 1 is running, PAIR it (recommended,")
    click.echo("     no hub URL or token to copy by hand):")
    click.echo(
        f"       AMPLIFIER_BROWSER_BRIDGE_TOKEN={token_result.token} "
        f"AMPLIFIER_BROWSER_BRIDGE_HUB_URL=ws://{resolved_host}:{hub_port}/agent amplifier-browser-bridge pair"
    )
    click.echo("       Click the extension's toolbar icon to open its Settings page, enter the")
    click.echo('       printed code under "Pair with a hub", and click Pair.')
    click.echo("")
    click.echo("     Or configure it manually instead:")
    click.echo('       Open Settings -> "Manual configuration (advanced)" ->')
    click.echo(f"       Hub URL: ws://{resolved_host}:{hub_port}/device")
    click.echo(f"       Token:   {token_result.token}")
    click.echo("       Click Save.")
    click.echo("")
    click.echo("     The hub and this token exist so the agent reaches your browser over your own ")
    click.echo('     network -- no relay server in the path. See README\'s "Why the setup is this long".')
    click.echo("")
    click.echo("  4. Confirm it worked:")
    click.echo(
        f"       AMPLIFIER_BROWSER_BRIDGE_TOKEN={token_result.token} amplifier-browser-bridge doctor "
        f"--hub-url ws://{resolved_host}:{hub_port}/agent"
    )


# How long to wait for a just-installed service's hub to actually accept
# connections before giving up and reporting a partial-failure state. This
# project's hub is a small aiohttp app with no slow startup work (no DB
# migrations, no model loading) -- real installs bind well under a second: 8s
# is generous headroom, not a number chosen to paper over a slow start.
_SERVICE_READY_TIMEOUT_S = 8.0
_SERVICE_READY_POLL_S = 0.25


def _wait_for_hub_reachable(host: str, port: int, token: str | None, *, timeout: float | None = None) -> bool:
    """Poll (never sleep-and-hope) until the hub answers on its `/agent` route
    with the given token, or `timeout` elapses. Used right after `service
    install` to turn "the unit file was written" into "the hub is actually
    reachable" -- these are not the same fact, and `service install`'s own
    `Restart=on-failure` retry loop means a bad bind fails LOUDLY over time
    rather than instantly, so a single immediate check would be too eager to
    report a false failure."""
    deadline = time.monotonic() + (timeout if timeout is not None else _SERVICE_READY_TIMEOUT_S)
    url = f"ws://{host}:{port}/agent"
    while True:
        try:
            asyncio.run(HubClient(url, token=token).list_devices())
            return True
        except (HubError, OSError, TimeoutError):
            if time.monotonic() >= deadline:
                return False
            time.sleep(_SERVICE_READY_POLL_S)


def _print_doctor_checks(checks: list[DoctorCheck]) -> bool:
    """Print each check's icon/name/message (plus any indented `detail`); return
    True iff any check failed.

    Shared by the standalone `doctor` command and `init`'s guided flow's final
    confirmation step, so the two can never print doctor output in two
    different shapes.
    """
    any_failed = False
    for check in checks:
        icon = {"ok": "[ok]  ", "fail": "[FAIL]", "skipped": "[skip]"}[check.status]
        click.echo(f"{icon} {check.name}: {check.message}")
        if check.detail:
            # Indented, on its own line(s) -- see DoctorCheck's docstring: this is
            # the same real information as before, laid out so the headline above
            # stays a single skimmable clause instead of a 700-character wall.
            for line in check.detail.splitlines():
                click.echo(f"         {line}")
        if check.status == "fail":
            any_failed = True
    return any_failed


# ---------------------------------------------------------------------------
# Onboarding audit log -- local-only instrumentation for `init`'s auto-advance
# watch (see `_watch_for_device_connection` below).
#
# Judgment call, stated plainly: the councils asked for confirmation/abandonment
# counters "wired at ship, not deferred." This project is a privacy-focused local
# tool with zero telemetry and an existing local, human-readable JSONL audit log
# (audit.py) as its one established observability mechanism. Recording onboarding
# outcomes (auto-detected vs. timed-out-and-fell-back vs. abandoned) to THAT SAME
# kind of local, append-only file satisfies the councils' intent -- these numbers
# exist somewhere, inspectable, the moment this ships -- without adding anything
# that leaves the machine. A phone-home metrics pipeline would contradict this
# project's entire premise (own network, no third-party relay -- see README's
# "Why the setup is this long"); a local file does not. If this project ever
# wants aggregate cross-user numbers, that is a deliberate, separately-reviewed
# decision to make later -- not something to back into via onboarding
# instrumentation.
# ---------------------------------------------------------------------------


def _onboarding_audit_log(token_file: Path) -> AuditLog:
    """The onboarding-specific audit log, sitting beside the token file (the one
    path `init` always knows, regardless of whether the user chose a background
    service or a foreground hub, or hasn't started the hub's OWN audit log yet).
    Deliberately a separate file from the hub's own `hub-audit.jsonl`/
    `amplifier-browser-bridge-audit.jsonl` -- `init` runs as its own short-lived CLI
    process, never as (or alongside) a running `Hub` instance, so there is no
    single shared `AuditLog` object to reuse here."""
    return AuditLog(token_file.parent / "onboarding-audit.jsonl")


# How long `init`'s guided flow watches for the paired browser to actually
# connect before giving up on auto-detection and falling back to a manual
# confirm (see `_watch_for_device_connection`). 4 minutes is comfortably inside
# the pairing ticket's own 10-minute TTL (pairing.py's DEFAULT_TICKET_TTL_SECONDS)
# while still being an honest, bounded wait rather than an unbounded hang -- see
# this function's docstring for the "visible waiting state, timeout fallback"
# requirement this exists to satisfy.
_DEVICE_WATCH_TIMEOUT_S = 240.0
_DEVICE_WATCH_POLL_S = 2.0
_DEVICE_WATCH_HEARTBEAT_S = 15.0  # how often to print a "still waiting" line


def _watch_for_device_connection(
    host: str,
    port: int,
    token: str | None,
    *,
    ttl_seconds: float,
    timeout: float | None = None,
    poll: float | None = None,
    heartbeat: float | None = None,
) -> dict[str, Any] | None:
    """Replace a manual "did you finish pairing? [Y/n]" prompt with the hub's own
    observation of the event it's actually waiting on: the browser connecting.

    Maintainer finding this fixes: "init should complete automatically after the
    server install decision is made ... if you want to have it hold off on some
    actions until a browser is connected, then it should watch for that and then
    automatically continue and resolve w/o user interaction." Product/design
    council condition on this fix: a VISIBLE waiting state (never a silent hang)
    and a TIMEOUT FALLBACK to manual confirmation if the event never arrives
    (never a silent jump-cut) -- both are implemented here, not left implicit.

    Polls `list_devices()` (same mechanism `_wait_for_hub_reachable` already uses
    for the service-install step, and `doctor`'s own `device_connected` check) --
    never a bare `sleep()` hoping something changed (this project's own
    "poll, don't sleep" convention, CONTRIBUTING.md).

    Returns the first LIVE device's dict (registry.py's `DeviceRecord.to_dict()`
    shape) the moment one appears, or `None` if `timeout` elapses first with none
    live -- `None` is the honest "nothing arrived," never confused with an
    exception or a crash.
    """
    timeout_s = timeout if timeout is not None else _DEVICE_WATCH_TIMEOUT_S
    poll_s = poll if poll is not None else _DEVICE_WATCH_POLL_S
    heartbeat_s = heartbeat if heartbeat is not None else _DEVICE_WATCH_HEARTBEAT_S

    start = time.monotonic()
    deadline = start + timeout_s
    last_heartbeat = start
    client = HubClient(f"ws://{host}:{port}/agent", token=token)

    remaining_ttl = max(0, int(ttl_seconds))
    click.echo(
        f"  Waiting for the browser to connect... (checking every {poll_s:.0f}s; code "
        f"expires in ~{remaining_ttl // 60}m{remaining_ttl % 60:02d}s; will ask after "
        f"{int(timeout_s // 60)}m{int(timeout_s % 60):02d}s if nothing connects)"
    )
    while True:
        try:
            devices = asyncio.run(client.list_devices())
        except (HubError, OSError, TimeoutError):
            devices = []
        live = [d for d in devices if d.get("tier") == "live"]
        if live:
            return live[-1]  # most-recently-registered live device -- the one just paired

        now = time.monotonic()
        if now >= deadline:
            return None
        if now - last_heartbeat >= heartbeat_s:
            elapsed = int(now - start)
            remaining_wait = int(deadline - now)
            click.echo(
                f"    ...still waiting ({elapsed}s elapsed; auto-detect gives up in {remaining_wait}s)"
            )
            last_heartbeat = now
        time.sleep(poll_s)


def _offer_service_install(
    *,
    resolved_host: str,
    hub_port: int,
    token_result: TokenResult,
    detected_note: str | None,
    yes: bool,
) -> bool:
    """Guided-flow step 1: offer to install and start the hub as a background
    service, verify it actually came up, and report which happened.

    Returns True iff the hub is confirmed reachable at the end (the caller may
    then continue the guided flow into pairing). Returns False for an honest
    "not now" -- the user declined, or this platform has no service support at
    all (`service.py`'s deliberate, explicit Windows/no-systemctl gap) -- in
    which case the caller falls back to `_print_remaining_steps` and stops;
    False is never returned silently. A service that DID install but never
    becomes reachable is not a "not now" -- it's a real failure -- so that case
    raises `click.ClickException` instead of returning False (see below).
    """
    service_info = describe_service()
    install_it = service_info.supported
    if install_it and not yes:
        click.echo("")
        click.echo("  1. Start the hub as a background service (recommended -- survives logout and reboot).")
        if detected_note:
            click.echo(f"     {detected_note}")
        install_it = click.confirm(
            f"     Install and start it now? (amplifier-browser-bridge service install "
            f"--host {resolved_host} --port {hub_port})",
            default=True,
        )

    if not install_it:
        click.echo("")
        if not service_info.supported:
            click.echo(f"     {service_info.detail}")
        return False

    try:
        info = service_install(resolved_host, hub_port, token_result.token_file)
    except (ServiceUnsupportedError, OSError) as e:
        raise click.ClickException(
            f"could not install the hub service: {e}\n"
            "  Your token and staged extension above are unaffected -- run the hub directly instead:\n"
            f"    AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE={token_result.token_file} amplifier-browser-bridge "
            f"hub --host {resolved_host} --port {hub_port}"
        ) from e

    click.echo(f"     Installed and started the {SERVICE_NAME} service ({info.platform}).")

    if not _wait_for_hub_reachable(resolved_host, hub_port, token_result.token):
        raise click.ClickException(
            f"the {SERVICE_NAME} service installed but never became reachable at "
            f"ws://{resolved_host}:{hub_port}/agent within {_SERVICE_READY_TIMEOUT_S:.0f}s.\n"
            "  Your token and staged extension from above are still valid -- this is a partial "
            "setup, not a lost one. Check what's wrong with:\n"
            "    amplifier-browser-bridge service status\n"
            "    amplifier-browser-bridge service logs\n"
            "  Then, once fixed, get a pairing code with:\n"
            f"    AMPLIFIER_BROWSER_BRIDGE_TOKEN={token_result.token} "
            f"AMPLIFIER_BROWSER_BRIDGE_HUB_URL=ws://{resolved_host}:{hub_port}/agent amplifier-browser-bridge pair"
        )
    click.echo(f"     Confirmed: hub reachable at ws://{resolved_host}:{hub_port}/agent")
    return True


@main.command()
@click.option(
    "--dest",
    default=None,
    help=f"Directory to stage the extension into (default: {DEFAULT_STAGE_DIR}). Stable across "
    "re-runs -- an unpacked extension's identity (and its chrome.storage.local config) is tied "
    "to this exact path, so re-running `amplifier-browser-bridge init` after a `git pull` overwrites the JS/HTML/"
    "manifest files here WITHOUT disturbing a previously-configured token/hub-url.",
)
@click.option("--token-file", default=None, help="Path to the hub token file (see auth.py docstring).")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Regenerate the token even if one already exists at --token-file. Rotating a token "
    "requires re-pasting it into the extension's options page afterward.",
)
@click.option("--hub-host", default=None, help=_HUB_HOST_HELP)
@click.option(
    "--hub-port",
    default=DEFAULT_PORT,
    show_default=True,
    help="Port to print in the printed `amplifier-browser-bridge hub`/`service install` commands.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Take the recommended action at each guided step without asking (installs the hub "
    "service; never blocks waiting for you to load the extension into Edge -- prints that and "
    "the pairing command as a manual step instead, since there's no one here to say when you're "
    "ready). Also forces the guided flow to run even without a real terminal, for a script that "
    "explicitly wants the service pre-installed.",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    default=False,
    help="Never prompt or install the service automatically -- just print the remaining manual "
    "steps, exactly like `init` did before this flag existed. This is also the automatic "
    "behavior whenever stdin/stdout aren't a real terminal (CI, scripts, digital twins); pass "
    "this explicitly only to force that behavior from an interactive shell.",
)
def init(
    dest: str | None,
    token_file: str | None,
    force: bool,
    hub_host: str | None,
    hub_port: int,
    yes: bool,
    non_interactive: bool,
) -> None:
    """First-run setup: generate a hub token, stage the extension, then get you to a
    redeemable pairing code -- interactively, when run from a real terminal.

    Always does two things first, each idempotent (safe to re-run, e.g. after `git pull`):

    \b
    1. Ensures a hub token exists (generates one on first run; reuses it on later
       runs unless --force).
    2. Stages the extension's runtime files into a stable directory (default under
       ~/.local/share) -- NOT this repo checkout, so re-running after an update
       never changes the path an already-loaded extension was loaded from, which is
       what lets its saved chrome.storage.local config survive the update.

    From a real terminal (or with --yes), it then walks you through the rest: offers
    to install and start the hub as a background service (declining leaves you the
    exact foreground command instead -- nothing is silently skipped), confirms the
    hub actually came up, mints a pairing code, and hands you ONE link that already
    carries it -- open it on the browser you're adding and it downloads the
    extension, walks through "Load unpacked", and has the code ready to paste. From
    there it WATCHES for that browser to actually connect and continues on its own
    the moment it does -- no "did you finish yet?" prompt to answer. If nothing
    connects within a few minutes it falls back to asking, rather than waiting
    forever; if the code expires anyway, `amplifier-browser-bridge pair` mints a
    fresh one any time.

    Piped, scripted, or run without a terminal attached (CI, a digital twin, --non-
    interactive), it never prompts and never installs anything beyond the token and
    the staged extension -- it just prints the exact remaining manual steps, same as
    every earlier release. Loading an unpacked extension in edge://extensions IS a
    manual step regardless of mode -- Edge has no CLI/API for it, and this command
    never pretends otherwise.
    """
    try:
        token_result = ensure_token_file(token_file, force=force)
    except OSError as e:
        raise click.ClickException(f"could not write token file: {e}") from e

    try:
        staged_dir = stage_extension(dest)
    except (ExtensionSourceNotFoundError, ExtensionIntegrityError) as e:
        raise click.ClickException(str(e)) from e

    action = "Generated new" if token_result.created_new else "Reusing existing"
    click.echo(f"{action} hub token (stored in {token_result.token_file}).")
    click.echo(f"Staged extension -> {staged_dir}")

    _warn_divergent_token_siblings(token_result.token_file, token_result.token)

    resolved_host, detected_note = _resolve_hub_host(hub_host)
    if is_wildcard_bind(resolved_host):
        click.echo("")
        click.echo(wildcard_bind_warning(resolved_host, hub_port))

    click.echo("")

    if not _resolve_interactivity(yes=yes, non_interactive=non_interactive):
        _print_remaining_steps(
            token_result=token_result,
            staged_dir=staged_dir,
            resolved_host=resolved_host,
            hub_port=hub_port,
            detected_note=detected_note,
        )
        return

    hub_ready = _offer_service_install(
        resolved_host=resolved_host,
        hub_port=hub_port,
        token_result=token_result,
        detected_note=detected_note,
        yes=yes,
    )
    if not hub_ready:
        _print_remaining_steps(
            token_result=token_result,
            staged_dir=staged_dir,
            resolved_host=resolved_host,
            hub_port=hub_port,
            detected_note=detected_note,
        )
        return

    if yes:
        # --yes automates the service install but never blocks on anything past
        # it (documented behavior, unchanged by this fix -- see the flag's own
        # help text): a script that asked for --yes gets the exact manual steps
        # printed instead of a live wait, since there's nothing here to watch
        # FOR in an unattended run.
        click.echo("")
        click.echo("  2. Load the extension -- open on the browser being paired:")
        click.echo(f"       {_setup_url(resolved_host, hub_port)}")
        click.echo(f"     (same machine? edge://extensions -> Load unpacked -> {staged_dir})")
        click.echo("")
        click.echo("  3. Get a pairing code when ready (fresh each time, so it can't expire on you):")
        click.echo(
            f"       AMPLIFIER_BROWSER_BRIDGE_TOKEN={token_result.token} "
            f"AMPLIFIER_BROWSER_BRIDGE_HUB_URL=ws://{resolved_host}:{hub_port}/agent amplifier-browser-bridge pair"
        )
        click.echo("     The extension should pair itself; otherwise Settings -> Pair.")
        click.echo("")
        click.echo("  4. Confirm:")
        click.echo(
            f"       AMPLIFIER_BROWSER_BRIDGE_TOKEN={token_result.token} amplifier-browser-bridge doctor "
            f"--hub-url ws://{resolved_host}:{hub_port}/agent"
        )
        return

    # Real interactive terminal from here on. Mint the pairing code NOW -- not
    # lazily after a "loaded?" confirm -- so the ONE link handed to the user in
    # step 2 below already carries it. Real-run finding this fixes: sending the
    # user to a bare, code-less setup URL first, and only producing the code-
    # carrying link later (in a terminal they've already left to go load the
    # extension), is what made getting a pairing code "a hassle" -- it forced a
    # trip back to this terminal for something the page could have shown them
    # from the start. 4 minutes of watch-loop headroom below (well inside the
    # ticket's 10-minute TTL) means the code is almost never sitting idle for
    # long even though it's minted a little earlier than strictly necessary.
    try:
        pairing_result = asyncio.run(
            HubClient(f"ws://{resolved_host}:{hub_port}/agent", token=token_result.token).create_pairing(
                ttl_seconds=DEFAULT_TICKET_TTL_SECONDS
            )
        )
    except HubError as e:
        raise click.ClickException(str(e)) from e
    if not pairing_result.get("ok"):
        raise click.ClickException(pairing_result.get("error") or "pairing request failed")

    code = f"{pairing_result['ticket']}@{resolved_host}:{hub_port}"
    expires_at = time.time() + DEFAULT_TICKET_TTL_SECONDS

    # Auto-copies the link to the terminal's clipboard via OSC 52 (works over SSH --
    # see clipboard.py) before printing it -- maintainer feedback: "can't we auto
    # put it into the user's clipboard". The printout below is the fallback for any
    # terminal that doesn't support OSC 52.
    link = _setup_pair_url(resolved_host, hub_port, code, expires_at=expires_at)
    copied = copy_to_clipboard(link)
    expires_str = time.strftime("%H:%M:%S", time.localtime(expires_at))
    suffix = ", copied to clipboard" if copied else ""
    click.echo("")
    click.echo(f"  2. Open on the browser you're adding (expires {expires_str}{suffix}):")
    click.echo(f"       {link}")
    click.echo(f"     Same machine? edge://extensions -> Load unpacked -> {staged_dir}")
    click.echo("     Expired, or a different browser? amplifier-browser-bridge pair")

    # Replaces TWO separate [Y/n] prompts ("Loaded, and its Settings page is
    # open?" / "Entered the code and clicked Pair?") with observation of the
    # event those prompts existed to ask about: the hub sees the device
    # connect. See `_watch_for_device_connection`'s docstring for the visible-
    # waiting-state and timeout-fallback requirements this satisfies.
    audit = _onboarding_audit_log(token_result.token_file)
    audit.record("onboarding_watch_started", ttl_seconds=DEFAULT_TICKET_TTL_SECONDS)
    click.echo("")
    watch_start = time.monotonic()
    device = _watch_for_device_connection(
        resolved_host, hub_port, token_result.token, ttl_seconds=DEFAULT_TICKET_TTL_SECONDS
    )
    elapsed_s = round(time.monotonic() - watch_start, 1)

    if device is not None:
        audit.record(
            "onboarding_watch_device_observed", device_id=device.get("device_id"), elapsed_s=elapsed_s
        )
        click.echo(
            f"  Connected: device {device.get('device_id')} ({device.get('label', '?')}, "
            f"{device.get('platform', '?')}) -- continuing automatically."
        )
    else:
        audit.record("onboarding_watch_timeout", elapsed_s=elapsed_s)
        click.echo("")
        if not click.confirm("  Still there? Finished loading the extension and pairing it?", default=True):
            audit.record("onboarding_manual_fallback_declined")
            click.echo("")
            click.echo("No problem -- check any time with:")
            click.echo(
                f"  AMPLIFIER_BROWSER_BRIDGE_TOKEN={token_result.token} amplifier-browser-bridge doctor "
                f"--hub-url ws://{resolved_host}:{hub_port}/agent"
            )
            return
        audit.record("onboarding_manual_fallback_confirmed")

    click.echo("")
    click.echo("  3. Confirming...")
    checks = asyncio.run(
        run_doctor(f"ws://{resolved_host}:{hub_port}/agent", token_result.token, token_result.token_file)
    )
    any_failed = _print_doctor_checks(checks)
    if any_failed:
        raise click.ClickException("one or more checks failed -- see above.")
    click.echo("\nAll checks passed. Try: amplifier-browser-bridge devices")


@main.group(name="service")
def service_group() -> None:
    """Run the hub as a background OS service (systemd --user on Linux, launchd on
    macOS) so it survives logout and reboot instead of living in a terminal you have
    to keep open.

    Not implemented on Windows in this release -- see INSTALL.md's Windows section;
    run `amplifier-browser-bridge hub ...` directly there, or wrap it in your own
    Task Scheduler entry / NSSM service.
    """


@service_group.command(name="install")
@click.option("--host", default=None, help=_HUB_HOST_HELP)
@click.option("--port", default=DEFAULT_PORT, show_default=True, help="Port for the hub to bind.")
@click.option(
    "--token-file",
    default=None,
    help="Path to the hub token file (see auth.py docstring). Default: $AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE "
    "or the standard location `amplifier-browser-bridge init` writes to. If it doesn't exist yet, one is "
    "generated here (same as `init` would) so a service never starts with auth silently disabled just "
    "because `init` was skipped.",
)
@click.option(
    "--audit-log",
    default=None,
    help="Path to the JSONL audit log. Default: ~/.local/share/amplifier-browser-bridge/hub-audit.jsonl -- "
    "NOT the foreground hub's cwd-relative default, since a service has no meaningful current directory.",
)
@click.option(
    "--command-timeout",
    type=float,
    default=None,
    help="Default device-round-trip wait, in seconds (see `hub --command-timeout`).",
)
@click.option(
    "--android-artifact",
    default=None,
    help="Path to a pre-built Android CRX/`.bin` to serve at GET /setup/android-extension.bin "
    "(see `hub --android-artifact`'s help for the full reasoning). Baked into the service unit "
    "as an explicit argument, same as --host/--port/--token-file.",
)
def service_install_cmd(
    host: str | None,
    port: int,
    token_file: str | None,
    audit_log: str | None,
    command_timeout: float | None,
    android_artifact: str | None,
) -> None:
    """Install (or re-install) the hub as a background service and start it.

    Safe to re-run -- e.g. after this machine's Tailscale IP changes, re-run this
    (omit --host to auto-detect the new one, or pass it explicitly) to rebake and
    restart the service under the new address. Rotating the token's CONTENTS
    (`amplifier-browser-bridge init --force`) does NOT need this re-run -- `service
    restart` alone is enough, since only the token FILE PATH is baked in here, not
    its contents.
    """
    resolved_host, detected_note = _resolve_hub_host(host)
    if is_wildcard_bind(resolved_host):
        click.echo(wildcard_bind_warning(resolved_host, port), err=True)

    resolved_token_file = resolve_token_file(token_file)
    if not resolved_token_file.is_file():
        try:
            token_result = ensure_token_file(resolved_token_file)
        except OSError as e:
            raise click.ClickException(f"could not write token file: {e}") from e
        click.echo(f"Generated new hub token (stored in {token_result.token_file}).")

    try:
        info = service_install(
            resolved_host,
            port,
            resolved_token_file,
            audit_log=audit_log,
            command_timeout=command_timeout,
            android_artifact=android_artifact,
        )
    except ServiceUnsupportedError as e:
        raise click.ClickException(str(e)) from e

    click.echo(f"Installed and started the {SERVICE_NAME} service ({info.platform}).")
    if detected_note:
        click.echo(f"  {detected_note}")
    click.echo(f"  Unit: {info.unit_path}")
    click.echo(f"  Hub URL for the extension: ws://{resolved_host}:{port}/device")
    click.echo(f"  Token file: {resolved_token_file}")
    click.echo("")
    click.echo("Check it: amplifier-browser-bridge service status")
    click.echo(
        f"Confirm it worked: amplifier-browser-bridge doctor --hub-url ws://{resolved_host}:{port}/agent"
    )


@service_group.command(name="uninstall")
def service_uninstall_cmd() -> None:
    """Stop and remove the hub service for this user."""
    try:
        service_uninstall()
    except ServiceUnsupportedError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Removed the {SERVICE_NAME} service.")


@service_group.command(name="start")
def service_start_cmd() -> None:
    """Start the installed hub service."""
    try:
        service_start()
    except ServiceUnsupportedError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Started the {SERVICE_NAME} service.")


@service_group.command(name="stop")
def service_stop_cmd() -> None:
    """Stop the hub service without uninstalling it."""
    try:
        service_stop()
    except ServiceUnsupportedError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Stopped the {SERVICE_NAME} service.")


@service_group.command(name="restart")
def service_restart_cmd() -> None:
    """Restart the hub service -- e.g. after rotating the token file's contents."""
    try:
        service_restart()
    except ServiceUnsupportedError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Restarted the {SERVICE_NAME} service.")


@service_group.command(name="status")
def service_status_cmd() -> None:
    """Show whether the hub service is installed and running.

    Prints the service manager's own raw status output (`systemctl --user status` /
    `launchctl print`) so nothing is lost relative to running that command by hand.
    """
    info = describe_service()
    click.echo(f"platform: {info.platform}")
    click.echo(f"installed: {info.installed}")
    click.echo(f"active: {info.active}")
    click.echo(f"detail: {info.detail}")
    if not info.installed:
        return
    click.echo("")
    try:
        service_status()
    except ServiceUnsupportedError as e:
        raise click.ClickException(str(e)) from e


@service_group.command(name="logs")
def service_logs_cmd() -> None:
    """Stream or print the hub service's logs."""
    try:
        service_logs()
    except ServiceUnsupportedError as e:
        raise click.ClickException(str(e)) from e


@main.command()
@click.option(
    "--hub-url",
    default=None,
    help="Hub agent-route URL to check (default: $AMPLIFIER_BROWSER_BRIDGE_HUB_URL or ws://127.0.0.1:8900/agent).",
)
@click.option("--token", default=None, help="Token to check (default: $AMPLIFIER_BROWSER_BRIDGE_TOKEN).")
@click.option("--token-file", default=None, help="Path to the hub's token file, for the local-only checks.")
def doctor(hub_url: str | None, token: str | None, token_file: str | None) -> None:
    """Diagnose the setup chain: token file, hub reachability, token match, device connected.

    Each check reports ok/fail; once a check fails, checks that depend on it report
    'skipped' with a reason rather than a confusing second failure -- fix the first
    failure and re-run.
    """
    effective_url = hub_url or DEFAULT_HUB_URL
    effective_token = token if token is not None else DEFAULT_TOKEN
    checks = asyncio.run(run_doctor(effective_url, effective_token, token_file))

    any_failed = _print_doctor_checks(checks)

    if any_failed:
        raise click.ClickException("one or more checks failed -- see above.")
    click.echo("\nAll checks passed.")


@main.command()
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help=(
        "Interface to bind. Safe by default (loopback only -- reachable from THIS machine only, "
        "not the tailnet). For cross-device use, pass this machine's own Tailscale IP explicitly "
        "(see `tailscale ip -4`, or run `amplifier-browser-bridge init` for an auto-detected recommendation). "
        "Passing a wildcard address (0.0.0.0, ::) binds every network interface this machine has -- "
        "home Wi-Fi, hotel Wi-Fi, a corporate LAN, not just the tailnet -- and prints a loud warning "
        "naming that exposure (security review finding: this was previously the silent default)."
    ),
)
@click.option("--port", default=DEFAULT_PORT, show_default=True)
@click.option("--token-file", default=None, help="Path to a token JSON file (see auth.py docstring).")
@click.option(
    "--audit-log",
    default=None,
    help="Path to the JSONL audit log (default: ./amplifier-browser-bridge-audit.jsonl).",
)
@click.option(
    "--command-timeout",
    type=float,
    default=DEFAULT_COMMAND_TIMEOUT,
    show_default=True,
    help=(
        "Default device-round-trip wait, in seconds, for any command that doesn't override it "
        "with args.timeout_s / the CLI's per-command --timeout (see docs/PROTOCOL.md's "
        "'Command timeout' section)."
    ),
)
@click.option(
    "--android-artifact",
    default=None,
    help="Path to a pre-built Android CRX/`.bin` (from `scripts/package-android.sh`) to serve at "
    "GET /setup/android-extension.bin. Default: $AMPLIFIER_BROWSER_BRIDGE_ANDROID_ARTIFACT, or "
    "unset -- that route then 404s with an actionable message, and the /setup page's Android "
    "section shows no download link, instead of ever guessing a path. This file carries a live "
    "hub credential baked in (android_bake.py) -- serving it is opt-in on purpose (see hub.py's "
    "'Onboarding' section for the full reasoning).",
)
def hub(
    host: str,
    port: int,
    token_file: str | None,
    audit_log: str | None,
    command_timeout: float,
    android_artifact: str | None,
) -> None:
    """Run the hub: device registry, per-device command queue, routing, audit log."""
    if is_wildcard_bind(host):
        # A1 fix (security review finding): --host default changed from the
        # silently-permissive "0.0.0.0" to loopback-only "127.0.0.1". A caller
        # who explicitly chooses a wildcard bind still can -- it's a legitimate
        # choice on some setups (e.g. a container/VM whose only route to the
        # tailnet IS via a wildcard bind) -- but the exposure is now named
        # loudly, every time, rather than being an invisible default.
        click.echo(wildcard_bind_warning(host, port), err=True)
    resolved_token_file = resolve_token_file(token_file)
    token_store = load_token_store(token_file)
    audit_path = audit_log or os.environ.get(
        "AMPLIFIER_BROWSER_BRIDGE_AUDIT_LOG", "./amplifier-browser-bridge-audit.jsonl"
    )
    resolved_android_artifact_raw = android_artifact or os.environ.get(
        "AMPLIFIER_BROWSER_BRIDGE_ANDROID_ARTIFACT"
    )
    resolved_android_artifact = (
        Path(resolved_android_artifact_raw).expanduser() if resolved_android_artifact_raw else None
    )
    hub_instance = Hub(
        token_store=token_store,
        audit_log=AuditLog(audit_path),
        command_timeout=command_timeout,
        # Lets a successful `pair` redemption persist its freshly-minted
        # per-device token into the SAME file `token_store` above was loaded
        # from -- see Hub.__init__'s docstring and pairing.py's module docstring.
        token_file=resolved_token_file,
        android_artifact=resolved_android_artifact,
    )
    app = hub_instance.build_app()
    banner = (
        f"amplifier-browser-bridge hub listening on ws://{host}:{port}/device (extensions) "
        f"and ws://{host}:{port}/agent (agents); audit log -> {audit_path}"
    )
    try:
        asyncio.run(serve_hub(app, host, port, on_bound=lambda: click.echo(banner, err=True)))
    except HubBindError as e:
        raise click.ClickException(str(e)) from e


if __name__ == "__main__":
    main()
