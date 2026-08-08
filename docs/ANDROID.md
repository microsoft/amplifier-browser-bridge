# Android operator runbook

**Read this before attempting to sideload.** This document is the accumulated,
hard-won knowledge of packaging and sideloading this extension onto Edge Canary on
Android -- some of it (the packaging traps) was proven by direct experimentation on
this project's dev host; the rest (actual on-device install/run behavior) was
proven on real hardware **in a separate, throwaway probe kit**, not with this
project's own code. See "What remains unproven" at the end of this document for
the honest line between the two.

## Prerequisites

- **Edge Canary or Beta on Android.** Arbitrary/unpacked or sideloaded extension
  loading is **not available on stable Edge Android** as of this writing. Install
  Edge Canary from the Play Store.
- **Tailscale installed and connected on the phone.** The extension dials out to
  the hub over the tailnet; if Tailscale isn't connected, or isn't routing traffic
  for Edge, the extension will never reach the hub no matter how correctly it's
  installed.
- **Edge Canary must NOT be excluded from Tailscale's split-tunnel app list.**
  Android's Tailscale client defaults to routing *all* apps through the tailnet
  (an inclusive default), with per-app *exclusion* as the opt-out. Double-check
  Tailscale's app settings and confirm Edge Canary (and Edge Canary specifically,
  not just "Edge") is not in the excluded list. A silently-excluded browser
  produces the same symptom as no Tailscale at all: the extension connects to
  nothing and never appears in `amplifier-browser-bridge devices`.
- **The hub's tailnet IP literal, never a MagicDNS name.** `scripts/package-android.sh`
  now resolves this automatically (via the same `tailscale ip -4` auto-detection
  `amplifier-browser-bridge init` already uses -- see design doc section 4 on why IP
  literals, not MagicDNS names, are required everywhere) and bakes it into the built
  artifact, together with the hub's current token, so a fresh install already knows
  where to connect -- **no options-page visit required.** Override with
  `--hub-host`/`--hub-url` (or the matching env vars) if auto-detection would pick
  the wrong value. See "Zero-configuration builds" below. The options page remains
  available (toolbar icon, on Desktop) and still lets you replace these values, but
  is no longer a *required* step -- see the next section for why that distinction
  matters specifically on Android.

## Two packaging traps (already discovered -- don't rediscover them)

These cost real debugging time against a throwaway probe kit before this project
existed. Both are handled by `scripts/package-android.sh` and the serving
instructions below -- read this section so you understand *why* the script and
the runbook do what they do, not just *that* they do it.

1. **Chromium intercepts `.crx` downloads and routes them into the extension
   installer -- on Edge Android, that path silently discards the file.** You will
   see an HTTP 200 with a completed download, and then nothing: no file in
   Downloads, no error, no toast. This is not a bug in this project's server or
   script; it is the browser's own download-interception behavior for anything
   the OS' MIME sniffing recognizes as `application/x-chrome-extension`.
   **Fix: serve the identical bytes under a neutral extension** (`.bin`), with
   `Content-Type: application/octet-stream` and
   `Content-Disposition: attachment`, then **rename the downloaded file to
   `.crx` on-device** (in My Files / a file manager) before installing it.

2. **A renamed `.zip` is NOT a valid `.crx`.** A real CRX3 file is `Cr24` magic +
   a version field + a signed protobuf header + the zip payload -- not a zip with
   a different extension. Installing a fake one fails **silently**: no error
   dialog, just nothing happens. `scripts/package-android.sh` never hand-rolls
   this; it always shells out to a real Chromium/Edge/Chrome binary's
   `--pack-extension` flag, which produces a genuine CRX3. `scripts/verify_crx.py`
   (run automatically by the packaging script) checks the `Cr24` magic, version,
   and header length before declaring success -- see "What this verifies" below.

## Zero-configuration builds (baked hub URL + token)

**Why this exists.** `chrome.runtime.openOptionsPage()` -- wired to both the toolbar
click and `onInstalled` in `background.js` -- does nothing usable on Edge Android
(field report, 2026-08). There is also no way to type a 32-character extension ID by
hand to reach `chrome-extension://<id>/options.html` directly, and a web page cannot
link to it either unless it's declared in `web_accessible_resources` (it now is, on
the Android manifest only -- see "What `web_accessible_resources` does and does not
give you" below). Without some other channel, a fresh Android sideload had **no
reachable path** to enter a hub URL/token at all -- not an inconvenience, a dead end.

**The fix.** `scripts/package-android.sh` now resolves a hub URL + token (read-only
with respect to the hub's own configuration -- see
`amplifier_browser_bridge.android_bake`'s docstring) and bakes them into
`bundled_config.json`, written **only** inside that script's temporary build-stage
directory -- never into the tracked `extension/` source tree, and never committed.
`background.js` fetches that file at startup and hands it to
`extension/bundled_config.mjs`'s `resolveBundledConfigAdoption()`, which adopts it
into `chrome.storage.local` **only** on a genuinely first-run install -- see that
module's docstring for the exact "never clobber an existing or user-edited config"
invariant, and `extension/bundled_config.test.mjs` for the proof. The options page
(when reachable) shows a provenance line ("these values arrived bundled with this
install...") so a baked default is never mistaken for something you typed.

**Overriding the baked values:**

```bash
scripts/package-android.sh --hub-host 100.124.126.19 --hub-port 8900
scripts/package-android.sh --hub-url ws://100.124.126.19:8900/device
# env var equivalents: AMPLIFIER_BROWSER_BRIDGE_BAKE_HUB_HOST / _BAKE_HUB_PORT / _BAKE_HUB_URL
scripts/package-android.sh --allow-no-token   # dev-only: bakes an artifact with auth DISABLED
```

Auto-detection (no flags) mirrors `amplifier-browser-bridge init`'s own host
resolution: `tailscale ip -4` for the host, port `8900` by default. Unlike `init`,
there is **no loopback fallback** -- a baked config resolving to `127.0.0.1` would
tell the *phone* to connect to itself, so the build refuses loudly instead
(`BUILD REFUSED -- Could not auto-detect...`) rather than shipping something silently
wrong.

**Security disclosure.** The built artifact now carries a live credential -- see
`SECURITY.md`'s "The Android build now embeds a live hub credential in the artifact
itself" section before distributing a build made this way. The build script itself
prints this disclosure at the end of a successful run; do not strip that output from
CI logs or a build wrapper.

### What `web_accessible_resources` does and does not give you

`manifest.android.json` declares `options.html` in `web_accessible_resources`
(`matches: ["<all_urls>"]`) so a web page (e.g. an install landing page) *could*
link to `chrome-extension://<id>/options.html` once the extension ID is known. **This
is unverified on Edge Android** -- it is a low-cost convenience, not a mechanism the
zero-configuration flow depends on. Because the build now bakes in a working hub
URL/token, the extension is expected to connect on its own without anyone ever
opening the options page at all; treat any options-page link as a nice-to-have for
manually overriding the baked values, never as a required step.

## Building the package

```bash
# from the repo root
scripts/package-android.sh
```

This:

1. Stages `extension/background.js`, `injected.js`, `options.html`/`options.js`, and the
   dependency-free `.mjs` helper modules (including `bundled_config.mjs` and
   `effects_collector.mjs`) together with `manifest.android.json` (renamed to
   `manifest.json`) -- the Android-safe
   manifest variant that omits the `debugger` permission (genuinely absent on Edge
   Android; requesting a permission the platform doesn't have is unnecessary risk
   for store/policy review, and every command still works via injection --
   `chrome.scripting` -- without it. Only CDP-requiring commands (`trusted` input,
   `capture_hidden` screenshot) will fail loud with a clear "capability
   unavailable on this device" error instead of silently degrading -- see
   `hub.py`'s `_ensure_cdp_attached` and `docs/POLICY.md`).
2. Bakes a hub URL + token into `bundled_config.json` inside the staging directory
   -- see "Zero-configuration builds" above.
3. **Checks extension integrity** (every static import and manifest reference
   resolves within the staged set) -- the same gate `scripts/package.sh` has always
   had (its Gate 4), now also wired into this script. This is not new caution for
   its own sake: this pass found the staged set had been missing
   `effects_collector.mjs` (a real, already-shipped instance of the exact 87ce68d
   failure mode -- an unresolved top-level import silently kills the entire MV3
   service worker) precisely because this gate had never been wired in here. It is
   now, and a future omission like it will refuse the build instead of shipping
   silently broken.
4. Packs that staged directory into a real CRX3 via a real Chromium/Chrome/Edge
   binary's `--pack-extension` (auto-detects Playwright's bundled Chromium if no
   system browser is present -- this is what this project's own dev host uses,
   since it has no system browser at all).
5. Reuses a **stable signing key** across rebuilds, stored **outside this repo**
   at `~/.config/amplifier-browser-bridge/android-signing-key.pem` by default
   (override with `AMPLIFIER_BROWSER_BRIDGE_ANDROID_SIGNING_KEY`). Reusing the key keeps the extension
   ID stable across rebuilds -- Android's sideload-by-file flow treats a new ID as
   a different extension, which would otherwise mean losing settings/permissions
   on every rebuild. **Never commit this key.** It is never written into the repo
   by this script, and `.gitignore` also defends against `*.pem` as a backstop.
6. Writes the result to `dist/android/amplifier-browser-bridge-android-v<version>.crx`
   (`dist/` is gitignored -- this is a build artifact, not source), restricting the
   staging directory (`chmod 700`), the baked config file, `dist/android/`, and the
   final `.crx` (all `chmod 600`/`700`) so the credential-bearing artifact can't land
   somewhere world-readable by accident -- see "Zero-configuration builds" above.
7. Runs `scripts/verify_crx.py` automatically and prints the result.

### What `verify_crx.py` checks (and what it explicitly does not)

It confirms, purely by inspecting bytes on this machine:

- Real CRX3 structure: `Cr24` magic, version `3`, a plausible header length, and a
  zip payload immediately following the header that starts with the correct zip
  magic (`PK\x03\x04`).
- `manifest.json` exists at the zip root and parses as JSON.
- The manifest is `manifest_version: 3`, has no `debugger` permission, and
  declares `background.service_worker: background.js`.
- The extension ID computed from the signing key (SHA-256 of the DER-encoded
  public key, first 16 bytes, hex nibbles mapped `0-9a-f` -> `a-p`) -- the same
  algorithm Chromium itself uses, so this should match whatever ID Edge reports
  once installed.
- File size and SHA-256 of the packaged artifact.

It does **not** confirm the file will actually install or run on a real Android
device. That is a genuinely different question -- see the final section.

## Serving and sideloading

1. **Serve the `.crx` bytes as `.bin`.** Whatever you use to get the file onto the
   phone (a simple HTTP server, a file share, email attachment), the download
   trap above means Edge on Android must not recognize it as a CRX at download
   time. Rename the file (or serve it under a different name/`Content-Type`) so
   it downloads as, e.g., `amplifier-browser-bridge-android-v0.4.0.bin`.

   A minimal one-liner from this box, for example:
   ```bash
   cd dist/android
   cp amplifier-browser-bridge-android-v0.4.0.crx amplifier-browser-bridge-android-v0.4.0.bin
   python3 -m http.server 8765 --bind 0.0.0.0
   # then, on the phone's browser, fetch:
   # http://<this-machine's-tailnet-IP>:8765/amplifier-browser-bridge-android-v0.4.0.bin
   ```
2. **On the phone**, once the `.bin` file is downloaded, use a file manager (e.g.
   My Files) to rename it back to `.crx` before the install step below.
3. **Enable Developer Options in Edge Canary**, if not already done: Settings ->
   About Microsoft Edge -> tap the build number **5 times**.
4. **Settings -> Developer Options -> "Extension install by crx"** -- this
   **requires a local file path; it will not accept a URL** (confirmed). Point it
   at the renamed `.crx` file from step 2.
5. Confirm the install prompt. If it silently fails with no dialog and no
   toast, see Troubleshooting below -- this is almost always trap #2 above (a
   file that isn't really CRX3, or wasn't renamed correctly).

## Onboarding requirement: battery-optimization exemption

**This is a requirement, not a tip.** Do this immediately after install, before
expecting the device to be reliably reachable:

- Remove Edge Canary from any "sleeping apps" / "put unused apps to sleep" list
  (Samsung/One UI naming; other OEMs have equivalent settings under battery/app
  management).
- Grant Edge Canary a battery-optimization exemption ("Don't optimize" /
  "Unrestricted" battery usage).
- Allow background activity for Edge Canary.

**Why this is not optional, with measured numbers** (design doc section 2, section
5 -- measured on a real device, screen off, in a separate probe kit; the same
conclusion an independent project, `fable-wa`, reached on its own):

| Configuration | Observed dark window | Self-recovery |
|---|---|---|
| **Without** the exemption | **509 seconds dark**, zero reconnects observed | Effectively unreachable until the device is physically touched/woken |
| **With** the exemption | 5 dark windows of **43-133 seconds each** | Every one self-reconnected in **under 2 seconds** |

The underlying MV3 service worker itself was never observed to be evicted in
either case (`restartCount: 0` throughout) -- only the *socket* dies under Android
Doze. With the exemption, the hub's `intermittent` tier (see `tiers.py`,
`INTERMITTENT_MAX_SECONDS = 150s`, padded above the measured 133s ceiling) is the
correct classification for a phone in this state: commands queue and drain
automatically the moment the socket reconnects, typically well under 2.5 minutes.
Without the exemption, a phone can sit **dormant** for many minutes at a stretch
with no way to know when (or whether) it will reconnect on its own.

## What success looks like

1. `amplifier-browser-bridge devices` (from the agent host) lists the phone's device_id with
   `"label": "edge-android"` and `"connected": true` shortly after the extension
   loads and Tailscale is connected.
2. `hello`'s reported `capabilities` show `"debugger": false` (correctly absent --
   do not treat this as a bug) and the rest (`windows`, `tabs`/`scripting` once a
   real tab exists, `storage`, `alarms`, `downloads`) `true`.
3. A command like `amplifier-browser-bridge tabs <device_id>` returns a real tab listing from the
   phone.
4. **The device appears automatically, with no options-page interaction at all**
   (zero-configuration builds -- see above): install the CRX, apply the battery
   exemption, and the phone shows up in `amplifier-browser-bridge devices` on its
   own once Tailscale connects, because the hub URL/token were already baked in.
   If it doesn't, check the options page (if reachable) for the provenance line --
   an absent or "these values were entered manually" (rather than "arrived
   bundled") message means this build didn't have a valid `bundled_config.json`;
   rebuild and check the packaging script's own output for a "BUILD REFUSED" line.
5. After locking the screen (with the battery exemption applied), the device
   transitions to `"tier": "intermittent"` in `amplifier-browser-bridge devices`, and a command issued
   while it's dark returns `{"status": "queued", ...}` immediately rather than
   hanging -- then drains automatically (see `docs/PROTOCOL.md`'s tier section)
   once the phone's next Doze maintenance window reconnects it.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Download completes but nothing appears in Downloads/My Files | Trap #1 -- Chromium intercepted the `.crx` MIME type and silently discarded it | Serve as `.bin` with `Content-Type: application/octet-stream`, rename to `.crx` on-device before installing |
| "Extension install by crx" does nothing -- no dialog, no error, no toast | Trap #2 -- the file isn't a real CRX3 (was a renamed `.zip`, was corrupted in transit, or the rename in step 2 above didn't actually change the extension) | Rebuild with `scripts/package-android.sh` (never hand-roll a `.crx`); re-verify with `scripts/verify_crx.py`; re-download and re-check the file's size/SHA-256 matches what the script printed |
| "Extension install by crx" prompts for a **URL** instead of accepting your file, or otherwise seems to want a network location | Misreading of the feature -- it requires a **local file path**, confirmed; there is no URL-based install path on Edge Android | Ensure the file is downloaded and renamed locally on the device first, then point the file picker at it |
| Extension installs, but never appears in `amplifier-browser-bridge devices` | Tailscale not connected on the phone, Edge Canary excluded from the tailnet, or (only if the baked config was invalid/missing) the Hub URL was never configured, or uses a MagicDNS name instead of an IP literal | Check Tailscale's connection state and per-app exclusion list on the phone; open the extension's options page (toolbar icon, if reachable) and check the provenance line -- "arrived bundled" means baking worked, anything else means rebuild and check the packaging script's output for `BUILD REFUSED` |
| Device connects with a stale/wrong hub URL or token after a hub token rotation | The installed build's `bundled_config.json` was baked BEFORE the token was rotated (`amplifier-browser-bridge init --force`) -- a rebuild is required, baking doesn't happen automatically when the hub's token changes | Rebuild (`scripts/package-android.sh`) so the new token gets baked in, then reinstall on the device (same signing key -> same extension ID -> `chrome.storage.local` config still gets re-adopted only if this install never completed setup before -- see "Zero-configuration builds" above for why an already-configured install is never silently overwritten; you may need to re-enter the new token by hand on an install that already adopted the old one) |
| Device connects, but goes dark for many minutes with no self-recovery | Battery-optimization exemption not applied ("sleeping apps" still active) | Apply the onboarding requirement above; re-test with the screen locked for a few minutes and confirm the tier transitions to `intermittent` (not stuck `dormant` forever) and self-heals |
| `capabilities.debugger` is `false` | **This is correct, not a bug.** `chrome.debugger` (CDP) is genuinely absent on Edge Android | No action -- CDP-requiring commands (`trusted` input, `capture_hidden` screenshot) will fail loud with a clear capability-unavailable error; everything else works via injection |
| Extension ID changes between rebuilds | The signing key wasn't reused -- either it was deleted, or `AMPLIFIER_BROWSER_BRIDGE_ANDROID_SIGNING_KEY` pointed somewhere different between runs | Locate/restore the original key; back it up going forward. A changed ID means Android treats the rebuilt package as a different extension |
| `scripts/package-android.sh` prints `BUILD REFUSED -- No hub token found` | No token exists at the resolved token file path and `AMPLIFIER_BROWSER_BRIDGE_HUB_TOKEN` isn't set | Run `amplifier-browser-bridge init` first to provision one (this script never generates/writes one itself -- see `android_bake.py`'s docstring), or pass `--allow-no-token` for a dev-only, auth-disabled build |
| `scripts/package-android.sh` prints `BUILD REFUSED -- Could not auto-detect this machine's Tailscale IP` | `tailscale ip -4` is unavailable or failed, and no `--hub-host`/`--hub-url` was given | Pass `--hub-host <this machine's tailnet IP>` explicitly -- there is deliberately no loopback fallback here (see "Zero-configuration builds" above) |

## What remains unproven (read before believing this "just works")

**This project explicitly did not claim Android works, and this document does
not either.** The following was measured on real Edge Android hardware, but with
a **separate, throwaway probe-kit extension** -- not this project's own code --
so treat it as evidence that the *platform* behaves this way, not as proof that
*this extension* has been run there:

- That `scripts/package-android.sh`'s output actually installs via "Extension
  install by crx" on a real device. Everything this document verifies is
  structural (real CRX3, correct manifest, correct ID) -- not "Edge Android
  accepted and ran it."
- Whether CRX sideload works on Edge Android **stable**, or only on
  Canary/Beta -- untested; Canary is the only channel this runbook assumes.
- Whether Edge Add-ons store review would pass a browser-remote-control
  extension at all (relevant only if store distribution, rather than sideload,
  is later pursued).
- The exact on-device behavior of *this* extension's background.js reconnect
  logic, capability probe, and command dispatch under real Android Doze cycling
  -- the measured dark-window numbers above come from the probe kit's simpler
  heartbeat-only extension, not from `extension/background.js`'s full command
  vocabulary and CDP-escalation logic (most of which is inert on Android anyway,
  since `chrome.debugger` is absent there).
- Whether the battery-optimization / "sleeping apps" onboarding steps are named
  identically on non-Samsung OEM skins -- the settings exist on stock Android and
  every OEM variant, but the exact menu path was only confirmed on a Samsung
  device.
- **Zero-configuration builds (the "Zero-configuration builds" section above) have
  NOT been confirmed on a real Android device.** This pass verified, on Linux, that
  `scripts/package-android.sh` bakes the intended values into `bundled_config.json`
  inside the packed CRX (unpacked and inspected directly -- see the packaging
  script's own output for the exact command), and that `background.js`'s adoption
  decision logic is correct via its unit tests
  (`extension/bundled_config.test.mjs`). Whether Edge Android's service worker
  actually fetches and adopts that bundled file on a genuine first install, and
  whether the options page's "web_accessible_resources" link to itself actually
  resolves there, are unverified on-device -- the same honest gap this section's
  opening paragraph already applies to everything else in this runbook.

**Bottom line: this document packages the artifact and verifies everything
checkable without a phone. Confirming it actually installs, connects, and
operates on a real Android device is the next, explicitly unproven step.**
