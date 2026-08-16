"""Tests for archive_catalog.py -- Layer 1 structural inventory (pure, no
model) and Layer 2 opt-in per-tab LLM cataloging (via an injected fake
summarizer, or the real default summarizer with `vision.extract_text`
monkeypatched -- NO real LLM call, NO real network, NO real screenshot ever
touched). All fixtures below are synthetic: example.com/example.org URLs,
fabricated titles, fabricated "what"/"who"/"why_kept" text -- never real
browsing data.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from amplifier_browser_bridge.archive_catalog import (
    CatalogError,
    SummarizerValidationError,
    build_inventory,
    default_catalog_path,
    load_catalog,
    make_default_summarizer,
    render_catalog_markdown,
    run_archive_catalog,
)

# A synthetic, tiny "screenshot" -- deliberately fake bytes (mirrors the same
# convention modules/tool-browser-bridge/tests/test_mount.py already uses for
# vision_read fixtures: `b"\xff\xd8\xfake-jpeg"`). Never a real screenshot.
_FAKE_JPEG = b"\xff\xd8\xfake-jpeg-bytes-for-tests\xff\xd9"


def _tab(
    tab_id: int,
    *,
    window_id: int = 1,
    url: str = "https://example.com/",
    title: str = "Example Page",
    **kw: Any,
) -> dict[str, Any]:
    base = {
        "tab_id": tab_id,
        "window_id": window_id,
        "url": url,
        "title": title,
        "discarded": False,
        "asleep": False,
        "pinned": False,
        "group_id": None,
    }
    base.update(kw)
    return base


def _make_archive_dir(tmp_path: Path) -> Path:
    archive_dir = tmp_path / "archive_d1_20260816T000000Z"
    archive_dir.mkdir(parents=True)
    return archive_dir


def _write_tabs_json(archive_dir: Path, tabs: list[dict[str, Any]]) -> None:
    (archive_dir / "tabs.json").write_text(json.dumps(tabs), encoding="utf-8")


def _write_windows_json(
    archive_dir: Path, windows: list[dict[str, Any]], tab_groups: list[dict[str, Any]]
) -> None:
    (archive_dir / "windows.json").write_text(
        json.dumps({"windows": windows, "tab_groups": tab_groups}), encoding="utf-8"
    )


def _write_screenshot(archive_dir: Path, tab_id: int, data: bytes = _FAKE_JPEG, ext: str = "jpg") -> Path:
    tab_dir = archive_dir / "tabs" / str(tab_id)
    tab_dir.mkdir(parents=True, exist_ok=True)
    path = tab_dir / f"screenshot.{ext}"
    path.write_bytes(data)
    return path


def _write_markdown(archive_dir: Path, tab_id: int, text: str) -> Path:
    markdown_dir = archive_dir / "tabs" / str(tab_id) / "markdown"
    markdown_dir.mkdir(parents=True, exist_ok=True)
    path = markdown_dir / "page.extracted.md"
    path.write_text(text, encoding="utf-8")
    return path


class _RecordingSummarizer:
    """A fake `CatalogSummarizer` -- records every call, returns a canned,
    fully-synthetic response. NO network, NO real model, ever."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response or {
            "what": "A synthetic test article about widgets.",
            "who": "Fictional Author",
            "why_kept": "Reference for a fabricated test scenario.",
            "topics": ["widgets", "testing"],
            "value": "medium",
        }

    async def __call__(
        self,
        *,
        tab_id: int,
        url: str,
        title: str,
        markdown: str | None,
        screenshot_bytes: bytes | None,
        screenshot_media_type: str | None,
        lens: str | None,
        retry_reason: str | None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "tab_id": tab_id,
                "url": url,
                "title": title,
                "markdown": markdown,
                "screenshot_bytes": screenshot_bytes,
                "screenshot_media_type": screenshot_media_type,
                "lens": lens,
                "retry_reason": retry_reason,
            }
        )
        return self._response


class _FailNTimesThenSucceedSummarizer:
    """Raises `SummarizerValidationError` for the first `fail_count` calls,
    then returns a canned synthetic response -- used to prove the
    orchestrator's one-bounded-retry behavior."""

    def __init__(self, fail_count: int) -> None:
        self.fail_count = fail_count
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if len(self.calls) <= self.fail_count:
            raise SummarizerValidationError("missing a non-empty 'what' field (synthetic test failure)")
        return {
            "what": "Recovered after retry.",
            "who": "",
            "why_kept": "",
            "topics": [],
            "value": "low",
        }


# ---------------------------------------------------------------------------
# Layer 1: pure structural inventory -- no model, no I/O of its own
# ---------------------------------------------------------------------------


def test_build_inventory_duplicates_state_and_domain_counts_with_no_model():
    tabs = [
        _tab(1, url="https://example.com/a", title="A"),
        _tab(2, url="https://example.com/a", title="A dup"),
        _tab(3, url="https://example.com/b", title="B", discarded=True),
        _tab(4, url="https://example.org/c", title="C", pinned=True),
        _tab(5, url="https://example.org/d", title="D", asleep=True),
    ]

    inventory = build_inventory(tabs)

    assert inventory["total_tabs"] == 5
    assert inventory["state"] == {"awake": 3, "asleep": 1, "discarded": 1, "pinned": 1}
    assert inventory["duplicate_tab_waste"] == 1
    assert len(inventory["duplicates"]) == 1
    dup = inventory["duplicates"][0]
    assert dup["url"] == "https://example.com/a"
    assert dup["count"] == 2
    assert dup["closeable"] == 1
    assert set(dup["tab_ids"]) == {1, 2}
    domains = {d["domain"]: d["count"] for d in inventory["by_domain"]}
    assert domains["example.com"] == 3
    assert domains["example.org"] == 2


def test_build_inventory_per_window_breakdown():
    tabs = [
        _tab(1, window_id=10),
        _tab(2, window_id=10, discarded=True),
        _tab(3, window_id=20, pinned=True),
    ]

    inventory = build_inventory(tabs)

    assert inventory["by_window"]["10"] == {"total": 2, "awake": 1, "discarded": 1, "asleep": 0, "pinned": 0}
    assert inventory["by_window"]["20"] == {"total": 1, "awake": 1, "discarded": 0, "asleep": 0, "pinned": 1}


def test_build_inventory_uses_windows_json_tab_group_metadata_when_present():
    tabs = [_tab(1, group_id=7), _tab(2, group_id=7)]
    windows = {
        "windows": [{"window_id": 1}],
        "tab_groups": [{"group_id": 7, "window_id": 1, "title": "Research", "color": "blue"}],
    }

    inventory = build_inventory(tabs, windows)

    assert inventory["by_group"]["7"]["title"] == "Research"
    assert inventory["by_group"]["7"]["color"] == "blue"
    assert inventory["by_group"]["7"]["total"] == 2


def test_build_inventory_top_n_bounds_duplicates_and_domains_not_aggregate_counts():
    tabs = []
    for i in range(50):
        tabs.append(_tab(i * 2, url=f"https://example.com/dup{i}"))
        tabs.append(_tab(i * 2 + 1, url=f"https://example.com/dup{i}"))  # every URL duplicated once

    inventory = build_inventory(tabs, top_n=5)

    assert len(inventory["duplicates"]) == 5  # bounded
    assert inventory["duplicate_tab_waste"] == 50  # aggregate, NOT bounded by top_n


# ---------------------------------------------------------------------------
# run_archive_catalog: pre-flight failure, Layer-1-only default
# ---------------------------------------------------------------------------


def test_raises_when_archive_dir_has_no_tabs_json(tmp_path: Path):
    not_an_archive = tmp_path / "not_an_archive"
    not_an_archive.mkdir()

    async def _run():
        await run_archive_catalog(not_an_archive)

    with pytest.raises(CatalogError, match="tabs.json"):
        asyncio.run(_run())


@pytest.mark.asyncio
async def test_default_catalog_false_returns_layer1_only_no_model_no_summarizer_needed(tmp_path: Path):
    """catalog=False (the default) never touches Layer 2 at all -- not even a
    summarizer needs to be constructed/injected."""
    archive_dir = _make_archive_dir(tmp_path)
    _write_tabs_json(archive_dir, [_tab(1), _tab(2, url="https://example.com/", discarded=True)])

    outcome = await run_archive_catalog(archive_dir)
    manifest = outcome["result"]

    assert outcome["ok"] is True
    assert manifest["catalog"] is None
    assert manifest["inventory"]["total_tabs"] == 2
    assert Path(manifest["manifest_path"]).is_file()
    on_disk = json.loads(Path(manifest["manifest_path"]).read_text(encoding="utf-8"))
    assert on_disk["catalog"] is None


# ---------------------------------------------------------------------------
# Layer 2: the lens reaches the summarizer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lens_reaches_the_injected_summarizer(tmp_path: Path):
    archive_dir = _make_archive_dir(tmp_path)
    _write_tabs_json(archive_dir, [_tab(1)])
    _write_markdown(archive_dir, 1, "Some fabricated synthetic page text about widgets.")
    recorder = _RecordingSummarizer()
    lens_text = "A fabricated test reader who works on distributed systems and trusts Jane Doe's writing."

    await run_archive_catalog(archive_dir, catalog=True, lens=lens_text, summarizer=recorder)

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["lens"] == lens_text


@pytest.mark.asyncio
async def test_no_lens_is_none_not_empty_string(tmp_path: Path):
    archive_dir = _make_archive_dir(tmp_path)
    _write_tabs_json(archive_dir, [_tab(1)])
    _write_markdown(archive_dir, 1, "Fabricated synthetic text.")
    recorder = _RecordingSummarizer()

    await run_archive_catalog(archive_dir, catalog=True, summarizer=recorder)

    assert recorder.calls[0]["lens"] is None


# ---------------------------------------------------------------------------
# Layer 2: screenshot-preferred / markdown-fallback / no_content-recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screenshot_preferred_when_present(tmp_path: Path):
    archive_dir = _make_archive_dir(tmp_path)
    _write_tabs_json(archive_dir, [_tab(1)])
    _write_screenshot(archive_dir, 1)
    _write_markdown(archive_dir, 1, "Some fabricated markdown text too.")
    recorder = _RecordingSummarizer()

    outcome = await run_archive_catalog(archive_dir, catalog=True, summarizer=recorder)

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["screenshot_bytes"] == _FAKE_JPEG
    assert outcome["result"]["catalog"]["tabs_cataloged"] == 1


@pytest.mark.asyncio
async def test_markdown_fallback_when_no_screenshot(tmp_path: Path):
    archive_dir = _make_archive_dir(tmp_path)
    _write_tabs_json(archive_dir, [_tab(1)])
    _write_markdown(archive_dir, 1, "Only fabricated markdown text, no screenshot at all.")
    recorder = _RecordingSummarizer()

    await run_archive_catalog(archive_dir, catalog=True, summarizer=recorder)

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["screenshot_bytes"] is None
    assert recorder.calls[0]["markdown"] == "Only fabricated markdown text, no screenshot at all."


@pytest.mark.asyncio
async def test_no_content_is_recorded_not_silently_skipped_and_summarizer_never_called(tmp_path: Path):
    archive_dir = _make_archive_dir(tmp_path)
    _write_tabs_json(archive_dir, [_tab(1)])  # no tabs/1/ directory at all
    recorder = _RecordingSummarizer()

    outcome = await run_archive_catalog(archive_dir, catalog=True, summarizer=recorder)
    result = outcome["result"]
    sidecar = load_catalog(archive_dir)

    assert recorder.calls == []  # never called -- no fabricated summary
    assert sidecar["tabs"]["1"]["status"] == "no_content"
    assert "reason" in sidecar["tabs"]["1"]
    assert result["catalog"]["tabs_no_content"] == 1
    assert result["catalog"]["status"] == "ok_with_skips"


# ---------------------------------------------------------------------------
# Layer 2: fail-loud on a response missing what/value, with one bounded retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_error_is_retried_exactly_once_then_succeeds(tmp_path: Path):
    archive_dir = _make_archive_dir(tmp_path)
    _write_tabs_json(archive_dir, [_tab(1)])
    _write_markdown(archive_dir, 1, "Fabricated text.")
    summarizer = _FailNTimesThenSucceedSummarizer(fail_count=1)

    outcome = await run_archive_catalog(archive_dir, catalog=True, summarizer=summarizer)
    sidecar = load_catalog(archive_dir)

    assert len(summarizer.calls) == 2  # one failure + one retry
    assert summarizer.calls[0]["retry_reason"] is None
    assert summarizer.calls[1]["retry_reason"] is not None
    assert "what" in summarizer.calls[1]["retry_reason"]
    assert sidecar["tabs"]["1"]["status"] == "ok"
    assert sidecar["tabs"]["1"]["what"] == "Recovered after retry."
    assert outcome["result"]["catalog"]["tabs_cataloged"] == 1


@pytest.mark.asyncio
async def test_validation_error_after_the_one_retry_is_recorded_failed_not_fabricated(tmp_path: Path):
    archive_dir = _make_archive_dir(tmp_path)
    _write_tabs_json(archive_dir, [_tab(1)])
    _write_markdown(archive_dir, 1, "Fabricated text.")
    summarizer = _FailNTimesThenSucceedSummarizer(fail_count=99)  # always fails

    outcome = await run_archive_catalog(archive_dir, catalog=True, summarizer=summarizer)
    sidecar = load_catalog(archive_dir)

    assert len(summarizer.calls) == 2  # exactly one retry, never more
    assert sidecar["tabs"]["1"]["status"] == "failed"
    assert "after 2 attempt(s)" in sidecar["tabs"]["1"]["error"]
    assert outcome["result"]["catalog"]["tabs_failed"] == 1
    assert outcome["result"]["catalog"]["status"] == "ok_with_failures"
    assert any(f["tab_id"] == "1" for f in outcome["result"]["catalog"]["failures"])


@pytest.mark.asyncio
async def test_non_validation_exception_is_permanent_no_retry(tmp_path: Path):
    archive_dir = _make_archive_dir(tmp_path)
    _write_tabs_json(archive_dir, [_tab(1)])
    _write_markdown(archive_dir, 1, "Fabricated text.")

    calls: list[int] = []

    async def _always_raises(**kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        raise RuntimeError("synthetic non-validation failure (e.g. a config error)")

    await run_archive_catalog(archive_dir, catalog=True, summarizer=_always_raises)
    sidecar = load_catalog(archive_dir)

    assert len(calls) == 1  # NOT retried -- only SummarizerValidationError is retryable
    assert sidecar["tabs"]["1"]["status"] == "failed"
    assert "synthetic non-validation failure" in sidecar["tabs"]["1"]["error"]


# ---------------------------------------------------------------------------
# Manifest-return: the tool must NEVER return the catalog judgment text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manifest_never_contains_catalog_judgment_text(tmp_path: Path):
    archive_dir = _make_archive_dir(tmp_path)
    _write_tabs_json(archive_dir, [_tab(1)])
    _write_markdown(archive_dir, 1, "Fabricated synthetic article body.")
    secret_marker = "UNIQUE_FABRICATED_JUDGMENT_TEXT_MARKER_12345"
    recorder = _RecordingSummarizer(
        response={
            "what": secret_marker,
            "who": "Fictional Author",
            "why_kept": "Fabricated reason for keeping this synthetic tab.",
            "topics": ["synthetic"],
            "value": "high",
        }
    )

    outcome = await run_archive_catalog(archive_dir, catalog=True, summarizer=recorder)

    # Load-bearing: the marker must never appear in the returned manifest...
    assert secret_marker not in json.dumps(outcome)
    # ...but it MUST be persisted to the sidecar file on disk.
    sidecar_path = default_catalog_path(archive_dir)
    assert secret_marker in sidecar_path.read_text(encoding="utf-8")
    # The manifest's per-tab entry is STATUS + value tier only, never judgment text.
    catalog_summary = outcome["result"]["catalog"]
    assert "what" not in json.dumps(catalog_summary)
    assert catalog_summary["value_tally"]["high"] == 1


@pytest.mark.asyncio
async def test_tab_ids_not_in_tabs_json_is_recorded_not_found(tmp_path: Path):
    archive_dir = _make_archive_dir(tmp_path)
    _write_tabs_json(archive_dir, [_tab(1)])
    _write_markdown(archive_dir, 1, "Fabricated text.")
    recorder = _RecordingSummarizer()

    outcome = await run_archive_catalog(archive_dir, catalog=True, tab_ids=[1, 999], summarizer=recorder)
    sidecar = load_catalog(archive_dir)

    assert sidecar["tabs"]["999"]["status"] == "not_found"
    assert outcome["result"]["catalog"]["tabs_not_found"] == 1
    assert outcome["result"]["catalog"]["status"] == "ok_with_skips"


# ---------------------------------------------------------------------------
# render_catalog_markdown: bounded rendering of a pathological field
# ---------------------------------------------------------------------------


def test_render_catalog_markdown_truncates_a_pathologically_long_field():
    pathological_what = "X" * 100_000  # a deliberately huge synthetic field
    catalog = {
        "tabs": {
            "1": {
                "status": "ok",
                "tab_id": 1,
                "url": "https://example.com/",
                "title": "Example",
                "what": pathological_what,
                "who": "",
                "why_kept": "",
                "topics": [],
                "value": "high",
            }
        }
    }

    rendered = render_catalog_markdown(catalog)

    assert len(rendered) < 5_000  # bounded, independent of the 100,000-char field
    assert "chars]" in rendered  # names exactly how much was cut, never silent


def test_render_catalog_markdown_bounds_entries_per_section_independent_of_catalog_size():
    catalog = {
        "tabs": {
            str(i): {
                "status": "ok",
                "tab_id": i,
                "url": f"https://example.com/{i}",
                "title": f"Page {i}",
                "what": "Fabricated synthetic summary.",
                "who": "",
                "why_kept": "",
                "topics": [],
                "value": "medium",
            }
            for i in range(500)
        }
    }

    rendered = render_catalog_markdown(catalog, max_per_section=10)

    assert rendered.count("- **Page") == 10
    assert "more not shown" in rendered


def test_render_catalog_markdown_groups_by_value_and_skips_non_ok_entries():
    catalog = {
        "tabs": {
            "1": {
                "status": "ok",
                "tab_id": 1,
                "url": "https://example.com/1",
                "title": "High one",
                "what": "w",
                "value": "high",
            },
            "2": {"status": "no_content", "tab_id": 2, "url": "https://example.com/2", "title": "Skipped"},
            "3": {
                "status": "failed",
                "tab_id": 3,
                "url": "https://example.com/3",
                "title": "Failed",
                "error": "boom",
            },
        }
    }

    rendered = render_catalog_markdown(catalog)

    assert "High one" in rendered
    assert "Skipped" not in rendered
    assert "Failed" not in rendered


def test_render_catalog_markdown_empty_catalog_is_honest_not_fabricated():
    rendered = render_catalog_markdown({"tabs": {}})
    assert "no cataloged tabs" in rendered


# ---------------------------------------------------------------------------
# make_default_summarizer: the real default implementation, with
# vision.extract_text monkeypatched -- NO real network call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_summarizer_reuses_vision_extract_text_with_screenshot_as_image(monkeypatch):
    captured: dict[str, Any] = {}

    async def _fake_extract_text(images, prompt, *, config=None, media_type="image/jpeg"):
        captured["images"] = images
        captured["prompt"] = prompt
        captured["media_type"] = media_type
        return {
            "text": '{"what": "Synthetic finding.", "who": "", "why_kept": "", "topics": [], "value": "low"}'
        }

    monkeypatch.setattr("amplifier_browser_bridge.archive_catalog._vision_extract_text", _fake_extract_text)

    summarizer = make_default_summarizer()
    result = await summarizer(
        tab_id=1,
        url="https://example.com/",
        title="Example",
        markdown=None,
        screenshot_bytes=_FAKE_JPEG,
        screenshot_media_type="image/jpeg",
        lens="A fabricated reader lens.",
        retry_reason=None,
    )

    assert captured["images"] == [_FAKE_JPEG]
    assert "READER LENS" in captured["prompt"]
    assert "A fabricated reader lens." in captured["prompt"]
    assert result["what"] == "Synthetic finding."
    assert result["value"] == "low"


@pytest.mark.asyncio
async def test_default_summarizer_text_only_call_uses_empty_images_list(monkeypatch):
    captured: dict[str, Any] = {}

    async def _fake_extract_text(images, prompt, *, config=None, media_type="image/jpeg"):
        captured["images"] = images
        return {"text": '{"what": "Text-only finding.", "value": "medium"}'}

    monkeypatch.setattr("amplifier_browser_bridge.archive_catalog._vision_extract_text", _fake_extract_text)

    summarizer = make_default_summarizer()
    await summarizer(
        tab_id=1,
        url="https://example.com/",
        title="Example",
        markdown="Fabricated synthetic markdown body.",
        screenshot_bytes=None,
        screenshot_media_type=None,
        lens=None,
        retry_reason=None,
    )

    assert captured["images"] == []


@pytest.mark.asyncio
async def test_default_summarizer_raises_validation_error_on_missing_what(monkeypatch):
    async def _fake_extract_text(images, prompt, *, config=None, media_type="image/jpeg"):
        return {"text": '{"value": "high"}'}  # missing "what"

    monkeypatch.setattr("amplifier_browser_bridge.archive_catalog._vision_extract_text", _fake_extract_text)

    summarizer = make_default_summarizer()
    with pytest.raises(SummarizerValidationError, match="what"):
        await summarizer(
            tab_id=1,
            url="https://example.com/",
            title="Example",
            markdown="Fabricated text.",
            screenshot_bytes=None,
            screenshot_media_type=None,
            lens=None,
            retry_reason=None,
        )


@pytest.mark.asyncio
async def test_default_summarizer_raises_validation_error_on_invalid_value(monkeypatch):
    async def _fake_extract_text(images, prompt, *, config=None, media_type="image/jpeg"):
        return {"text": '{"what": "Something.", "value": "extremely-high"}'}  # not a valid tier

    monkeypatch.setattr("amplifier_browser_bridge.archive_catalog._vision_extract_text", _fake_extract_text)

    summarizer = make_default_summarizer()
    with pytest.raises(SummarizerValidationError, match="value"):
        await summarizer(
            tab_id=1,
            url="https://example.com/",
            title="Example",
            markdown="Fabricated text.",
            screenshot_bytes=None,
            screenshot_media_type=None,
            lens=None,
            retry_reason=None,
        )


@pytest.mark.asyncio
async def test_default_summarizer_tolerates_a_markdown_code_fence(monkeypatch):
    async def _fake_extract_text(images, prompt, *, config=None, media_type="image/jpeg"):
        return {"text": '```json\n{"what": "Fenced finding.", "value": "low"}\n```'}

    monkeypatch.setattr("amplifier_browser_bridge.archive_catalog._vision_extract_text", _fake_extract_text)

    summarizer = make_default_summarizer()
    result = await summarizer(
        tab_id=1,
        url="https://example.com/",
        title="Example",
        markdown="Fabricated text.",
        screenshot_bytes=None,
        screenshot_media_type=None,
        lens=None,
        retry_reason=None,
    )

    assert result["what"] == "Fenced finding."
