"""Build the desktop sideload artifact as an in-memory zip, for the hub's own
`GET /setup/extension.zip` route (see hub.py and onboarding.py).

## Single source of truth, not a second list

This does NOT hand-pick which files to ship. It calls `stage_extension()`
(setup.py) -- the exact same function `amplifier-browser-bridge init` already
uses to stage a local unpacked install -- which in turn:

1. Copies exactly `_EXTENSION_FILES` (setup.py's own single source of truth).
   `scripts/package.sh`'s Gate 1 derives its own required-file list from this
   SAME constant via AST inspection, so the live hub download, the locally
   staged directory, and the pinned release zip `scripts/package.sh` produces
   can never diverge on WHICH files ship. Diverging there is the exact "two
   lists" failure mode this project has hit before (commit 87ce68d: a file
   referenced by a static import was omitted from the staging whitelist,
   silently killing the entire MV3 service worker on load) -- this module
   deliberately has no list of its own to fall out of sync.
2. Runs `verify_extension_integrity()` against the staged output -- the same
   integrity gate `scripts/package.sh`'s Gate 4 runs -- so a staged set
   missing a static import or a manifest-referenced file fails loud here
   too, at request time, not only at release-build time.

## What this module does NOT share with `scripts/package.sh`, and why that's fine

The zip CONTAINER mechanism itself: `scripts/package.sh` uses `zip -X` with
every file's mtime pinned to a fixed reference timestamp, specifically so the
PUBLISHED release asset is byte-reproducible across CI runs (its own SHA256
is meaningful to compare across builds). This module uses Python's stdlib
`zipfile` instead, with no such pinning -- because this artifact is a live,
on-demand convenience download served to someone mid-onboarding; nothing
compares its bytes across repeated hub requests, so reproducibility has no
consumer here. What ships (the file LIST) and whether it's internally
consistent (the integrity check) -- the two things that have actually broken
in this repo's history -- are identical between the two paths. Only the
zip-writing mechanics differ, deliberately, for a property this path doesn't
need.

For a real (non-editable) `pip`/`uv tool install`, `stage_extension()`'s own
`find_extension_source()` already resolves the extension source from inside
the installed wheel (`pyproject.toml` force-includes `extension/`) -- this
module needs no wheel-layout knowledge of its own. `INSTALL.md` is bundled
the same way (see `find_install_md` below and `pyproject.toml`'s matching
force-include entry) so a downloaded zip carries its own install
instructions even though the hub's `/setup` page already repeats them.
"""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path

from .setup import _EXTENSION_FILES, stage_extension

__all__ = ["build_extension_zip_bytes", "find_install_md"]


def find_install_md() -> Path | None:
    """Best-effort locate `INSTALL.md` to bundle alongside the extension
    files, mirroring `setup.find_extension_source`'s resolution order
    (packaged install first, dev checkout fallback).

    Returns `None` (never raises) if it can't be found -- `INSTALL.md`
    inside the zip is a nice-to-have; the hub's own `/setup` page already
    carries the same instructions, so its absence must never block the
    desktop download itself.
    """
    packaged = Path(__file__).resolve().parent / "INSTALL.md"
    if packaged.is_file():
        return packaged
    dev_checkout = Path(__file__).resolve().parents[2] / "INSTALL.md"
    if dev_checkout.is_file():
        return dev_checkout
    return None


def build_extension_zip_bytes(*, source: str | Path | None = None) -> bytes:
    """Build the desktop sideload zip in memory and return its bytes.

    Stages into a fresh temporary directory (via `stage_extension`, cleaned
    up unconditionally on return) rather than reusing `init`'s persistent
    staging directory -- this function must never touch, or depend on the
    state of, a user's already-configured local install.

    Raises `ExtensionSourceNotFoundError` / `ExtensionIntegrityError` (never
    guesses, never ships a partial zip) if the extension source can't be
    found or fails integrity -- the exact same exceptions `stage_extension`
    itself raises, so a caller (hub.py) can handle them identically to how
    `init` already does.
    """
    with tempfile.TemporaryDirectory(prefix="amplifier-browser-bridge-setup-zip-") as tmp:
        staged = stage_extension(dest=Path(tmp) / "extension", source=source)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name in _EXTENSION_FILES:
                zf.write(staged / name, arcname=name)
            install_md = find_install_md()
            if install_md is not None:
                zf.write(install_md, arcname="INSTALL.md")
        return buf.getvalue()
