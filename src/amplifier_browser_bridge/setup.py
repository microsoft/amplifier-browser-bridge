"""First-run setup helpers for `amplifier-browser-bridge init` (see cli.py).

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
and re-copying files into that SAME path on every `amplifier-browser-bridge init` re-run means the
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
    """Locate the `extension/` directory to stage from.

    Resolution order:
        1. `AMPLIFIER_BROWSER_BRIDGE_EXTENSION_SRC` environment variable, if set -- always wins, for the
           rare case of a moved/custom extension source.
        2. Packaged alongside the installed module: `pyproject.toml` force-includes
           the whole `extension/` tree into the wheel at
           `amplifier_browser_bridge/extension/` (see `[tool.hatch.build.targets.
           wheel.force-include]`), so `Path(__file__).resolve().parent / "extension"`
           finds it for ANY install made from that wheel -- `uv tool install .`,
           `pip install .`, a published PyPI release, all of them. This is the path
           real (non-editable) installs take.
        3. Dev checkout fallback: `src/amplifier_browser_bridge/setup.py` ->
           `parents[2]` is the repo root, so `parents[2] / "extension"` finds the
           real source tree directly. This is the path an editable install
           (`uv pip install -e .` from inside a clone) takes -- editable installs
           import straight from the checkout, so `__file__` points at the actual
           source file, not a copy, and the packaged copy from step 2 doesn't apply.

    Raises `ExtensionSourceNotFoundError` (never guesses) if none of these resolve to
    a real directory containing `manifest.json`.
    """
    override = os.environ.get("AMPLIFIER_BROWSER_BRIDGE_EXTENSION_SRC")
    if override:
        candidate = Path(override).expanduser()
        if (candidate / "manifest.json").is_file():
            return candidate
        raise ExtensionSourceNotFoundError(
            f"AMPLIFIER_BROWSER_BRIDGE_EXTENSION_SRC={override!r} does not contain manifest.json -- check the path."
        )

    packaged = Path(__file__).resolve().parent / "extension"
    if (packaged / "manifest.json").is_file():
        return packaged

    dev_checkout = Path(__file__).resolve().parents[2] / "extension"
    if (dev_checkout / "manifest.json").is_file():
        return dev_checkout

    raise ExtensionSourceNotFoundError(
        f"Could not locate the extension/ source directory. Looked for a packaged "
        f"copy at {packaged} and a dev checkout at {dev_checkout}, and found neither. "
        "This should not happen for a normal `pip install`/`uv tool install` of this "
        "package -- if you've moved the extension source elsewhere (or are running "
        "from a nonstandard layout), set AMPLIFIER_BROWSER_BRIDGE_EXTENSION_SRC to its path."
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
    page; silently rotating it on every `amplifier-browser-bridge init` re-run would be its own version
    of the "update destroys your working config" bug this project is fixing.
    """
    import json

    file_path = Path(
        path or os.environ.get("AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE") or DEFAULT_TOKEN_FILE
    ).expanduser()

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
