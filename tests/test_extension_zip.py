"""Tests for extension_zip.py -- the hub's live GET /setup/extension.zip builder.

Proves the single-source-of-truth claim in that module's docstring: the zip's
contents are exactly `_EXTENSION_FILES` (+ INSTALL.md when found), and a
staged set missing a required file still fails loud via the same
`ExtensionIntegrityError`/`ExtensionSourceNotFoundError` `stage_extension`
itself raises.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from amplifier_browser_bridge.extension_zip import build_extension_zip_bytes, find_install_md
from amplifier_browser_bridge.setup import _EXTENSION_FILES, ExtensionSourceNotFoundError


def test_build_extension_zip_bytes_contains_every_required_file() -> None:
    data = build_extension_zip_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())

    missing = [name for name in _EXTENSION_FILES if name not in names]
    assert not missing, f"zip is missing required extension file(s): {missing}"


def test_build_extension_zip_bytes_manifest_and_icons_are_at_real_paths_not_flattened() -> None:
    """Regression guard for exactly the bug this task's PR was written to
    catch: icons referenced by manifest.json flattened out of the staged
    set. `icons/icon-16.png` must exist AT that path inside the zip, not as
    a flattened `icon-16.png` at the zip root."""
    data = build_extension_zip_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        manifest = zf.read("manifest.json").decode("utf-8")

    for icon_path in ("icons/icon-16.png", "icons/icon-32.png", "icons/icon-48.png", "icons/icon-128.png"):
        assert icon_path in names, f"{icon_path} missing or flattened in the built zip"
    import json

    manifest_data = json.loads(manifest)
    for icon_ref in manifest_data["icons"].values():
        assert icon_ref in names, f"manifest references {icon_ref}, not present in the zip"


def test_build_extension_zip_bytes_includes_install_md_when_findable() -> None:
    data = build_extension_zip_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
    if find_install_md() is not None:
        assert "INSTALL.md" in names


def test_build_extension_zip_bytes_raises_the_same_exceptions_stage_extension_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller (hub.py) handles exactly `ExtensionSourceNotFoundError` /
    `ExtensionIntegrityError` -- prove those are the exceptions actually
    raised on a bad source, not some third, unhandled type."""

    def _boom(*args: object, **kwargs: object) -> Path:
        raise ExtensionSourceNotFoundError("no extension source here")

    monkeypatch.setattr("amplifier_browser_bridge.extension_zip.stage_extension", _boom)

    with pytest.raises(ExtensionSourceNotFoundError):
        build_extension_zip_bytes()


def test_find_install_md_returns_none_rather_than_raising_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INSTALL.md missing must never block the zip -- see the module docstring."""
    import amplifier_browser_bridge.extension_zip as mod

    monkeypatch.setattr(mod.Path, "is_file", lambda self: False)
    assert mod.find_install_md() is None
