"""MHTML -> Markdown conversion: turn an archived page's raw MHTML capture into
markdown, written to disk, for a human (or a downstream text-processing pipeline)
to read. This module owns only the CONVERSION -- reading an `.mhtml` file that
already exists on disk and producing markdown files (and asset sidecars) that
already exist on disk. It knows nothing about `archive.py`'s depth ladder, tabs,
or devices -- see `archive_convert.py` for the orchestrator that wires this into
an existing browser-state archive.

## Why MHTML is the conversion source, not outer_html

Measured live, on the same real browser, against the same real tabs: the
JS-injection capture route (`read` -> text, `page_state` -> outer_html) failed on
7 of 7 real tabs -- including github.com and huggingface.co -- timing out at both
90s and 120s budgets. The CDP-based route (`mhtml`, `screenshot`, `nav_history`)
succeeded 3 of 3 on those same tabs, including a browser error page that JS
injection could not touch at all (see archive.py's module docstring, "the
depth-ladder note on CDP vs. JS-injection routes"). So `outer_html` is not
reliably obtainable in this system, and MHTML is the only full-page capture this
converter can depend on.

This deliberately deviates from the published extraction-research recommendation
(extract from live-rendered `outerHTML`, not a saved MHTML snapshot). Every
benchmark behind that recommendation was measured against server-rendered HTML;
this system's DOM is post-hydration, fetched over a websocket from a remote
browser -- a configuration nobody has benchmarked, and one where the "preferred"
source (`outer_html`) simply isn't reliably available at all.

## Extract, don't convert-then-clean

Trafilatura's own published benchmark scores html2text-style whole-document
conversion at F1 0.663 against a raw-HTML do-nothing baseline of 0.667 --
converting everything and stripping boilerplate afterward starts below doing
nothing. Trafilatura itself scores 0.924, is pure Python, and emits markdown
directly. So the EXTRACTED-content output below runs trafilatura's main-content
identification on the (asset-rewritten) HTML directly, not a convert-then-clean
pipeline.

## Two outputs, never one

The WCXB benchmark (2,008 pages) shows extraction quality swings 0.42-0.93 by
PAGE TYPE, and 47% of real pages are non-articles (forum threads, product pages,
docs sites, ...) where a main-content extractor can silently delete the content
a caller actually wanted. So this module always writes BOTH a `*.extracted.md`
(trafilatura's best guess at the article/main content) and a `*.full_page.md`
(a deliberately unfiltered, whole-document html2text pass) -- a bad extraction is
recoverable from the full-page file rather than silently lossy.

## Assets: content-addressed sidecars, never inlined

MHTML is a multipart MIME document (RFC 2557): the HTML lives in one part;
images/CSS/fonts/etc. live in sibling parts, addressed by `Content-Location`
(the resolved URL Chrome fetched) and/or `Content-ID` (a `cid:` reference). Chrome
already resolved lazy-loading at capture time -- these bytes are the real assets
actually rendered, so this module never re-fetches or guesses which attribute
held the "true" URL. Every asset part is written to a SHARED, content-addressed
`assets/` sidecar directory (named `<sha256-of-bytes><ext>`), not base64-inlined
into the markdown -- base64 inflates ~33% and destroys markdown's own
readability, and content-addressed naming dedupes identical assets (a shared
logo/icon/font) across every page converted into the same archive. The HTML's
own asset references are rewritten to the sidecar's relative path BEFORE
conversion, so the emitted markdown links to real files on disk, not
`https://...`/`cid:...` references that mean nothing outside the browser that
captured them.

## Known limitations (fail loud, never silently mangled)

- Nested/merged-cell tables (`colspan`/`rowspan` > 1) cannot be expressed as
  markdown pipe tables -- this is a FORMAT limitation of markdown itself, not a
  gap in this converter's tooling. This module does not attempt to preserve span
  structure (that would require emitting raw HTML mid-markdown, a much larger and
  more fragile undertaking for a documented format limit). Instead, every table
  with a span cell is named explicitly in the returned manifest's
  `tables_with_merged_cells` list (index + a short text preview) -- so a caller
  knows exactly which tables in the emitted markdown may have lost cell-merge
  structure, rather than discovering it by silently getting wrong-looking output
  with no explanation.
- A multi-part MHTML containing more than one `text/html` (or
  `application/xhtml+xml`) body -- i.e. a page with `<iframe>`s captured as
  separate frame documents -- is the documented hard case (the one known public
  MHTML-to-markdown implementation flags it as unsolved). This module FAILS LOUD
  (`ConversionError`, naming every frame's `Content-Location`) rather than
  silently converting only the first body as if it were the whole page.

## Optional dependency: lazy, fail-loud-at-point-of-use, never at import time

`html2text`, `trafilatura`, and `lxml` (trafilatura's own transitive dependency,
imported directly here too for `_find_tables_with_merged_cells`'s use of
`lxml.etree.ParserError`/`lxml.html.fromstring`) all live ONLY in this repo's
optional `convert` extra (`pyproject.toml`'s `[project.optional-dependencies]`)
-- deliberately, so the core lib/CLI stay lean (see that extra's own comment).
Importing THIS MODULE must never require them: they are imported lazily, inside
the functions that actually use them, via `_import_or_raise` below. A caller
who never touches conversion (e.g. every other browser-bridge tool) pays
nothing for them and is never blocked from loading by their absence. Only
actually calling `convert_mhtml`/`convert_mhtml_file` without the `convert`
extra installed fails -- loudly, with the exact remediation
(`pip install 'amplifier-browser-bridge[convert]'`), never a bare
`ModuleNotFoundError` that doesn't name what's missing or how to fix it.
"""

from __future__ import annotations

import hashlib
import importlib
import mimetypes
import os
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Any

_CONVERT_EXTRA_HINT = (
    "MHTML-to-markdown conversion requires the optional 'convert' extra. "
    "Install it with: pip install 'amplifier-browser-bridge[convert]'"
)


def _import_or_raise(module_name: str) -> Any:
    """Lazily imports `module_name`, converting any `ImportError` (most often a
    plain `ModuleNotFoundError`) into the `convert` extra's clear, actionable
    remediation message -- see module docstring's "Optional dependency"
    section. Call this from inside a function at the point the dependency is
    actually needed; never at module import time.
    """
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise ImportError(_CONVERT_EXTRA_HINT) from exc


class ConversionError(ValueError):
    """Raised for a conversion-blocking failure: malformed MHTML, no `text/html`
    part found, or more than one `text/html` part found (the documented
    multi-frame hard case -- see module docstring). Never raised for a merely
    imperfect extraction (e.g. trafilatura finding no main content, or a table
    with merged cells) -- those are recorded in the returned result and the
    conversion still completes; see `tables_with_merged_cells` and
    `extracted_markdown.status`.
    """


def _leaf_parts(msg: Message) -> list[Message]:
    """Every non-multipart part of an MHTML document, in the order MIME parsing
    encounters them. A degenerate single-part "MHTML" (no `multipart/related`
    wrapper at all -- a bare `text/html` message) is handled by `walk()` itself:
    it yields just the message, which is not multipart, so it becomes the sole
    leaf part.
    """
    return [part for part in msg.walk() if not part.is_multipart()]


def _content_id_key(content_id: str) -> str:
    """Normalizes a `Content-ID` header (`<foo@bar>`) to the bare `cid:foo@bar`
    form MHTML's own HTML uses to reference it (`<img src="cid:foo@bar">`)."""
    return f"cid:{content_id.strip().strip('<>')}"


def _rewrite_asset_refs(html_text: str, ref_map: dict[str, str]) -> str:
    """Replaces every literal occurrence of an asset's `Content-Location` URL or
    `cid:...` reference with its sidecar's relative path. Plain substring
    replacement (not DOM-aware attribute parsing) is deliberate: it works
    identically inside `src="..."`, `srcset="url 1x, url 2x"`, and CSS `url(...)`
    -- anywhere the exact reference string appears -- without needing separate
    handling for each attribute/context. References are replaced LONGEST FIRST so
    a short reference that happens to be a prefix of a longer one (e.g. a bare
    `cid:foo` vs. `cid:foo-2x`) can never corrupt the longer one.
    """
    for original_ref in sorted(ref_map, key=len, reverse=True):
        html_text = html_text.replace(original_ref, ref_map[original_ref])
    return html_text


def _span_value_over_one(value: str | None) -> bool:
    if not value:
        return False
    try:
        return int(value.strip()) > 1
    except ValueError:
        return False


def _find_tables_with_merged_cells(html_text: str) -> list[dict[str, Any]]:
    """Identifies every `<table>` containing a cell with `colspan`/`rowspan` > 1 --
    see module docstring's "Known limitations" section. Returns an empty list for
    malformed HTML lxml cannot parse at all rather than raising: this is
    best-effort diagnostic metadata, not something that should block an otherwise
    successful conversion.

    Lazily imports `lxml` (module docstring's "Optional dependency" section) --
    only reached from `convert_mhtml`, never at module import time.
    """
    lxml_etree = _import_or_raise("lxml.etree")
    lxml_html = _import_or_raise("lxml.html")
    try:
        tree = lxml_html.fromstring(html_text)
    except lxml_etree.ParserError:
        return []
    found: list[dict[str, Any]] = []
    for index, table in enumerate(tree.iter("table")):
        spanned_cells = table.xpath(".//*[@colspan or @rowspan]")
        has_merge = any(
            _span_value_over_one(cell.get("colspan")) or _span_value_over_one(cell.get("rowspan"))
            for cell in spanned_cells
        )
        if not has_merge:
            continue
        first_row_text = " ".join(t.strip() for t in table.xpath(".//tr[1]//text()") if t.strip())
        found.append({"table_index": index, "first_row_preview": first_row_text[:120]})
    return found


def _full_page_markdown(html_text: str) -> str:
    """The deliberately-unfiltered whole-document pass -- see module docstring's
    "Two outputs, never one" section. `body_width = 0` disables html2text's
    default 78-column hard-wrapping, which would otherwise break long URLs and
    asset paths across lines.

    Lazily imports `html2text` (module docstring's "Optional dependency"
    section) -- only reached from `convert_mhtml`, never at module import time.
    """
    html2text = _import_or_raise("html2text")
    converter = html2text.HTML2Text()
    converter.body_width = 0
    return converter.handle(html_text)


def convert_mhtml(
    mhtml_bytes: bytes,
    *,
    assets_dir: Path,
    markdown_dir: Path,
    base_name: str = "page",
) -> dict[str, Any]:
    """Converts one MHTML document's bytes into markdown, written to disk under
    `markdown_dir` (`<base_name>.extracted.md`, `<base_name>.full_page.md`), with
    every non-HTML MIME part written as a content-addressed sidecar under the
    SHARED `assets_dir` (shared across every page converted into the same
    archive, so identical assets dedupe -- see module docstring). Returns a
    manifest -- paths, byte counts, per-asset metadata, known-limitation
    warnings -- never the markdown text itself.

    Raises `ConversionError` if the document contains zero or more than one
    `text/html`/`application/xhtml+xml` part (see module docstring's "Known
    limitations" section) BEFORE anything is written to disk.
    """
    msg = BytesParser(policy=policy.default).parsebytes(mhtml_bytes)
    leaf_parts = _leaf_parts(msg)
    html_parts = [p for p in leaf_parts if p.get_content_type() in ("text/html", "application/xhtml+xml")]

    if not html_parts:
        raise ConversionError(
            "no text/html (or application/xhtml+xml) part found in this MHTML document -- "
            "nothing to convert. This is not a valid page capture from archive.py's "
            "_record_mhtml_capture."
        )
    if len(html_parts) > 1:
        locations = [p.get("Content-Location") or "(no Content-Location)" for p in html_parts]
        raise ConversionError(
            f"this MHTML document contains {len(html_parts)} separate text/html parts (frames) -- "
            "a page with <iframe>s captured as separate frame documents. Multi-frame MHTML is the "
            "documented hard case this converter does not attempt to merge (see module docstring's "
            '"Known limitations" section): converting only the first body would silently discard '
            f"the rest of the page. Frame Content-Locations: {locations}"
        )

    html_part = html_parts[0]
    html_bytes = html_part.get_payload(decode=True)
    if not isinstance(html_bytes, (bytes, bytearray)):
        raise ConversionError(
            f"the text/html part's payload could not be decoded to bytes (got {type(html_bytes).__name__})"
        )
    charset = html_part.get_content_charset() or "utf-8"
    html_text = html_bytes.decode(charset, errors="replace")

    assets_dir.mkdir(parents=True, exist_ok=True)
    ref_map: dict[str, str] = {}
    asset_entries: list[dict[str, Any]] = []
    for part in leaf_parts:
        if part is html_part:
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, (bytes, bytearray)):
            continue
        digest = hashlib.sha256(payload).hexdigest()
        content_type = part.get_content_type()
        ext = mimetypes.guess_extension(content_type) or ""
        sidecar_path = assets_dir / f"{digest}{ext}"
        already_on_disk = sidecar_path.exists()
        if not already_on_disk:
            sidecar_path.write_bytes(payload)

        location = part.get("Content-Location")
        content_id = part.get("Content-ID")
        rel_path = os.path.relpath(sidecar_path, start=markdown_dir)
        if location:
            ref_map[location] = rel_path
        if content_id:
            ref_map[_content_id_key(content_id)] = rel_path

        asset_entries.append(
            {
                "content_type": content_type,
                "bytes": len(payload),
                "path": str(sidecar_path),
                "deduped": already_on_disk,
                "content_location": location,
                "content_id": content_id,
            }
        )

    markdown_dir.mkdir(parents=True, exist_ok=True)
    rewritten_html = _rewrite_asset_refs(html_text, ref_map)
    tables_with_merged_cells = _find_tables_with_merged_cells(rewritten_html)

    # Lazily imported (module docstring's "Optional dependency" section) --
    # this is the point of use, not module import time.
    trafilatura = _import_or_raise("trafilatura")
    extracted_markdown = trafilatura.extract(
        rewritten_html,
        output_format="markdown",
        include_images=True,
        include_links=True,
        include_tables=True,
        with_metadata=False,
    )
    extracted_path = markdown_dir / f"{base_name}.extracted.md"
    if extracted_markdown:
        extracted_path.write_text(extracted_markdown, encoding="utf-8")
        extracted_status = "ok"
        extracted_bytes = len(extracted_markdown.encode("utf-8"))
    else:
        # trafilatura found no identifiable main content -- e.g. a page that is
        # mostly navigation/UI chrome with no article-shaped text region. This is
        # not a ConversionError: the full-page output below still has everything.
        extracted_path.write_text("", encoding="utf-8")
        extracted_status = "empty"
        extracted_bytes = 0

    full_page_text = _full_page_markdown(rewritten_html)
    full_page_path = markdown_dir / f"{base_name}.full_page.md"
    full_page_path.write_text(full_page_text, encoding="utf-8")

    return {
        "extracted_markdown": {
            "path": str(extracted_path),
            "bytes": extracted_bytes,
            "status": extracted_status,
        },
        "full_page_markdown": {
            "path": str(full_page_path),
            "bytes": len(full_page_text.encode("utf-8")),
        },
        "assets": {
            "dir": str(assets_dir),
            "count": len(asset_entries),
            "items": asset_entries,
        },
        "tables_with_merged_cells": tables_with_merged_cells,
    }


def convert_mhtml_file(
    mhtml_path: str | Path,
    *,
    assets_dir: str | Path,
    markdown_dir: str | Path,
    base_name: str = "page",
) -> dict[str, Any]:
    """`convert_mhtml`, reading its input from an existing `.mhtml` file on disk
    rather than raw bytes -- the entry point `archive_convert.py` uses to convert
    an already-captured `page.mhtml` after the fact."""
    path = Path(mhtml_path).expanduser()
    return convert_mhtml(
        path.read_bytes(),
        assets_dir=Path(assets_dir).expanduser(),
        markdown_dir=Path(markdown_dir).expanduser(),
        base_name=base_name,
    )
