"""Basic envelope/vocabulary sanity checks -- protocol.py is the single source of
truth for message-type and command-name spelling shared across hub.py, client.py,
and docs/PROTOCOL.md (and mirrored by hand in the extension's JS)."""

from __future__ import annotations

from amplifier_browser_bridge.protocol import (
    BROWSER_LEVEL_COMMANDS,
    COMMANDS,
    PAGE_WORLD_COMMANDS,
    envelope,
    new_id,
    now_iso,
)


def test_new_id_is_unique() -> None:
    ids = {new_id() for _ in range(100)}
    assert len(ids) == 100


def test_now_iso_is_parseable_and_has_timezone() -> None:
    from datetime import datetime

    ts = now_iso()
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None


def test_envelope_shape() -> None:
    env = envelope("hello", device_id="d1")
    assert env["type"] == "hello"
    assert env["device_id"] == "d1"
    assert "v" in env
    assert "id" in env


def test_envelope_respects_explicit_id() -> None:
    env = envelope("ping", msg_id="fixed-id")
    assert env["id"] == "fixed-id"


def test_page_world_and_browser_level_partition_all_commands() -> None:
    assert PAGE_WORLD_COMMANDS | BROWSER_LEVEL_COMMANDS == COMMANDS
    assert PAGE_WORLD_COMMANDS & BROWSER_LEVEL_COMMANDS == set()


def test_command_vocabulary_matches_design_doc() -> None:
    expected = {
        "snapshot",
        "click",
        "type",
        "key",
        "scroll",
        "navigate",
        "back",
        "forward",
        "read",
        "tabs",
        "tab_open",
        "tab_close",
        "tab_activate",
        "screenshot",
        "wait_for",
        "wait_text",
        # Phase 4: CDP escalation (design doc §7).
        "attach",
        "detach",
        # Real-profile hardening: self-service extension reload.
        "reload",
        # Content-extraction mechanisms (design doc's "Mechanism, not policy").
        "fetch_bytes",
        "grab_image",
        "downloads_list",
        "download",
        "wait_download",
    }
    assert COMMANDS == expected


def test_content_extraction_commands_are_browser_level() -> None:
    """None of the new content-extraction commands are dispatched into
    injected.js's page-world path -- fetch_bytes/downloads_list/download/
    wait_download are device-only, and grab_image runs in the page's MAIN
    world via its own executeScript call, not window.__abb.dispatch()."""
    for name in ("fetch_bytes", "grab_image", "downloads_list", "download", "wait_download"):
        assert name in BROWSER_LEVEL_COMMANDS
        assert name not in PAGE_WORLD_COMMANDS


def test_reload_is_a_browser_level_command() -> None:
    """`reload` is device-only (like `tab_open`) -- no tab involved, so it must
    not be dispatched into injected.js's page-world path."""
    assert "reload" in BROWSER_LEVEL_COMMANDS
    assert "reload" not in PAGE_WORLD_COMMANDS
