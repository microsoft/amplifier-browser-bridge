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
# Zero-configuration install (bakes a hub URL + token into the build)
# ---------------------------------------------------------------------
# There is no reachable path to chrome-extension://<id>/options.html on Edge Android
# by hand (see docs/ANDROID.md's "Zero-configuration builds" section and
# extension/bundled_config.mjs's docstring for the full rationale). This script now
# resolves a hub URL + token (via amplifier_browser_bridge.android_bake, read-only
# w.r.t. the hub's own config -- see that module's docstring) and bakes them into
# `bundled_config.json` inside $STAGE_EXT ONLY -- this file is NEVER written into the
# tracked extension/ source tree, and is not committed. background.js adopts it as a
# FIRST-RUN DEFAULT ONLY on install (extension/bundled_config.mjs).
#
# SECURITY: the built artifact now carries a live credential -- see SECURITY.md's
# "The Android build now embeds a live hub credential in the artifact itself"
# section. This script prints that disclosure at build time (never silently);
# do not strip that output from CI logs or a build wrapper.
#
# Usage:
#   scripts/package-android.sh
#   scripts/package-android.sh --hub-host 100.124.126.19 --hub-port 8900
#   scripts/package-android.sh --hub-url ws://100.124.126.19:8900/device
#   scripts/package-android.sh --allow-no-token   # dev-only: bakes auth-disabled
#   CHROME_BIN=/path/to/chrome scripts/package-android.sh
#   AMPLIFIER_BROWSER_BRIDGE_ANDROID_SIGNING_KEY=/secure/path/key.pem scripts/package-android.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXTENSION_SRC="$REPO_ROOT/extension"
DIST_DIR="$REPO_ROOT/dist/android"
KEY_PATH="${AMPLIFIER_BROWSER_BRIDGE_ANDROID_SIGNING_KEY:-$HOME/.config/amplifier-browser-bridge/android-signing-key.pem}"

# --------------------------------------------------------------------------
# Argument parsing -- the "overridable by flag or env" surface for the baked hub
# URL/token. Distinct env var names from the hub-side AMPLIFIER_BROWSER_BRIDGE_HUB_URL
# (cli.py's own default for the /agent route) deliberately: that variable means a
# different route (/agent, not /device) and reusing it here would silently bake the
# wrong path into the artifact if someone had it set for an unrelated CLI invocation.
# --------------------------------------------------------------------------
BAKE_HUB_URL="${AMPLIFIER_BROWSER_BRIDGE_BAKE_HUB_URL:-}"
BAKE_HUB_HOST="${AMPLIFIER_BROWSER_BRIDGE_BAKE_HUB_HOST:-}"
BAKE_HUB_PORT="${AMPLIFIER_BROWSER_BRIDGE_BAKE_HUB_PORT:-8900}"
BAKE_ALLOW_NO_TOKEN="${AMPLIFIER_BROWSER_BRIDGE_BAKE_ALLOW_NO_TOKEN:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        --hub-url) BAKE_HUB_URL="$2"; shift 2 ;;
        --hub-host) BAKE_HUB_HOST="$2"; shift 2 ;;
        --hub-port) BAKE_HUB_PORT="$2"; shift 2 ;;
        --allow-no-token) BAKE_ALLOW_NO_TOKEN="1"; shift ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            echo "Usage: $0 [--hub-url ws://host:port/device] [--hub-host HOST] [--hub-port PORT] [--allow-no-token]" >&2
            exit 1
            ;;
    esac
done

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
# STAGE_DIR briefly holds bundled_config.json (a live credential in plaintext, see
# below) before it's packed into the CRX -- restrict it to the owner as defense in
# depth, on top of it being a private /tmp path and always cleaned up by the trap above.
chmod 700 "$STAGE_DIR"

cp "$EXTENSION_SRC/background.js" "$STAGE_EXT/"
cp "$EXTENSION_SRC/injected.js" "$STAGE_EXT/"
cp "$EXTENSION_SRC/options.html" "$STAGE_EXT/"
cp "$EXTENSION_SRC/options.js" "$STAGE_EXT/"
cp "$EXTENSION_SRC/config_validate.mjs" "$STAGE_EXT/"
cp "$EXTENSION_SRC/bundled_config.mjs" "$STAGE_EXT/"
cp "$EXTENSION_SRC/frame_refs.mjs" "$STAGE_EXT/"
cp "$EXTENSION_SRC/combine_frames.mjs" "$STAGE_EXT/"
cp "$EXTENSION_SRC/ref_registry.mjs" "$STAGE_EXT/"
cp "$EXTENSION_SRC/args_bool.mjs" "$STAGE_EXT/"
cp "$EXTENSION_SRC/fetch_utils.mjs" "$STAGE_EXT/"
cp "$EXTENSION_SRC/download_claim.mjs" "$STAGE_EXT/"
cp "$EXTENSION_SRC/effects_collector.mjs" "$STAGE_EXT/"
cp "$EXTENSION_SRC/manifest.android.json" "$STAGE_EXT/manifest.json"

VERSION=$(python3 -c "import json; print(json.load(open('$STAGE_EXT/manifest.json'))['version'])")
echo "Packaging version: $VERSION" >&2

# --------------------------------------------------------------------------
# Bake a hub URL + token into bundled_config.json (zero-configuration Android
# install -- see this script's header comment and docs/ANDROID.md). Written ONLY
# into $STAGE_EXT -- never into the tracked extension/ source tree.
#
# Uses amplifier_browser_bridge.android_bake (read-only w.r.t. the hub's own
# token file) via the same `sys.path.insert(0, 'src')` convention scripts/package.sh
# already uses for its own inline Python steps, rather than requiring this package
# be installed into whatever Python environment runs this script.
# --------------------------------------------------------------------------
echo "" >&2
echo "Baking hub URL + token into bundled_config.json..." >&2
BAKE_ARGS=(--stage-dir "$STAGE_EXT" --hub-port "$BAKE_HUB_PORT")
[ -n "$BAKE_HUB_URL" ] && BAKE_ARGS+=(--hub-url "$BAKE_HUB_URL")
[ -n "$BAKE_HUB_HOST" ] && BAKE_ARGS+=(--hub-host "$BAKE_HUB_HOST")
[ -n "$BAKE_ALLOW_NO_TOKEN" ] && BAKE_ARGS+=(--allow-no-token)
if ! python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/src')
from amplifier_browser_bridge.android_bake import cli_main
sys.exit(cli_main(sys.argv[1:]))
" "${BAKE_ARGS[@]}"; then
    exit 1
fi
# The freshly-written bundled_config.json carries a live credential in plaintext
# until it's packed into the CRX below -- restrict it to the owner (defense in
# depth; write_bundled_config() already does this, this is belt-and-suspenders
# against that function ever changing without this script noticing).
chmod 600 "$STAGE_EXT/bundled_config.json"

# --------------------------------------------------------------------------
# Extension integrity gate -- previously present in scripts/package.sh (Gate 4) but
# NOT wired into this script, which meant a staging omission here (like the real one
# this pass found and fixed: effects_collector.mjs was never copied above, even
# though background.js imports it at module top level -- exactly the 87ce68d
# failure mode) shipped completely undetected. Verifies the CONSEQUENCE -- that
# $STAGE_EXT is self-contained -- not the staging list against itself; see
# extension_integrity.py's own docstring for why.
# --------------------------------------------------------------------------
echo "" >&2
echo "Checking extension integrity (imports + manifest refs resolve within \$STAGE_EXT)..." >&2
if ! python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/src')
from pathlib import Path
from amplifier_browser_bridge.extension_integrity import ExtensionIntegrityError, verify_extension_integrity
try:
    verify_extension_integrity(Path('$STAGE_EXT'))
except ExtensionIntegrityError as e:
    print(f'BUILD REFUSED -- {e}', file=sys.stderr)
    sys.exit(1)
"; then
    exit 1
fi
echo "  OK -- every import and manifest reference resolves within the staged set." >&2

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
chmod 700 "$DIST_DIR"
OUT_CRX="$DIST_DIR/amplifier-browser-bridge-android-v${VERSION}.crx"
cp "$GENERATED_CRX" "$OUT_CRX"
# The built artifact now carries the same live credential bundled_config.json did --
# restrict its permissions so it can't land somewhere world-readable by accident. This
# does NOT survive the file being copied/uploaded elsewhere -- see SECURITY.md.
chmod 600 "$OUT_CRX"

echo "" >&2
echo "Built: $OUT_CRX" >&2
echo "" >&2
echo "SECURITY: this artifact embeds a live hub credential (bundled_config.json, baked" >&2
echo "above). Anyone who obtains this file can connect to the hub as this device." >&2
echo "Treat it the same way you would treat the hub's own token file. See SECURITY.md's" >&2
echo "'The Android build now embeds a live hub credential in the artifact itself'" >&2
echo "section and docs/ANDROID.md's 'Zero-configuration builds' section." >&2
echo "" >&2

# --------------------------------------------------------------------------
# Self-verify everything that CAN be verified from Linux -- see docs/ANDROID.md
# for the honest list of what cannot be (actual sideload/install behavior on a
# real Android device).
# --------------------------------------------------------------------------
python3 "$REPO_ROOT/scripts/verify_crx.py" "$OUT_CRX" --key "$KEY_PATH"
