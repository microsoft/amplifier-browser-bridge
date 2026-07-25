"""abb -- thin CLI adapter over the amplifier_browser_bridge library.

All logic lives in the lib (hub.py, client.py, addressing.py, ...). This module only
parses argv, builds a Target/HubClient, prints JSON, and translates library exceptions
into click.ClickException. Nothing here should ever need a unit test of its own --
if it does, that logic belongs in the lib instead.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import click

from .addressing import TargetError, parse_target
from .audit import AuditLog
from .auth import load_token_store
from .client import HubClient, HubError
from .hub import DEFAULT_PORT, Hub
from .protocol import COMMANDS

DEFAULT_HUB_URL = os.environ.get("ABB_HUB_URL", "ws://127.0.0.1:8900/agent")
DEFAULT_TOKEN = os.environ.get("ABB_TOKEN")


def _client() -> HubClient:
    return HubClient(DEFAULT_HUB_URL, token=DEFAULT_TOKEN)


def _print(obj: Any) -> None:
    click.echo(json.dumps(obj, indent=2, default=str))


def _run_command(target_str: str, command: str, args: dict[str, Any]) -> None:
    try:
        target = parse_target(target_str)
    except TargetError as e:
        raise click.ClickException(str(e)) from e
    if target.ref and "ref" not in args:
        args = {**args, "ref": target.ref}
    try:
        result = asyncio.run(_client().command(target, command, args))
    except HubError as e:
        raise click.ClickException(str(e)) from e
    _print(result)


@click.group()
def main() -> None:
    """abb -- Amplifier Browser Bridge CLI.

    Target strings address a command: `device_id`, `device_id/tab_id`, or
    `device_id/window_id/tab_id`, optionally with a trailing `#ref`.
    Configure the hub via ABB_HUB_URL (default ws://127.0.0.1:8900/agent) and
    ABB_TOKEN (if the hub has auth enabled).
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
def tabs(target: str) -> None:
    """List tabs for a device (optionally scoped to a window)."""
    _run_command(target, "tabs", {})


@main.command()
@click.argument("target")
def snapshot(target: str) -> None:
    """Accessibility-style snapshot of a tab: stable element refs for click/type."""
    _run_command(target, "snapshot", {})


@main.command()
@click.argument("target")
def read(target: str) -> None:
    """Read the visible text of a tab."""
    _run_command(target, "read", {})


@main.command(name="click")
@click.argument("target")
@click.argument("ref")
def click_cmd(target: str, ref: str) -> None:
    """Click an element by ref (from a prior snapshot)."""
    _run_command(target, "click", {"ref": ref})


@main.command(name="type")
@click.argument("target")
@click.argument("ref")
@click.argument("text")
def type_cmd(target: str, ref: str, text: str) -> None:
    """Type text into an element by ref."""
    _run_command(target, "type", {"ref": ref, "text": text})


@main.command()
@click.argument("target")
@click.argument("url")
def navigate(target: str, url: str) -> None:
    """Navigate a tab to a URL."""
    _run_command(target, "navigate", {"url": url})


@main.command(name="tab-open")
@click.argument("device")
@click.argument("url", required=False, default="about:blank")
@click.option(
    "--active/--background",
    default=False,
    help="Open as the active tab (default: background -- co-working etiquette).",
)
def tab_open(device: str, url: str, active: bool) -> None:
    """Open a new tab on a device. Target is device-only; no tab exists yet to address."""
    _run_command(device, "tab_open", {"url": url, "active": active})


@main.command(name="tab-close")
@click.argument("target")
def tab_close(target: str) -> None:
    """Close a tab."""
    _run_command(target, "tab_close", {})


@main.command(name="tab-activate")
@click.argument("target")
def tab_activate(target: str) -> None:
    """Bring a tab to the foreground. Use sparingly -- co-working etiquette favors
    acting on background tabs without stealing focus wherever a command allows it."""
    _run_command(target, "tab_activate", {})


@main.command()
@click.argument("target")
def screenshot(target: str) -> None:
    """Screenshot a tab. In this injection-only phase, only the active tab of a
    focused window can be captured -- see design doc §7 (CDP escalation is later)."""
    _run_command(target, "screenshot", {})


@main.command(name="wait-for")
@click.argument("target")
@click.argument("selector")
@click.option("--timeout-ms", default=10000, show_default=True)
def wait_for(target: str, selector: str, timeout_ms: int) -> None:
    """Poll (don't sleep) until a CSS selector matches, or time out."""
    _run_command(target, "wait_for", {"selector": selector, "timeout_ms": timeout_ms})


@main.command(name="wait-text")
@click.argument("target")
@click.argument("text")
@click.option("--timeout-ms", default=10000, show_default=True)
def wait_text(target: str, text: str, timeout_ms: int) -> None:
    """Poll (don't sleep) until visible text contains a substring, or time out."""
    _run_command(target, "wait_text", {"text": text, "timeout_ms": timeout_ms})


@main.command(name="cmd")
@click.argument("target")
@click.argument("command")
@click.option("--arg", "raw_args", multiple=True, help="key=value, repeatable")
def cmd(target: str, command: str, raw_args: tuple[str, ...]) -> None:
    """Escape hatch: run any vocabulary command with free-form args."""
    if command not in COMMANDS:
        raise click.ClickException(f"unknown command: {command}. Valid: {sorted(COMMANDS)}")
    args: dict[str, Any] = {}
    for kv in raw_args:
        if "=" not in kv:
            raise click.ClickException(f"--arg must be key=value, got: {kv}")
        k, v = kv.split("=", 1)
        args[k] = v
    _run_command(target, command, args)


@main.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=DEFAULT_PORT, show_default=True)
@click.option("--token-file", default=None, help="Path to a token JSON file (see auth.py docstring).")
@click.option("--audit-log", default=None, help="Path to the JSONL audit log (default: ./abb-audit.jsonl).")
def hub(host: str, port: int, token_file: str | None, audit_log: str | None) -> None:
    """Run the hub: device registry, per-device command queue, routing, audit log."""
    from aiohttp import web

    token_store = load_token_store(token_file)
    audit_path = audit_log or os.environ.get("ABB_AUDIT_LOG", "./abb-audit.jsonl")
    hub_instance = Hub(token_store=token_store, audit_log=AuditLog(audit_path))
    app = hub_instance.build_app()
    click.echo(
        f"amplifier-browser-bridge hub listening on ws://{host}:{port}/device (extensions) "
        f"and ws://{host}:{port}/agent (agents); audit log -> {audit_path}",
        err=True,
    )
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
