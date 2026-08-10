# Permission justifications

This document exists for three readers, in order of how likely they are to
actually read it: a security reviewer deciding whether to trust this
extension, an IT administrator deciding whether to approve a force-install
via `ExtensionInstallForcelist`, and a colleague deciding whether to install
it on their own machine. All three deserve the same answer, and the same
honesty about where that answer is weaker than it sounds.

**This is not a store-listing "why we need this" form filled out to pass
review.** The sibling projects this release kit is modeled on
(`teams-transcript-md`, `loop-page-md`) could write clean, confident
justifications because their permission requests are genuinely narrow --
`activeTab`, no host permissions, a handful of well-scoped APIs. This project
cannot make that argument. Its entire purpose requires broad capability, and
the honest version of this document says so rather than dressing a broad
grant in narrow-sounding language.

Three permissions carry the most risk. Each gets its own section below,
including -- per this project's own stated commitment to honest
disclosure -- an explicit statement of where the defense is weaker than a
security reviewer would want, rather than a uniformly confident answer.

---

## 1. `<all_urls>` (host permission)

### What it grants

Unrestricted `chrome.scripting.executeScript` injection and DOM access on
every site the browser can navigate to, with no per-site prompt and no
per-site scoping. This is one of the two broadest permissions Chrome's
extension platform offers (the other being `chrome.debugger`, below).

### The defense

This is not a narrow feature that happens to need broad access; broad access
**is** the feature. The project's stated design goal, in the maintainer's own
words (`docs/POLICY.md` section 1): *"I generally want it to be able to
access what I access so that it can leverage/see what I've seen."* An agent
that can only act on a hand-picked list of domains cannot do what this
project sets out to do -- be a second operator sharing an arbitrary,
unpredictable browsing session, not a single-site integration. Scoping this
down to specific host patterns would not make the extension safer in any way
that matters to its actual threat model; it would make it a different,
narrower product that no longer does the thing it was built to do.

Given that the capability is genuinely required, the actual security
question is not "can this be narrower" but "what limits what an agent
holding this capability can actually do," and the answer lives entirely
outside the manifest, in the hub's policy engine:

- A denylist makes financial, healthcare, identity-provider, and
  password-manager hosts **invisible** to the agent, not merely denied when
  addressed (`docs/POLICY.md` section 2).
- A fixed set of irreversible/world-visible actions (purchase, send, delete,
  OAuth grant, file upload, account creation, permission change) require an
  explicit confirmation before dispatch (`docs/POLICY.md` section 3).
- Every dispatched command and every policy decision is written to an audit
  log (`docs/POLICY.md` section 6) -- broad access is compensated by "the
  human can review everything after the fact," not by narrowing the grant.
- The capability itself is bound to hub-observed state, not caller-asserted
  state (`docs/POLICY.md` section 2, "Capability binding") -- a
  prompt-injected agent can *want* a different target; it cannot *address*
  one policy has not permitted.

### Where this defense is honestly weaker than it sounds

- **None of the above lives in the manifest or the browser's own permission
  model.** `<all_urls>` itself grants everything described above with no
  further gate; the compensating controls are a Python process the extension
  trusts completely. A reviewer evaluating the extension in isolation --
  which is exactly what a store reviewer or an `ExtensionInstallForcelist`
  policy review does -- sees an unconditional, unscoped grant with no
  in-manifest signal of the restraint the hub imposes at runtime.
- **The denylist is short and hand-maintained** (`docs/POLICY.md` section 2:
  "~5 categories... explicitly incomplete... not a security boundary suitable
  for regulated data without review"). It is a starting point, not a
  certification.
- **This permission is requested unconditionally at install time**, not via
  Chrome's `optional_host_permissions` + `chrome.permissions.request()`
  pattern, which would let a user grant it narrower or later. See
  [D1](../.amplifier/goals/honest-disclosure-and-release-kit.md) for why a
  reduced-permission variant is a real, undecided option rather than
  something this document can wave away.

**Verdict: honestly justifiable as a deliberate, disclosed design choice --
but only as "broad access is the feature, and here is what compensates for
it," never as "this permission is narrow" or "this permission is safe in
isolation."** A reviewer who needs the manifest itself to prove restraint
will not find that proof here.

---

## 2. `chrome.debugger` (CDP)

### What it grants

Full Chrome DevTools Protocol access to any attached tab: arbitrary
JavaScript evaluation, network interception, trusted (`isTrusted: true`)
synthetic input dispatch, and screenshot capture regardless of tab focus.
This is the single most powerful permission the extension platform exposes --
broader in raw capability than `<all_urls>` itself, because CDP operates
below the page's own security model rather than within it. Chrome displays a
mandatory, unsuppressable "being debugged" banner while attached specifically
because this permission is considered exceptional.

### What this project actually uses it for

Two narrow, named capabilities (`cdp.py`'s `requires_cdp`, design doc §7):

1. **Trusted input dispatch** (`args.trusted=true` on `click`/`type`/`key`) --
   some pages check `event.isTrusted` to reject synthetic automation;
   `injected.js`'s ordinary `dispatchEvent` calls cannot produce
   `isTrusted: true` events, and CDP's `Input.dispatchMouseEvent`/
   `dispatchKeyEvent` can.
2. **Hidden-tab screenshot capture** (`args.capture_hidden=true` on
   `screenshot`) -- `chrome.tabs.captureVisibleTab` only works on the tab
   that is already active in a focused window; `Page.captureScreenshot` via
   CDP can capture a tab regardless of focus, which matters specifically
   because this project's whole premise is acting on a device the human is
   not looking at.

Both are opt-in per command (`cdp.requires_cdp`), attach lazily on first use,
and soft-detach after 20 seconds of no CDP-requiring activity specifically so
the debugging banner does not linger (`cdp.py`, `DEFAULT_SOFT_DETACH_IDLE_SECONDS`) --
this is a deliberate, engineered attempt to minimize how much of this
permission's footprint is visible or active at any given moment.

### Where this defense is honestly weaker than it sounds

This is the permission where an honest defense runs out fastest, and this
section says so plainly rather than manufacturing confidence:

- **The permission is requested unconditionally in the manifest**, not lazily
  via `chrome.permissions.request()` at the moment a command actually needs
  it. Two named, narrow use cases do not need a permission that is present
  from the moment the extension installs -- they need it present at the
  moment either use case first fires. This project does not do that today,
  and that gap is real, not cosmetic: a reviewer's risk assessment has to
  price in "this extension holds full CDP capability at all times," not "this
  extension can ask for CDP capability twice a year."
- **The actual capability granted is far broader than the two use cases
  drawing on it.** `chrome.debugger` does not have a narrower "just trusted
  input and just screenshots" mode -- granting it grants arbitrary CDP
  access, including domains this project does not use today (full `Network.*`
  interception, `Runtime.evaluate` outside the page's own script contexts,
  `Page.navigate`, and more). The manifest cannot express "only these two CDP
  methods"; Chrome's permission model is all-or-nothing at the API-namespace
  level. Any correctness argument about "we only use it for X and Y" is a
  claim about this version of this codebase, not a claim the platform
  enforces.
- **A live experiment in this repo's own history found CDP could reach and
  drive the extension's own options page** -- attach to a
  `chrome-extension://` target, evaluate JS in it, click its buttons, dispatch
  input (`docs/designs/approval-channel-options.md`, cancellation note).
  That finding was about a proposed in-extension approval UI, but it is
  direct evidence that this permission's reach is broader than "the two
  documented use cases," and that intuitions about what CDP can and cannot
  touch should be checked, not assumed.
- **This is the permission a reduced-permission build variant would drop
  first** ([D1](../.amplifier/goals/honest-disclosure-and-release-kit.md)) --
  not because the two use cases it serves are illegitimate, but because the
  gap between "what we use it for" and "what it grants" is the widest of any
  permission this extension requests.

**Verdict: the use cases are legitimate and narrowly described, but the
permission grant itself is disproportionate to them, requested more broadly
(unconditional, at install time) than it needs to be, and not something this
document can respond to with a clean "yes, this is justified" the way it can
for the other two permissions here. Record this as the honest finding, not
as a solved problem: a security reviewer or IT admin should treat
`chrome.debugger` as the permission most likely to justify a "no" or a
"reduced-permission variant only," and that reaction would be a reasonable
one, not a misunderstanding to be argued out of.**

---

## 3. The persistent WebSocket connection to the agent host (the "hub")

### What it does

The extension's background service worker maintains, or repeatedly
re-establishes, an outbound WebSocket connection to a hub address configured
on its options page. Over that connection, the hub can send it commands at
any time the connection is open, with no per-command user-facing prompt.

### The defense

The load-bearing distinction that makes this defensible is **who the hub
is**: it is not a server operated by this project's maintainers, and no
build of this extension hardcodes an address belonging to them. The hub is a
program the user installs and runs themselves (`amplifier-browser-bridge
hub`), at an address the user chooses and enters into the extension's
options page. This is structurally different from the "phones home to the
vendor" pattern that legitimately worries reviewers about persistent
outbound connections in other extensions:

- There is no vendor-operated endpoint this extension is capable of
  reaching on its own. Change the options page's Hub URL and the extension's
  entire network behavior follows -- there is no fallback, second channel,
  or hardcoded default that survives that change.
- The connection is analogous to a VPN client's persistent tunnel to a
  self-hosted concentrator, or a home-automation bridge's persistent
  connection to a self-hosted hub -- both accepted patterns for
  "software that needs a standing connection to infrastructure you run."
- The extension dials **out**; it never listens on an inbound port itself
  (design doc §3.1). This is what lets it work behind NAT and survive
  network roaming without port-forwarding, and it also means there is no
  additional inbound attack surface on the browser's device beyond the
  outbound socket itself.
- Every command received over this connection still passes through the same
  policy choke point as any other (`PolicyEngine.evaluate`,
  `docs/POLICY.md` section 2) -- holding the connection open does not bypass
  the denylist, confirmation gates, or audit log.

### Where this defense is honestly weaker than it sounds

- **"You control the hub" is a claim about the common case, not a structural
  guarantee.** Nothing in the extension verifies that the hub address
  configured on the options page is one the user actually operates, versus
  one someone else configured for them, talked them into pasting in, or that
  survived from a shared/managed device image. The extension trusts whatever
  hub is at the configured address completely, and has no way to
  distinguish "my own hub" from "a hub someone else controls" once a URL and
  token are entered.
- **Whatever the hub operator's security practices are, this extension
  inherits them entirely.** If the hub runs on a poorly-secured shared
  machine, binds wider than intended (`THREAT_MODEL.md`'s "Where the load-bearing
  boundary actually is"), or leaks its token, this extension has no
  independent defense against that -- it was designed to trust its
  configured hub, not to verify it.
- **A silent, wide network exposure is possible without any code change
  here at all** -- see `THREAT_MODEL.md`'s accounting of the hub's own bind
  address and Tailscale's default allow-all ACL policy. This extension's
  behavior (dial out, obey commands) is identical whether the hub is safely
  scoped or accidentally exposed to an entire home network; it has no way to
  tell the difference.

**Verdict: honestly justifiable as a deliberate architectural choice -- the
persistent connection reaches infrastructure the user runs, not a vendor
endpoint -- but the defense is conditional on the user's own hub deployment
being sound, not something this extension can verify or enforce on its own.
A reviewer should read this alongside `THREAT_MODEL.md`'s "Where the load-bearing
boundary actually is" section before accepting "you control the hub" as a
complete answer.**

---

## 4. `clipboardRead`

### What it grants

Lets the extension call `navigator.clipboard.readText()` without a per-call
permission prompt (Chrome's extension permission model grants this outright,
unlike a normal web page, which needs a fresh user gesture and/or a focused
document each time -- see `docs/designs/browser-bridge.md`'s pairing section
for what was actually verified in practice). In principle this lets the
extension read anything on the system clipboard, at any time its options page
is open, not just a pairing code.

### The defense

Added specifically for zero-copy-paste pairing (options.js's
`runPairingDiscovery`): if no already-open `/setup` tab carries a pairing code
(the preferred, origin-checked mechanism -- see `pair_discovery.mjs`), the
options page falls back to the clipboard as the next-best "the user shouldn't
have to type or paste anything" rung, before finally asking for a manual
paste. What is read is validated with the exact same parser the manual-entry
field uses (`parsePairingCode`) -- content that doesn't look like this
project's own `ticket@host:port` shape is discarded immediately and never
displayed, logged, or transmitted anywhere. The clipboard is never written
here at all (only read); nothing this extension does puts anything new on it.

### Where this defense is honestly weaker than it sounds

- **The permission itself is not scoped to "only during pairing."** Chrome's
  manifest model is all-or-nothing, same limitation named for `chrome.debugger`
  above -- the extension technically *could* read the clipboard at other
  times; it simply doesn't, in this version of this codebase.
- **A pairing code is short-lived and narrow in what it can do on its own**
  (see `pairing.py`'s module docstring), which bounds the actual harm of a
  clipboard read finding something it shouldn't -- but that is a property of
  what gets validated and acted on, not a limit the permission grant itself
  enforces.

**Verdict: honestly justifiable -- the capability is used for exactly one
narrow purpose, validated before use, and only ever read, but the permission
Chrome actually grants is broader than that one purpose, same pattern as
every other permission in this document.**

---

## Summary for a time-pressed reviewer

| Permission | Can be honestly defended as-is? | Caveat |
|---|---|---|
| `<all_urls>` | Yes, as a deliberate design choice | Compensating controls live entirely outside the manifest, in a trusted hub process; the denylist is short and hand-maintained |
| `chrome.debugger` | **Only partially** | Two narrow use cases; permission granted is far broader than either, requested unconditionally rather than lazily -- treat a "no" here as reasonable, not a misunderstanding |
| Persistent hub WebSocket | Yes, conditionally | Depends entirely on the user's own hub deployment being sound; the extension cannot verify this |
| `clipboardRead` | Yes, as a narrow, validated-before-use fallback | The grant itself is broader than the one purpose it's used for -- same limitation as every Chrome permission in this document |

If you are approving a force-install policy or a store submission and need
exactly one takeaway: **`chrome.debugger` is the permission this document
could not fully defend.** See [D1](../.amplifier/goals/honest-disclosure-and-release-kit.md)
for the specification of a reduced-permission build variant that drops it.
