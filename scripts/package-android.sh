#!/usr/bin/env bash
# package-android.sh -- build a signed, versioned CRX3 package of the extension for
# sideloading onto Edge Canary on Android.
#
# Encodes two packaging traps discovered the hard way with the throwaway probe kit
# (see docs/ANDROID.md and docs/designs/browser-bridge.md §2/§7):
#
#   1. A renamed .zip is NOT a valid .crx -- Edge Android's installer fails silently
#      on anything that isn't real CRX3 (`Cr24` magic + version + signed header +
#      zip payload). This script produces a real CRX3 via Chromium's own
#      --pack-extension, never a hand-rolled zip.
#   2. Chromium/Edge intercepts .crx downloads and routes them into the extension
#      installer, which silently discards the file on Android. This script does NOT
#      solve that (it's a serving-time concern, not a packaging one) -- see
#      docs/ANDROID.md for serving the output as .bin and renaming on-device.
#
# Uses manifest.android.json (omits the `debugger` permission -- genuinely absent on
# Edge Android; see design doc §2/§7) rather than the desktop manifest.json.
#
# Reuses a stable signing key across rebuilds so the extension ID doesn't change
# every time this script runs (Android's sideload-by-crx flow treats a new ID as a
# different extension). The key is NEVER written into this repo -- default location
# is under $HOME/.config, override with AMPLIFIER_BROWSER_BRIDGE_ANDROID_SIGNING_KEY. Never commit it.
#
# Usage:
#   scripts/package-android.sh
#   CHROME_BIN=/path/to/chrome scripts/package-android.sh
#   AMPLIFIER_BROWSER_BRIDGE_ANDROID_SIGNING_KEY=/secure/path/key.pem scripts/package-android.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXTENSION_SRC="$REPO_ROOT/extension"
DIST_DIR="$REPO_ROOT/dist/android"
KEY_PATH="${AMPLIFIER_BROWSER_BRIDGE_ANDROID_SIGNING_KEY:-$HOME/.config/amplifier-browser-bridge/android-signing-key.pem}"

# --------------------------------------------------------------------------
# Locate a Chromium/Chrome/Edge binary capable of --pack-extension. Playwright's
# bundled Chromium works headless for this even on a box with no system browser
# (measured on this project's aarch64 dev host -- see docs/ANDROID.md).
# --------------------------------------------------------------------------
find_chrome() {
    if [ -n "${CHROME_BIN:-}" ] && [ -x "${CHROME_BIN}" ]; then
        echo "$CHROME_BIN"
        return 0
    fi
    for candidate in \
        "$(command -v microsoft-edge 2>/dev/null || true)" \
        "$(command -v google-chrome 2>/dev/null || true)" \
        "$(command -v chromium 2>/dev/null || true)"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    # Playwright's bundled Chromium -- newest chromium-* directory under the cache.
    local pw_cache="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
    local newest
    newest=$(ls -d "$pw_cache"/chromium-*/chrome-linux/chrome 2>/dev/null | sort -V | tail -1 || true)
    if [ -n "$newest" ] && [ -x "$newest" ]; then
        echo "$newest"
        return 0
    fi
    echo "ERROR: no Chromium/Chrome/Edge binary found. Set CHROME_BIN explicitly." >&2
    return 1
}

CHROME_BIN="$(find_chrome)"
echo "Using browser binary: $CHROME_BIN" >&2

# --------------------------------------------------------------------------
# Stage a build directory: the extension's JS files + the Android manifest
# (renamed to manifest.json -- --pack-extension packs whatever manifest.json it
# finds in the directory it's given; it has no notion of "which manifest variant").
# --------------------------------------------------------------------------
STAGE_DIR="$(mktemp -d /tmp/amplifier-browser-bridge-android-build.XXXXXX)"
STAGE_EXT="$STAGE_DIR/extension"
mkdir -p "$STAGE_EXT"
cleanup() { rm -rf "$STAGE_DIR"; }
trap cleanup EXIT

cp "$EXTENSION_SRC/background.js" "$STAGE_EXT/"
cp "$EXTENSION_SRC/injected.js" "$STAGE_EXT/"
cp "$EXTENSION_SRC/options.html" "$STAGE_EXT/"
cp "$EXTENSION_SRC/options.js" "$STAGE_EXT/"
cp "$EXTENSION_SRC/config_validate.mjs" "$STAGE_EXT/"
cp "$EXTENSION_SRC/frame_refs.mjs" "$STAGE_EXT/"
cp "$EXTENSION_SRC/combine_frames.mjs" "$STAGE_EXT/"
cp "$EXTENSION_SRC/ref_registry.mjs" "$STAGE_EXT/"
cp "$EXTENSION_SRC/args_bool.mjs" "$STAGE_EXT/"
cp "$EXTENSION_SRC/fetch_utils.mjs" "$STAGE_EXT/"
cp "$EXTENSION_SRC/download_claim.mjs" "$STAGE_EXT/"
cp "$EXTENSION_SRC/manifest.android.json" "$STAGE_EXT/manifest.json"

VERSION=$(python3 -c "import json; print(json.load(open('$STAGE_EXT/manifest.json'))['version'])")
echo "Packaging version: $VERSION" >&2

# --------------------------------------------------------------------------
# Pack. First run (no key yet) lets Chromium generate one, which we then move to
# the stable, gitignored, outside-the-repo location for reuse on every future run.
# --------------------------------------------------------------------------
mkdir -p "$(dirname "$KEY_PATH")"
PACK_ARGS=(--headless --no-sandbox "--pack-extension=$STAGE_EXT")
if [ -f "$KEY_PATH" ]; then
    echo "Reusing existing signing key: $KEY_PATH" >&2
    PACK_ARGS+=("--pack-extension-key=$KEY_PATH")
else
    echo "No signing key found at $KEY_PATH -- generating a new one (first build)." >&2
fi

"$CHROME_BIN" "${PACK_ARGS[@]}" >"$STAGE_DIR/pack.log" 2>&1 || {
    echo "ERROR: --pack-extension failed:" >&2
    cat "$STAGE_DIR/pack.log" >&2
    exit 1
}

GENERATED_CRX="$STAGE_EXT.crx"
GENERATED_PEM="$STAGE_EXT.pem"

if [ ! -f "$GENERATED_CRX" ]; then
    echo "ERROR: pack-extension did not produce a .crx. Log:" >&2
    cat "$STAGE_DIR/pack.log" >&2
    exit 1
fi

if [ ! -f "$KEY_PATH" ]; then
    if [ ! -f "$GENERATED_PEM" ]; then
        echo "ERROR: no signing key existed and none was generated. Cannot guarantee a stable extension ID." >&2
        exit 1
    fi
    mv "$GENERATED_PEM" "$KEY_PATH"
    chmod 600 "$KEY_PATH"
    echo "Saved new signing key to: $KEY_PATH (back this up -- losing it changes the extension ID on every future rebuild)" >&2
fi

mkdir -p "$DIST_DIR"
OUT_CRX="$DIST_DIR/amplifier-browser-bridge-android-v${VERSION}.crx"
cp "$GENERATED_CRX" "$OUT_CRX"

echo "" >&2
echo "Built: $OUT_CRX" >&2
echo "" >&2

# --------------------------------------------------------------------------
# Self-verify everything that CAN be verified from Linux -- see docs/ANDROID.md
# for the honest list of what cannot be (actual sideload/install behavior on a
# real Android device).
# --------------------------------------------------------------------------
python3 "$REPO_ROOT/scripts/verify_crx.py" "$OUT_CRX" --key "$KEY_PATH"
