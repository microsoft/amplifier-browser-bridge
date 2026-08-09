# Desktop distribution beyond "Load unpacked"

`INSTALL.md` covers the one distribution path this project has actually
exercised: unzip, enable Developer mode, **Load unpacked**. This document
covers the three non-store distribution mechanisms an organization would
reach for instead -- CRX packaging, a self-hosted update manifest, and
`ExtensionInstallForcelist` -- and is explicit about which parts are
established platform behavior (documented by Chromium/Microsoft) versus
untested against this specific extension.

**None of this has precedent in this project's own testing.** Unlike
`docs/ANDROID.md`, whose packaging claims were measured against real
hardware in a throwaway probe kit, everything below is derived from Chromium
and Microsoft's own published documentation and general platform behavior,
not from a live experiment run against this extension. Read the "What
remains unverified" section at the end before treating any of this as a
ready-to-follow runbook.

---

## 1. CRX packaging for desktop

### The mechanics (established, and consistent with what this project
### already measured for Android)

A real CRX3 file is `Cr24` magic bytes + a version field + a signed protobuf
header + a zip payload -- not a zip with a different file extension. This is
exactly the packaging trap `docs/ANDROID.md` documents finding the hard way
(`scripts/package-android.sh`'s header comment, "packaging traps"), and the
mechanics are identical for a desktop-targeted CRX: pack the extension
directory with a real Chromium/Edge/Chrome binary's `--pack-extension` flag,
never hand-roll the container format. `scripts/verify_crx.py` (already
written for the Android build) verifies the `Cr24` magic, version, and header
length purely from the file's bytes, and would work identically against a
desktop-targeted CRX -- it has no Android-specific logic in it.

A desktop CRX build would reuse the same signing-key-stability requirement
`scripts/package-android.sh` already implements: reuse one key across
rebuilds (stored outside the repo, never committed) so the extension's ID
stays stable, because a new ID is treated as a different extension by
anything that pins to a specific ID (an update manifest, or
`ExtensionInstallForcelist`'s `extension_id;update_url` pairing, both below).

**A desktop build would use `extension/manifest.json`** (the full manifest,
including the `debugger` permission), not `manifest.android.json` -- desktop
Edge has `chrome.debugger` available, unlike Android. `scripts/package.sh`
(this project's desktop zip builder, for the sideload-by-unpacked-folder
path) already validates this manifest; a desktop CRX build would be a
sibling script that packs the same staged file set into a CRX3 instead of a
zip, following `scripts/package-android.sh`'s structure.

### What is genuinely uncertain: whether a desktop user can install the
### resulting file at all

Unlike Android's "Extension install by crx" developer-mode feature (measured
and documented in `docs/ANDROID.md`), **modern desktop Chrome and Edge do not
offer an equivalent "install this local .crx file" affordance for an
ordinary user.** As of recent Chromium versions, installing a bare `.crx`
file by drag-and-drop or double-click -- outside the Chrome Web Store / Edge
Add-ons and outside an enterprise policy -- is blocked by design: the
browser disables the extension and shows a message to the effect of "This
extension is not listed in the Chrome Web Store and may have been added
without your knowledge." This is a long-standing, deliberate anti-malware
measure, not a bug, and it is **not** something this project's own code can
route around.

**What this means concretely: on desktop, a packaged CRX is not, by itself,
a distribution mechanism a typical user can act on.** It only becomes
installable through one of:

- **`ExtensionInstallForcelist`** (below) -- an enterprise policy an IT
  admin applies; the end user never manually installs anything.
- **Developer mode's "Load unpacked"** on the *unzipped* extension directory
  -- which is exactly what `scripts/package.sh` and `INSTALL.md` already
  support, and does not need a CRX at all.
- A store listing (explicitly out of scope for this project today --
  see `SCOPE-OUTS` in this project's own goal tracking).

**A desktop CRX build is therefore only useful as an input to
`ExtensionInstallForcelist`'s self-hosted update manifest path below -- not
as a standalone artifact to hand a colleague, unlike the Android CRX (which
a real device's Developer Options *can* install directly from a local
file).** This is the single most important distinction between the Android
and desktop CRX stories, and conflating them would overstate what this
project can actually deliver on desktop.

---

## 2. The self-hosted update manifest

### The mechanics (Chromium's own documented autoupdate protocol)

`ExtensionInstallForcelist` (below) needs an **update URL** -- an HTTPS
endpoint returning an XML document in Chromium's own update-manifest format,
which the browser polls periodically (documented at
<https://developer.chrome.com/docs/apps/autoupdate/>). The minimal shape:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<gupdate xmlns="http://www.google.com/update2/response" protocol="2.0">
  <app appid="YOUR_EXTENSION_ID">
    <updatecheck codebase="https://your-host/amplifier-browser-bridge-v0.4.0.crx"
                 version="0.4.0" />
  </app>
</gupdate>
```

- `appid` must match the extension ID computed from the signing key
  (`scripts/verify_crx.py` already computes this the same way Chromium
  does: SHA-256 of the DER-encoded public key, first 16 bytes, hex nibbles
  mapped `0-9a-f` -> `a-p` -- see that script's `compute_extension_id`).
- `codebase` must be an HTTPS URL serving the actual `.crx` bytes.
- `version` must match the CRX's own `manifest.json` version, or the browser
  will not consider it an update.
- Per Microsoft's and Google's own policy documentation (confirmed via their
  published policy references, not this project's own testing): **the
  `ExtensionInstallForcelist` entry's update URL is used only for the
  *initial* install.** Subsequent update checks use the `update_url` field
  inside the *installed* extension's own `manifest.json` -- which this
  project's `manifest.json` does not currently declare. A real deployment
  would need to add an `update_url` key to `manifest.json` pointing at the
  same (or a versioned-forever) manifest endpoint, or updates would only
  ever happen via `ExtensionSettings`' override mechanism instead.

### Hosting requirements

- HTTPS is mandatory; Chromium's autoupdate client refuses plain HTTP.
- The endpoint must stay reachable indefinitely for as long as any deployed
  copy of the extension should keep receiving updates -- this is an ongoing
  hosting commitment, not a one-time publish.
- The `.crx` referenced by `codebase` must be signed with the **same**
  signing key as every previous version (see "signing-key stability" above)
  -- a new key produces a new extension ID, which an existing
  `ExtensionInstallForcelist`/update-manifest deployment would treat as an
  entirely different extension, not an update to the existing one.

---

## 3. `ExtensionInstallForcelist`

### The mechanics (Chromium/Microsoft's own documented enterprise policy)

An administrator sets this policy (via Windows Group Policy/registry, macOS
`.plist` configuration profile, or a Linux policy JSON file) to a list of
`extension_id;update_url` pairs. Each listed extension is silently installed
on every managed device, without any store listing and without any
per-device manual "Load unpacked" step, and cannot be disabled or removed by
the end user (per Microsoft's own policy documentation for
`ExtensionInstallForcelist`).

This is the **only** desktop distribution mechanism in this document that
does not require a manual, per-device install step from a non-technical
user -- which is exactly why it is the most attractive path for an
organization wanting to roll this extension out at scale, and also why it
carries the most administrative weight: it bypasses every review process
(Chrome Web Store, Edge Add-ons) that would otherwise have looked at this
extension's permissions before it reached an end user's machine.

### What force-installing costs the user: the debugger banner disappears

**Verified in Chromium source, and rarely stated anywhere:** a force-installed
extension is exempted from the browser's *"started debugging this browser"*
warning entirely. `chrome/browser/extensions/api/debugger/debugger_api.cc`
sets `suppress_warning` when
`Manifest::IsPolicyLocation(extension_->location())` is true -- the same
exemption as the `--silent-debugger-extension-api` switch.

For this extension specifically, that means: a copy deployed by
`ExtensionInstallForcelist` can escalate to CDP (`trusted` input,
`capture_hidden` screenshots) on a user's real, logged-in browser **with no
visible indication whatsoever**. On a manually-installed copy, that banner is
the user's one free, unfakeable signal that escalation happened, and their one
always-available way to cut it off (its Cancel button detaches every session
the extension holds).

This is Chromium's behavior, not a choice this project makes, and it is not an
argument against force-install -- an organization deploying agent software to
managed devices has other, better controls (`ExtensionSettings`'
`runtime_blocked_hosts`, below; the hub's own audit log and kill switch). It
*is* something to decide deliberately rather than discover later: choosing this
channel means choosing to give up that disclosure, so whatever replaces it
should be named in the deployment plan. See
[DEBUGGER_BANNER.md](DEBUGGER_BANNER.md) for the full behavior and its sources.

### Companion policy worth knowing about: `ExtensionSettings`

Microsoft's own documentation for `ExtensionInstallForcelist` notes that its
update URL is for **initial** installation only, and that `ExtensionSettings`
can override the update URL used for subsequent updates. `ExtensionSettings`
also supports `runtime_blocked_hosts`/`runtime_allowed_hosts` -- a real,
policy-level way to further scope where a force-installed extension's
content-script/host-permission access actually reaches, independent of what
the manifest itself declares. An organization deploying this extension via
force-install has a real, additional lever here beyond the manifest's
`<all_urls>` grant -- worth pairing with the honest accounting in
`docs/permission-justifications.md`.

---

## What remains unverified (read before treating this as a runbook)

- **No desktop CRX has actually been built for this project.** Everything
  in section 1 is a direct application of the same mechanics
  `scripts/package-android.sh` already proved out for Android, applied by
  reasoning, not by running a desktop-targeted build and inspecting its
  output. Producing one (a sibling `scripts/package-desktop-crx.sh`, using
  `extension/manifest.json` instead of the Android variant) is a
  reasonable next step, not something this pass did.
- **No update manifest has been hosted and polled by a real Edge instance.**
  The XML shape above is Chromium's own documented format; whether a real,
  self-hosted manifest correctly drives an update in Edge specifically
  (rather than Chrome) has not been tested against this extension.
- **No `ExtensionInstallForcelist` policy has been applied to a real
  managed device in this project's testing.** The mechanics above are
  Microsoft's and Google's own published policy documentation, not a
  measured result the way the Android sideload runbook's dark-window
  numbers are.
- **Whether the desktop-blocked-CRX-install behavior described in section 1
  is bypassed by any additional, undocumented flag or setting** (analogous
  to how Android's Developer Options exposes "Extension install by crx") has
  not been checked. Treat "no user-facing way to install a bare desktop CRX
  outside enterprise policy or a store" as the working assumption until
  someone verifies otherwise on a real, current Edge desktop build.

**Bottom line: the CRX-packaging mechanics are a safe, well-understood
extension of work this project has already proven out for Android. The
distribution mechanisms built on top of them -- self-hosted update
manifests, `ExtensionInstallForcelist` -- are Chromium/Microsoft's own
documented enterprise features, described here in good faith but never
exercised end-to-end against this specific extension. Treat this document as
a map of the territory, not a report of having walked it.**
