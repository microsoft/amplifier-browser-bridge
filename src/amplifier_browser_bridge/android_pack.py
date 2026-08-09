"""On-demand Android CRX packing -- the hub packs its OWN artifact, from its
OWN currently-running token, at request time, instead of an operator running
`scripts/package-android.sh` by hand and pointing the hub at the result.

## The problem this replaces

Before this module, `/setup`'s Android section said, verbatim: "No build
available on this hub yet -- the operator needs to run
`scripts/package-android.sh` and point this hub at the result
(`--android-artifact`)." That is an operator step nobody actually performs --
which meant the Android download was dead on arrival for anyone who wasn't
also the person running the hub from a checkout with the packaging script
close at hand.

## The constraint that shapes this module

The Android CRX bakes a **live per-hub credential** into `bundled_config.json`
at build time (see `android_bake.py`'s module docstring). That means the
artifact is not a static thing that can be built once and committed, or
attached to a GitHub release: every hub has a different token, and any given
hub's artifact goes stale the moment `amplifier-browser-bridge init --force`
rotates it. Whatever packs the artifact MUST do so from the running hub's
CURRENT token, at the moment it's requested -- exactly what this module does
(see `build_android_crx`, which reads `hub_token` as a parameter the caller
supplies fresh from `Hub.token_store` on every request; see hub.py's
`_handle_setup_android_artifact`).

## What packing an installable CRX genuinely requires

A CRX3 file is `Cr24` magic + a version field + a **signed** protobuf header +
a zip payload -- not a zip with a different extension (see docs/ANDROID.md's
"Two packaging traps"). Producing that signed header requires a real
Chromium/Chrome/Edge binary's `--pack-extension` flag; there is no pure-Python
way to hand-roll one that Edge Android's installer will accept. This module's
`find_packer_binary()` is therefore a HARD PREREQUISITE, not an optimization:
if no such binary is present on the host running the hub, on-demand packing
is genuinely impossible, and this module says so plainly
(`PackerUnavailableError`) rather than serving something that looks like a
download and 404s or silently fails to install.

## Same signing key as the manual script, on purpose

Reuses `scripts/package-android.sh`'s own signing-key location/env var
(`~/.config/amplifier-browser-bridge/android-signing-key.pem`, override via
`AMPLIFIER_BROWSER_BRIDGE_ANDROID_SIGNING_KEY`) so an artifact built by this
module and one built by the manual script share the SAME extension ID --
Android's sideload-by-file flow treats a new ID as a different extension, so
whichever path an operator or the hub itself uses, rebuilding never forces a
reinstall-and-lose-settings on the device. This is a well-known literal path,
duplicated across a bash script and this Python module rather than shared
mechanically across languages -- see IMPLEMENTATION_PHILOSOPHY.md's
"conventions via instructions, not code" guidance.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .android_bake import BakedConfig, write_bundled_config
from .extension_integrity import ExtensionIntegrityError, verify_extension_integrity
from .setup import _EXTENSION_FILES, ExtensionSourceNotFoundError, find_extension_source

__all__ = [
    "ANDROID_STAGE_FILES",
    "DEFAULT_SIGNING_KEY_PATH",
    "AndroidPackError",
    "PackerUnavailableError",
    "build_android_crx",
    "find_packer_binary",
    "stage_android_extension",
]

DEFAULT_SIGNING_KEY_PATH = Path("~/.config/amplifier-browser-bridge/android-signing-key.pem")

# The Android runtime file set. Deliberately DERIVED from setup.py's
# `_EXTENSION_FILES` (this project's single source of truth for "what does the
# runtime actually import/reference") rather than a second hand-maintained
# list -- `scripts/package-android.sh`'s own hardcoded copy list drifted out
# of sync with it (missing `pairing_code.mjs`/`pair_discovery.mjs`, both
# imported by `options.js` since the zero-copy-paste pairing feature landed --
# exactly the 87ce68d failure-mode class `extension_integrity.py` exists to
# catch). This module never repeats that mistake: it takes the desktop list
# and swaps only the one file that must differ (the manifest).
ANDROID_STAGE_FILES: tuple[str, ...] = tuple(
    "manifest.android.json" if name == "manifest.json" else name for name in _EXTENSION_FILES
)


class AndroidPackError(RuntimeError):
    """Raised when staging, baking, integrity-checking, or invoking the
    packer binary fails for any reason OTHER than the packer simply not being
    present (see `PackerUnavailableError` for that case)."""


class PackerUnavailableError(AndroidPackError):
    """Raised when no Chromium/Chrome/Edge binary capable of `--pack-extension`
    can be found. This is the HONEST-UNAVAILABLE case (see module docstring) --
    callers (hub.py) must render this as "can't build a package right now,
    here's exactly why", never retry silently, and never serve a broken file
    in its place."""


def find_packer_binary() -> Path | None:
    """Locate a Chromium/Chrome/Edge binary capable of `--pack-extension`.

    Resolution order (mirrors `scripts/package-android.sh`'s own `find_chrome`,
    kept in sync deliberately -- both must agree on what counts as "a packer
    is present" or an operator could get a different answer from the manual
    script than from the hub's on-demand path):

        1. `CHROME_BIN` env var, if set and executable.
        2. `microsoft-edge` / `google-chrome` / `chromium` on PATH.
        3. Playwright's own bundled Chromium (`~/.cache/ms-playwright` by
           default, or `PLAYWRIGHT_BROWSERS_PATH`) -- works headless for this
           even on a host with no system browser at all (this project's own
           dev host has none; see docs/ANDROID.md).

    Returns `None` (never raises, never guesses) if nothing usable is found --
    callers turn that into `PackerUnavailableError` with an actionable message.
    """
    env_bin = os.environ.get("CHROME_BIN")
    if env_bin:
        candidate = Path(env_bin)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate

    for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found)

    pw_cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "~/.cache/ms-playwright").expanduser()
    if pw_cache.is_dir():
        candidates = sorted(pw_cache.glob("chromium-*/chrome-linux/chrome"))
        if candidates:
            return candidates[
                -1
            ]  # highest-numbered (newest) build, matching the shell script's `sort -V | tail -1`

    return None


def stage_android_extension(dest: Path, *, source: str | Path | None = None) -> Path:
    """Copy the Android runtime file set into `dest`, renaming
    `manifest.android.json` to `manifest.json` at the destination (the packer
    packs whatever `manifest.json` it finds; it has no notion of "which
    manifest variant"). Raises `ExtensionSourceNotFoundError` if a required
    file is missing from the source tree -- never ships a partial stage."""
    source_path = Path(source).expanduser() if source else find_extension_source()
    dest.mkdir(parents=True, exist_ok=True)
    for name in ANDROID_STAGE_FILES:
        src_file = source_path / name
        if not src_file.is_file():
            raise ExtensionSourceNotFoundError(f"expected extension file missing from source: {src_file}")
        if name == "manifest.android.json":
            dest_file = dest / "manifest.json"
        else:
            dest_file = dest / name
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
    return dest


@dataclass
class _PackResult:
    crx_path: Path
    pem_path: Path | None  # only set when a NEW key was generated this run


async def _run_packer(chrome_bin: Path, stage_dir: Path, *, signing_key_path: Path | None) -> _PackResult:
    """Invoke `chrome_bin --pack-extension=stage_dir [--pack-extension-key=...]`
    and locate its output. Chromium writes `<stage_dir>.crx` (and, when no key
    was supplied, `<stage_dir>.pem`) alongside the staged directory -- this
    mirrors `scripts/package-android.sh`'s own invocation shape exactly."""
    args = ["--headless", "--no-sandbox", f"--pack-extension={stage_dir}"]
    if signing_key_path is not None and signing_key_path.is_file():
        args.append(f"--pack-extension-key={signing_key_path}")

    proc = await asyncio.create_subprocess_exec(
        str(chrome_bin), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        raise AndroidPackError(
            f"packer binary {chrome_bin} exited {proc.returncode} while packing {stage_dir}: "
            f"{stdout.decode(errors='replace')}"
        )

    generated_crx = stage_dir.with_suffix(stage_dir.suffix + ".crx")
    if not generated_crx.is_file():
        raise AndroidPackError(
            f"packer binary {chrome_bin} did not produce a .crx at {generated_crx}. Output: "
            f"{stdout.decode(errors='replace')}"
        )
    generated_pem = stage_dir.with_suffix(stage_dir.suffix + ".pem")
    return _PackResult(crx_path=generated_crx, pem_path=generated_pem if generated_pem.is_file() else None)


async def build_android_crx(
    *,
    hub_url: str,
    hub_token: str,
    signing_key_path: str | Path | None = None,
    chrome_bin: str | Path | None = None,
    source: str | Path | None = None,
) -> bytes:
    """Build a real, installable, signed CRX3 for the Android sideload path,
    baking `hub_url`/`hub_token` into it -- from the hub's OWN in-memory
    state, at request time (see module docstring's "constraint" section).

    Raises:
        PackerUnavailableError: no Chromium/Chrome/Edge binary was found and
            none was explicitly supplied via `chrome_bin`.
        AndroidPackError: staging, integrity-checking, or the packer
            subprocess itself failed for any other reason.

    Returns the packed CRX3 bytes. Callers are responsible for caching (this
    function always does the full pack; see hub.py's cache, keyed on the
    token actually baked in, so a request only re-packs when something that
    would change the artifact -- the token -- has actually changed).
    """
    resolved_chrome = Path(chrome_bin).expanduser() if chrome_bin else find_packer_binary()
    if resolved_chrome is None:
        raise PackerUnavailableError(
            "no Chromium/Chrome/Edge binary found to pack a CRX with (checked CHROME_BIN, "
            "microsoft-edge/google-chrome/chromium on PATH, and Playwright's bundled Chromium "
            "cache). Set CHROME_BIN to a real browser binary, or pre-build with "
            "scripts/package-android.sh and pass --android-artifact instead."
        )

    key_path = (
        Path(signing_key_path).expanduser() if signing_key_path else DEFAULT_SIGNING_KEY_PATH.expanduser()
    )

    with tempfile.TemporaryDirectory(prefix="amplifier-browser-bridge-android-pack-") as tmp:
        stage_dir = Path(tmp) / "extension"
        try:
            stage_android_extension(stage_dir, source=source)
        except ExtensionSourceNotFoundError as e:
            raise AndroidPackError(str(e)) from e

        config = BakedConfig(
            hub_url=hub_url,
            hub_token=hub_token,
            generated_at="",  # cosmetic only; on-demand builds don't need a real timestamp
            token_masked="",
            auth_disabled=not hub_token,
        )
        write_bundled_config(stage_dir, config)

        try:
            verify_extension_integrity(stage_dir)
        except ExtensionIntegrityError as e:
            raise AndroidPackError(f"staged Android build failed its own integrity check: {e}") from e

        key_path.parent.mkdir(parents=True, exist_ok=True)
        result = await _run_packer(resolved_chrome, stage_dir, signing_key_path=key_path)

        if not key_path.is_file():
            if result.pem_path is None:
                raise AndroidPackError(
                    "no signing key existed and the packer did not generate one -- cannot "
                    "guarantee a stable extension ID across rebuilds."
                )
            shutil.move(str(result.pem_path), str(key_path))
            try:
                key_path.chmod(0o600)
            except OSError:
                pass

        return result.crx_path.read_bytes()
