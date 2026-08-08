# Publishing and distribution

**Short answer: this extension is not published to a browser extension store, and
submitting it is not planned.** It is distributed by sideload. This document records
that decision and its reasoning, and describes how a release actually gets built.

If you came here looking for Chrome Web Store or Edge Add-ons submission steps, they
do not exist for this project on purpose. See "Why not a store listing" below.

## How this is actually distributed

Two paths, both sideload, both already documented elsewhere:

| Audience | Path | Entry point |
| --- | --- | --- |
| Someone running the hub themselves | `amplifier-browser-bridge init` stages the extension to `~/.local/share/amplifier-browser-bridge/extension`, then Edge's **Load unpacked** | [README.md](README.md) |
| Someone handed a zip by a peer | Unzip, then **Load unpacked** on the unzipped folder | [INSTALL.md](INSTALL.md), which ships *inside* the zip |
| Edge on Android | A CRX3 package from `scripts/package-android.sh` — a different artifact, not interchangeable with the desktop zip | `docs/ANDROID.md` |

## Building a desktop release

```
scripts/package.sh
```

Output is `dist/amplifier-browser-bridge-extension-v<VERSION>.zip`, where `<VERSION>`
is read from `extension/manifest.json`'s `version` field — that manifest is the single
source of truth for the version, so bumping it there is the whole of "cutting a
release."

The script is a build **gate**, not a zip helper. It refuses to produce an artifact on:

1. **Missing required files** — the runtime file set is derived by parsing
   `_EXTENSION_FILES` out of `src/amplifier_browser_bridge/setup.py` rather than
   re-typing it, so the zip and `init`'s staging can never drift apart. `INSTALL.md` is
   appended, because the zip's reader has no CLI to print the remaining steps for them.
2. **A malformed manifest** — must parse as JSON, must be `manifest_version: 3`, must
   carry a `version`.
3. **A JS syntax error or a failing test** — every shipped `.js`/`.mjs` is copied to a
   temp `.mjs` before `node --check` (Node otherwise treats a bare `.js` as CommonJS and
   rejects `import`), and `node --test extension/*.test.mjs` must pass in full.
4. **A staged set that is not internally consistent** — every static import a shipped
   file declares, and every file `manifest.json` references, must resolve *within the
   staged output*. This gate exists because a real shipped bug (`87ce68d`) staged a
   `background.js` importing a module that was never staged beside it, silently killing
   the service worker on its next instantiation.

Every staged file's mtime is pinned to a fixed timestamp before zipping, so the printed
SHA256 is reproducible across runs from identical source. `zip -X` alone is not enough:
it strips uid/gid but not each entry's DOS date/time, which `cp` sets to "now."

A representative run:

```
Built: dist/amplifier-browser-bridge-extension-v0.4.0.zip
Size:  68951 bytes
SHA256: c187f03be56ae2e3215821fb1f76cfcdd84738c4b386d293952e3acdb09374ca
```

## Why not a store listing

Not an oversight, and not "we haven't got round to it." README.md already states plainly
that this repository has no packaged release, no CI history, and has not been submitted
to the Edge Add-ons store.

The substantive reason is the permission surface. This extension requests `<all_urls>`
host permissions plus `debugger`, `tabs`, `downloads`, and `scripting`, and its stated
purpose is to let a *remote* agent observe and drive the user's real, authenticated
browser session. A store listing requires a "single purpose" statement and a
per-permission justification written for a reviewer whose job is to keep exactly this
capability out of a general-audience catalog. Writing that copy would not be a
documentation exercise with an uncertain outcome; it would be a submission very likely
to be refused, for defensible reasons.

The audience this software actually has — someone who already runs the hub on their own
tailnet and reads [SECURITY.md](SECURITY.md) before deploying it — reaches it through
sideload. That is the distribution model, and it is served.

## Decisions recorded

Reviewed 2026-08-08 against two sibling extensions by the same maintainer
(`teams-transcript-md`, `loop-page-md`), which each ship a `store-assets/` directory and
a store-submission `PUBLISH.md`. Both were considered here and declined.

**`store-assets/` — declined; no directory created.**
Every artifact in those directories has exactly one consumer: a store dashboard form.
Promo tiles at store-mandated 1400×560 and 440×280, screenshots at 1280×800, listing
prose, per-permission justifications, privacy-tab answers, search keywords, a
single-purpose statement. With no submission, each has zero consumers here. Creating the
directory anyway — holding placeholders, or a note explaining its own emptiness — would
be the shape of the exemplars without their substance.

**A store-submission guide — declined; this file is not one.**
Both exemplars' `PUBLISH.md` are dashboard walkthroughs: developer-account setup, the
Chrome $5 fee, field-by-field listing copy, review-wait etiquette. That content has no
target here. What generalizes is the part above — how the artifact is built and what
gates it — so that is what this file carries.

**What would be needed if this is ever revisited.** So the decision is reversible with
knowledge rather than rediscovered: a single-purpose statement, per-permission
justifications (the `debugger` and `<all_urls>` entries being the load-bearing ones),
privacy-policy hosting (the repo already has `privacy/`), 1280×800 screenshots, promo
tiles at the two dimensions above, and a developer account per store. The exemplars'
`store-assets/build_promo.py` (Pillow) and `build_promo.sh` (headless-Chromium render of
an HTML template) are working generators for the tiles.

### Outcome of that review

The same review also asked whether this project should adopt the exemplars' popup UI.
Every item reached a terminal state:

| Item | Terminal state | Evidence |
| --- | --- | --- |
| Toolbar popup UI | **PASS — built**, as a read-only *status* surface rather than the exemplars' action-trigger shape, which has no equivalent action here | `extension/popup.{html,css,js}` (`01fb234`); renders only the five fields `amplifier_browser_bridge_get_status` actually returns; logic verified out-of-tree 12/12, rendered and checked in three states |
| `store-assets/` | **FAIL — declined**, no directory created | Store submission is out of scope, and every artifact in the exemplars' version has exactly one consumer: a store dashboard form (reasoning above) |
| Store-submission guide | **FAIL — declined**, replaced by this file | The exemplars' version is a dashboard walkthrough with no target here; what generalises is the build-and-gating section above (`6a9d278`) |

**The popup is committed inert, and does not function until (1) and (2) land.** That is
a deliberate outcome, not an unfinished one: the change that added it was scoped to
`extension/popup.{html,css,js}`, `store-assets/` and this file, and was instructed to
*record* the manifest edit rather than make it, because a sibling change owns both
manifests. Every item below is therefore **blocked for that change** — none could have
been resolved by it — and belongs to a reconciliation step that owns the files in
question. They are prerequisites, not optional follow-ups.

Measured rather than assumed, in a throwaway copy of this tree (nothing was applied
here): applying (1) **alone** makes `scripts/package.sh` refuse the build — *"extension
integrity check failed … referenced file(s) missing from the shipped set"*. Applying (1)
and (2) **together** builds cleanly and stages `popup.css`, `popup.html` and `popup.js`
into the artifact (75045 bytes, SHA256 `488b064e…`). Land them in one commit.

1. **Manifest edit — BLOCKED.** Add `"default_popup": "popup.html"` to the existing
   `"action"` block in `manifest.json` *and* `manifest.android.json`. Blocker: file
   ownership forbade touching either manifest; a sibling change owns both, and editing
   one would have caused a merge conflict rather than a courtesy.
2. **`_EXTENSION_FILES` edit — BLOCKED.** Add `"popup.html"`, `"popup.css"`, `"popup.js"`
   in `src/amplifier_browser_bridge/setup.py`. Blocker: outside the owned paths. Must land
   in the same commit as (1) — see the measurement above.
3. **`background.js`'s `chrome.action.onClicked` — BLOCKED.** Becomes dead code once (1)
   lands, and the toolbar click stops reaching the options page at all. Blocker: outside
   the owned paths. This is why the popup's Settings button is not garnish.
4. **No `popup.test.mjs` — BLOCKED.** Leaves `popup.js` the only module here without the
   repo's test-per-module coverage. Blocker: ownership covered `popup.{html,css,js}`, not
   a new test file. The logic was verified out-of-tree instead; that harness should be
   committed here when the popup is activated.
5. **README.md's "its only UI" — BLOCKED.** Accurate today, stale the moment (1) lands.
   Blocker: README.md was explicitly out of scope.
6. **Edge-on-Android reachability — BLOCKED.** Whether a popup is reachable through
   Android's extension menu — the affordance that already proved unreliable for the
   options page. Blocker: answering it needs real device hardware, which was not
   available. Not simulated and not assumed; it stays open until someone can put the
   build on a phone.

Full activation detail lives in `extension/popup.js`'s header, next to the code it
gates, rather than in a second copy here that could drift out of step.

## Related

- [INSTALL.md](INSTALL.md) — peer-facing sideload steps; ships inside the zip
- [README.md](README.md) — what this project is and its current maturity
- [SECURITY.md](SECURITY.md) — threat model; read before deploying
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, tests, lint and type checking
