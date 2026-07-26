"""First-run setup helpers for `abb init` (see cli.py).

This is the fix for the concrete gap that made this project unusable by anyone who
wasn't us: the only way to configure a token used to be hand-editing a tracked source
file (`extension/config.js`), which every `git pull`/file-copy update silently
clobbered. Configuration now lives in the extension's `chrome.storage.local` (see
`extension/options.js`/`background.js`), entered once through its options page --
this module generates the token the user pastes there, and stages a stable directory
to load the extension from.

The staging directory is the other half of the fix: an unpacked Edge extension's
identity (and therefore its `chrome.storage.local` config) is tied to the exact
directory path it was loaded from. Loading directly from a git checkout means every
`git pull` that changes the checkout path (a fresh clone, a rename) creates a *new*
extension identity with empty storage -- update wipes the configuration. Staging to a
stable, non-repo path (default `~/.local/share/amplifier-browser-bridge/extension`)
and re-copying files into that SAME path on every `abb init` re-run means the
extension's identity -- and its stored config -- never changes across updates.
"""

from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from .auth import DEFAULT_TOKEN_FILE, load_token_store

DEFAULT_STAGE_DIR = Path("~/.local/share/amplifier-browser-bridge/extension")

# Runtime files the extension actually needs at load time. Deliberately excludes
# *.test.mjs (dev-only, node-runnable tests with zero chrome.* usage -- see
# CONTRIBUTING.md's "Extension JavaScript" section) so the staged directory Edge
# loads is exactly the shipped runtime, nothing more.
_EXTENSION_FILES = (
    "background.js",
    "injected.js",
    "options.html",
    "options.js",
    "config_validate.mjs",
    "frame_refs.mjs",
    "combine_frames.mjs",
    "ref_registry.mjs",
    "args_bool.mjs",
    "fetch_utils.mjs",
    "download_claim.mjs",
    "manifest.json",
)


class ExtensionSourceNotFoundError(RuntimeError):
    """Raised when `stage_extension` cannot locate a source `extension/` directory."""


def find_extension_source() -> Path:
    """Locate the repo's `extension/` directory to stage from.

    Resolution order:
        1. `ABB_EXTENSION_SRC` environment variable, if set.
        2. Relative to this installed package's file -- works for the editable
           source install this project documents today (`uv pip install -e .` from
           inside a clone; see README.md/CONTRIBUTING.md). `src/amplifier_browser_bridge/
           setup.py` -> parents[2] is the repo root.

    Raises `ExtensionSourceNotFoundError` (never guesses) if neither resolves to a
    real directory containing `manifest.json` -- this project is not yet published as
    a wheel bundling extension assets (see docs/AGENT_SURFACES.md's "Local development
    note" for the same honest caveat about the sibling Amplifier tool module), so a
    non-source install has no other way to find these files.
    """
    override = os.environ.get("ABB_EXTENSION_SRC")
    if override:
        candidate = Path(override).expanduser()
        if (candidate / "manifest.json").is_file():
            return candidate
        raise ExtensionSourceNotFoundError(
            f"ABB_EXTENSION_SRC={override!r} does not contain manifest.json -- check the path."
        )

    inferred = Path(__file__).resolve().parents[2] / "extension"
    if (inferred / "manifest.json").is_file():
        return inferred

    raise ExtensionSourceNotFoundError(
        "Could not locate the extension/ source directory. This project is not yet "
        "published as a standalone package with bundled extension assets -- `abb init` "
        "expects to run from an editable install of a git checkout "
        "(`uv pip install -e .` from inside the clone). If you've moved the extension "
        "source elsewhere, set ABB_EXTENSION_SRC to its path."
    )


def stage_extension(dest: str | Path | None = None, source: str | Path | None = None) -> Path:
    """Copy the runtime extension files into a stable directory, safe to re-run.

    Re-running this (e.g. after `git pull`) overwrites the JS/HTML/manifest files in
    place but never touches `dest` itself as a path, and has zero interaction with
    Chrome's own per-install `chrome.storage.local` -- that storage is keyed by the
    browser to the extension's load path (Chrome/Edge's own mechanism, entirely
    outside this filesystem), so overwriting files at the SAME path leaves a
    previously-configured install's token/hub-url intact. This is the whole point:
    an update is a re-run of this function against the same `dest`, not a fresh
    directory.
    """
    dest_path = Path(dest).expanduser() if dest else DEFAULT_STAGE_DIR.expanduser()
    source_path = Path(source).expanduser() if source else find_extension_source()

    dest_path.mkdir(parents=True, exist_ok=True)
    for name in _EXTENSION_FILES:
        src_file = source_path / name
        if not src_file.is_file():
            raise ExtensionSourceNotFoundError(f"expected extension file missing from source: {src_file}")
        shutil.copy2(src_file, dest_path / name)

    return dest_path


def generate_token() -> str:
    """A fresh, high-entropy hub token -- 32 hex chars (128 bits), matching the
    shape `auth.py`'s TokenStore expects (an opaque string, no format requirement
    beyond that)."""
    return secrets.token_hex(16)


@dataclass
class TokenResult:
    token: str
    token_file: Path
    created_new: bool


def ensure_token_file(path: str | Path | None = None, *, force: bool = False) -> TokenResult:
    """Idempotently ensure a hub token exists in the token file `load_token_store`
    (auth.py) reads by default.

    Does NOT regenerate (and clobber) an existing token unless `force=True` -- an
    existing token likely already matches what's pasted into a browser's options
    page; silently rotating it on every `abb init` re-run would be its own version
    of the "update destroys your working config" bug this project is fixing.
    """
    import json

    file_path = Path(path or os.environ.get("ABB_TOKEN_FILE") or DEFAULT_TOKEN_FILE).expanduser()

    if not force and file_path.is_file():
        store = load_token_store(file_path)
        if store.default_token:
            return TokenResult(token=store.default_token, token_file=file_path, created_new=False)

    token = generate_token()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps({"default": token, "devices": {}}, indent=2) + "\n", encoding="utf-8")
    # Token files carry a live credential -- restrict to the owner, matching the
    # posture .gitignore documents for anything under this path (never committed).
    try:
        file_path.chmod(0o600)
    except OSError:
        pass  # best-effort; not fatal (e.g. some filesystems/platforms don't support chmod)

    return TokenResult(token=token, token_file=file_path, created_new=True)
