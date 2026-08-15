"""Convert an existing browser-state archive's captured MHTML pages into markdown,
AFTER THE FACT, from what `archive.py`'s `run_archive` already wrote to disk. This
is a distinct, later, OPT-IN step a caller invokes explicitly -- never run
automatically as part of capture itself.

## Why this is a separate step, not a `run_archive` flag

`run_archive` composes ten wire commands against a live, possibly-hundreds-of-tabs
browser -- its job is MECHANISM: reliable capture, written straight to disk, as
fast and unopinionated as possible (see `archive.py`'s module docstring).
MHTML-to-markdown conversion is pure local CPU work with no browser/network
involvement at all, and which markdown flavor a caller wants (or whether they want
it converted at all) is a POLICY decision only the caller can make correctly --
the same mechanism/policy split `vision_read.py`/`vision.py` already establish for
the vision-extraction feature (calling an external model is a policy decision the
hub/extension must never bake in automatically). Coupling conversion into the
capture path would force every archive run to pay trafilatura/lxml's CPU cost
whether or not the caller ever wants markdown, and would block a caller who
wants to re-run conversion later (different markdown flavor, a bugfix in this
module, etc.) without re-capturing the browser.

## Manifest, never the payload

Like `browser_archive`, this returns only a MANIFEST -- paths, counts, byte
sizes, per-tab status, warnings -- NEVER the markdown text itself. A converted
page can be many KB of markdown; returning it as a tool's return value would
recreate the exact context-truncation failure `archive.py`'s module docstring
describes for `browser_tabs`/raw MHTML/outer_html.

## Shared, archive-wide assets directory

Every tab's assets are written into ONE `archive_dir/assets/` directory (not a
per-tab one) so identical assets (a shared logo, icon, or web font referenced by
multiple pages) dedupe across the whole archive, not just within a single page --
see `mhtml_convert.py`'s module docstring. Meaningful at the scale this project
already designs for (hundreds of tabs, `archive.py`'s "no-wake guarantee"
section).

## Not-found accounting mirrors `run_archive`'s own fix

A `tab_ids` id with no `page.mhtml` on disk (never captured at MHTML depth, or a
typo) is recorded the same way `archive.py`'s own `tab_ids` handling now records a
vanished tab (see that module's "not_found" per-tab state): a real entry in
`manifest["tabs"][tab_id]`, counted in `summary["tabs_not_found"]`, never a
silent gap.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .mhtml_convert import ConversionError, convert_mhtml_file

__all__ = ["ConversionError", "run_archive_convert"]

_NOT_CAPTURED_REASON = (
    "no page.mhtml on disk for this tab_id -- either this archive was captured at a depth "
    "below L4 (archive.py's depth ladder only writes MHTML at L4+), or this tab_id does not "
    "exist in this archive at all. This is benign (nothing to convert), not a conversion "
    "failure, but every requested tab_id is still accounted for here rather than silently "
    "dropped -- mirroring archive.py's own 'not_found' per-tab state."
)


def _not_captured_entry() -> dict[str, Any]:
    return {"status": "not_captured", "reason": _NOT_CAPTURED_REASON}


def run_archive_convert(
    archive_dir: str | Path,
    *,
    tab_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Converts every tab's `page.mhtml` (or just `tab_ids`, if given) under
    `archive_dir/tabs/*/` into markdown, writing `<tab_dir>/markdown/page.extracted.md`
    and `<tab_dir>/markdown/page.full_page.md`, plus content-addressed asset
    sidecars under the shared `archive_dir/assets/`. Returns
    `{"ok": True, "result": <manifest>}`; the manifest is also written to
    `archive_dir/conversion_manifest.json`.

    `tab_ids`, if given, restricts conversion to that subset -- a requested id with
    no `page.mhtml` on disk gets a `{"status": "not_captured", ...}` entry (see
    module docstring) rather than being silently skipped. If omitted, every tab
    directory under `archive_dir/tabs/` with a `page.mhtml` is converted.

    Raises `ConversionError` immediately (before converting anything) if
    `archive_dir` does not look like a `run_archive` output directory at all (no
    `tabs/` subdirectory) -- distinct from a per-tab conversion failure, which is
    recorded in the manifest and does not stop the run (mirroring
    `archive.py`'s own pre-flight-vs-per-tab failure split).
    """
    root = Path(archive_dir).expanduser()
    tabs_dir = root / "tabs"
    if not tabs_dir.is_dir():
        raise ConversionError(
            f"{root} does not look like a browser-state archive directory -- no tabs/ "
            "subdirectory found. Pass the archive_dir from browser_archive's own manifest, "
            "not an individual tab directory or an unrelated path."
        )

    assets_dir = root / "assets"

    if tab_ids is not None:
        candidate_ids = [str(t) for t in tab_ids]
    else:
        candidate_ids = sorted((p.name for p in tabs_dir.iterdir() if p.is_dir()), key=lambda s: (len(s), s))

    per_tab: dict[str, Any] = {}
    failures: list[dict[str, Any]] = []

    for tab_id in candidate_ids:
        tab_dir = tabs_dir / tab_id
        mhtml_path = tab_dir / "page.mhtml"
        if not mhtml_path.is_file():
            per_tab[tab_id] = _not_captured_entry()
            continue

        markdown_dir = tab_dir / "markdown"
        try:
            result = convert_mhtml_file(
                mhtml_path,
                assets_dir=assets_dir,
                markdown_dir=markdown_dir,
                base_name="page",
            )
        except ConversionError as e:
            per_tab[tab_id] = {"status": "failed", "error": str(e)}
            failures.append({"scope": "tab", "tab_id": tab_id, "error": str(e)})
        else:
            result["status"] = "ok"
            per_tab[tab_id] = result

    tabs_converted = sum(1 for t in per_tab.values() if t.get("status") == "ok")
    tabs_failed = sum(1 for t in per_tab.values() if t.get("status") == "failed")
    tabs_not_captured = sum(1 for t in per_tab.values() if t.get("status") == "not_captured")

    manifest: dict[str, Any] = {
        "archive_dir": str(root),
        "assets_dir": str(assets_dir),
        "tabs": per_tab,
        "summary": {
            "tabs_requested": len(candidate_ids),
            "tabs_converted": tabs_converted,
            "tabs_failed": tabs_failed,
            "tabs_not_captured": tabs_not_captured,
            "has_failures": bool(failures),
        },
        "failures": failures,
    }

    # Same rollup discipline as archive.py's own manifest["status"]: "ok" is reserved
    # for zero failures AND zero benign gaps; a not_captured tab is benign (nothing
    # failed -- there was simply no MHTML to convert) so it never adds to `failures`,
    # but it still moves status away from plain "ok", mirroring archive.py's
    # "not_found" tabs landing in "ok_with_skips" rather than being silently absorbed.
    if failures:
        manifest["status"] = "ok_with_failures"
    elif tabs_not_captured > 0:
        manifest["status"] = "ok_with_skips"
    else:
        manifest["status"] = "ok"

    manifest_path = root / "conversion_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    manifest["manifest_path"] = str(manifest_path)

    return {"ok": True, "result": manifest}
