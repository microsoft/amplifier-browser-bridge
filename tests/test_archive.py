"""Tests for archive.py -- the browser-state archive orchestrator.

No real HubClient/websocket/browser is exercised here -- a fake `_ArchiveClient`
(same duck-typing pattern as test_vision.py's fake for vision_read.py) answers
`list_devices()`/`command()` from a canned script, and every assertion inspects
either the returned manifest or the files actually written under `tmp_path`.

Covers CONTRIBUTING.md's evidence-based testing convention for this feature:
depth-ladder composition, the no-wake guarantee, per-tab failure recording
without faking overall success, impossible-depth loud failure, and manifest
shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from amplifier_browser_bridge.addressing import Target
from amplifier_browser_bridge.archive import ArchiveError, run_archive

# ---------------------------------------------------------------------------
# Fixtures: a fake device + a scriptable fake HubClient
# ---------------------------------------------------------------------------

_DEVICE_ID = "d1"


def _device_record(**capabilities: bool) -> dict[str, Any]:
    return {
        "device_id": _DEVICE_ID,
        "profile_id": "p1",
        "label": "edge-macos",
        "platform": "MacIntel",
        "capabilities": {
            "scripting": True,
            "windows": True,
            "tab_groups": True,
            "debugger": False,
            "capture_visible_tab": True,
            "downloads": True,
            "storage": True,
            "alarms": True,
            "history": True,
            "bookmarks": True,
            "sessions": True,
            "top_sites": True,
            "reading_list": True,
            "cookies": True,
            **capabilities,
        },
        "protocol_version": 1,
        "connected": True,
        "tier": "live",
        "last_seen": "2026-08-14T00:00:00+00:00",
        "queue_length": 0,
    }


def _tab(
    tab_id: int,
    *,
    window_id: int = 1,
    url: str = "https://example.com",
    title: str = "Test Page",
    discarded: bool = False,
    asleep: bool = False,
) -> dict[str, Any]:
    return {
        "tab_id": tab_id,
        "window_id": window_id,
        "url": url,
        "title": title,
        "active": False,
        "index": 0,
        "discarded": discarded,
        "asleep": asleep,
        "status": "unloaded" if (discarded or asleep) else "complete",
        "group_id": None,
        "fav_icon_url": None,
        "pinned": False,
        "audible": False,
        "muted": False,
        "mute_reason": None,
        "last_accessed": 1000,
    }


_WINDOWS_RESULT = {
    "ok": True,
    "result": {
        "windows": [
            {
                "window_id": 1,
                "focused": True,
                "state": "normal",
                "type": "normal",
                "incognito": False,
                "top": 0,
                "left": 0,
                "width": 1200,
                "height": 800,
            }
        ],
        "tab_groups": [
            {"group_id": 1, "window_id": 1, "title": "Group A", "color": "blue", "collapsed": False}
        ],
    },
}


class FakeArchiveClient:
    """Duck-typed `_ArchiveClient` -- scripted per-command responses, no
    network/websocket of any kind. `by_command` maps a command name to either
    a single canned response (returned for every call) or a list of
    responses (consumed in order, one per call -- for a command invoked once
    per tab)."""

    def __init__(self, devices: list[dict[str, Any]], by_command: dict[str, Any]) -> None:
        self._devices = devices
        self._by_command = by_command
        self._call_index: dict[str, int] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []  # (device_id, command, args)

    async def list_devices(self) -> list[dict[str, Any]]:
        return self._devices

    async def command(self, target: Target, command: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((target.device_id, command, dict(args)))
        scripted = self._by_command.get(command, {"ok": True, "result": {}})
        if isinstance(scripted, list):
            idx = self._call_index.get(command, 0)
            self._call_index[command] = idx + 1
            return scripted[idx] if idx < len(scripted) else scripted[-1]
        return scripted


def _basic_client(
    *,
    tabs: list[dict[str, Any]] | None = None,
    capabilities: dict[str, bool] | None = None,
    extra_commands: dict[str, Any] | None = None,
) -> FakeArchiveClient:
    tabs = tabs if tabs is not None else [_tab(101), _tab(102)]
    by_command: dict[str, Any] = {
        "windows": _WINDOWS_RESULT,
        "tabs": {"ok": True, "result": tabs},
        "read": {
            "ok": True,
            "result": {"url": "https://example.com", "title": "Test Page", "text": "hello world"},
        },
        "page_state": {
            "ok": True,
            "result": {
                "url": "https://example.com",
                "title": "Test Page",
                "outer_html": "<html><body>hi</body></html>",
                "outer_html_chars": 29,
                "outer_html_truncated": False,
                "forms": [],
                "local_storage": {},
                "session_storage": {},
                "scroll": {"x": 0, "y": 0},
            },
        },
        "screenshot": {
            "ok": True,
            "result": {"tab_id": 101, "format": "jpg", "base64": "aGVsbG8=", "via": "captureVisibleTab"},
        },
        "mhtml": {"ok": True, "result": {"tab_id": 101, "format": "mhtml", "bytes": 5, "data": "MHTML-DATA"}},
        "nav_history": {
            "ok": True,
            "result": {
                "tab_id": 101,
                "current_index": 0,
                "entries": [{"id": 1, "url": "https://example.com"}],
            },
        },
        "history_list": {
            "ok": True,
            "result": {"entries": [{"url": "https://example.com", "title": "Test Page"}]},
        },
        "bookmarks_list": {"ok": True, "result": {"entries": [{"id": "1", "title": "Bar", "url": None}]}},
        "sessions_list": {"ok": True, "result": {"recently_closed": [], "devices": []}},
        "top_sites": {
            "ok": True,
            "result": {"entries": [{"url": "https://example.com", "title": "Test Page"}]},
        },
        "reading_list": {"ok": True, "result": {"entries": []}},
        "cookies_list": {"ok": True, "result": {"entries": [{"name": "session", "value": "secret-token"}]}},
    }
    if extra_commands:
        by_command.update(extra_commands)
    devices = [_device_record(**(capabilities or {}))]
    return FakeArchiveClient(devices, by_command)


# ---------------------------------------------------------------------------
# Depth ladder composition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l0_does_not_contact_any_tab(tmp_path: Path) -> None:
    """L0 must work with zero tab contact -- no read/page_state/screenshot/
    mhtml/nav_history call for any tab, even though tabs exist."""
    client = _basic_client()
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L0")

    assert result["ok"] is True
    manifest = result["result"]
    assert manifest["tabs"] == {}
    per_tab_commands = {"read", "page_state", "screenshot", "mhtml", "nav_history"}
    assert not any(cmd in per_tab_commands for (_dev, cmd, _args) in client.calls)
    # Inventory-only commands DID run.
    assert any(cmd == "windows" for (_dev, cmd, _args) in client.calls)
    assert any(cmd == "tabs" for (_dev, cmd, _args) in client.calls)


@pytest.mark.asyncio
async def test_l1_adds_text_only(tmp_path: Path) -> None:
    client = _basic_client()
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    manifest = result["result"]
    for tab_entry in manifest["tabs"].values():
        assert set(tab_entry["captures"]) == {"text"}


@pytest.mark.asyncio
async def test_l2_adds_dom_on_top_of_text(tmp_path: Path) -> None:
    client = _basic_client()
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L2")
    manifest = result["result"]
    for tab_entry in manifest["tabs"].values():
        assert set(tab_entry["captures"]) == {"text", "dom"}


@pytest.mark.asyncio
async def test_l3_adds_screenshots(tmp_path: Path) -> None:
    client = _basic_client()
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L3")
    manifest = result["result"]
    for tab_entry in manifest["tabs"].values():
        assert set(tab_entry["captures"]) == {"text", "dom", "screenshot"}


@pytest.mark.asyncio
async def test_l4_adds_mhtml_and_requires_debugger(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L4")
    manifest = result["result"]
    for tab_entry in manifest["tabs"].values():
        assert set(tab_entry["captures"]) == {"text", "dom", "screenshot", "mhtml"}


@pytest.mark.asyncio
async def test_l5_adds_nav_history_and_profile_data(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    manifest = result["result"]
    for tab_entry in manifest["tabs"].values():
        assert set(tab_entry["captures"]) == {"text", "dom", "screenshot", "mhtml", "nav_history"}
    assert manifest["profile"] is not None
    assert set(manifest["profile"]) == {
        "history",
        "bookmarks",
        "sessions",
        "top_sites",
        "reading_list",
        "cookies",
    }


@pytest.mark.asyncio
async def test_each_level_is_a_strict_superset_of_the_previous(tmp_path: Path) -> None:
    """The depth ladder's own contract: L(n+1)'s per-tab capture keys must be a
    strict superset of L(n)'s."""
    prior_keys: set[str] = set()
    for i, depth in enumerate(("L0", "L1", "L2", "L3", "L4", "L5")):
        client = _basic_client(capabilities={"debugger": True})
        result = await run_archive(client, _DEVICE_ID, tmp_path / depth, depth=depth)
        manifest = result["result"]
        if manifest["tabs"]:
            keys = set(next(iter(manifest["tabs"].values()))["captures"])
        else:
            keys = set()
        assert prior_keys <= keys, f"{depth} lost capture(s) present at a lower depth: {prior_keys - keys}"
        prior_keys = keys
        del i  # only used for readability of the loop


# ---------------------------------------------------------------------------
# No-wake guarantee
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discarded_tab_is_skipped_not_captured_by_default(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101, discarded=True)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L2")
    manifest = result["result"]
    entry = manifest["tabs"]["101"]
    assert entry["status"] == "skipped"
    assert "wake=True" in entry["reason"]
    assert entry["captures"] == {}
    # No per-tab command was ever issued for this tab.
    per_tab_calls = [c for c in client.calls if c[0] == _DEVICE_ID and c[1] in ("read", "page_state")]
    assert per_tab_calls == []


@pytest.mark.asyncio
async def test_asleep_tab_is_also_skipped_by_default(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101, asleep=True)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    entry = result["result"]["tabs"]["101"]
    assert entry["status"] == "skipped"


@pytest.mark.asyncio
async def test_wake_true_allows_capturing_a_discarded_tab(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101, discarded=True)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", wake=True)
    entry = result["result"]["tabs"]["101"]
    assert entry["status"] == "ok"
    assert "text" in entry["captures"]
    # The wake=True opt-in was actually forwarded to the wire command.
    read_calls = [c for c in client.calls if c[1] == "read"]
    assert read_calls and read_calls[0][2].get("wake") is True


@pytest.mark.asyncio
async def test_non_discarded_tabs_never_receive_a_wake_arg_by_default(tmp_path: Path) -> None:
    """Even for AWAKE tabs, the orchestrator must never pass wake=true unless
    the caller explicitly opted in -- this is the general guarantee, not just
    the discarded-tab special case."""
    client = _basic_client(tabs=[_tab(101, discarded=False, asleep=False)])
    await run_archive(client, _DEVICE_ID, tmp_path, depth="L2")
    for _dev, _cmd, args in client.calls:
        assert "wake" not in args


@pytest.mark.asyncio
async def test_archive_never_passes_activate(tmp_path: Path) -> None:
    """The archive orchestrator must never steal focus -- no call, at any
    depth, ever carries an `activate` arg."""
    client = _basic_client(capabilities={"debugger": True})
    await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    for _dev, _cmd, args in client.calls:
        assert "activate" not in args


# ---------------------------------------------------------------------------
# Per-tab failure recorded, run continues, overall result is honest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_tab_failing_does_not_abort_the_run(tmp_path: Path) -> None:
    client = _basic_client(
        tabs=[_tab(101), _tab(102)],
        extra_commands={
            "read": [
                {"ok": True, "result": {"url": "https://example.com", "title": "Test Page", "text": "hello"}},
                {"ok": False, "error": "tab 102 is discarded: ..."},
            ]
        },
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    manifest = result["result"]
    assert manifest["tabs"]["101"]["status"] == "ok"
    assert manifest["tabs"]["102"]["status"] == "failed"
    assert manifest["tabs"]["102"]["captures"]["text"]["status"] == "failed"


@pytest.mark.asyncio
async def test_failures_are_recorded_at_the_top_level_and_never_buried(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101)], extra_commands={"read": {"ok": False, "error": "boom"}})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    manifest = result["result"]
    assert len(manifest["failures"]) == 1
    assert manifest["failures"][0]["scope"] == "tab"
    assert manifest["failures"][0]["tab_id"] == 101
    assert manifest["failures"][0]["capture"] == "text"
    assert manifest["failures"][0]["error"] == "boom"


@pytest.mark.asyncio
async def test_overall_status_is_never_plain_ok_when_a_tab_failed(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101)], extra_commands={"read": {"ok": False, "error": "boom"}})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    manifest = result["result"]
    assert manifest["status"] == "ok_with_failures"
    assert manifest["summary"]["has_failures"] is True
    assert manifest["summary"]["tabs_failed"] == 1


@pytest.mark.asyncio
async def test_overall_status_is_ok_with_skips_when_only_skips_occurred(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101, discarded=True)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    manifest = result["result"]
    assert manifest["status"] == "ok_with_skips"
    assert manifest["summary"]["has_failures"] is False
    assert manifest["summary"]["tabs_skipped"] == 1


@pytest.mark.asyncio
async def test_overall_status_is_plain_ok_when_everything_succeeds(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L2")
    manifest = result["result"]
    assert manifest["status"] == "ok"
    assert manifest["summary"] == {
        "windows_inventoried": 1,
        "tab_groups_inventoried": 1,
        "tabs_inventoried": 1,
        "tabs_capture_attempted": 1,
        "tabs_captured": 1,
        "tabs_partial": 0,
        "tabs_skipped": 0,
        "tabs_failed": 0,
        "profile": None,
        "has_failures": False,
    }


# ---------------------------------------------------------------------------
# Per-tab status is not binary: "partial" is the middle state between "ok"
# and "failed" -- the bug ff5bc16 fixed one level up (run-level summary),
# missed one level down (per-tab status).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tab_with_mixed_success_and_failure_reports_partial_not_ok_or_failed(
    tmp_path: Path,
) -> None:
    """The bug, observed live against a real tab whose page was a browser error
    page: `text`/`dom` (JS injection) failed outright ("Frame with ID 0 is
    showing error page") while `mhtml`/`screenshot`/`nav_history` (CDP-based)
    all succeeded, landing ~147KB of real artifacts on disk. That tab is
    neither a clean success nor a total loss -- it must report `"partial"`,
    and `summary` must count it as neither a captured tab nor a failed one."""
    client = _basic_client(
        tabs=[_tab(101)],
        capabilities={"debugger": True},
        extra_commands={
            "read": {"ok": False, "error": "Frame with ID 0 is showing error page"},
            "page_state": {"ok": False, "error": "Frame with ID 0 is showing error page"},
        },
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    manifest = result["result"]
    entry = manifest["tabs"]["101"]

    assert entry["status"] == "partial"
    assert entry["captures"]["text"]["status"] == "failed"
    assert entry["captures"]["dom"]["status"] == "failed"
    assert entry["captures"]["screenshot"]["status"] == "ok"
    assert entry["captures"]["mhtml"]["status"] == "ok"
    assert entry["captures"]["nav_history"]["status"] == "ok"

    # The load-bearing assertions: not a clean capture, not a total failure.
    assert manifest["summary"]["tabs_captured"] == 0
    assert manifest["summary"]["tabs_failed"] == 0
    assert manifest["summary"]["tabs_partial"] == 1


@pytest.mark.asyncio
async def test_tab_with_every_capture_failing_still_reports_failed(tmp_path: Path) -> None:
    """The other end of the same axis: a tab where NOTHING succeeded must still
    report `"failed"`, not `"partial"` -- `"partial"` is reserved for the
    genuine middle ground, not a synonym for any failure at all."""
    client = _basic_client(
        tabs=[_tab(101)],
        capabilities={"debugger": True},
        extra_commands={
            "read": {"ok": False, "error": "boom-text"},
            "page_state": {"ok": False, "error": "boom-dom"},
            "screenshot": {"ok": False, "error": "boom-screenshot"},
            "mhtml": {"ok": False, "error": "boom-mhtml"},
            "nav_history": {"ok": False, "error": "boom-nav"},
        },
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    manifest = result["result"]
    entry = manifest["tabs"]["101"]

    assert entry["status"] == "failed"
    assert manifest["summary"]["tabs_failed"] == 1
    assert manifest["summary"]["tabs_partial"] == 0
    assert manifest["summary"]["tabs_captured"] == 0


@pytest.mark.asyncio
async def test_skipped_tab_is_distinct_from_ok_partial_and_failed_tabs(tmp_path: Path) -> None:
    """A tab intentionally skipped (no-wake guarantee) is a FOURTH state, never
    confused with any of the three capture outcomes -- exercised here
    alongside a fully-ok tab and a fully-failed tab in the same run."""
    client = _basic_client(
        tabs=[_tab(101, discarded=True), _tab(102), _tab(103)],
        extra_commands={
            "read": [
                {"ok": True, "result": {"url": "https://example.com", "title": "Test Page", "text": "hi"}},
                {"ok": False, "error": "boom"},
            ],
        },
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    manifest = result["result"]

    assert manifest["tabs"]["101"]["status"] == "skipped"
    assert manifest["tabs"]["102"]["status"] == "ok"
    assert manifest["tabs"]["103"]["status"] == "failed"

    summary = manifest["summary"]
    assert summary["tabs_skipped"] == 1
    assert summary["tabs_captured"] == 1
    assert summary["tabs_failed"] == 1
    assert summary["tabs_partial"] == 0


@pytest.mark.asyncio
async def test_run_level_status_is_not_plain_ok_when_a_tab_is_partial(tmp_path: Path) -> None:
    """A run containing a partial tab must not be reported as plain `"ok"` --
    the per-tab capture failures underlying the partial status always land in
    the top-level `failures` list (via `_capture_tab`'s `record`), which forces
    the already-existing degraded-status branch. Verified explicitly rather
    than assumed, since this is the caller-visible guarantee from the bug
    report ("keep manifest['status'] honest")."""
    client = _basic_client(
        tabs=[_tab(101)],
        capabilities={"debugger": True},
        extra_commands={"read": {"ok": False, "error": "boom-text"}},
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    manifest = result["result"]

    assert manifest["tabs"]["101"]["status"] == "partial"
    assert manifest["status"] == "ok_with_failures"
    assert manifest["status"] != "ok"


@pytest.mark.asyncio
async def test_partial_tab_failures_still_carry_original_error_text(tmp_path: Path) -> None:
    """`manifest["failures"]` is what told the reporter the real cause of the
    live bug ("Frame with ID 0 is showing error page") -- it must not get
    quieter just because the tab's rolled-up status is now `"partial"` instead
    of `"failed"`."""
    client = _basic_client(
        tabs=[_tab(101)],
        capabilities={"debugger": True},
        extra_commands={
            "read": {"ok": False, "error": "Frame with ID 0 is showing error page"},
            "page_state": {"ok": False, "error": "Frame with ID 0 is showing error page"},
        },
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    manifest = result["result"]

    assert manifest["tabs"]["101"]["status"] == "partial"
    text_failures = [f for f in manifest["failures"] if f["capture"] == "text"]
    dom_failures = [f for f in manifest["failures"] if f["capture"] == "dom"]
    assert len(text_failures) == 1
    assert text_failures[0]["error"] == "Frame with ID 0 is showing error page"
    assert len(dom_failures) == 1
    assert dom_failures[0]["error"] == "Frame with ID 0 is showing error page"


# ---------------------------------------------------------------------------
# Manifest-accuracy bug: summary must never read as "nothing was archived"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l0_summary_reports_tabs_inventoried_not_zero(tmp_path: Path) -> None:
    """The bug, observed live against a real 735-tab profile: an L0 archive
    wrote every tab to disk successfully, but `summary["tabs_total"]` (computed
    from the empty per-tab capture manifest) reported `0`, and there was no
    other unmissable field in `summary` saying otherwise. `tabs_inventoried`
    must report the real count at L0 -- the depth that, by design, captures
    zero page content."""
    n = 735
    tabs = [_tab(100 + i) for i in range(n)]
    client = _basic_client(tabs=tabs)
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L0")
    manifest = result["result"]

    assert manifest["status"] == "ok"
    assert manifest["tabs_inventory"]["count"] == n
    # The load-bearing assertion: `summary` itself -- not just a sibling key --
    # must make the real count unmissable, and must never claim zero.
    assert manifest["summary"]["tabs_inventoried"] == n
    assert manifest["summary"]["tabs_inventoried"] != 0
    # L0 legitimately captures no page content -- that is success, not failure,
    # and must remain distinguishable from the inventory count above.
    assert manifest["summary"]["tabs_capture_attempted"] == 0
    assert manifest["summary"]["tabs_captured"] == 0
    assert manifest["summary"]["tabs_skipped"] == 0
    assert manifest["summary"]["tabs_failed"] == 0
    assert manifest["summary"]["has_failures"] is False


@pytest.mark.asyncio
async def test_l0_summary_also_reports_windows_and_tab_groups_inventoried(tmp_path: Path) -> None:
    """The same under-reporting bug could equally hit the windows/tab-groups
    axes -- both are captured outside `tab_manifest` too."""
    client = _basic_client()
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L0")
    manifest = result["result"]
    assert manifest["summary"]["windows_inventoried"] == manifest["windows"]["count"] == 1
    assert manifest["summary"]["tab_groups_inventoried"] == manifest["tab_groups"]["count"] == 1


@pytest.mark.asyncio
async def test_deeper_depth_still_distinguishes_inventoried_from_captured(tmp_path: Path) -> None:
    """At L2 with every tab capturing successfully, `tabs_inventoried` and
    `tabs_captured` happen to be equal in VALUE -- but they must remain
    distinct KEYS reporting distinct axes (inventory vs. content capture),
    never collapsed into a single number."""
    tabs = [_tab(101), _tab(102), _tab(103)]
    client = _basic_client(tabs=tabs)
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L2")
    summary = result["result"]["summary"]
    assert summary["tabs_inventoried"] == 3
    assert summary["tabs_capture_attempted"] == 3
    assert summary["tabs_captured"] == 3
    # Distinct keys must both be present and correct, not merged into one.
    assert "tabs_inventoried" in summary
    assert "tabs_capture_attempted" in summary


@pytest.mark.asyncio
async def test_tabs_inventoried_is_full_count_even_when_tab_ids_narrows_capture(
    tmp_path: Path,
) -> None:
    """`tab_ids` restricts L1+ per-tab CAPTURE to a subset, but the INVENTORY
    axis must always report the true total -- never silently narrowed to
    match the capture subset."""
    tabs = [_tab(101), _tab(102), _tab(103)]
    client = _basic_client(tabs=tabs)
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", tab_ids=[101])
    summary = result["result"]["summary"]
    assert summary["tabs_inventoried"] == 3
    assert summary["tabs_capture_attempted"] == 1
    assert summary["tabs_captured"] == 1


@pytest.mark.asyncio
async def test_windows_inventoried_is_none_not_zero_when_windows_capture_fails(
    tmp_path: Path,
) -> None:
    """A failed `windows` capture must report `None` (unknown), never a bare
    `0` that could be misread as "zero windows exist" -- and must still push
    the overall run to a degraded status, never plain `"ok"`."""
    client = _basic_client(extra_commands={"windows": {"ok": False, "error": "boom"}})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L0")
    manifest = result["result"]
    assert manifest["status"] == "ok_with_failures"
    assert manifest["summary"]["has_failures"] is True
    assert manifest["summary"]["windows_inventoried"] is None
    assert manifest["summary"]["tab_groups_inventoried"] is None
    # The inventory failure is not silently absorbed -- it is a real, named
    # top-level failure.
    assert any(f["scope"] == "windows" for f in manifest["failures"])


@pytest.mark.asyncio
async def test_tabs_inventoried_is_none_not_zero_when_tabs_capture_fails(tmp_path: Path) -> None:
    client = _basic_client(extra_commands={"tabs": {"ok": False, "error": "boom"}})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L0")
    manifest = result["result"]
    assert manifest["status"] == "ok_with_failures"
    assert manifest["summary"]["tabs_inventoried"] is None
    assert any(f["scope"] == "tabs_inventory" for f in manifest["failures"])


@pytest.mark.asyncio
async def test_profile_summary_is_none_below_l5_and_populated_at_l5(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": True})
    below = await run_archive(client, _DEVICE_ID, tmp_path / "L2", depth="L2")
    assert below["result"]["summary"]["profile"] is None

    at_l5 = await run_archive(client, _DEVICE_ID, tmp_path / "L5", depth="L5")
    profile_summary = at_l5["result"]["summary"]["profile"]
    assert profile_summary is not None
    # 5 _PROFILE_SPECS items + cookies (skipped by default, opt-in) == 6.
    assert profile_summary["items_total"] == 6
    assert profile_summary["items_captured"] == 5
    assert profile_summary["items_skipped"] == 1  # cookies, intentional opt-out
    assert profile_summary["items_failed"] == 0
    # The intentional cookies opt-out is not a failure and must not push
    # overall status away from plain "ok".
    assert at_l5["result"]["status"] == "ok"


@pytest.mark.asyncio
async def test_profile_summary_counts_a_real_profile_item_failure(tmp_path: Path) -> None:
    client = _basic_client(
        capabilities={"debugger": True},
        extra_commands={"history_list": {"ok": False, "error": "boom"}},
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    manifest = result["result"]
    profile_summary = manifest["summary"]["profile"]
    assert profile_summary is not None
    assert profile_summary["items_failed"] == 1
    assert profile_summary["items_captured"] == 4
    assert manifest["status"] == "ok_with_failures"
    assert manifest["summary"]["has_failures"] is True


@pytest.mark.asyncio
async def test_queued_response_mid_run_is_recorded_as_a_failure_not_a_hang(tmp_path: Path) -> None:
    """A device that goes non-live mid-archive returns {"status": "queued", ...}
    for a per-tab command -- this must be recorded as a failure, never
    awaited/polled/retried (which would risk hanging the whole archive)."""
    client = _basic_client(
        tabs=[_tab(101)],
        extra_commands={"read": {"status": "queued", "command_id": "c1", "tier": "intermittent"}},
    )
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1")
    entry = result["result"]["tabs"]["101"]
    assert entry["status"] == "failed"
    assert "queued" in entry["captures"]["text"]["error"]


# ---------------------------------------------------------------------------
# Impossible depth -- fail loud, never silently degrade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l4_without_debugger_raises_before_writing_anything(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": False})
    with pytest.raises(ArchiveError) as exc_info:
        await run_archive(client, _DEVICE_ID, tmp_path, depth="L4")
    assert "debugger" in str(exc_info.value)
    assert "L4" in str(exc_info.value)
    # Nothing was written to disk -- a hard pre-flight failure creates no
    # archive directory at all.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_l5_without_debugger_also_raises(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": False})
    with pytest.raises(ArchiveError):
        await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")


@pytest.mark.asyncio
async def test_l3_without_debugger_does_not_raise_it_just_cannot_use_capture_hidden(tmp_path: Path) -> None:
    """L3 (screenshots) has no unconditional CDP requirement -- only L4/L5 do.
    A device without `debugger` can still run L3; screenshot simply omits
    capture_hidden and may fail per-tab (recorded, not a hard stop) for a
    background tab."""
    client = _basic_client(capabilities={"debugger": False}, tabs=[_tab(101)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L3")
    assert result["ok"] is True
    screenshot_calls = [c for c in client.calls if c[1] == "screenshot"]
    assert screenshot_calls and "capture_hidden" not in screenshot_calls[0][2]


@pytest.mark.asyncio
async def test_l3_with_debugger_uses_capture_hidden(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": True}, tabs=[_tab(101)])
    await run_archive(client, _DEVICE_ID, tmp_path, depth="L3")
    screenshot_calls = [c for c in client.calls if c[1] == "screenshot"]
    assert screenshot_calls[0][2].get("capture_hidden") is True


@pytest.mark.asyncio
async def test_unknown_depth_raises_before_any_hub_call(tmp_path: Path) -> None:
    client = _basic_client()
    with pytest.raises(ArchiveError) as exc_info:
        await run_archive(client, _DEVICE_ID, tmp_path, depth="L99")
    assert "L99" in str(exc_info.value)
    assert client.calls == []  # not even list_devices()-adjacent work happened


@pytest.mark.asyncio
async def test_unknown_device_raises() -> None:
    client = FakeArchiveClient([], {})
    with pytest.raises(ArchiveError, match="unknown device"):
        await run_archive(client, "no-such-device", "/tmp/whatever", depth="L0")


# ---------------------------------------------------------------------------
# Cookies opt-in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cookies_never_collected_by_default_even_at_l5(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    manifest = result["result"]
    assert manifest["profile"]["cookies"]["status"] == "skipped"
    assert not any(cmd == "cookies_list" for (_dev, cmd, _args) in client.calls)


@pytest.mark.asyncio
async def test_cookies_collected_only_with_explicit_opt_in(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5", include_cookies=True)
    manifest = result["result"]
    assert manifest["profile"]["cookies"]["status"] == "ok"
    assert any(cmd == "cookies_list" for (_dev, cmd, _args) in client.calls)


# ---------------------------------------------------------------------------
# Manifest shape / disk layout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manifest_never_the_payload_only_paths_counts_bytes(tmp_path: Path) -> None:
    """The load-bearing constraint: run_archive's own return value must never
    contain a raw captured payload (page text, HTML, base64 image bytes,
    MHTML) -- only paths/counts/byte sizes/status."""
    client = _basic_client(capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L5")
    manifest = result["result"]
    serialized = json.dumps(manifest)
    assert "hello world" not in serialized  # the read() text
    assert "<html><body>hi</body></html>" not in serialized  # the page_state outerHTML
    assert "aGVsbG8=" not in serialized  # the screenshot base64
    assert "MHTML-DATA" not in serialized  # the mhtml data


@pytest.mark.asyncio
async def test_archive_directory_and_manifest_json_are_actually_written(tmp_path: Path) -> None:
    client = _basic_client(capabilities={"debugger": True})
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L2")
    manifest = result["result"]
    archive_dir = Path(manifest["archive_dir"])
    assert archive_dir.is_dir()
    assert (archive_dir / "manifest.json").is_file()
    on_disk_manifest = json.loads((archive_dir / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk_manifest["device_id"] == _DEVICE_ID
    assert (archive_dir / "windows.json").is_file()
    assert (archive_dir / "tabs.json").is_file()
    for tab_id in manifest["tabs"]:
        tab_dir = archive_dir / "tabs" / tab_id
        assert (tab_dir / "text.txt").is_file()
        assert (tab_dir / "dom.html").is_file()
        assert (tab_dir / "page_state.json").is_file()


@pytest.mark.asyncio
async def test_tab_ids_filter_restricts_per_tab_capture_but_not_inventory(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101), _tab(102), _tab(103)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", tab_ids=[101])
    manifest = result["result"]
    assert manifest["tabs_inventory"]["count"] == 3  # inventory is always complete
    assert set(manifest["tabs"]) == {"101"}


@pytest.mark.asyncio
async def test_tab_ids_filter_reports_ids_that_were_not_found(tmp_path: Path) -> None:
    client = _basic_client(tabs=[_tab(101)])
    result = await run_archive(client, _DEVICE_ID, tmp_path, depth="L1", tab_ids=[101, 999])
    manifest = result["result"]
    assert manifest["requested_tab_ids_not_found"] == [999]


@pytest.mark.asyncio
async def test_relative_dest_dir_with_tilde_is_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IMPLEMENTATION_PHILOSOPHY.md: any path that may contain `~` must be
    expanded -- a caller passing `~/archives` must not silently produce a
    literal `~` directory relative to cwd."""
    monkeypatch.setenv("HOME", str(tmp_path))
    client = _basic_client()
    result = await run_archive(client, _DEVICE_ID, "~/archives", depth="L0")
    archive_dir = Path(result["result"]["archive_dir"])
    assert archive_dir.is_relative_to(tmp_path / "archives")
    assert "~" not in str(archive_dir)
