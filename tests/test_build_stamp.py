"""Tests for build_stamp.py -- the build-freshness handshake (the sibling of
skew.py's command-set handshake).

Covers exactly the properties the real incident this module closes depends on:
identical file sets produce identical stamps, changing any shipped byte changes
the stamp, a locally-staged copy and the zip-served copy agree, a device
reporting no stamp is flagged stale (not "unknown, assume current"), and the
`BuildFreshness`/`describe_freshness` reporting surface.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from amplifier_browser_bridge.build_stamp import (
    BuildFreshness,
    BuildStampError,
    compute_build_stamp,
    compute_freshness,
    current_build_stamp,
    describe_freshness,
)
from amplifier_browser_bridge.extension_zip import build_extension_zip_bytes
from amplifier_browser_bridge.setup import _EXTENSION_FILES, find_extension_source, stage_extension


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# ---------------------------------------------------------------------------
# compute_build_stamp -- determinism and byte-sensitivity
# ---------------------------------------------------------------------------


def test_identical_file_sets_produce_identical_stamps(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    for d in (dir_a, dir_b):
        _write(d / "one.js", b"console.log(1);")
        _write(d / "two.js", b"console.log(2);")

    files = ("one.js", "two.js")
    assert compute_build_stamp(dir_a, files=files) == compute_build_stamp(dir_b, files=files)


def test_changing_a_single_shipped_byte_changes_the_stamp(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    _write(dir_a / "options.js", b"const x = 1;")
    _write(dir_b / "options.js", b"const x = 2;")  # one byte different

    files = ("options.js",)
    assert compute_build_stamp(dir_a, files=files) != compute_build_stamp(dir_b, files=files)


def test_adding_or_removing_a_shipped_file_changes_the_stamp(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    _write(dir_a / "one.js", b"1")
    _write(dir_b / "one.js", b"1")
    _write(dir_b / "two.js", b"2")

    assert compute_build_stamp(dir_a, files=("one.js",)) != compute_build_stamp(
        dir_b, files=("one.js", "two.js")
    )


def test_file_order_in_the_files_argument_does_not_matter(tmp_path: Path) -> None:
    """`compute_build_stamp` sorts internally -- the caller's iteration order
    of `files` must not leak into the digest."""
    d = tmp_path / "d"
    _write(d / "a.js", b"1")
    _write(d / "b.js", b"2")

    assert compute_build_stamp(d, files=("a.js", "b.js")) == compute_build_stamp(d, files=("b.js", "a.js"))


def test_renaming_a_file_with_identical_bytes_changes_the_stamp(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    _write(dir_a / "a.js", b"same content")
    _write(dir_b / "b.js", b"same content")

    assert compute_build_stamp(dir_a, files=("a.js",)) != compute_build_stamp(dir_b, files=("b.js",))


def test_compute_build_stamp_raises_build_stamp_error_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BuildStampError, match="missing.js"):
        compute_build_stamp(tmp_path, files=("missing.js",))


def test_compute_build_stamp_default_files_is_extension_files(tmp_path: Path) -> None:
    """Sanity check that the default `files` argument really is setup.py's
    single source of truth, not an independent copy -- staging the real
    extension source and hashing with the default `files=` must succeed."""
    staged = stage_extension(dest=tmp_path / "staged")
    # Must not raise -- every name in the default `files` (== _EXTENSION_FILES)
    # is present in a real staged directory.
    assert compute_build_stamp(staged)


# ---------------------------------------------------------------------------
# current_build_stamp -- hub's own "what should this be right now"
# ---------------------------------------------------------------------------


def test_current_build_stamp_matches_direct_computation_over_the_real_source() -> None:
    assert current_build_stamp() == compute_build_stamp(find_extension_source())


def test_current_build_stamp_matches_a_freshly_staged_copy(tmp_path: Path) -> None:
    """The core equivalence this module's design depends on: a source-tree
    read and a freshly staged copy's read must be byte-identical (staging
    only ever copies bytes, never transforms them)."""
    staged = stage_extension(dest=tmp_path / "staged")
    assert current_build_stamp() == compute_build_stamp(staged)


def test_current_build_stamp_honors_explicit_source_override(tmp_path: Path) -> None:
    override_dir = tmp_path / "override"
    for name in _EXTENSION_FILES:
        _write(override_dir / name, f"content of {name}".encode())

    assert current_build_stamp(override_dir) == compute_build_stamp(override_dir)


# ---------------------------------------------------------------------------
# Locally-staged copy vs zip-served copy MUST agree (the real gap named in
# extension_zip.py's module docstring: "the stamp it carries must agree with
# what a locally-staged copy would carry, or a remote install will look
# permanently stale").
# ---------------------------------------------------------------------------


def test_locally_staged_copy_and_zip_served_copy_produce_the_same_stamp(tmp_path: Path) -> None:
    local_stage = stage_extension(dest=tmp_path / "local")
    local_stamp = compute_build_stamp(local_stage)

    zip_bytes = build_extension_zip_bytes()
    extracted = tmp_path / "from-zip"
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(extracted)
    zip_stamp = compute_build_stamp(extracted)

    assert local_stamp == zip_stamp == current_build_stamp()


# ---------------------------------------------------------------------------
# BuildFreshness / compute_freshness / describe_freshness
# ---------------------------------------------------------------------------

_HUB_STAMP = "a" * 64


def test_device_reporting_no_stamp_is_flagged_stale_not_unknown() -> None:
    """The pre-stamp case -- mirrors skew.py's pre-handshake discipline
    exactly: `None` is a DEFINITIVELY STALE state, never conflated with
    'unknown, assume current.'"""
    report = compute_freshness(None, _HUB_STAMP)
    assert report.known is False
    assert report.current is False

    message = describe_freshness(report)
    assert message is not None
    assert "never reported a build stamp" in message
    assert "browser_update_extension" in message


def test_matching_stamp_is_current_with_no_summary() -> None:
    report = compute_freshness(_HUB_STAMP, _HUB_STAMP)
    assert report.known is True
    assert report.current is True
    assert describe_freshness(report) is None


def test_mismatched_stamp_is_not_current_and_names_the_gap() -> None:
    report = compute_freshness("b" * 64, _HUB_STAMP)
    assert report.known is True
    assert report.current is False

    message = describe_freshness(report)
    assert message is not None
    assert "does not match the hub's current build" in message
    assert "browser_update_extension" in message


def test_hub_side_computation_failure_is_never_blamed_on_the_device() -> None:
    """`hub_stamp=None` means THIS HUB could not compute its own expected
    stamp -- a hub-side problem, named via `hub_error`, never silently
    reported as if the device were the one at fault."""
    report = compute_freshness(_HUB_STAMP, None, hub_error="boom: extension/ source not found")
    assert report.current is False

    message = describe_freshness(report)
    assert message is not None
    assert "this hub could not compute its own current build stamp" in message
    assert "boom: extension/ source not found" in message


def test_to_summary_is_json_friendly() -> None:
    report = compute_freshness(_HUB_STAMP, _HUB_STAMP)
    summary = report.to_summary()
    assert summary == {
        "known": True,
        "current": True,
        "device_stamp": _HUB_STAMP,
        "hub_stamp": _HUB_STAMP,
        "summary": None,
    }


def test_build_freshness_current_is_false_when_hub_stamp_is_none_even_if_known() -> None:
    """A device cannot be 'current' relative to a hub that has no expected
    stamp of its own to compare against -- `known=True` alone is not enough."""
    report = BuildFreshness(known=True, device_stamp=_HUB_STAMP, hub_stamp=None)
    assert report.current is False
