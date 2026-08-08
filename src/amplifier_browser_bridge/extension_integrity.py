"""Verify that a MATERIALIZED extension directory -- the actual set of files about to
be loaded by a browser or zipped into a release artifact -- is internally consistent:
every static `import`/`export ... from` specifier a shipped .js/.mjs file declares
resolves to a file that is ALSO present in that same directory, and every file
manifest.json references (service worker, options page, icons, content scripts, ...)
exists there too.

Why this exists (the bug this closes)
--------------------------------------
Commit 87ce68d added `import { EffectsCollector, ... } from "./effects_collector.mjs";`
to the top of background.js but nobody added `effects_collector.mjs` to setup.py's
`_EXTENSION_FILES` staging whitelist. `stage_extension()` copied background.js into
the staged directory without its dependency. A top-level `import` that fails to
resolve kills the ENTIRE MV3 service worker script -- no listeners register, at all --
and neither the missing-file gate (which only checks the whitelist against itself) nor
the JS syntax gate (`node --check`, which parses but never resolves imports) noticed.

The fix is to check the CONSEQUENCE, not the list: verify the directory that will
actually ship is self-contained, by parsing what its own files say they need and
confirming those files are there. Run this against the whitelist's OUTPUT (a staged
directory / build-stage directory), never against the full `extension/` source tree --
the full source tree always contains every file, so checking it can never reproduce
this bug. The whitelist is only as good as what it's checked against.

Design choice: parse, don't load
---------------------------------
Two honest ways to verify an import graph resolves: (a) parse the sources and check
every relative specifier against the shipped file set, or (b) actually load the module
graph (e.g. via a real JS engine's dynamic `import()`) and let the runtime's own
resolver decide.

(b) is the *more* authoritative check in principle -- Node's own ES module resolver
cannot be fooled by anything our own parser might miss -- but it requires Node.js to
be present wherever the check runs. `stage_extension()` runs as part of
`amplifier-browser-bridge init`, on whatever host is running the hub; nothing else in
this package (see pyproject.toml's `dependencies`) requires Node, and the hub is meant
to run on minimal hosts. Making Node a hard new runtime dependency of `init`, just to
gain marginally stronger detection of a bug class that a much simpler check already
catches, would violate this project's own "necessity" test (IMPLEMENTATION_PHILOSOPHY.md
Decision-Making Framework, #1). `scripts/package.sh` already requires Node for its own
reasons (syntax-checking and running the real *.test.mjs suite) -- but `setup.py` must
not gain that requirement just for this.

So: (a), implemented carefully so it cannot be fooled by the things a naive regex could
miss. Two properties make this defensible instead of merely convenient:

1. Only specifiers on a line that STARTS (after leading whitespace) with the bare
   keyword `import` or `export` are matched (`re.MULTILINE`, anchored at `^`). A
   specifier merely *mentioned* in a `//` comment or a data string can never match,
   because in this codebase (and in valid JS generally) a comment line starts with
   `//`, not with the bare keyword -- there is no way to write a comment that satisfies
   this anchor. This is the same reasoning `IMPORT_RE` in the sibling *.test.mjs style
   already relies on implicitly by always writing imports as their own top-of-file
   statement.
2. Only *static* `import`/`export ... from` declarations are in scope -- not dynamic
   `import()` calls. This is deliberate, not a gap: a static top-level import is
   resolved during the module graph's linking phase, BEFORE any code runs, so an
   unresolvable one is fatal to the entire file (exactly this bug). A dynamic
   `import()` is an ordinary expression evaluated at its call site; a missing target
   there is a normal (catchable) promise rejection, not a whole-script kill switch --
   a fundamentally different failure mode this checker is not scoped to catch.

Non-relative (bare) specifiers (`import x from "some-package"`) are skipped -- there
are none in this codebase (it ships zero npm dependencies into the extension; grep
confirms every specifier is `./...`), and a bare specifier is not something staging
could have omitted by mistake in the first place.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = [
    "ExtensionIntegrityError",
    "collect_manifest_file_refs",
    "find_static_import_specifiers",
    "verify_extension_integrity",
]


class ExtensionIntegrityError(RuntimeError):
    """Raised when a materialized extension directory references a file (via a static
    import or a manifest.json field) that is not present in that same directory."""


# Matches `import ... from "spec"` / `export ... from "spec"`, anchored so the
# keyword must start the (whitespace-trimmed) line -- see module docstring point 1.
# `[\s\S]*?` (non-greedy, DOTALL-equivalent) lets this span the multi-line
# `import {\n  a,\n  b,\n} from "./x.mjs";` form this codebase uses.
_IMPORT_FROM_RE = re.compile(
    r'^[ \t]*(?:import|export)\b[\s\S]*?\bfrom\s+["\']([^"\']+)["\']',
    re.MULTILINE,
)
# Side-effect-only form: `import "spec";` (no `from`). Unused today but a real, valid
# ES module form -- covered so this checker doesn't silently miss it if introduced later.
_IMPORT_BARE_RE = re.compile(r'^[ \t]*import\s+["\']([^"\']+)["\']\s*;', re.MULTILINE)

# MV3 manifest keys that name a file expected to ship alongside manifest.json.
# Deliberately conservative: only keys this project's manifests plausibly use, per
# the MV3 schema. Extend if a new manifest field starts referencing a file.
_JS_SUFFIXES = (".js", ".mjs")


def find_static_import_specifiers(source: str) -> list[str]:
    """Return every *relative* (`./` or `../`) specifier from static
    `import`/`export ... from` declarations and bare `import "spec";` statements in
    `source`. Non-relative (bare package) specifiers are omitted -- see module
    docstring for why."""
    specifiers = [*_IMPORT_FROM_RE.findall(source), *_IMPORT_BARE_RE.findall(source)]
    return [s for s in specifiers if s.startswith(("./", "../"))]


def collect_manifest_file_refs(manifest: dict) -> list[str]:
    """Return every file path an MV3 manifest.json references, that is expected to
    ship as a sibling file (not a glob pattern, not a URL). Covers every field this
    project's manifests (manifest.json / manifest.android.json) use today, plus the
    common MV3 fields likely to be added later (icons, content scripts, popups) so
    this doesn't need to be re-taught the next time the manifest grows a field."""
    refs: list[str] = []

    background = manifest.get("background")
    if isinstance(background, dict) and isinstance(background.get("service_worker"), str):
        refs.append(background["service_worker"])

    options_ui = manifest.get("options_ui")
    if isinstance(options_ui, dict) and isinstance(options_ui.get("page"), str):
        refs.append(options_ui["page"])
    if isinstance(manifest.get("options_page"), str):  # legacy MV2-style key
        refs.append(manifest["options_page"])

    if isinstance(manifest.get("devtools_page"), str):
        refs.append(manifest["devtools_page"])

    action = manifest.get("action")
    if isinstance(action, dict):
        if isinstance(action.get("default_popup"), str):
            refs.append(action["default_popup"])
        _collect_icon_values(action.get("default_icon"), refs)

    _collect_icon_values(manifest.get("icons"), refs)

    for script in manifest.get("content_scripts") or []:
        if not isinstance(script, dict):
            continue
        refs.extend(p for p in (script.get("js") or []) if isinstance(p, str))
        refs.extend(p for p in (script.get("css") or []) if isinstance(p, str))

    sandbox = manifest.get("sandbox")
    if isinstance(sandbox, dict):
        refs.extend(p for p in (sandbox.get("pages") or []) if isinstance(p, str))

    overrides = manifest.get("chrome_url_overrides")
    if isinstance(overrides, dict):
        refs.extend(p for p in overrides.values() if isinstance(p, str))

    for war in manifest.get("web_accessible_resources") or []:
        if isinstance(war, dict):
            refs.extend(p for p in (war.get("resources") or []) if isinstance(p, str) and "*" not in p)

    return refs


def _collect_icon_values(value: object, refs: list[str]) -> None:
    """`icons` / `action.default_icon` are either a bare path string or a
    `{"<size>": "path"}` dict -- MV3 allows both shapes."""
    if isinstance(value, str):
        refs.append(value)
    elif isinstance(value, dict):
        refs.extend(p for p in value.values() if isinstance(p, str))


def verify_extension_integrity(shipped_dir: str | Path, *, manifest_name: str = "manifest.json") -> None:
    """Raise `ExtensionIntegrityError` naming every file that `shipped_dir`'s own
    manifest and JS/MJS files reference but that is NOT present in `shipped_dir`.

    Call this against the directory that will actually be loaded by a browser or
    zipped into a release artifact (a staged `init` directory, or `package.sh`'s
    build-stage directory) -- never against the full `extension/` source tree, which
    always contains every file and so can never reproduce a staging omission.
    """
    shipped_dir = Path(shipped_dir)
    manifest_path = shipped_dir / manifest_name
    if not manifest_path.is_file():
        raise ExtensionIntegrityError(
            f"{manifest_path} does not exist -- cannot verify extension integrity without a manifest."
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ExtensionIntegrityError(f"{manifest_path} is not valid JSON: {e}") from e

    problems: list[str] = []

    for ref in collect_manifest_file_refs(manifest):
        if not (shipped_dir / ref).is_file():
            problems.append(f"manifest.json references {ref!r}, but {shipped_dir / ref} does not exist")

    for js_file in sorted(shipped_dir.rglob("*")):
        if not js_file.is_file() or js_file.suffix not in _JS_SUFFIXES:
            continue
        source = js_file.read_text(encoding="utf-8")
        for spec in find_static_import_specifiers(source):
            resolved = (js_file.parent / spec).resolve()
            if not resolved.is_file():
                rel_importer = js_file.relative_to(shipped_dir)
                problems.append(
                    f"{rel_importer} imports {spec!r}, but {resolved} does not exist in the shipped directory"
                )

    if problems:
        bullets = "\n".join(f"  - {p}" for p in problems)
        raise ExtensionIntegrityError(
            f"extension integrity check failed for {shipped_dir} -- referenced file(s) missing "
            f"from the shipped set:\n{bullets}\n\n"
            "This is exactly the 87ce68d failure mode: a file is imported (or referenced by "
            "manifest.json) but was never staged/packaged alongside it. A missing top-level "
            "import kills the entire MV3 service worker with no listeners registered."
        )
