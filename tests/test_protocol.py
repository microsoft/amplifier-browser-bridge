"""Basic envelope/vocabulary sanity checks -- protocol.py is the single source of
truth for message-type and command-name spelling shared across hub.py, client.py,
and docs/PROTOCOL.md (and mirrored by hand in the extension's JS)."""

from __future__ import annotations

from amplifier_browser_bridge.protocol import (
    BROWSER_LEVEL_COMMANDS,
    CAPABILITY_KEYS,
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
        "describe",
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
        # Browser-state archive capability (D2).
        "windows",
        "page_state",
        "mhtml",
        "nav_history",
        "history_list",
        "bookmarks_list",
        "sessions_list",
        "top_sites",
        "reading_list",
        "cookies_list",
    }
    assert COMMANDS == expected


def test_content_extraction_commands_are_browser_level() -> None:
    """None of the new content-extraction commands are dispatched into
    injected.js's page-world path -- fetch_bytes/downloads_list/download/
    wait_download are device-only, and grab_image runs in the page's MAIN
    world via its own executeScript call, not window.__amplifierBrowserBridge.dispatch()."""
    for name in ("fetch_bytes", "grab_image", "downloads_list", "download", "wait_download"):
        assert name in BROWSER_LEVEL_COMMANDS
        assert name not in PAGE_WORLD_COMMANDS


def test_reload_is_a_browser_level_command() -> None:
    """`reload` is device-only (like `tab_open`) -- no tab involved, so it must
    not be dispatched into injected.js's page-world path."""
    assert "reload" in BROWSER_LEVEL_COMMANDS
    assert "reload" not in PAGE_WORLD_COMMANDS


def test_page_state_is_a_page_world_command() -> None:
    """`page_state` (D2, browser-state archive) needs DOM/storage access, so it
    must dispatch into injected.js like snapshot/read -- not directly against
    chrome.tabs/chrome.windows."""
    assert "page_state" in PAGE_WORLD_COMMANDS
    assert "page_state" not in BROWSER_LEVEL_COMMANDS


def test_capability_keys_include_archive_profile_data_keys() -> None:
    """D2 (browser-state archive): the five profile-data capabilities plus
    `cookies` must be reportable, alongside the pre-existing keys -- see
    background.js's probeCapabilities() for the matching behavioral probes."""
    expected_new = {"history", "bookmarks", "sessions", "top_sites", "reading_list", "cookies"}
    assert expected_new <= set(CAPABILITY_KEYS)
    # No duplicates -- each key appears exactly once.
    assert len(CAPABILITY_KEYS) == len(set(CAPABILITY_KEYS))


def test_archive_browser_level_commands_are_not_page_world() -> None:
    """None of the remaining archive commands touch injected.js -- windows/
    mhtml/nav_history are dispatched directly against chrome.windows/
    chrome.tabGroups/chrome.debugger, and the profile-data commands are
    device-only, exactly like downloads_list/download/wait_download."""
    for name in (
        "windows",
        "mhtml",
        "nav_history",
        "history_list",
        "bookmarks_list",
        "sessions_list",
        "top_sites",
        "reading_list",
        "cookies_list",
    ):
        assert name in BROWSER_LEVEL_COMMANDS
        assert name not in PAGE_WORLD_COMMANDS
