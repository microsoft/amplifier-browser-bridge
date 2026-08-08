#!/usr/bin/env bash
# package.sh -- build a shareable, sideload-ready zip of the DESKTOP extension.
#
# Output: dist/amplifier-browser-bridge-extension-v<VERSION>.zip
#
# Modeled directly on the release-kit pattern proven in two sibling extensions
# by the same maintainer (teams-transcript-md/package.sh, loop-page-md/package.sh):
# a build GATE that refuses to produce an artifact on a missing required file,
# malformed manifest, or a JS syntax/test failure, followed by a byte-
# reproducible zip (sorted `find` + `zip -X`) with a printed SHA256.
#
# Differences from those exemplars, both load-bearing for THIS project:
#   1. The runtime file set matches setup.py's `_EXTENSION_FILES` exactly --
#      that list is the existing single source of truth for what
#      `amplifier-browser-bridge init` stages from a real (non-editable) install.
#      Duplicating it here as a second hardcoded list would be exactly the kind
#      of drift IMPLEMENTATION_PHILOSOPHY.md warns against; this script derives
#      it by importing setup.py's own constant rather than re-typing it.
#   2. The syntax/test gate runs `node --test extension/*.test.mjs` -- this
#      project has real regression tests (unlike the exemplars' best-effort
#      single-file check), so failing tests refuse the build, not just failing
#      to parse.
#   3. This is NOT a CRX. Desktop sideload uses Edge's plain "Load unpacked" on
#      an unzipped folder (see INSTALL.md) -- no signing key, no CRX3 framing.
#      scripts/package-android.sh is the separate, CRX3-producing sibling for
#      the Android sideload path (see docs/ANDROID.md); the two are not
#      interchangeable and this script does not attempt to replace it.
#
# Usage:
#   scripts/package.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXTENSION_SRC="$REPO_ROOT/extension"
DIST_DIR="$REPO_ROOT/dist"

cd "$REPO_ROOT"

# --------------------------------------------------------------------------
# Gate 1: required files present. Derive the list from setup.py's own
# _EXTENSION_FILES constant (the pre-existing single source of truth for
# what a desktop install actually needs) rather than a second hand-maintained
# list -- see header comment.
# --------------------------------------------------------------------------
EXTENSION_FILES_JSON="$(python3 -c "
import ast
from pathlib import Path
src = Path('src/amplifier_browser_bridge/setup.py').read_text(encoding='utf-8')
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == '_EXTENSION_FILES' for t in node.targets):
        import json
        print(json.dumps(ast.literal_eval(node.value)))
        break
")"
if [ -z "$EXTENSION_FILES_JSON" ]; then
    echo "ERROR: could not locate _EXTENSION_FILES in src/amplifier_browser_bridge/setup.py -- refusing to guess the runtime file set." >&2
    exit 1
fi
mapfile -t REQUIRED < <(python3 -c "import json,sys; print('\n'.join(json.loads(sys.argv[1])))" "$EXTENSION_FILES_JSON")
# Always also required, even though setup.py's own staging list doesn't need
# it (init prints remaining manual steps instead) -- the zip's own reader has
# no CLI to print those steps for them, so INSTALL.md must ship in their place.
REQUIRED+=("INSTALL.md")

echo "Runtime files required (from setup.py's _EXTENSION_FILES + INSTALL.md):" >&2
printf '  - %s\n' "${REQUIRED[@]}" >&2

missing=()
for f in "${REQUIRED[@]}"; do
    if [ "$f" = "INSTALL.md" ]; then
        [ -f "$REPO_ROOT/$f" ] || missing+=("$f")
    else
        [ -f "$EXTENSION_SRC/$f" ] || missing+=("extension/$f")
    fi
done
if [ ${#missing[@]} -gt 0 ]; then
    echo "" >&2
    echo "BUILD REFUSED -- missing required files:" >&2
    printf '  - %s\n' "${missing[@]}" >&2
    exit 1
fi

# --------------------------------------------------------------------------
# Gate 2: manifest.json parses as JSON and looks like a real MV3 manifest.
# --------------------------------------------------------------------------
python3 -c "
import json, sys
try:
    m = json.load(open('extension/manifest.json', encoding='utf-8'))
except Exception as e:
    print(f'BUILD REFUSED -- extension/manifest.json is not valid JSON: {e}', file=sys.stderr)
    sys.exit(1)
if m.get('manifest_version') != 3:
    print('BUILD REFUSED -- manifest_version is not 3', file=sys.stderr)
    sys.exit(1)
if 'version' not in m:
    print('BUILD REFUSED -- manifest.json has no version field', file=sys.stderr)
    sys.exit(1)
" || exit 1

VERSION="$(python3 -c "import json; print(json.load(open('extension/manifest.json', encoding='utf-8'))['version'])")"
echo "Packaging version: $VERSION" >&2

# --------------------------------------------------------------------------
# Gate 3: every shipped .js/.mjs file parses, and the real regression suite
# passes. Unlike the exemplars (best-effort, `command -v node` optional),
# this project HAS a real test suite (extension/*.test.mjs, 107+ cases as of
# this writing) -- a missing `node` or a failing test refuses the build,
# full stop, rather than degrading to a warning.
# --------------------------------------------------------------------------
if ! command -v node >/dev/null 2>&1; then
    echo "BUILD REFUSED -- node is required to run the syntax/regression gate and was not found." >&2
    exit 1
fi

echo "" >&2
echo "Checking JS/MJS syntax..." >&2
# background.js/options.js are ES modules (background.js is declared
# "type": "module" in manifest.json; options.js is loaded via
# <script type="module"> from options.html) but carry a plain .js extension.
# `node --check` treats a bare .js file as CommonJS and rejects `import` --
# the same false-negative the exemplar package.sh scripts already worked
# around. Copy every .js/.mjs file to a temp .mjs before checking so the gate
# reflects how the browser actually parses it, not Node's extension guess.
CHECK_TMP="$(mktemp -d)"
trap 'rm -rf "$CHECK_TMP"' EXIT
for f in "${REQUIRED[@]}"; do
    case "$f" in
        *.js|*.mjs)
            tmp="$CHECK_TMP/$(basename "${f%.*}").mjs"
            cp "$EXTENSION_SRC/$f" "$tmp"
            if ! node --check "$tmp" 2>/tmp/package-sh-check-err; then
                echo "BUILD REFUSED -- syntax check failed: extension/$f" >&2
                cat /tmp/package-sh-check-err >&2
                rm -f /tmp/package-sh-check-err
                exit 1
            fi
            ;;
    esac
done
rm -f /tmp/package-sh-check-err
echo "  OK -- all shipped .js/.mjs files parse." >&2

echo "" >&2
echo "Running extension regression suite (node --test extension/*.test.mjs)..." >&2
if ! node --test extension/*.test.mjs >/tmp/package-sh-test-out 2>&1; then
    echo "BUILD REFUSED -- extension regression tests failed:" >&2
    cat /tmp/package-sh-test-out >&2
    rm -f /tmp/package-sh-test-out
    exit 1
fi
tail -8 /tmp/package-sh-test-out >&2
rm -f /tmp/package-sh-test-out

# --------------------------------------------------------------------------
# Build: stage exactly the required files, zip with a stable sorted order so
# the artifact is byte-reproducible across runs on the same source.
# --------------------------------------------------------------------------
NAME="amplifier-browser-bridge-extension"
STAGE="$DIST_DIR/${NAME}"
ZIP="$DIST_DIR/${NAME}-v${VERSION}.zip"

rm -rf "$STAGE" "$ZIP"
mkdir -p "$STAGE"

for f in "${REQUIRED[@]}"; do
    if [ "$f" = "INSTALL.md" ]; then
        cp "$REPO_ROOT/INSTALL.md" "$STAGE/"
    else
        cp "$EXTENSION_SRC/$f" "$STAGE/"
    fi
done

# Note: manifest.android.json, *.test.mjs, store-assets equivalents, README.md,
# docs/, and everything else in this repo are intentionally NOT included --
# this zip contains only what a sideloader's browser actually loads, plus the
# one document (INSTALL.md) written for them.

# --------------------------------------------------------------------------
# Gate 4: extension integrity -- the actual staged set (not the source tree,
# which always has every file and could never reproduce this) must be
# internally consistent: every static import a shipped .js/.mjs file declares,
# and every file manifest.json references, must exist in $STAGE. This is the
# exact gate that would have caught 87ce68d (background.js imports
# effects_collector.mjs; it was omitted from setup.py's _EXTENSION_FILES, so
# earlier builds staged a background.js importing a file never staged
# alongside it) -- see src/amplifier_browser_bridge/extension_integrity.py for
# why this is a parse-based check rather than an actual-load check.
# --------------------------------------------------------------------------
echo "" >&2
echo "Checking extension integrity (imports + manifest refs resolve within \$STAGE)..." >&2
if ! python3 -c "
import sys
sys.path.insert(0, 'src')
from pathlib import Path
from amplifier_browser_bridge.extension_integrity import ExtensionIntegrityError, verify_extension_integrity
try:
    verify_extension_integrity(Path('$STAGE'))
except ExtensionIntegrityError as e:
    print(f'BUILD REFUSED -- {e}', file=sys.stderr)
    sys.exit(1)
"; then
    rm -rf "$STAGE"
    exit 1
fi
echo "  OK -- every import and manifest reference resolves within the staged set." >&2

# Normalize every staged file's mtime to a fixed reference timestamp before
# zipping. `zip -X` alone (as used by the sibling projects' package.sh
# scripts) strips UID/GID/extra-field metadata but NOT each entry's DOS
# date/time, which is taken from the file's actual mtime -- `cp` sets that to
# "now" on every run, so two builds from IDENTICAL source produce DIFFERENT
# zip bytes (and therefore different SHA256) if run more than ~2 seconds
# apart. Verified against this script during development: two consecutive
# runs produced different hashes; the sibling projects' own package.sh
# scripts exhibit the identical issue when re-run seconds apart, despite
# their header comments claiming reproducibility. Pinning every mtime here
# (rather than assuming "rerun quickly enough") is what actually makes the
# SHA256 below reproducible across arbitrarily separated runs.
find "$STAGE" -type f -exec touch -t 202001010000 {} +

(
    cd "$STAGE"
    # -X strips file metadata that varies run-to-run (uid/gid/timestamps).
    find . -type f -print | LC_ALL=C sort | zip -X -9 -q "../$(basename "$ZIP")" -@
)
rm -rf "$STAGE"

SIZE_BYTES="$(stat -c%s "$ZIP" 2>/dev/null || stat -f%z "$ZIP")"
SHA="$(command -v sha256sum >/dev/null && sha256sum "$ZIP" | awk '{print $1}' \
       || shasum -a 256 "$ZIP" | awk '{print $1}')"

echo ""
echo "Built: $ZIP"
echo "Size:  ${SIZE_BYTES} bytes"
echo "SHA256: $SHA"
echo ""
echo "This is a DESKTOP sideload package (Load unpacked-equivalent via a zip)."
echo "For the Android CRX3 package, use scripts/package-android.sh instead."
echo ""
echo "Full peer-facing install steps are inside the zip as INSTALL.md."
