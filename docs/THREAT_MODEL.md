# Threat model: amplifier-browser-bridge

> **Reporting a vulnerability?** Do not open a public issue. See [SECURITY.md](../SECURITY.md)
> for the Microsoft Security Response Center (MSRC) reporting path.

This document is the project's own threat model. It was previously carried inside
`SECURITY.md`; it now lives here so that `SECURITY.md` stays the short, standard
Microsoft vulnerability-reporting file that GitHub surfaces in its "Report a
vulnerability" UI. No content was dropped in the move.

---

## Project-specific threat model

This is not a general-purpose library -- it lets an AI agent read and act inside a user's
**real, logged-in Microsoft Edge browser** on another device. That capability is the point of
the project, and it is also the entire attack surface. Report vulnerabilities related to any of
the following exactly as described above -- via MSRC, not a public issue.

**This section was rewritten after a security review found three separate cases where an
earlier version of this document asserted a property (a two-layer tailnet+token boundary, a
reachable kill switch, a buried-but-load-bearing classifier limit) that the code did not
actually enforce.** Treat every claim below as something you can verify by reading the named
file -- that is the standard this rewrite was held to, and the standard any future edit to this
section should be held to as well.

### Where the load-bearing boundary actually is: the per-device token, not the tailnet

Earlier revisions of this document described a two-layer model -- "the tailnet is the outer
boundary, the token is a second, narrower one" -- and treated the tailnet layer as doing real
work by default. It does not, for most deployments, and the code changes described below exist
because of that gap:

- **Tailscale's own default ACL policy allows every device on your tailnet to reach every other
  device on every port.** This is Tailscale's default, not something this project configures or
  can override. Unless you have written a restrictive ACL in your tailnet's admin console, any
  device you have ever authorized onto your tailnet -- a work laptop, a friend's shared machine, a
  device you later stop trusting -- can reach this hub's port. A starting-point restrictive policy
  is shipped at [`docs/tailscale-acl-example.hujson`](docs/tailscale-acl-example.hujson); `amplifier-browser-bridge doctor`
  now reports this disclosure and your bind-address exposure on every run (`network_exposure`
  check, `doctor.py`).
- **`amplifier-browser-bridge hub` now defaults to `--host 127.0.0.1`** (loopback only -- reachable from this
  machine alone), not the previous silent `0.0.0.0` default, which bound every network interface
  the host had (home Wi-Fi, hotel Wi-Fi, a corporate LAN) -- not only the tailnet. `amplifier-browser-bridge init`
  auto-detects this machine's own Tailscale IP (`tailscale ip -4`) as a safer cross-device-capable
  default, and prints a specific, named warning (`netinfo.wildcard_bind_warning`) any time a
  wildcard host is actually chosen -- by either command.
- **Given both of the above, the per-device shared token (`auth.py`) is the boundary that
  actually does the work in most real deployments.** Auth is **disabled by default in local
  development** (loudly logged as such); running a hub reachable from more than one device
  without setting `AMPLIFIER_BROWSER_BRIDGE_HUB_TOKEN` (or provisioning one via `amplifier-browser-bridge init`) means
  anything that can reach the port -- on the tailnet, or beyond it if the hub is bound wider than
  loopback -- can issue commands to every connected browser. Token comparison is constant-time
  (`hmac.compare_digest`, `auth.py`'s `_tokens_equal`) so this boundary cannot be defeated by a
  timing side-channel either.
- **One token controls every device connected to this hub.** `amplifier-browser-bridge init` provisions a
  single shared `default` token; `TokenStore`'s file format (`auth.py`) supports per-device
  tokens (a `devices` map keyed by `device_id`), but nothing in this codebase auto-provisions
  them -- an operator wanting per-device isolation must hand-edit the token file after each
  device's `device_id` is known (from a `devices`/`list_devices` call) and reconfigure that
  device's extension with its own token. Until you do that, a single stolen or leaked token is a
  **stolen-once, controls-everything** credential: rotate it (`amplifier-browser-bridge init --force`) if you
  suspect it has leaked, and treat it with the same care as any bearer credential that grants
  broad access.

If you find a way to reach the hub, an extension, or a command result from outside your
intended boundary without a valid token, or a way to defeat the token check itself, that is a
critical finding.

### The Android build now embeds a live hub credential in the artifact itself

`scripts/package-android.sh` bakes the current hub URL and token into
`bundled_config.json` inside its build-time staging directory (never into the
tracked `extension/` source tree -- see that script's own comments) so a freshly
sideloaded Android install already knows where to connect, with no options-page
visit required. See `docs/ANDROID.md`'s "Zero-configuration builds" section for the
mechanics and `extension/bundled_config.mjs`'s docstring for the first-run-only
adoption logic (a bundled value is a DEFAULT, never applied over an existing or
user-edited config).

**This means the built `.crx` (and the `.bin` it is temporarily served as during
transfer to the phone -- see docs/ANDROID.md's serving instructions) is now a
bearer credential, not just an installer.** Anyone who obtains that file can
extract `bundled_config.json` and connect to the hub as that device, exactly as if
they had read the token file directly. This is an accepted trade-off under this
project's stated trust model -- broad access by default to anyone who already has
access to the desktop and browser profiles this hub protects -- not an oversight:

- The packaging script restricts the artifact's permissions (`chmod 600` on both
  `bundled_config.json` during staging and the final `.crx`, `chmod 700` on the
  staging directory and `dist/android/`) as defense in depth, but this does **not**
  survive the file being copied, emailed, or uploaded somewhere else -- treat the
  `.crx`/`.bin` the same way you would treat the token file itself once it leaves
  this machine.
- If a baked artifact is ever exposed unintentionally (uploaded to a public
  location, sent over an insecure channel, left in a world-readable directory),
  **rotate the token** (`amplifier-browser-bridge init --force`) and rebuild -- the
  old baked token keeps working against the hub until the hub's own token file
  changes.
- Building with `--allow-no-token` (explicitly opted into, only when no token is
  found) produces an artifact with auth **disabled** baked in -- anyone who can
  reach the hub's bind address connects as this device with no credential check at
  all. `scripts/package-android.sh` prints a loud warning when this happens; do not
  distribute a build made this way.

If you find a way for the baked token to leak through a channel other than the
artifact file itself (e.g. logged in cleartext somewhere it shouldn't be, readable
by another local user despite the permission hardening above), treat that as a
finding under this document's existing reporting instructions.

**The one sentence anyone handed that file must be told, including a tester:**

> The Android artifact contains a live hub credential, and that credential **does not
> rotate**. If the file leaves your control, treat it as compromised.

### One credential, two trust models -- and the rotation story that spans them

The desktop token and the Android token are **the same secret**, governed two completely
different ways, and until this section nobody stated the relationship. Fixing either side in
isolation is not the fix; the relationship is the thing that needed narrating.

| | Desktop | Android |
|---|---|---|
| How it reaches the browser | Printed by `init`, **pasted by a human** into the options page | **Baked into the artifact** at build time, adopted silently on first run |
| Is it ever displayed? | Yes -- you saw it, you typed it | No -- it is never shown to the person installing |
| Can it be replaced in place? | Yes -- re-paste on the options page | Not reliably: `chrome.runtime.openOptionsPage()` does nothing usable on Edge Android (see `docs/ANDROID.md`) |
| What it lives inside | `chrome.storage.local` only | `chrome.storage.local` **and** a copyable file that keeps working |
| Blast radius if it leaks | The token | The token, plus a ready-to-install extension configured to use it |

The second column is the one that changes the security character: on desktop the credential is
visible and revisable by the person who owns it; on Android it is invisible, and it exists in a
second place -- a file -- that can be copied, backed up, forwarded, or left in a Downloads
folder. An unrotatable token baked into a shareable file is not a friendlier onboarding step; it
is the same bearer-capability shape this project's whole transport design exists to avoid,
wearing a friendlier hat.

**Rotation, end to end.** `amplifier-browser-bridge init --force` regenerates the hub's
`default` token. It is the **only** revocation mechanism this system has, and it is
all-or-nothing -- there is no way to revoke one artifact, one phone, or one desktop. Rotating to
kill a leaked file kills every device you have configured. In full, what you must then redo:

1. **The hub** -- `amplifier-browser-bridge service restart` (the unit bakes the token file's
   *path*, not its contents, so a restart is enough). A foreground `hub` must be stopped and
   restarted.
2. **Every desktop browser** -- open the options page (toolbar icon), paste the new token, Save.
   Until you do, that device fails `doctor`'s `token_match` and stops connecting.
3. **Every Android install** -- rebuild (`scripts/package-android.sh`, which reads the *new*
   token) and reinstall. **The sharp edge:** a baked value is a first-run DEFAULT and is never
   applied over an existing config (`extension/bundled_config.mjs`'s stated invariant), so
   reinstalling over an install that already completed setup leaves the **old** token in place
   and the device silently goes dark -- with no reachable options page on Android to correct it
   by hand. The reliable path is **uninstall the extension first, then install the freshly-baked
   artifact**, so it is genuinely a first run.
4. **Every previously-distributed `.crx`/`.bin`** is now dead. That is the point, and it is also
   the cost: you cannot invalidate the one that leaked without invalidating the ones that
   didn't.

Until step 3 completes, a rotated hub and a stale phone look exactly like a broken install.
Budget for that before rotating, rather than discovering it with a phone in your hand.

### Where a live credential ends up after install (artifact lifecycle)

The disclosures above cover the artifact in transit. They did **not** cover what happens to it
afterward, which is where a bearer credential actually spends most of its life. This section
exists because that gap was named and not previously written down:

| Situation | What is actually true | What to do |
|---|---|---|
| **After a successful install** | The `.crx`/`.bin` stays in the phone's Downloads folder, with a live token, indefinitely | Delete it from the device once the extension appears in `amplifier-browser-bridge devices` |
| **Cloud / OEM backup** | Downloads folders are commonly included in device backups (Samsung Cloud, Google One, and equivalents) -- a live hub credential then exists inside a third-party account, entirely outside your tailnet and outside every boundary this project's design reasons about | Delete the artifact before the next backup runs, or treat a compromise of that account as a hub-token compromise and rotate |
| **Device sale, return, repair, or trade-in** | A factory reset removes both the file and the installed extension's storage. An **un-reset** device does not: the installed extension still holds a working token in `chrome.storage.local`, and the artifact may still be in Downloads | Uninstall the extension **and** factory reset before the device leaves your hands. If it already left un-reset, rotate |
| **Device lost or stolen** | Locking or remotely wiping is not revocation from this hub's point of view, and this system cannot revoke one device | Rotate (`init --force`) and redo the full sequence above. There is no smaller lever |
| **Sharing a `.bin` to help someone troubleshoot** | That file is a working key to your browser. Attaching it to an issue, a chat, or a support thread hands over the hub | **Never share the artifact.** Share the build command (`scripts/package-android.sh`) and let them bake their own token |
| **The build host's `dist/android/`** | `chmod 600`/`700` is applied, but the artifact persists across rebuilds, and an explicit `--hub-url` may also sit in shell history | Treat `dist/android/` as secret material; prune old versioned artifacts rather than accumulating them |

**Known gaps in this model, stated rather than closed:**

- **No per-artifact credential.** One `default` token serves every device. `auth.py`'s
  `TokenStore` supports a per-device `devices` map, but nothing provisions it automatically, so
  in practice a leaked artifact is a leak of every device's credential.
- **No expiry.** A baked token is valid until the hub's token file changes -- forever, otherwise.
- **No per-artifact revocation.** See "all-or-nothing" above.
- **The hub cannot tell two holders of the same token apart** at authentication time, so a
  stolen artifact connecting alongside the real phone is not distinguishable as such by auth.
  It *is* visible after the fact: every command and result is audited (`audit.py`), and a second
  device appearing in `amplifier-browser-bridge devices` is observable.
- **The artifact carries no build identity.** `bundled_config.json` holds exactly `hubUrl` and
  `hubToken` (`android_bake.py`'s `write_bundled_config`) -- no build timestamp, no version, no
  identifier -- so a recovered leaked file cannot be traced to which build it came from or when.

### The classifier's label-extraction gap is bounded, not closed -- read this before relying on the confirmation gate

**This is the single most important paragraph in this document.** The confirmation-gate
classifier (`classify.py`) scores a proposed action's visible label/context text against a
family/phrase vocabulary to decide whether to require confirmation. Every signal it scores is
**page-asserted and therefore forgeable** -- `classify.py`'s own module docstring says so, and
`Classification.advisory` is always `True`. Beyond that general limitation, the EXTRACTION step
that hands this module its input text has specific, known gaps, closed only in part:

- **Unicode confusables (partially fixed)**: a label using look-alike Cyrillic/Greek characters
  for an otherwise-ordinary word (e.g. "Аdmin" with a Cyrillic А, U+0410) previously matched
  nothing. `classify.py`'s `_normalize_confusables` now catches the specific, narrow, commonly-
  abused subset of single-character Latin-lookalike substitutions from Cyrillic and Greek -- see
  that function's own comment for the exact character table and, explicitly, everything it does
  **not** cover (the full Unicode confusables skeleton transform, other scripts, ligatures,
  combining marks). A sufficiently exotic homoglyph outside that table still evades scoring.
- **Same-origin iframe content does NOT participate in classification by default.** `snapshot`/
  `read` only instrument the top frame (`background.js`: "Default: top frame (frameId 0) only")
  unless the caller explicitly passes `args.all_frames=true` or targets a frame-qualified ref.
  This is true regardless of whether the iframe is same-origin or cross-origin -- an action
  inside ANY iframe is invisible to the label-hint cache (`policy.py`'s `note_snapshot`/
  `_resolve_ref_hint`, keyed only by `(device_id, tab_id)`, never by frame) unless that opt-in was
  used for that snapshot. A page that places its highest-risk control inside an iframe evades the
  classifier's default posture entirely -- this is a real, currently-unclosed gap, disclosed here
  rather than fixed, because closing it (instrumenting every frame by default) has a real
  performance cost the design has not yet decided to pay by default; `args.all_frames=true`
  remains available to any caller who needs it for a specific tab.
- **`aria-labelledby` extraction is now implemented (was previously a real gap)**: an icon-only
  button whose accessible name comes from `aria-labelledby` (a reference to another element's
  text, not a direct child text node or `aria-label` attribute) previously extracted as empty --
  a completely ordinary way to build an accessible icon button, not an exotic evasion, and it
  silently starved the classifier of the label signal entirely. `injected.js`'s `nameOf()` (mirrored
  in the tested `extension/accessible_name.mjs`) now resolves it. This still implements only a
  load-bearing SUBSET of the W3C accessible-name computation -- see that module's docstring for
  exactly what's covered (`aria-labelledby` > `aria-label` > `alt` > text content) and what is
  deliberately not (native `<label for>`, `placeholder`/`title` attributes, embedded-control
  recursion, presentational-children pruning).
- **CSS-generated content (`::before`/`::after` with a `content` property) is not read at all.**
  `textContent` never includes generated content; a label that is entirely CSS-generated text
  extracts as empty and is invisible to every text-based signal in `classify.py`. Not addressed
  in this pass.

**None of the above makes the classifier a security boundary.** It raises the bar against
non-adversarial pages and the cheapest, most commonly-observed evasions; it does not, and is not
designed to, resist a page built specifically to defeat it. The only page-immune PREVENTION this
design has is caller-declared session write scope (`scope.py`) -- see the next section.

### Session scope is the only page-immune protection -- and its default is fully permissive, now visibly so

`scope.py`'s `SessionScope.permits_write` is the one check in this system that an adversarial
page cannot touch at all, because it is declared by the caller through a channel page content
never enters (`establish_session`, before any page content has been read). It is **opt-in**:
a `command` call that omits `session_id` runs under the pre-existing, fully-permissive default
every call site that predates `scope.py` already had (`docs/PROTOCOL.md`, "Migration"). This is
a deliberate choice, not an oversight -- the maintainer's own stated stance is broad access by
default (`docs/POLICY.md` section 1) -- but it was previously **silent**: nothing in the response
told a caller it had just run with no page-immune write restriction at all. Every
state-changing command result now carries a `scope_warning` field when this is the case (see
`hub.py`'s `SCOPE_UNSCOPED_WARNING`, `docs/PROTOCOL.md`'s "Sessions" section) -- the permissive
default is unchanged, but it is no longer invisible.

### The kill switch is now reachable by an operator, not library-callers only

`docs/POLICY.md` section 5 and this project's README have always described a hub-level kill
switch as an available control. Until this pass, it was reachable only by an embedding
application calling `Hub.engage_kill_switch()`/`disengage_kill_switch()` directly in-process --
no command in the shipped CLI, and no message in the wire protocol, reached it. `amplifier-browser-bridge
kill-switch engage|disengage|status` (and the matching `kill_switch_engage`/
`kill_switch_disengage`/`kill_switch_status` wire messages, `hub.py`) close that gap: same
`/agent` route, same token check, as every other command.

### The per-device token is a second, narrower boundary on top of tailnet identity

Tailnet identity is per-*device*, not per-*application* -- any other process or extension
running on an authorized device shares that device's tailnet identity. The per-device shared
token (`docs/PROTOCOL.md`, "Authentication") exists specifically to narrow that gap -- see
"Where the load-bearing boundary actually is" above for the full, current accounting of what
this token does and does not protect against by itself.

### Prompt injection from page content is an assumed, not a hypothetical, threat

The agent reads real page content (`snapshot`, `read`) and that content flows into whatever
model is driving the session. A malicious or compromised page can attempt to inject instructions
into the agent's context. This system's mitigation is structural, not linguistic: capability and
target binding are enforced by the hub -- specifically `PolicyEngine.evaluate` in
`policy.py`, called from `Hub.send_command` as the single choke point before any command reaches
a device or a queue (see `docs/POLICY.md` section 2, "Capability binding"). A prompt-injected
model can *want* a different target; it cannot *address* one that policy has not permitted, and
the policy engine's own record of what a tab actually is comes only from data the browser itself
reported, never from anything an agent's request asserts. No prompt-level instruction is treated
as a security control anywhere in this codebase.

### What the denylist does and does not cover

The consent model is denylist-shaped by design (`docs/POLICY.md`): broad read access by default,
a short hand-maintained list of sensitive host categories (financial, healthcare, identity
providers, password managers) that are made invisible to the agent, and confirmation gates on a
fixed set of irreversible actions (purchase, send, delete, OAuth grant, file upload, account
creation, permission change). Read `docs/POLICY.md` section 2 ("What the denylist does NOT
catch") and section 3 ("Other honest limits") before relying on this for anything beyond the
threat model it was designed for. In particular: the denylist can only judge a tab whose host
the hub has already observed; it has no path-level granularity; and most click/type-based
confirmation gates depend on the classifier's own honest limits above. This is documented as an
incomplete starting point, not a certified security boundary -- treat it that way when
evaluating this project for use with regulated data.

### Where the audit trail lives

Every dispatched command, every policy decision (denial, gate, confirmation, kill-switch event),
and every result is written to a JSONL audit log (see `audit.py` and `docs/POLICY.md` section 6).
This is the compensating control for a system that is broad-access by default: nothing the agent
does is invisible after the fact. If you find a code path that bypasses the audit log for any
action that reaches a real browser, treat that as a security finding, not a logging bug.

### CDP escalation, and the banner that is its only user-visible disclosure

Where this project attaches `chrome.debugger` (CDP) to a tab, that grants trusted input dispatch
and full page instrumentation for the duration of the attachment. Escalation is per-tab and on
demand -- only a `trusted` or `capture_hidden` arg triggers it (`cdp.py`'s `requires_cdp`,
`hub.py`'s `_ensure_cdp_attached`) -- and the hub soft-detaches after 20s idle so the browser's
warning clears rather than becoming permanent scenery. Any code path that attaches CDP silently,
fails to detach on idle, or fails to surface the banner state honestly to the user is a security
concern worth reporting.

**The browser's "started debugging this browser" banner is the only signal a user gets that this
escalation happened, and it has three properties worth stating plainly.** All three are read out
of current Chromium source; each file is named in
[docs/DEBUGGER_BANNER.md](docs/DEBUGGER_BANNER.md), which also lists everything about it that is
*not* verified:

- **It is browser-wide, not tab-scoped.** One banner per extension, shown on every tab in every
  window of the profile (`GlobalConfirmInfoBar`) -- so it is a disclosure that the extension is
  debugging *this browser*, never an indication of *which tab* is being driven. It is not a
  targeting signal and should not be read as one.
- **It is bounded, not session-long.** Chromium removes it 5 seconds after the last detach, so
  with this project's 20s soft-detach it clears roughly 25s after the agent stops. The
  corollary: **an idle-detach that fails leaves both the capability and its warning up
  indefinitely**, which is precisely why a failure to soft-detach is listed above as reportable.
- **Enterprise-policy installation suppresses it entirely.** Chromium exempts extensions whose
  install location is a policy location (`Manifest::IsPolicyLocation`, `debugger_api.cc`) from
  the warning. A copy of this extension deployed via `ExtensionInstallForcelist` (see
  [docs/DESKTOP_DISTRIBUTION.md](docs/DESKTOP_DISTRIBUTION.md)) can therefore use CDP on a
  user's real browser **with no visible indication at all**. That is Chromium's behavior, not
  this project's choice, but anyone evaluating the policy-install path should treat it as a
  disclosure control they are giving up, not one they still have.

This project does not use `--silent-debugger-extension-api` and does not ask users to. Any
future change that suppresses the banner by default -- by that switch, by policy install
presented as the recommended path, or by any other means -- should be treated as a security
change requiring disclosure, not a UX improvement.
