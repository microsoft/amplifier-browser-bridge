"""Tests for extension_integrity.py -- the gate that closes the 87ce68d hole:
`stage_extension()`'s whitelist and `package.sh`'s file list were checked against
THEMSELVES (do these named files exist?) but never against what the shipped files
actually need. A staged/zipped `background.js` importing a file that isn't also
present in the same directory silently kills the entire MV3 service worker on the
next load.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amplifier_browser_bridge.extension_integrity import (
    ExtensionIntegrityError,
    collect_manifest_file_refs,
    find_static_import_specifiers,
    verify_extension_integrity,
)

# ---------------------------------------------------------------------------
# find_static_import_specifiers
# ---------------------------------------------------------------------------


def test_finds_simple_named_import() -> None:
    src = 'import { truthy } from "./args_bool.mjs";\n'
    assert find_static_import_specifiers(src) == ["./args_bool.mjs"]


def test_finds_multiline_import_matching_download_claim_style() -> None:
    src = 'import {\n  pickCompletedDownload,\n  pickInterruptedDownload,\n} from "./download_claim.mjs";\n'
    assert find_static_import_specifiers(src) == ["./download_claim.mjs"]


def test_finds_multiple_imports_in_one_file() -> None:
    src = (
        'import { a } from "./a.mjs";\n'
        'import { b } from "./b.mjs";\n'
        'import { EffectsCollector } from "./effects_collector.mjs";\n'
    )
    assert find_static_import_specifiers(src) == ["./a.mjs", "./b.mjs", "./effects_collector.mjs"]


def test_finds_reexport_from_declaration() -> None:
    src = 'export { helper } from "./helper.mjs";\n'
    assert find_static_import_specifiers(src) == ["./helper.mjs"]


def test_finds_bare_side_effect_import() -> None:
    src = 'import "./polyfill.mjs";\n'
    assert find_static_import_specifiers(src) == ["./polyfill.mjs"]


def test_ignores_bare_package_specifiers() -> None:
    """No relative prefix -- not something staging could have omitted by mistake, and
    not resolvable as a sibling file in the shipped directory."""
    src = 'import something from "some-npm-package";\n'
    assert find_static_import_specifiers(src) == []


def test_ignores_specifier_only_mentioned_in_a_comment() -> None:
    """The load-bearing anti-fooling property: a specifier that appears only inside a
    `//` comment must NOT be treated as a real import -- the comment line starts with
    `//`, never with the bare `import`/`export` keyword the regex anchors on."""
    src = (
        "// injected.js is referenced by chrome.scripting.executeScript, not import\n"
        '// import { fake } from "./not_a_real_import.mjs";\n'
        'import { real } from "./real.mjs";\n'
    )
    assert find_static_import_specifiers(src) == ["./real.mjs"]


def test_ignores_dynamic_import_call() -> None:
    """Dynamic `import()` is deliberately out of scope -- see module docstring: it's an
    ordinary catchable expression, not a whole-script kill switch like a static
    top-level import."""
    src = 'const mod = await import("./maybe_missing.mjs");\n'
    assert find_static_import_specifiers(src) == []


# ---------------------------------------------------------------------------
# collect_manifest_file_refs
# ---------------------------------------------------------------------------


def test_collects_service_worker_and_options_page() -> None:
    manifest = {
        "background": {"service_worker": "background.js", "type": "module"},
        "options_ui": {"page": "options.html", "open_in_tab": True},
    }
    refs = collect_manifest_file_refs(manifest)
    assert set(refs) == {"background.js", "options.html"}


def test_collects_icons_dict_and_string_shapes() -> None:
    manifest = {
        "icons": {"16": "icon16.png", "48": "icon48.png"},
        "action": {"default_icon": "icon.png", "default_popup": "popup.html"},
    }
    refs = collect_manifest_file_refs(manifest)
    assert set(refs) == {"icon16.png", "icon48.png", "icon.png", "popup.html"}


def test_collects_content_scripts_js_and_css() -> None:
    manifest = {"content_scripts": [{"matches": ["<all_urls>"], "js": ["cs.js"], "css": ["cs.css"]}]}
    assert set(collect_manifest_file_refs(manifest)) == {"cs.js", "cs.css"}


def test_skips_glob_patterns_in_web_accessible_resources() -> None:
    manifest = {
        "web_accessible_resources": [{"resources": ["assets/*.png", "fixed.json"], "matches": ["<all_urls>"]}]
    }
    assert collect_manifest_file_refs(manifest) == ["fixed.json"]


def test_real_manifest_json_yields_exactly_the_files_it_references() -> None:
    """Cross-check against the actual shipped manifest.json (not a synthetic fixture)
    so this test breaks loudly if the real manifest grows a field this function
    doesn't yet know how to read.

    The icon entries arrived with the toolbar icon; they are listed here (rather
    than globbed) so that adding a manifest field without teaching
    `collect_manifest_file_refs` about it still fails, which is the whole point
    of pinning the exact set."""
    repo_root = Path(__file__).resolve().parents[1]
    manifest = json.loads((repo_root / "extension" / "manifest.json").read_text(encoding="utf-8"))
    assert set(collect_manifest_file_refs(manifest)) == {
        "background.js",
        "options.html",
        "icons/icon-16.png",
        "icons/icon-32.png",
        "icons/icon-48.png",
        "icons/icon-128.png",
    }


# ---------------------------------------------------------------------------
# verify_extension_integrity
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_verify_passes_on_a_fully_self_contained_directory(tmp_path: Path) -> None:
    _write(
        tmp_path / "manifest.json",
        json.dumps(
            {"background": {"service_worker": "background.js"}, "options_ui": {"page": "options.html"}}
        ),
    )
    _write(tmp_path / "background.js", 'import { x } from "./helper.mjs";\n')
    _write(tmp_path / "helper.mjs", "export const x = 1;\n")
    _write(tmp_path / "options.html", "<html></html>")

    verify_extension_integrity(tmp_path)  # must not raise


def test_verify_fails_when_a_js_import_target_is_missing(tmp_path: Path) -> None:
    """The exact 87ce68d shape: background.js is present and imports a sibling file
    that was never staged alongside it."""
    _write(tmp_path / "manifest.json", json.dumps({"background": {"service_worker": "background.js"}}))
    _write(tmp_path / "background.js", 'import { EffectsCollector } from "./effects_collector.mjs";\n')
    # effects_collector.mjs deliberately NOT written.

    with pytest.raises(ExtensionIntegrityError, match="effects_collector.mjs"):
        verify_extension_integrity(tmp_path)


def test_verify_fails_when_manifest_referenced_file_is_missing(tmp_path: Path) -> None:
    _write(
        tmp_path / "manifest.json",
        json.dumps(
            {"background": {"service_worker": "background.js"}, "options_ui": {"page": "options.html"}}
        ),
    )
    _write(tmp_path / "background.js", "// no imports\n")
    # options.html deliberately NOT written.

    with pytest.raises(ExtensionIntegrityError, match="options.html"):
        verify_extension_integrity(tmp_path)


def test_verify_fails_loud_when_manifest_is_missing(tmp_path: Path) -> None:
    with pytest.raises(ExtensionIntegrityError, match="manifest.json"):
        verify_extension_integrity(tmp_path)


def test_verify_fails_loud_on_malformed_manifest_json(tmp_path: Path) -> None:
    _write(tmp_path / "manifest.json", "{not valid json")
    with pytest.raises(ExtensionIntegrityError, match="not valid JSON"):
        verify_extension_integrity(tmp_path)


def test_verify_against_the_real_extension_source_tree_passes() -> None:
    """The full source checkout has every file, by definition -- this proves the
    checker doesn't false-positive against the real, currently-correct codebase."""
    repo_root = Path(__file__).resolve().parents[1]
    verify_extension_integrity(repo_root / "extension")  # must not raise
