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
    }
    assert COMMANDS == expected
