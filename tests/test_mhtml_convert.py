"""Tests for mhtml_convert.py -- MHTML -> markdown conversion.

No mock stands in for the MHTML format itself: every test builds a real,
hand-constructed multipart/MIME MHTML document (via Python's own `email`
module -- the same format `archive.py`'s `_record_mhtml_capture` writes to
disk from a real `mhtml` command response) and runs it through the real
`trafilatura`/`html2text`/`lxml` conversion pipeline. Only one test (explicitly
marked) monkeypatches trafilatura's own extraction result, to exercise this
module's OWN "no main content found" fallback branch without depending on
trafilatura's content-detection heuristics picking a particular outcome for a
synthetic page too small to be a realistic test of that heuristic.
"""

from __future__ import annotations

import base64
import hashlib
import quopri
from pathlib import Path

import pytest

from amplifier_browser_bridge.mhtml_convert import ConversionError, convert_mhtml, convert_mhtml_file

# A real 1x1 transparent PNG, used as synthetic (non-user) image payload bytes.
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

_BOUNDARY = "----MultipartBoundary--test----"


def _mime_part(headers: dict[str, str], body_text: str | None = None, body_bytes: bytes | None = None) -> str:
    lines = [f"{k}: {v}" for k, v in headers.items()]
    lines.append("")
    if body_bytes is not None:
        import base64 as _b64

        lines.append(_b64.encodebytes(body_bytes).decode("ascii").rstrip("\n"))
    elif body_text is not None:
        lines.append(body_text)
    return "\r\n".join(lines)


def _build_mhtml(
    *,
    html: str,
    extra_asset_parts: list[str] | None = None,
    extra_html_parts: list[str] | None = None,
) -> bytes:
    """Hand-builds a synthetic multipart/related MHTML document -- the same
    RFC 2557 shape Chrome's `mhtml` command produces (a `From`/`MIME-Version`/
    `Content-Type: multipart/related` header block, followed by MIME parts
    separated by `--<boundary>`). `extra_asset_parts`/`extra_html_parts` are
    pre-built MIME part strings (see `_mime_part`) appended after the primary
    HTML part, for tests that need more than one asset or more than one HTML
    body.
    """
    qp_html = quopri.encodestring(html.encode("utf-8")).decode("ascii")
    parts = [
        _mime_part(
            {
                "Content-Type": "text/html",
                "Content-Transfer-Encoding": "quoted-printable",
                "Content-Location": "https://example.com/",
            },
            body_text=qp_html,
        )
    ]
    parts.extend(extra_html_parts or [])
    parts.extend(extra_asset_parts or [])

    header = (
        "From: <Saved by Blink>\r\n"
        "Snapshot-Content-Location: https://example.com/\r\n"
        "Subject: Test Page\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/related;\r\n"
        '\ttype="text/html";\r\n'
        f'\tboundary="{_BOUNDARY}"\r\n\r\n'
    )
    body = "".join(f"--{_BOUNDARY}\r\n{part}\r\n" for part in parts) + f"--{_BOUNDARY}--\r\n"
    return (header + body).encode("utf-8")


def _asset_part(
    content_location: str | None = None, content_id: str | None = None, payload: bytes = _PNG_1X1
) -> str:
    headers = {"Content-Type": "image/png", "Content-Transfer-Encoding": "base64"}
    if content_location:
        headers["Content-Location"] = content_location
    if content_id:
        headers["Content-ID"] = f"<{content_id}>"
    return _mime_part(headers, body_bytes=payload)


_ARTICLE_HTML = """<!DOCTYPE html>
<html><head><title>Test Page</title></head>
<body>
<nav>Home | About | Contact</nav>
<article>
<h1>Hello World</h1>
<p>This is a real article paragraph with enough content that trafilatura's main-content
identification should recognize it as the actual page content, not boilerplate navigation
or footer chrome, so the extraction path has something real to find here.</p>
<img src="https://example.com/logo.png" alt="logo">
<img src="cid:cidimage@mhtml.test" alt="cid image">
<p>A second paragraph of real article content, following the images, so the extracted
region spans more than a single line and looks like genuine body text to the heuristic.</p>
</article>
<footer>Copyright 2026 Example Corp</footer>
</body></html>
"""


def _fixture_dirs(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "assets", tmp_path / "markdown"


# ---------------------------------------------------------------------------
# Round trip: real synthetic MHTML -> real trafilatura/html2text/lxml pipeline
# ---------------------------------------------------------------------------


def test_convert_mhtml_writes_both_markdown_outputs(tmp_path: Path) -> None:
    mhtml_bytes = _build_mhtml(
        html=_ARTICLE_HTML,
        extra_asset_parts=[
            _asset_part(content_location="https://example.com/logo.png"),
            _asset_part(content_id="cidimage@mhtml.test", content_location="https://example.com/cid-src.png"),
        ],
    )
    assets_dir, markdown_dir = _fixture_dirs(tmp_path)

    result = convert_mhtml(mhtml_bytes, assets_dir=assets_dir, markdown_dir=markdown_dir)

    extracted_path = Path(result["extracted_markdown"]["path"])
    full_page_path = Path(result["full_page_markdown"]["path"])
    assert extracted_path.is_file()
    assert full_page_path.is_file()

    extracted_text = extracted_path.read_text(encoding="utf-8")
    full_page_text = full_page_path.read_text(encoding="utf-8")

    # The extracted (main-content) output should have the article body...
    assert "Hello World" in extracted_text
    assert "real article paragraph" in extracted_text
    # ...but the FULL-PAGE output is the one guaranteed to also carry the nav/footer
    # chrome trafilatura's extraction is specifically designed to drop -- this is
    # the "bad extraction is recoverable" guarantee the module docstring describes.
    assert "Home | About | Contact" in full_page_text
    assert "Copyright 2026 Example Corp" in full_page_text


def test_convert_mhtml_writes_content_addressed_asset_sidecars(tmp_path: Path) -> None:
    mhtml_bytes = _build_mhtml(
        html=_ARTICLE_HTML,
        extra_asset_parts=[
            _asset_part(content_location="https://example.com/logo.png"),
            _asset_part(content_id="cidimage@mhtml.test", content_location="https://example.com/cid-src.png"),
        ],
    )
    assets_dir, markdown_dir = _fixture_dirs(tmp_path)

    result = convert_mhtml(mhtml_bytes, assets_dir=assets_dir, markdown_dir=markdown_dir)

    expected_digest = hashlib.sha256(_PNG_1X1).hexdigest()
    sidecar_path = assets_dir / f"{expected_digest}.png"
    assert sidecar_path.is_file()
    assert sidecar_path.read_bytes() == _PNG_1X1
    assert result["assets"]["count"] == 2
    assert {item["content_type"] for item in result["assets"]["items"]} == {"image/png"}


def test_convert_mhtml_rewrites_content_location_ref_before_conversion(tmp_path: Path) -> None:
    mhtml_bytes = _build_mhtml(
        html=_ARTICLE_HTML,
        extra_asset_parts=[
            _asset_part(content_location="https://example.com/logo.png"),
            _asset_part(content_id="cidimage@mhtml.test", content_location="https://example.com/cid-src.png"),
        ],
    )
    assets_dir, markdown_dir = _fixture_dirs(tmp_path)
    result = convert_mhtml(mhtml_bytes, assets_dir=assets_dir, markdown_dir=markdown_dir)

    full_page_text = Path(result["full_page_markdown"]["path"]).read_text(encoding="utf-8")
    # The original https:// asset URL must never survive into the markdown -- it
    # would be a dead link outside the browser that captured it.
    assert "https://example.com/logo.png" not in full_page_text
    expected_digest = hashlib.sha256(_PNG_1X1).hexdigest()
    assert f"{expected_digest}.png" in full_page_text


def test_convert_mhtml_rewrites_cid_ref_before_conversion(tmp_path: Path) -> None:
    mhtml_bytes = _build_mhtml(
        html=_ARTICLE_HTML,
        extra_asset_parts=[
            _asset_part(content_location="https://example.com/logo.png"),
            _asset_part(content_id="cidimage@mhtml.test", content_location="https://example.com/cid-src.png"),
        ],
    )
    assets_dir, markdown_dir = _fixture_dirs(tmp_path)
    result = convert_mhtml(mhtml_bytes, assets_dir=assets_dir, markdown_dir=markdown_dir)

    full_page_text = Path(result["full_page_markdown"]["path"]).read_text(encoding="utf-8")
    assert "cid:cidimage@mhtml.test" not in full_page_text
    expected_digest = hashlib.sha256(_PNG_1X1).hexdigest()
    assert f"{expected_digest}.png" in full_page_text


def test_convert_mhtml_dedupes_identical_asset_bytes_across_parts(tmp_path: Path) -> None:
    """Two asset parts with byte-identical payloads (a shared logo/icon
    referenced twice) must write only ONE sidecar file -- content-addressed
    naming is what makes this dedup possible, per module docstring."""
    mhtml_bytes = _build_mhtml(
        html=_ARTICLE_HTML,
        extra_asset_parts=[
            _asset_part(content_location="https://example.com/logo.png", payload=_PNG_1X1),
            _asset_part(content_id="cidimage@mhtml.test", payload=_PNG_1X1),
        ],
    )
    assets_dir, markdown_dir = _fixture_dirs(tmp_path)
    result = convert_mhtml(mhtml_bytes, assets_dir=assets_dir, markdown_dir=markdown_dir)

    on_disk = list(assets_dir.iterdir())
    assert len(on_disk) == 1
    assert result["assets"]["count"] == 2
    deduped_flags = sorted(item["deduped"] for item in result["assets"]["items"])
    assert deduped_flags == [False, True]


# ---------------------------------------------------------------------------
# Merged-cell tables: format limit, documented not silently mangled
# ---------------------------------------------------------------------------


def test_convert_mhtml_flags_table_with_merged_cells(tmp_path: Path) -> None:
    html = """<!DOCTYPE html><html><body>
<article>
<p>Enough surrounding paragraph text to give trafilatura something plausible to
identify as the main content region of this synthetic test page.</p>
<table>
<tr><td rowspan="2">Merged</td><td>B</td></tr>
<tr><td>C</td></tr>
</table>
<p>More trailing article text so the region around the table looks like real body
content rather than an isolated fragment.</p>
</article>
</body></html>"""
    mhtml_bytes = _build_mhtml(html=html)
    assets_dir, markdown_dir = _fixture_dirs(tmp_path)
    result = convert_mhtml(mhtml_bytes, assets_dir=assets_dir, markdown_dir=markdown_dir)

    assert len(result["tables_with_merged_cells"]) == 1
    assert result["tables_with_merged_cells"][0]["table_index"] == 0
    assert "Merged" in result["tables_with_merged_cells"][0]["first_row_preview"]


def test_convert_mhtml_does_not_flag_plain_table(tmp_path: Path) -> None:
    html = """<!DOCTYPE html><html><body>
<article>
<p>Enough surrounding paragraph text to give trafilatura something plausible to
identify as the main content region of this synthetic test page, for real.</p>
<table><tr><td>A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>
<p>More trailing article text so the region around the table looks like real body
content rather than an isolated fragment, to be safe.</p>
</article>
</body></html>"""
    mhtml_bytes = _build_mhtml(html=html)
    assets_dir, markdown_dir = _fixture_dirs(tmp_path)
    result = convert_mhtml(mhtml_bytes, assets_dir=assets_dir, markdown_dir=markdown_dir)

    assert result["tables_with_merged_cells"] == []


# ---------------------------------------------------------------------------
# Fail loud: multi-frame MHTML and missing-HTML-part cases
# ---------------------------------------------------------------------------


def test_convert_mhtml_raises_for_multiple_html_parts(tmp_path: Path) -> None:
    """A page with <iframe>s captured as separate frame documents -- the
    documented hard case. Converting only the first body would silently
    discard the rest of the page, so this must fail loud instead."""
    second_html_part = _mime_part(
        {
            "Content-Type": "text/html",
            "Content-Transfer-Encoding": "quoted-printable",
            "Content-Location": "https://example.com/frame2.html",
        },
        body_text=quopri.encodestring(b"<html><body>frame two</body></html>").decode("ascii"),
    )
    mhtml_bytes = _build_mhtml(html=_ARTICLE_HTML, extra_html_parts=[second_html_part])
    assets_dir, markdown_dir = _fixture_dirs(tmp_path)

    with pytest.raises(ConversionError) as exc_info:
        convert_mhtml(mhtml_bytes, assets_dir=assets_dir, markdown_dir=markdown_dir)

    message = str(exc_info.value)
    assert "2 separate text/html parts" in message
    assert "https://example.com/frame2.html" in message
    # Nothing should have been written to disk before the loud failure.
    assert not markdown_dir.exists() or not any(markdown_dir.iterdir())


def test_convert_mhtml_raises_when_no_html_part_present(tmp_path: Path) -> None:
    header = (
        "From: <Saved by Blink>\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/related;\r\n"
        '\ttype="text/html";\r\n'
        f'\tboundary="{_BOUNDARY}"\r\n\r\n'
    )
    body = f"--{_BOUNDARY}\r\n{_asset_part(content_location='https://example.com/only.png')}\r\n--{_BOUNDARY}--\r\n"
    mhtml_bytes = (header + body).encode("utf-8")
    assets_dir, markdown_dir = _fixture_dirs(tmp_path)

    with pytest.raises(ConversionError, match="no text/html"):
        convert_mhtml(mhtml_bytes, assets_dir=assets_dir, markdown_dir=markdown_dir)


# ---------------------------------------------------------------------------
# "No main content found" is not a ConversionError -- it's recorded and the
# conversion still completes (the full-page output still has everything).
# ---------------------------------------------------------------------------


def test_convert_mhtml_records_empty_status_when_extraction_finds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercises this module's OWN fallback branch for a falsy trafilatura
    result, via monkeypatch -- trafilatura's own content-detection heuristic
    is third-party behavior this test does not need to (and should not try
    to) re-verify; every other test in this file uses the real pipeline
    end-to-end."""
    import amplifier_browser_bridge.mhtml_convert as mod

    monkeypatch.setattr(mod.trafilatura, "extract", lambda *a, **k: None)
    mhtml_bytes = _build_mhtml(html=_ARTICLE_HTML)
    assets_dir, markdown_dir = _fixture_dirs(tmp_path)

    result = convert_mhtml(mhtml_bytes, assets_dir=assets_dir, markdown_dir=markdown_dir)

    assert result["extracted_markdown"]["status"] == "empty"
    assert result["extracted_markdown"]["bytes"] == 0
    assert Path(result["extracted_markdown"]["path"]).read_text(encoding="utf-8") == ""
    # The full-page output is unaffected -- it never depends on trafilatura at all.
    assert Path(result["full_page_markdown"]["path"]).read_text(encoding="utf-8") != ""


# ---------------------------------------------------------------------------
# convert_mhtml_file: reads bytes from an actual file on disk
# ---------------------------------------------------------------------------


def test_convert_mhtml_file_reads_from_disk(tmp_path: Path) -> None:
    mhtml_path = tmp_path / "page.mhtml"
    mhtml_path.write_bytes(_build_mhtml(html=_ARTICLE_HTML))
    assets_dir, markdown_dir = _fixture_dirs(tmp_path)

    result = convert_mhtml_file(mhtml_path, assets_dir=assets_dir, markdown_dir=markdown_dir)

    assert Path(result["extracted_markdown"]["path"]).is_file()
