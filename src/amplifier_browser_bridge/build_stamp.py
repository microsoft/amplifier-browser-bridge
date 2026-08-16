"""Content-derived build-freshness detection -- the freshness half of the
version-skew story (Tier 0 handshake, skew.py's sibling).

## The gap this closes

Tier 0's command-set handshake (skew.py) answers "what can this device DO" --
capability skew. It does NOT answer "is this device running the CURRENT
build": a change that touches zero commands (a bug fix, a UI fix, a security
fix in options.js -- see commits 6175ce4/cc140c5) is invisible to it. Proven
live: pointing `run_update_extension` at a real, genuinely-outdated browser
(command-complete, but two commits stale on everything that isn't a command)
returned `already_current: true` -- false. Neither commit added or removed a
command, so the extension had nothing to report differently, and `skew.py`'s
`in_sync` stayed `True` throughout.

`manifest_version` cannot fill this gap either -- it is hand-maintained and
already measured to drift in this exact repo (pyproject.toml said `0.1.0`,
manifest.json said `0.4.0`, same repo, same day, no CI; see skew.py's module
docstring). A content-derived stamp -- a hash of the bytes of the files that
actually ship -- cannot drift the same way: nobody has to remember to bump
it, and if a single byte of a single shipped file changes, the stamp changes.

## Same file set, same algorithm, both sides

The device and the hub must derive the stamp identically, or the comparison
is worthless. Both sides hash exactly `setup._EXTENSION_FILES` -- the
existing, single source of truth for "what actually ships"
(`stage_extension()` already materializes exactly this set;
`extension_zip.py`'s fresh temp stage for the HTTP download path produces the
same set from the same source, so a locally staged install and a remotely
downloaded zip can never disagree about what "current" means). The device
(`extension/background.js`'s `computeBuildStamp()`) mirrors this file list BY
HAND -- the same "keep the two protocol implementations in sync by hand"
convention this repo already applies to `SUPPORTED_COMMANDS`/
`PAGE_WORLD_COMMANDS` -- guarded against drift by
`tests/test_extension_command_parity.py`.

## No generated file, no circularity

The stamp is never written into a file that ships. There is nothing to
exclude from the hash and no self-containment / whitelist entanglement to
manage (`extension_integrity.py`'s checker and `_EXTENSION_FILES` stay
exactly as they were). The hub computes the stamp by reading bytes that
already exist on disk (`compute_build_stamp`, below); the extension computes
it by fetching its own already-loaded resources
(`chrome.runtime.getURL` + `fetch`) and hashing them with
`crypto.subtle.digest("SHA-256", ...)`. Both sides read bytes that already
exist -- neither needs to keep a NEW artifact in sync with a whitelist.

## The pre-stamp case

Mirrors skew.py's pre-handshake discipline exactly: an extension that has
never reported a `build_stamp` at all is not "unknown, assume current" -- it
is a DEFINITIVELY STALE build (`BuildFreshness.known = False`), the same
treatment skew.py already gives a device that has never reported a command
set.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .setup import _EXTENSION_FILES, find_extension_source

__all__ = [
    "BuildFreshness",
    "BuildStampError",
    "compute_build_stamp",
    "compute_freshness",
    "current_build_stamp",
    "describe_freshness",
]


class BuildStampError(RuntimeError):
    """Raised when `compute_build_stamp` cannot read one of the shipped
    files it needs to hash from `shipped_dir`."""


def compute_build_stamp(shipped_dir: str | Path, *, files: Sequence[str] = _EXTENSION_FILES) -> str:
    """SHA-256 over the bytes of every file in `files` (default:
    `setup._EXTENSION_FILES` -- the authoritative "what actually ships"
    list), read from `shipped_dir`, in a fixed (sorted) order.

    Deterministic regardless of `shipped_dir`'s own identity: a freshly
    staged directory (`stage_extension()`'s output), a fresh temp stage
    built for the HTTP zip download (`extension_zip.py`), and the tracked
    `extension/` source tree itself all carry byte-identical copies of
    every file in `_EXTENSION_FILES` (`shutil.copy2` makes an exact byte
    copy) -- so this function returns the SAME digest for all three. That
    equivalence is the entire point: a locally staged install and a
    remotely downloaded zip must agree on what "current" means, or a remote
    install would look permanently stale (see `extension_zip.py`'s module
    docstring).

    Each file's contribution to the hash is its name (UTF-8 bytes) + a NUL
    separator + its raw bytes + a NUL separator -- the exact byte layout
    `extension/background.js`'s `computeBuildStamp()` reproduces, so the two
    sides can never disagree over encoding or ordering.

    Raises `BuildStampError` (never guesses, never hashes a partial set) if
    any named file is missing or unreadable in `shipped_dir`.
    """
    shipped_dir = Path(shipped_dir)
    hasher = hashlib.sha256()
    for name in sorted(files):
        file_path = shipped_dir / name
        try:
            data = file_path.read_bytes()
        except OSError as e:
            raise BuildStampError(
                f"cannot compute build stamp: {file_path} ({name!r} from the shipped file set) "
                f"is missing or unreadable: {e}"
            ) from e
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(data)
        hasher.update(b"\x00")
    return hasher.hexdigest()


def current_build_stamp(source: str | Path | None = None) -> str:
    """The build stamp this hub's OWN currently-loaded extension source
    would produce right now -- computed directly from
    `find_extension_source()`'s result (or an explicit override), with no
    staging step required (see `compute_build_stamp`'s docstring for why a
    source-tree read and a freshly staged copy's read are always
    identical).

    Read fresh on every call rather than cached: the ground truth genuinely
    lives on disk, and a long-running hub process should reflect a `git
    pull` to its own source without requiring a restart to notice --
    reading and hashing `_EXTENSION_FILES` (a couple dozen small files) costs
    microseconds, not enough to justify caching's invalidation complexity.

    Raises `ExtensionSourceNotFoundError` (from `find_extension_source`) or
    `BuildStampError` (from `compute_build_stamp`) exactly as those
    functions do -- never guesses a stamp for a source it couldn't fully
    read.
    """
    src = Path(source).expanduser() if source else find_extension_source()
    return compute_build_stamp(src)


@dataclass(frozen=True)
class BuildFreshness:
    """`known=False` means the device has never reported a build stamp at
    all (pre-stamp extension, or one that hasn't reconnected since
    updating) -- a distinct, definitively-stale state, not merely
    `current=False` with nothing to compare (mirrors skew.py's
    `SkewReport.known`). `hub_stamp=None` means THIS HUB could not compute
    its own expected stamp -- a hub-side installation problem, named via
    `hub_error`, never silently blamed on the device."""

    known: bool
    device_stamp: str | None
    hub_stamp: str | None
    hub_error: str | None = None

    @property
    def current(self) -> bool:
        return self.known and self.hub_stamp is not None and self.device_stamp == self.hub_stamp

    def to_summary(self) -> dict[str, Any]:
        """JSON-friendly shape, attached to every `devices`/`browser_devices`
        entry (`hub.py`'s `_devices_snapshot`) alongside `skew` -- so an
        agent can see a stale BUILD before ever failing a command against
        it, the same way it already sees stale COMMANDS."""
        return {
            "known": self.known,
            "current": self.current,
            "device_stamp": self.device_stamp,
            "hub_stamp": self.hub_stamp,
            "summary": describe_freshness(self),
        }


def compute_freshness(
    device_stamp: str | None, hub_stamp: str | None, *, hub_error: str | None = None
) -> BuildFreshness:
    """Compare one device's self-reported `build_stamp` (from `hello`, or
    `None` if it never reported one) against this hub's own currently
    expected stamp (`current_build_stamp()`, or `None` if the hub could not
    compute one -- see `hub_error`)."""
    return BuildFreshness(
        known=device_stamp is not None, device_stamp=device_stamp, hub_stamp=hub_stamp, hub_error=hub_error
    )


def describe_freshness(report: BuildFreshness) -> str | None:
    """One-line, actionable summary of `report`, or `None` when there is
    nothing to report (current)."""
    if report.hub_stamp is None:
        detail = f" ({report.hub_error})" if report.hub_error else ""
        return (
            f"this hub could not compute its own current build stamp{detail} -- build-freshness "
            "comparison is unavailable until this hub's own extension source is fixed."
        )
    if report.current:
        return None
    if not report.known:
        return (
            "this extension has never reported a build stamp -- a definitively stale build "
            "(pre-stamp extension, or one that hasn't reconnected since updating). "
            "Update it (browser_update_extension), then reconnect."
        )
    return (
        "this extension's build stamp does not match the hub's current build -- the shipped files "
        "differ from what this hub would install (a bug fix, UI change, or other update not "
        "reflected in the command set). Update the extension (browser_update_extension)."
    )
