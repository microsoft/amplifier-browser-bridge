"""Tests for paging.py -- pure-logic filtering/pagination/summarization of a raw
`tabs` command response. No hub, no HubClient, no network: every fixture here is
a hand-built, synthetic response dict.
"""

from __future__ import annotations

import pytest

from amplifier_browser_bridge.paging import (
    DEFAULT_LIMIT,
    TabsPayloadError,
    shape_tabs_response,
)


def _tab(
    tab_id: int,
    *,
    window_id: int = 1,
    url: str = "https://example.com/",
    title: str = "Example Page",
    discarded: bool = False,
    asleep: bool = False,
    status: str = "complete",
) -> dict:
    return {
        "tab_id": tab_id,
        "window_id": window_id,
        "url": url,
        "title": title,
        "active": False,
        "index": 0,
        "discarded": discarded,
        "asleep": asleep,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Pass-through: queued and error responses are never touched.
# ---------------------------------------------------------------------------


def test_queued_response_passes_through_completely_untouched():
    queued = {
        "status": "queued",
        "command_id": "cmd-42",
        "tier": "intermittent",
        "last_seen": "2026-07-25T17:58:02.001+00:00",
        "queue_position": 1,
    }
    result = shape_tabs_response(queued, limit=5, offset=0, window_id=1, url_contains="whatever")
    assert result is queued  # identical object, not a reshaped copy


def test_ok_false_error_response_passes_through_completely_untouched():
    error = {"ok": False, "error": "target is not accessible under current policy"}
    result = shape_tabs_response(error, summary=True)
    assert result is error


# ---------------------------------------------------------------------------
# Fail loud on an unexpected shape -- never silently return an empty list.
# ---------------------------------------------------------------------------


def test_non_dict_response_fails_loud():
    with pytest.raises(TabsPayloadError, match="expected a dict response"):
        shape_tabs_response(["not", "a", "dict"])  # type: ignore[arg-type]


def test_ok_true_with_non_list_result_fails_loud():
    with pytest.raises(TabsPayloadError, match="list of tab dicts"):
        shape_tabs_response({"ok": True, "result": {"unexpected": "shape"}})


def test_ok_true_with_list_of_non_dicts_fails_loud():
    with pytest.raises(TabsPayloadError, match="non-dict item"):
        shape_tabs_response({"ok": True, "result": ["not-a-tab-dict", 42]})


def test_ok_true_with_missing_result_fails_loud():
    with pytest.raises(TabsPayloadError, match="list of tab dicts"):
        shape_tabs_response({"ok": True})


def test_dict_with_neither_ok_nor_status_fails_loud():
    with pytest.raises(TabsPayloadError, match="neither 'ok' nor 'status'"):
        shape_tabs_response({"something": "else"})


def test_negative_offset_fails_loud():
    tabs = [_tab(1)]
    with pytest.raises(ValueError, match="offset must be >= 0"):
        shape_tabs_response({"ok": True, "result": tabs}, offset=-1)


def test_negative_limit_fails_loud():
    tabs = [_tab(1)]
    with pytest.raises(ValueError, match="limit must be >= 0"):
        shape_tabs_response({"ok": True, "result": tabs}, limit=-1)


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_window_id_filter_is_exact_match():
    tabs = [_tab(1, window_id=10), _tab(2, window_id=20), _tab(3, window_id=10)]
    result = shape_tabs_response({"ok": True, "result": tabs}, window_id=10)
    r = result["result"]
    assert r["total"] == 3
    assert r["matched"] == 2
    assert {t["tab_id"] for t in r["tabs"]} == {1, 3}


def test_url_contains_filter_is_case_insensitive_substring():
    tabs = [
        _tab(1, url="https://Example.com/docs"),
        _tab(2, url="https://test.example/other"),
        _tab(3, url="https://unrelated.test/"),
    ]
    result = shape_tabs_response({"ok": True, "result": tabs}, url_contains="EXAMPLE")
    r = result["result"]
    assert r["total"] == 3
    assert r["matched"] == 2
    assert {t["tab_id"] for t in r["tabs"]} == {1, 2}


def test_title_contains_filter_is_case_insensitive_substring():
    tabs = [
        _tab(1, title="Test Page One"),
        _tab(2, title="Another Document"),
        _tab(3, title="test page two"),
    ]
    result = shape_tabs_response({"ok": True, "result": tabs}, title_contains="TEST PAGE")
    r = result["result"]
    assert r["matched"] == 2
    assert {t["tab_id"] for t in r["tabs"]} == {1, 3}


def test_filters_combine_before_pagination():
    tabs = [
        _tab(1, window_id=1, url="https://example.com/a", title="Example A"),
        _tab(2, window_id=1, url="https://example.com/b", title="Example B"),
        _tab(3, window_id=2, url="https://example.com/c", title="Example C"),
        _tab(4, window_id=1, url="https://other.test/", title="Other"),
    ]
    result = shape_tabs_response({"ok": True, "result": tabs}, window_id=1, url_contains="example.com")
    r = result["result"]
    assert r["total"] == 4
    assert r["matched"] == 2
    assert {t["tab_id"] for t in r["tabs"]} == {1, 2}


# ---------------------------------------------------------------------------
# Pagination: offset/limit boundaries, limit=0 unlimited, has_more
# ---------------------------------------------------------------------------


def test_default_pagination_uses_default_limit_and_offset_zero():
    tabs = [_tab(i) for i in range(5)]
    result = shape_tabs_response({"ok": True, "result": tabs})
    r = result["result"]
    assert r["offset"] == 0
    assert r["limit"] == DEFAULT_LIMIT
    assert r["returned"] == 5
    assert r["has_more"] is False


def test_limit_smaller_than_matched_paginates_and_reports_has_more():
    tabs = [_tab(i) for i in range(10)]
    result = shape_tabs_response({"ok": True, "result": tabs}, limit=3, offset=0)
    r = result["result"]
    assert r["returned"] == 3
    assert [t["tab_id"] for t in r["tabs"]] == [0, 1, 2]
    assert r["has_more"] is True
    assert r["matched"] == 10
    assert r["total"] == 10


def test_offset_advances_the_page_and_has_more_flips_false_on_last_page():
    tabs = [_tab(i) for i in range(10)]
    result = shape_tabs_response({"ok": True, "result": tabs}, limit=3, offset=9)
    r = result["result"]
    assert [t["tab_id"] for t in r["tabs"]] == [9]
    assert r["returned"] == 1
    assert r["has_more"] is False


def test_offset_past_the_end_returns_an_empty_page_not_an_error():
    tabs = [_tab(i) for i in range(3)]
    result = shape_tabs_response({"ok": True, "result": tabs}, limit=10, offset=100)
    r = result["result"]
    assert r["tabs"] == []
    assert r["returned"] == 0
    assert r["has_more"] is False
    assert r["matched"] == 3


def test_limit_zero_means_unlimited():
    tabs = [_tab(i) for i in range(250)]
    result = shape_tabs_response({"ok": True, "result": tabs}, limit=0)
    r = result["result"]
    assert r["returned"] == 250
    assert r["limit"] == 0
    assert r["has_more"] is False


def test_limit_zero_still_honors_offset():
    tabs = [_tab(i) for i in range(5)]
    result = shape_tabs_response({"ok": True, "result": tabs}, limit=0, offset=2)
    r = result["result"]
    assert [t["tab_id"] for t in r["tabs"]] == [2, 3, 4]
    assert r["returned"] == 3
    assert r["has_more"] is False


# ---------------------------------------------------------------------------
# Summary mode
# ---------------------------------------------------------------------------


def test_summary_mode_returns_no_tab_list():
    tabs = [_tab(i) for i in range(5)]
    result = shape_tabs_response({"ok": True, "result": tabs}, summary=True)
    r = result["result"]
    assert "tabs" not in r
    assert r["summary"] is True


def test_summary_mode_reports_per_window_counts_and_discarded_asleep():
    tabs = [
        _tab(1, window_id=100, discarded=True),
        _tab(2, window_id=100, asleep=True),
        _tab(3, window_id=100),
        _tab(4, window_id=200, discarded=True, asleep=True),
    ]
    result = shape_tabs_response({"ok": True, "result": tabs}, summary=True)
    r = result["result"]
    assert r["total"] == 4
    assert r["matched"] == 4
    assert r["discarded"] == 2
    assert r["asleep"] == 2
    assert r["windows"][100] == {"count": 3, "discarded": 1, "asleep": 1}
    assert r["windows"][200] == {"count": 1, "discarded": 1, "asleep": 1}


def test_summary_mode_respects_filters():
    tabs = [
        _tab(1, window_id=1, url="https://example.com/"),
        _tab(2, window_id=2, url="https://other.test/"),
    ]
    result = shape_tabs_response({"ok": True, "result": tabs}, summary=True, window_id=1)
    r = result["result"]
    assert r["total"] == 2  # unfiltered grand total
    assert r["matched"] == 1  # only the filtered-in tab
    assert r["windows"] == {1: {"count": 1, "discarded": 0, "asleep": 0}}


def test_empty_tab_list_is_a_valid_ok_true_result_not_a_shape_error():
    result = shape_tabs_response({"ok": True, "result": []})
    r = result["result"]
    assert r["total"] == 0
    assert r["matched"] == 0
    assert r["tabs"] == []
    assert r["has_more"] is False
