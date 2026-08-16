"""Tests for archive_convert.py -- the after-the-fact MHTML -> markdown
orchestrator that wires mhtml_convert.py into an existing browser-state
archive directory (archive.py's run_archive output).

Builds real archive_dir/tabs/<id>/page.mhtml files on disk (real synthetic
MHTML, same builder as test_mhtml_convert.py) rather than mocking the
filesystem -- these tests exercise the real conversion pipeline end-to-end,
just orchestrated across multiple tabs instead of a single call.
"""

from __future__ import annotations

import base64
import json
import quopri
from pathlib import Path

import pytest

from amplifier_browser_bridge.archive_convert import ConversionError, run_archive_convert

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_BOUNDARY = "----MultipartBoundary--test----"

_SIMPLE_HTML = """<!DOCTYPE html><html><body>
<article><p>Some real article text for this synthetic archived tab, long enough
to give trafilatura something plausible to identify as the main content.</p></article>
</body></html>"""


def _mhtml_bytes(html: str = _SIMPLE_HTML, *, asset_content_location: str | None = None) -> bytes:
    header = (
        "From: <Saved by Blink>\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: multipart/related;\r\n"
        '\ttype="text/html";\r\n'
        f'\tboundary="{_BOUNDARY}"\r\n\r\n'
    )
    qp_html = quopri.encodestring(html.encode("utf-8")).decode("ascii")
    parts = [
        (
            f"--{_BOUNDARY}\r\n"
            "Content-Type: text/html\r\n"
            "Content-Transfer-Encoding: quoted-printable\r\n"
            "Content-Location: https://example.com/\r\n\r\n"
            f"{qp_html}\r\n"
        )
    ]
    if asset_content_location:
        b64 = base64.encodebytes(_PNG_1X1).decode("ascii").rstrip("\n")
        parts.append(
            f"--{_BOUNDARY}\r\n"
            "Content-Type: image/png\r\n"
            "Content-Transfer-Encoding: base64\r\n"
            f"Content-Location: {asset_content_location}\r\n\r\n"
            f"{b64}\r\n"
        )
    body = "".join(parts) + f"--{_BOUNDARY}--\r\n"
    return (header + body).encode("utf-8")


def _write_tab_mhtml(archive_dir: Path, tab_id: int, mhtml: bytes) -> None:
    tab_dir = archive_dir / "tabs" / str(tab_id)
    tab_dir.mkdir(parents=True, exist_ok=True)
    (tab_dir / "page.mhtml").write_bytes(mhtml)


def _make_archive_dir(tmp_path: Path) -> Path:
    archive_dir = tmp_path / "archive_d1_20260815T000000Z"
    (archive_dir / "tabs").mkdir(parents=True)
    return archive_dir


# ---------------------------------------------------------------------------
# Basic conversion across multiple tabs
# ---------------------------------------------------------------------------


def test_converts_every_tab_with_an_mhtml_capture(tmp_path: Path) -> None:
    archive_dir = _make_archive_dir(tmp_path)
    _write_tab_mhtml(archive_dir, 101, _mhtml_bytes())
    _write_tab_mhtml(archive_dir, 102, _mhtml_bytes())

    outcome = run_archive_convert(archive_dir)
    manifest = outcome["result"]

    assert manifest["status"] == "ok"
    assert set(manifest["tabs"]) == {"101", "102"}
    assert manifest["tabs"]["101"]["status"] == "ok"
    assert manifest["tabs"]["102"]["status"] == "ok"
    assert manifest["summary"]["tabs_converted"] == 2
    assert manifest["summary"]["tabs_failed"] == 0
    assert manifest["summary"]["tabs_not_captured"] == 0
    assert Path(manifest["tabs"]["101"]["extracted_markdown"]["path"]).is_file()


def test_tab_without_mhtml_capture_reports_not_captured_not_silently_skipped(tmp_path: Path) -> None:
    """A tab archived below L4 (no MHTML captured for it at all) must still
    get an accounted-for entry -- never just absent from manifest["tabs"]."""
    archive_dir = _make_archive_dir(tmp_path)
    _write_tab_mhtml(archive_dir, 101, _mhtml_bytes())
    (archive_dir / "tabs" / "102").mkdir()  # tab dir exists, but no page.mhtml (e.g. L2 archive)

    outcome = run_archive_convert(archive_dir)
    manifest = outcome["result"]

    assert manifest["tabs"]["102"]["status"] == "not_captured"
    assert "reason" in manifest["tabs"]["102"]
    assert manifest["summary"]["tabs_not_captured"] == 1
    assert manifest["status"] == "ok_with_skips"
    assert manifest["summary"]["has_failures"] is False


def test_tab_ids_filter_restricts_conversion_and_reports_not_captured_for_the_rest(
    tmp_path: Path,
) -> None:
    archive_dir = _make_archive_dir(tmp_path)
    _write_tab_mhtml(archive_dir, 101, _mhtml_bytes())
    _write_tab_mhtml(archive_dir, 102, _mhtml_bytes())

    outcome = run_archive_convert(archive_dir, tab_ids=[101, 999])
    manifest = outcome["result"]

    assert set(manifest["tabs"]) == {"101", "999"}
    assert manifest["tabs"]["101"]["status"] == "ok"
    assert manifest["tabs"]["999"]["status"] == "not_captured"
    assert manifest["summary"]["tabs_requested"] == 2


# ---------------------------------------------------------------------------
# Shared, content-addressed assets directory across the whole archive
# ---------------------------------------------------------------------------


def test_shares_content_addressed_assets_dir_across_tabs(tmp_path: Path) -> None:
    """The same logo referenced by two different tabs must dedupe into ONE
    sidecar file under the archive-wide assets/ dir, not one per tab."""
    archive_dir = _make_archive_dir(tmp_path)
    _write_tab_mhtml(archive_dir, 101, _mhtml_bytes(asset_content_location="https://example.com/logo.png"))
    _write_tab_mhtml(archive_dir, 102, _mhtml_bytes(asset_content_location="https://example.com/logo.png"))

    outcome = run_archive_convert(archive_dir)
    manifest = outcome["result"]

    assets_dir = Path(manifest["assets_dir"])
    assert assets_dir == archive_dir / "assets"
    png_files = list(assets_dir.glob("*.png"))
    assert len(png_files) == 1  # deduped across both tabs
    assert (
        manifest["tabs"]["101"]["assets"]["items"][0]["path"]
        == manifest["tabs"]["102"]["assets"]["items"][0]["path"]
    )


# ---------------------------------------------------------------------------
# Failures: a per-tab conversion failure degrades status without stopping
# the whole run, mirroring archive.py's own no-abort-on-one-bad-tab behavior.
# ---------------------------------------------------------------------------


def test_one_tab_failing_to_convert_does_not_abort_the_run(tmp_path: Path) -> None:
    archive_dir = _make_archive_dir(tmp_path)
    _write_tab_mhtml(archive_dir, 101, _mhtml_bytes())
    # A malformed MHTML (no text/html part at all) for this tab -- triggers
    # mhtml_convert.ConversionError, which run_archive_convert must catch per
    # tab, not let propagate and abort every other tab's conversion.
    bad_header = (
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/related; boundary="X"\r\n\r\n'
        "--X\r\nContent-Type: image/png\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        + base64.encodebytes(_PNG_1X1).decode("ascii")
        + "\r\n--X--\r\n"
    )
    _write_tab_mhtml(archive_dir, 102, bad_header.encode("utf-8"))

    outcome = run_archive_convert(archive_dir)
    manifest = outcome["result"]

    assert manifest["tabs"]["101"]["status"] == "ok"
    assert manifest["tabs"]["102"]["status"] == "failed"
    assert "error" in manifest["tabs"]["102"]
    assert manifest["status"] == "ok_with_failures"
    assert any(f["tab_id"] == "102" for f in manifest["failures"])


def test_multi_frame_mhtml_reports_failed_not_partial_conversion(tmp_path: Path) -> None:
    """The documented hard case (multiple text/html frame bodies) must fail
    loud per-tab, never silently convert only the first frame."""
    archive_dir = _make_archive_dir(tmp_path)
    second_frame = (
        f"--{_BOUNDARY}\r\n"
        "Content-Type: text/html\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n"
        "Content-Location: https://example.com/frame2.html\r\n\r\n"
        + quopri.encodestring(b"<html><body>frame two</body></html>").decode("ascii")
        + "\r\n"
    )
    header = (
        "MIME-Version: 1.0\r\n"
        f'Content-Type: multipart/related; boundary="{_BOUNDARY}"\r\n\r\n'
        f"--{_BOUNDARY}\r\n"
        "Content-Type: text/html\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n"
        "Content-Location: https://example.com/\r\n\r\n"
        + quopri.encodestring(_SIMPLE_HTML.encode("utf-8")).decode("ascii")
        + "\r\n"
        + second_frame
        + f"--{_BOUNDARY}--\r\n"
    )
    _write_tab_mhtml(archive_dir, 101, header.encode("utf-8"))

    outcome = run_archive_convert(archive_dir)
    manifest = outcome["result"]

    assert manifest["tabs"]["101"]["status"] == "failed"
    assert "frame" in manifest["tabs"]["101"]["error"].lower()


# ---------------------------------------------------------------------------
# Pre-flight failure: not an archive directory at all
# ---------------------------------------------------------------------------


def test_raises_when_archive_dir_has_no_tabs_subdirectory(tmp_path: Path) -> None:
    not_an_archive = tmp_path / "not_an_archive"
    not_an_archive.mkdir()

    with pytest.raises(ConversionError, match="tabs/"):
        run_archive_convert(not_an_archive)


# ---------------------------------------------------------------------------
# conversion_manifest.json is actually written to disk
# ---------------------------------------------------------------------------


def test_writes_conversion_manifest_json_to_disk(tmp_path: Path) -> None:
    archive_dir = _make_archive_dir(tmp_path)
    _write_tab_mhtml(archive_dir, 101, _mhtml_bytes())

    outcome = run_archive_convert(archive_dir)
    manifest = outcome["result"]

    manifest_path = Path(manifest["manifest_path"])
    assert manifest_path == archive_dir / "conversion_manifest.json"
    assert manifest_path.is_file()
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert on_disk["status"] == "ok"
    assert set(on_disk["tabs"]) == {"101"}
