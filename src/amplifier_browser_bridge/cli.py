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
import time
from collections import Counter
from pathlib import Path
from typing import Any

import click

from .addressing import TargetError, parse_target
from .audit import AuditLog
from .auth import extract_token_value, find_sibling_token_files, load_token_store, mask_token
from .client import HubClient, HubError
from .doctor import run_doctor
from .hub import DEFAULT_COMMAND_TIMEOUT, DEFAULT_PORT, Hub, HubBindError, serve_hub
from .legacy_env import warn_legacy_env_vars
from .policy import Denylist, host_of
from .protocol import COMMANDS
from .setup import DEFAULT_STAGE_DIR, ExtensionSourceNotFoundError, ensure_token_file, stage_extension
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
    # Checked before any subcommand logic runs, so a leftover ABB_* variable
    # (from before this project dropped the acronym) produces this legible
    # message instead of a confusing downstream default/error several layers
    # in -- see legacy_env.py's module docstring and MIGRATION.md.
    warn_legacy_env_vars()


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
@click.option(
    "--hub-host",
    default="0.0.0.0",
    show_default=True,
    help="Host to print in the printed `amplifier-browser-bridge hub` command.",
)
@click.option(
    "--hub-port",
    default=DEFAULT_PORT,
    show_default=True,
    help="Port to print in the printed `amplifier-browser-bridge hub` command.",
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


def init(dest: str | None, token_file: str | None, force: bool, hub_host: str, hub_port: int) -> None:
    """First-run setup: generate a hub token, stage the extension, print next steps.

    Does three things, each idempotent (safe to re-run, e.g. after `git pull`):

    \b
    1. Ensures a hub token exists (generates one on first run; reuses it on later
       runs unless --force).
    2. Stages the extension's runtime files into a stable directory (default under
       ~/.local/share) -- NOT this repo checkout, so re-running after an update
       never changes the path an already-loaded extension was loaded from, which is
       what lets its saved chrome.storage.local config survive the update.
    3. Prints the exact remaining manual steps. Loading an unpacked extension in
       edge://extensions IS a manual step (Edge has no CLI/API for it) -- this command
       does not pretend otherwise.
    """
    try:
        token_result = ensure_token_file(token_file, force=force)
    except OSError as e:
        raise click.ClickException(f"could not write token file: {e}") from e

    try:
        staged_dir = stage_extension(dest)
    except ExtensionSourceNotFoundError as e:
        raise click.ClickException(str(e)) from e

    action = "Generated new" if token_result.created_new else "Reusing existing"
    click.echo(f"{action} hub token (stored in {token_result.token_file}).")
    click.echo(f"Staged extension -> {staged_dir}")

    _warn_divergent_token_siblings(token_result.token_file, token_result.token)

    click.echo("")
    click.echo("Remaining steps (manual -- Edge has no CLI for these):")
    click.echo("")
    click.echo("  1. Start the hub:")
    click.echo(
        f"       AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE={token_result.token_file} amplifier-browser-bridge hub --host {hub_host} --port {hub_port}"
    )
    click.echo("")
    click.echo("  2. Load the extension:")
    click.echo("       edge://extensions -> enable Developer mode -> Load unpacked ->")
    click.echo(f"       select: {staged_dir}")
    click.echo("")
    click.echo("  3. Configure it:")
    click.echo("       Click the extension's toolbar icon (its only UI) to open the options page.")
    click.echo("       Hub URL: ws://<this machine's tailnet IP>:" + f"{hub_port}/device")
    click.echo(f"       Token:   {token_result.token}")
    click.echo("       Click Save.")
    click.echo("")
    click.echo("  4. Confirm it worked:")
    click.echo(
        f"       AMPLIFIER_BROWSER_BRIDGE_TOKEN={token_result.token} amplifier-browser-bridge doctor --hub-url ws://127.0.0.1:{hub_port}/agent"
    )


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

    any_failed = False
    for check in checks:
        icon = {"ok": "[ok]  ", "fail": "[FAIL]", "skipped": "[skip]"}[check.status]
        click.echo(f"{icon} {check.name}: {check.message}")
        if check.status == "fail":
            any_failed = True

    if any_failed:
        raise click.ClickException("one or more checks failed -- see above.")
    click.echo("\nAll checks passed.")


@main.command()
@click.option("--host", default="0.0.0.0", show_default=True)
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
def hub(host: str, port: int, token_file: str | None, audit_log: str | None, command_timeout: float) -> None:
    """Run the hub: device registry, per-device command queue, routing, audit log."""
    token_store = load_token_store(token_file)
    audit_path = audit_log or os.environ.get(
        "AMPLIFIER_BROWSER_BRIDGE_AUDIT_LOG", "./amplifier-browser-bridge-audit.jsonl"
    )
    hub_instance = Hub(
        token_store=token_store, audit_log=AuditLog(audit_path), command_timeout=command_timeout
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
