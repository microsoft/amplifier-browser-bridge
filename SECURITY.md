<!-- BEGIN MICROSOFT SECURITY.MD V0.0.9 BLOCK -->

## Security

Microsoft takes the security of our software products and services seriously, which includes
all source code repositories managed through our GitHub organizations, which include
[Microsoft](https://github.com/microsoft), [Azure](https://github.com/Azure),
[DotNet](https://github.com/dotnet), [AspNet](https://github.com/aspnet), and
[Xamarin](https://github.com/xamarin).

If you believe you have found a security vulnerability in any Microsoft-owned repository
that meets [Microsoft's definition of a security vulnerability](https://aka.ms/security.md/definition),
please report it to us as described below.

## Reporting Security Issues

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, please report them to the Microsoft Security Response Center (MSRC) at
[https://msrc.microsoft.com/create-report](https://aka.ms/security.md/msrc/create-report).

If you prefer to submit without logging in, send email to
[secure@microsoft.com](mailto:secure@microsoft.com). If possible, encrypt your message with our
PGP key; please download it from the
[Microsoft Security Response Center PGP Key page](https://aka.ms/security.md/msrc/pgp).

You should receive a response within 24 hours. If for some reason you do not, please follow up
via email to ensure we received your original message. Additional information can be found at
[microsoft.com/msrc](https://aka.ms/security.md/msrc).

Please include the requested information listed below (as much as you can provide) to help us
better understand the nature and scope of the possible issue:

  * Type of issue (e.g. buffer overflow, SQL injection, cross-site scripting, etc.)
  * Full paths of source file(s) related to the manifestation of the issue
  * The location of the affected source code (tag/branch/commit or direct URL)
  * Any special configuration required to reproduce the issue
  * Step-by-step instructions to reproduce the issue
  * Proof-of-concept or exploit code (if possible)
  * Impact of the issue, including how an attacker might exploit the issue

This information will help us triage your report more quickly.

If you are reporting for a bug bounty, more complete reports can contribute to a higher bounty
award. Please visit our [Microsoft Bug Bounty Program](https://aka.ms/security.md/msrc/bounty)
page for more details about our active programs.

## Preferred Languages

We prefer all communications to be in English.

## Policy

Microsoft follows the principle of [Coordinated Vulnerability Disclosure](https://aka.ms/security.md/cvd).

<!-- END MICROSOFT SECURITY.MD BLOCK -->

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

### CDP escalation (when present)

Where this project attaches `chrome.debugger` (CDP) to a tab, that grants trusted input
dispatch and full page instrumentation for the duration of the attachment, and Edge will show an
unsuppressable "being debugged" banner while attached. Any code path that attaches CDP silently,
fails to detach on idle, or fails to surface the banner state honestly to the user is a security
concern worth reporting.
