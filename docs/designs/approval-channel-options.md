# Options brief: the human-approval channel for `redeem: "out_of_band"`

**Status:** **CANCELLED (2026-07-26, same day).** Phase 6 (a dedicated human-approval channel) will
not be built. See §0 below for the decision, the evidence that forced it, and what would reopen it.
The remainder of this document is preserved as-written — the candidate analysis, the reachability
measurements, and the residual-risk accounting are still the most complete record of why this is
hard, and a future reader considering the same idea should read them before re-deriving this
ground. Do not delete this document; it is the reason the next person doesn't have to redo this
work.
**Date:** 2026-07-26
**Extends:** `docs/designs/confirmation-gate.md` §3 Candidate E, §9 item 10, §15 step 6
**Decides:** through what channel a human approves a gated action, such that the driving model
cannot approve on their behalf
**Does not decide:** what fires a gate (that is `classify.py`/`effects.py`, already built), or
what a session may do at all (that is `scope.py`, already built)

This document is a candidate set, not a design. Every candidate is stated at its strongest,
including the ones I argue against, because a council reviewing a rigged comparison produces a
worthless verdict.

---

## 0. Cancellation (2026-07-26, same day) — read this first

**Phase 6 is cancelled. There will be no human-approval channel.** Two things forced this,
independently, either one would have been sufficient on its own:

**1. The recommended channel does not survive contact with the tool it needs to defend against.**
§4.D's own reasoning rested on a measured claim: `chrome-extension://` pages are outside
`<all_urls>`, so an agent's `scripting.executeScript`/`click`/`type` reach cannot touch the
extension's own approval UI (§2.2, §4.D, `SCRATCH.md` R6). That claim is true, and it is also not
the whole story — it only rules out one of the two ways an agent can reach a page. A live
experiment against a real Chromium with this extension loaded, targeting the extension's own
`options.html`, produced this transcript, verbatim:

```
target: chrome-extension://<id>/options.html
CDP attach                 → ATTACHED
Runtime.evaluate           → "Amplifier Browser Bridge -- Setup | chrome-extension://..."
synthetic click landed     → True
Input.dispatchMouseEvent   → ACCEPTED
```

An agent holding `chrome.debugger` can attach to the extension's own approval UI via CDP, evaluate
JavaScript inside it, and dispatch trusted input at it (`Input.dispatchMouseEvent`, which produces
real `isTrusted: true` events — the same mechanism `docs/DECISION_GUIDE.md`'s `trusted`/
`capture_hidden` modifiers use legitimately). `<all_urls>` genuinely does constrain
`chrome.scripting.executeScript` — but CDP is a separate door, and this system already holds the
key to it (`trusted=true`/`capture_hidden=true` require exactly the `debugger` capability that
makes this attack possible). **The extension's own UI is not a boundary on desktop.** Every
candidate in §3/§4 that routes approval through a page the extension controls (D, and E as a
layered addition) inherits this hole.

One asymmetry is worth recording rather than discarding: `chrome.debugger` is genuinely absent on
Edge Android (measured, `SCRATCH.md` R7 — "genuinely absent," confirmed against Microsoft's own,
occasionally-wrong API matrix). So the extension-UI boundary *would* hold on the one device this
project measured as hardest to reach. That is not enough to save the desktop case, where the
maintainer actually works, but it is precisely the shape of thing that could reopen this decision
— see the reopening conditions below.

**2. The simpler answer was already built, and reaching past it for a channel repeats a mistake
this same design doc's own review panel already corrected once.** From the review that closed this
decision out: *"The answer to 'we can't reach a sleeping device' is not 'add a dependency we
already said no to.' A session whose only human is an unreachable phone doesn't get out-of-band
capability. You built the fix for this in phase 5 and are about to un-build it in phase 6."*
`scope.py`'s caller-declared, narrow-only, seal-on-first-read write scope (Candidate C, already
shipped) is the one mechanism in this whole design that is genuinely page-immune (§2's lemma) and
requires no new channel, no new token, no new device-reachability story. The correct response to
"the human might be unreachable" is not a bigger boundary around unreachability — it is a smaller
session. **An unreachable human means a narrower session, not new infrastructure.**

### What actually changes

`redeem: "out_of_band"` is renamed to `redeem: "unredeemable"` — the honest name for what this
value has done since the review-panel FAIL was closed (`docs/designs/confirmation-gate.md` §16.1):
since no channel exists to redeem it, and per this cancellation none ever will, a confirmation
declared this way cannot be redeemed at all, by any route, ever. This is not a temporary gap
pending Phase 6; it is the permanent, correct behavior. It remains genuinely useful: declaring it
is how an operator says "this session is unattended; a gate firing here means stop, not wait" —
distinct from `redeem: "agent"`'s self-attestation, which is a real (if narrow) defense against an
*accidental* click and no defense at all against an *injected* one. See `docs/PROTOCOL.md`,
`docs/DECISION_GUIDE.md`, and `README.md` for where an operator now learns this plainly, and
`docs/designs/confirmation-gate.md` §16 for the code-level consequences.

### What would reopen this decision

The maintainer's own words on the residual gap: *"I can live with `redeem: agent` for now."* Two
things, either one, would be grounds to revisit a real channel rather than accepting that residual
risk indefinitely:

1. **A channel whose security property is *measured*, not assumed.** §4.D's failure mode above was
   exactly this: the boundary was argued from a true-but-partial measurement (`<all_urls>`
   exclusion) rather than tested against the actual adversary capability (CDP) the system grants
   elsewhere. A future proposal needs to run the equivalent of this section's experiment against
   *every* capability the agent holds, not just the one the design doc's own lemma already covers.
2. **A per-session way to deny `chrome.debugger` entirely**, so an agent operating under that
   denial genuinely cannot reach CDP, and the extension-UI boundary (§4.D) becomes real on desktop
   the same way it already is on Android. This is a session-scope feature (`scope.py`-shaped, not a
   new channel) — restricting *capabilities* a session holds, symmetric with `scope.py`'s existing
   restriction of *origins* a session may write to. Nobody has designed this yet; it is a real
   candidate for a future `scope.py` extension, not a rejected idea.

Absent one of those two, do not re-propose a human-approval channel on the same shape as §3/§4
below — it will fail the same way, for the same reason.

---

## 1. What is actually being decided

Phases 1–5 shipped a gate that can fire, mint a single-use token, and declare — per session —
that the token is `redeem: "out_of_band"`. Nothing redeems it that way. The only redemption path
in the codebase is `type: "confirm"` on the hub's `/agent` WebSocket (`hub.py:594`,
`hub.py:629`), which is the driving agent's own route.

That is not an approval. It is an attestation: it forces the agent to make a second, separately
audited decision, which has real value against *accidental* action and none whatsoever against a
prompt-injected one. Prompt injection from page content is an assumed, not hypothetical, threat
in this project (`SECURITY.md` §"Prompt injection from page content is an assumed…").

So the question is narrow and sharp: **what channel carries a human's approval, and what makes
that channel unreachable by the model?**

### 1.1 Four prerequisites, true for every candidate

These are not part of the choice. They are load-bearing for all of it, and if a council picks a
channel without them, the channel is decoration.

| # | Prerequisite | Current state | Why it is load-bearing |
|---|---|---|---|
| **P1** | `PendingConfirmation` must record the token's `redeem` mode, and `_handle_agent_confirm` must refuse an `out_of_band` token on the `/agent` route. | **Missing.** `PendingConfirmation` (`policy.py:486`) has no `redeem` field; `redeem` is computed from `scope.redeem` into `PolicyDecision` and put on the wire (`hub.py:827`), then never consulted again. | Without it, `redeem: "out_of_band"` is advisory text in a JSON response. The agent redeems on `/agent` regardless. **Every candidate below is worthless until this is fixed.** |
| **P2** | The approver credential must be a *different secret* from `ABB_HUB_TOKEN`. | **Missing.** One `TokenStore` default token validates both the `/device` `hello` and every `/agent` request (`auth.py:38`). | If the approval endpoint accepts the hub token, the agent — which must hold the hub token to send commands at all — holds the approver credential too. |
| **P3** | The agent must not be able to drive the approval surface with the browser it already controls. | **Not considered anywhere.** See §2.2. | This is the attack specific to *this* system. It would not be a flaw in any other product. |
| **P4** | Pending approvals are in-memory (`policy.py:629`, `self._confirmations`) and die on hub restart. | True today; tolerable at the current 300s TTL, less tolerable at the longer TTL §5.1 argues for. | A human who taps Approve after a hub restart gets an unexplained failure. Either persist, or say so honestly in the docs. |

---

## 2. Threat model, sharpened

The design doc's provenance lemma (§2) sorts *page* signals. This decision needs the same
treatment applied to the *agent host*, which the doc has not done.

### 2.1 What the adversary holds

Assume the driving model is prompt-injected through page content. It has:

1. **The hub token.** It must, to issue any command.
2. **A shell on the agent host,** in the normal Amplifier configuration. This is the fact that
   reorders the candidate list. A model with `bash` on the hub host can run `abb approve <token>`,
   read `~/.config/amplifier-browser-bridge/tokens.json`, read the audit log, and `curl` any hub
   route.
3. **Control of the browser.** `navigate`, `tab_open`, `tab_close`, `click`, `type`, `snapshot`,
   `read`, `screenshot`, and `reload` (which restarts the extension's service worker) on every
   connected device.

Point 2 gives the real criterion, which is stronger and more useful than "out of band":

> **The approval must require a secret the agent's process does not hold, or a physical act it
> cannot perform, or both.** "Different protocol route" is not sufficient. "Different process on
> the same host" is not sufficient.

Two structurally different ways to satisfy it:

- **A different secret** — an approver credential that never exists on the agent host. Works with
  any transport. Cheap.
- **A different device** — the approval originates from a node the agent is not running on, and
  the hub verifies that cryptographically (Tailscale ACL / `whois` on the source address, or a
  per-device token). Strong, and composes with the first.

### 2.2 The self-drive attack (P3)

**The agent controls a browser. If the approval surface is a web page, the agent can browse to
it and click Approve.**

Concretely: an approval page served by the hub at `http://100.124.126.19:8900/approve` is an
ordinary `http://` origin. The extension holds `host_permissions: ["<all_urls>"]`. The agent can
`tab_open` that URL, `snapshot` it, and `click` the Approve button. Complete bypass, using only
the capabilities the system exists to provide.

Two facts bound this:

- **`chrome-extension://` pages are outside `<all_urls>`.** Measured on this project's own probe
  rig (`SCRATCH.md` R6, real Edge 150 macOS): the two `executeScript` failures were legitimate —
  the active tab was the extension's own `chrome-extension://` page, "which `<all_urls>` correctly
  does not cover." An approval UI hosted *inside the extension* is structurally unreachable by
  `click`/`type`/`read`. This is a measured property, not an argument.
- **Suppression is still available.** `tab_close` is a browser-level command that works on any
  tab, including an extension page. `reload` restarts the service worker. The agent cannot
  *forge* an approval on an extension page; it can *destroy the prompt*. Whether that matters
  depends entirely on the timeout policy (§5.1) — with timeout-as-deny it is a denial of
  approval, not a bypass.

Any candidate that puts the approval on an `http(s)://` origin needs an explicit mitigation:
the extension must refuse page-world commands and `tab_close` on the hub's own origin. That is
self-knowledge (the extension already stores its hub URL), not site knowledge, so it does not
violate `CONTRIBUTING.md`'s "the extension carries zero site knowledge" rule. But it is a guard
the whole candidate rests on, and failing silently is its natural failure mode.

### 2.3 Reachability, as measured (not assumed)

From this project's own instrumentation (`SCRATCH.md` R5, R7):

| Tier | Behavior | Evidence |
|---|---|---|
| **Desktop** | Persistent. 568 heartbeats over **142 min, zero gaps**, max interval 15.1s. | R5 |
| **Mobile + battery exemption** | Intermittent, self-healing. 5 blackouts of 44.5 / 43.4 / 73.5 / 132.7 / 129.0 s (mean 85s), reconnect <2s, close code 1001. | R7 Doze A/B round 2 |
| **Mobile, default settings** | Effectively unreachable. One 509s blackout, close code 1006. | R7 round 1 |

The MV3 service worker never died in either mobile round — only the *socket* did. The phone is
addressable during maintenance windows and not otherwise.

**The consequence that reorders the candidates: the tailnet gives you addressability, not
wakeability.** No self-hosted channel on the tailnet can wake a Dozing Android. Only a
platform push service (FCM/APNs) holds a socket the OS keeps alive. This is a physical
constraint from this project's own measurements, not an implementation detail — see §4.C.

---

## 3. Comparison

### 3.1 Security

| # | Candidate | Who authors the approval | What the model can do to it | Model-immune? |
|---|---|---|---|---|
| **A** | Prompt on the agent host (`abb approve`) | Whoever has the TTY | **Run `abb approve` itself** — it has a shell on that host. Also read the approver token from disk. | **No**, unless the agent is sandboxed off the host |
| **B** | Push to phone, third-party (FCM/APNs/ntfy/Pushover) | Human, tapping on the phone | Cannot see or dismiss it; cannot approve without the approver credential. Could *spoof* a push if it obtains the topic key (annoyance, not bypass). Can flood gates to induce approval fatigue. | **Yes**, given P2 |
| **C** | Push to phone, self-hosted on tailnet | Human, on a device they run | Same as B — but see §4.C: **this channel cannot wake a sleeping phone at all**, so on mobile the question is often moot | Yes, but frequently unreachable |
| **D** | Extension's own UI (badge + popup + `chrome-extension://` page) | Human, clicking inside an extension page — `isTrusted`, in a context `<all_urls>` does not cover | **Suppress**, not forge: `tab_close` the approval tab, `reload` the extension. Cannot script the page. | **Yes** for forgery; **no** for availability |
| **E** | Approval page served by the hub on the tailnet | Human, in a browser on any tailnet node | **Drive it** — `tab_open` + `click`, complete bypass, unless the hub's own origin is guarded (§2.2) | Only with the guard; guard fails silently |
| **F** | A2A handoff to the human's own assistant | Nominally the human — **actually a second model** | Persuade the second model. The approval prompt must quote page text, which is attacker-influenced. Self-attestation moved one hop. | **Only if** the second agent is a strict courier that cannot itself approve |
| **G** | Pre-authorization at session establishment (`scope.py`) | Human, while present, before walking away | Nothing — narrow-only, sealed on first page read. Already page-immune and model-immune. | **Yes** — but it is not a channel |
| **H** | Post-hoc: execute, attribute loudly, human reverses | Nobody approves; the human is *informed* | Cannot suppress a browser-asserted effect (design doc §2). Can act before the human reads. | N/A — different primitive |

### 3.2 Operations

| # | Desktop latency | Mobile (exempt) | Mobile (default) | Human unreachable → | Setup burden on a new user | New deps | Cost when it fires wrongly |
|---|---|---|---|---|---|---|---|
| **A** | Instant, if the human is at the box | n/a | n/a | Gate hangs until TTL | **Zero** — the CLI exists | None | A blocked task; the human is right there to unblock |
| **B** | Seconds | Seconds (push wakes the device) | **Seconds — the only candidate that works here** | Buzz goes unanswered; TTL expires | Account, API key or self-host, mobile app install, per-device registration | **A service outside the tailnet** | **Worst in set.** A 2am buzz. Repeated → reflexive tapping → the channel silently degrades to self-attestation-by-habit |
| **C** | Instant (142min/0-gap socket) | ≤130s worst observed | **Unreachable** | Same as B | An approver client to run + a second token | An approver process | Moderate — a desktop toast |
| **D** | Instant | ≤130s worst observed (one maintenance window) | Seen when the human next opens the browser | Badge sits; TTL expires | **Zero** — the extension is already installed, connected, authenticated | **None** | **Best in set.** A number on a toolbar icon. No modal, no sound, no interruption |
| **E** | Instant | n/a (needs a browser session) | n/a | Link goes unclicked | `tailscale serve` config, or a bearer token in a URL | Serve config; optionally `tailscale cert` | Low — an unvisited link |
| **F** | Whatever the assistant's channel is | Inherits the assistant's push | Inherits the assistant's push | Depends on the assistant | **Highest** — a second agent, an identity model between agents | An entire second system | Depends; likely good (the human already reads that channel) |
| **G** | n/a — collected up front | n/a | n/a | Nothing to be unreachable for | One decision per session, by the *harness*, not the human | None (built) | A denied action with a specific `out_of_scope` error |
| **H** | n/a | n/a | n/a | The action already happened; the notice waits | None (effects already ship) | None | **Zero false-positive cost** — nothing is blocked. The cost is on the false-*negative* side |

---

## 4. Per-candidate detail

### A — Prompt on the agent host (`abb approve <token>`)

**Shape.** A new CLI verb and a hub route the `/agent` protocol does not expose. `abb pending`
lists open requests; `abb approve <token>` redeems one. Authenticated by an approver token
distinct from `ABB_HUB_TOKEN` (P2).

**The strongest case for it.** It is the only candidate that works with zero devices connected
and zero additional software. For the maintainer sitting at the Linux box — which is the actual
posture during development, and during any attended session — it is instant, free, dependency-
free, and obviously correct. It is also the fallback every other candidate needs when its own
transport is down. Shipping it is not optional; the only question is what it is *claimed to be*.

**Why it fails the founding premise.** The human is somewhere else. That is the premise of the
whole project. Worse, when the agent runs on that same host with a shell — the normal Amplifier
configuration — the boundary is illusory: the model runs `abb approve` itself. Reading the
approver token from `/dev/tty` rather than stdin raises the bar slightly (a non-interactive
subprocess has a harder time), but a subprocess generally inherits the controlling terminal, and
the token file is readable either way.

**Honest label.** `abb approve` is out-of-band with respect to the *protocol* and not with
respect to the *host*. It is a real boundary only when the agent cannot execute commands on the
hub host — i.e. a sandboxed or containerized agent. Say that in the docs, in those words. The
alternative — shipping it as "human approval" — is precisely the fabricated attestation this
whole design exists to eliminate.

**Verdict:** ship, correctly labeled, as the degenerate case. Not the answer to the question.

### B — Push to the phone, third-party (FCM / APNs / ntfy / Pushover)

**Shape.** On an `out_of_band` gate, the hub publishes a notification through a push service. The
notification carries a summary and a deep link. The human taps; the approval is redeemed —
either through the push service's action buttons, or (better) by opening a redemption surface
back over the tailnet, so the *approval* never traverses the third party even though the
*notification* does.

**The strongest case for it, stated properly.** This is the only candidate that reaches a human
whose phone is asleep — which, given the measured Doze data, is most of "the human is somewhere
else." A platform push service works precisely because the OS holds a persistent socket that the
app does not have to; that is the thing Tailscale cannot give you at any price.

And the project's anti-relay stance is narrower than it first appears. The rejected pattern
(`browser-relay`'s Cloudflare Worker + bearer Device ID, `SCRATCH.md`: *"anyone with it can
control this browser"*) was a **control plane** relay — a third party that can drive the browser.
A push relay carries a *notification*, and if the approval is redeemed back over the tailnet, the
third party never holds a capability at all. It sees metadata and whatever is in the notification
body. That is a materially different exposure, and a council should not be allowed to dismiss B
by citing a precedent that does not actually cover it.

**Where it genuinely fails.**
- The notification body is the context (§5.2), and it is length-limited. A 200-character body
  cannot carry provenance-marked signal detail. Tap-through mitigates, but tap-through on a
  sleeping phone in a dark room is where reflexive approval lives.
- **Approval fatigue is the failure mode, and it is silent.** A channel that buzzes converts,
  over weeks, into a channel the human taps without reading. At that point it is
  self-attestation with extra steps, and nothing in the system can detect the transition.
- Setup burden is the highest of any transport candidate: an account or a self-hosted server, an
  API key, a mobile app, per-device registration. This project's onboarding already includes a
  battery-optimization exemption walkthrough; adding a push provisioning flow is not free.
- It adds a dependency outside the tailnet, which is a position this project has taken publicly.

**Verdict:** the right escape hatch, behind an operator-configured flag, off by default. Not the
default channel — but the honest reason is ergonomic and dependency-driven, not "we rejected
relays."

### C — Push to the phone, self-hosted on the tailnet

**Shape.** A `/approve` WebSocket route on the hub. The human's device runs a small approver
client (CLI daemon, menu-bar app, or PWA) holding an approver token, and gets pushed the request.

**The strongest case for it.** On desktop this is excellent and nearly free: the measured
connection profile is 142 minutes with zero gaps and a 15.1s max interval. A menu-bar item that
lights up instantly, self-hosted, no third party, cryptographic device identity from the tailnet.
For the maintainer's MacBook and Windows desktop, this is a genuinely good answer.

**Where it fails, decisively, and this is the finding that matters.** **"Push to the phone,
self-hosted on the tailnet" is not an available option.** A backgrounded browser or app on
Android has its network suspended by Doze; this project measured 44–133s dark windows *with* the
battery exemption applied, and a 509s window without it. A tailnet socket cannot be woken by the
hub — the hub is not what wakes an Android device; the platform push service is. So C on mobile
collapses into either (a) FCM/APNs, which is candidate B, or (b) "the phone finds out during the
next maintenance window," which is exactly candidate D's latency profile with an extra app to
install.

**Verdict:** a strictly worse D. It has the same reachability on every tier, plus an app to
install, plus a second token to provision. If a council wants to argue for C, the argument has to
be that a dedicated approver surface carries context better than a browser extension can — and
D's extension page is a full HTML document, so that argument is hard to make.

### D — The extension's own UI as the approval surface

**Shape.** On an `out_of_band` gate the hub pushes an `approval_request` over the already-open
`/device` socket to **every connected device** (see §5.5 for why broadcast). The extension:

1. Increments a badge on its toolbar icon (`chrome.action.setBadgeText` — **no permission
   required**; the `action` key is already in `manifest.json`).
2. Renders pending requests in a popup (`default_popup`, a free manifest addition) and, for the
   full detail view, an extension page at `chrome-extension://<id>/approve.html`.
3. On Approve, sends an `approval` message back up the `/device` socket. The hub matches it to
   the token and redeems.
4. Optionally raises an OS toast via `chrome.notifications` (costs a manifest permission; **must
   be behaviorally probed**, per this project's own rule — Microsoft's Android API matrix has
   been measured wrong twice in `SCRATCH.md` R7).

Pending state lives in the hub (source of truth) and is re-fetched on every device reconnect, so
it survives a closed tab, a `reload`, and a Doze blackout.

**Why it is model-immune for forgery.** The approval originates from a click inside a
`chrome-extension://` page. `<all_urls>` does not cover that origin — **measured on this
project's own rig** (`SCRATCH.md` R6). The agent has no `executeScript` reach there, so it
cannot synthesize the click. The approval message travels the `/device` route, which the agent
does not speak, from a device authenticated by its own device token.

**Why setup burden being zero is a first-order safety property, not a convenience.** The stated
failure mode of this whole subsystem is *"a channel that fires often enough to annoy will be
disabled, and a disabled gate protects nothing."* The same logic applies one step earlier: a
channel that requires provisioning will not be provisioned. The extension is already installed,
already connected, already authenticated, on every device the human uses. Zero new accounts, zero
new tokens, zero new processes, zero new dependencies. No other candidate is close.

**Behavior when it is wrong.** A number on a toolbar icon. No modal, no sound, no interruption,
no 2am buzz. This is the cheapest false-positive cost available anywhere in the candidate set,
and it is the direct answer to the "no approval nightmare" constraint.

**Reachability matches the measured tiers exactly**, because it *is* the measured channel:
desktop instant; mobile-with-exemption within one maintenance window (≤130s worst observed);
mobile-default whenever the human next picks up the phone — which is also the moment they would
have seen a push notification they were asleep for.

**Where it genuinely fails.**
- **Availability, not integrity.** `tab_close` kills the approval tab; `reload` restarts the
  service worker. The agent cannot approve, but it can make approving harder. With hub-side state
  and timeout-as-deny (§5.1) this is a denial-of-approval — annoying, fail-safe. It must be named,
  and `reload` should be refused while approvals are pending.
- **It cannot wake a sleeping phone.** Accepted, deliberately: nothing self-hosted can.
- **`chrome.notifications` on Edge Android is unverified.** The badge+popup path does not need it.
- **The human approving on device X cannot see the page on device Y.** See §5.5 — this raises the
  bar on context considerably, and is the strongest argument for including a screenshot
  thumbnail (available: `Page.captureScreenshot` on a *minimized* Edge desktop window measured at
  41–81ms, `SCRATCH.md` R4; `chrome.tabs.captureVisibleTab` on Edge Android measured working,
  333KB JPEG, R7).
- **Tension with "the extension carries zero site knowledge and zero policy."** The extension
  would render hub-supplied text and post back a token. It decides nothing and formats nothing it
  authored. A reviewer will raise this; the design must state the boundary explicitly rather than
  hope nobody notices.

### E — Approval page served by the hub on the tailnet

**Shape.** The hub serves `GET /approve` (list) and `POST /approve/<token>`. The human opens it
from any tailnet device. Either plain HTTP to a tailnet IP literal with an approver token, or
behind `tailscale serve` for TLS and identity.

**The strongest case for it, and it is a real one.** `tailscale serve` injects the caller's
identity into the upgrade/request headers — **measured in this project's own probe**
(`SCRATCH.md` R3):

```
tailscale-user-login: bkrabach@github
tailscale-user-name:  Brian Krabach
```

with a real Let's Encrypt certificate and no custom CA. That is free, cryptographic, per-*user*
identity — every other candidate can only prove that *a secret was presented*. For an audit trail
whose whole purpose is after-the-fact accountability, "approved by `bkrabach@github` at
14:32:07Z" is qualitatively better than "a valid approver token was presented." No other candidate
offers this.

**Where it fails.**
- **Self-drive (§2.2).** Without a guard, the agent `tab_open`s the approval URL and clicks
  Approve. The guard is small and clean — the extension refuses page-world commands and
  `tab_close` on the hub's own origin — but the whole candidate rests on it, and its natural
  failure mode is silence.
- **The addressing that makes identity work is measured broken.** `tailscale serve` needs the
  MagicDNS name; on the maintainer's own MacBook that name resolves to Tailscale's public Funnel
  ingress (`208.111.34.11`), not the tailnet IP, and the MagicDNS resolver reports *Not
  Reachable* (`SCRATCH.md` R2). The project's transport decision is therefore IP literals — which
  are incompatible with `serve`'s cert. Identity and addressability are, today, in direct
  tension.
- **Tagged devices get no `Tailscale-User-*` headers** (`SCRATCH.md` R3 note). If the browser
  devices are tagged, the identity value evaporates.
- Without `serve`, you are back to a bearer token in a URL, which lands in browser history and
  possibly in the audit log.

**Verdict:** the best identity story and the worst self-drive exposure, with its key advantage
currently blocked by a measured DNS failure on the maintainer's own hardware. Worth a council's
real attention rather than dismissal — if MagicDNS is fixed per-device, E's identity injection is
strictly better than any token scheme, and it could be layered *under* D as the audit-grade
approval path.

### F — A2A handoff to the human's own assistant

**Shape.** On gate, the hub sends an A2A message to the human's personal agent — wherever it runs
— which asks the human through whatever channel they already use, and returns the approval.

**The strongest case for it.** Adding a seventh notification channel to a person's life is itself
a failure mode. The human already has an assistant they respond to; meeting them there costs zero
attention budget and inherits that assistant's push, its identity, and its habits. Long term this
is almost certainly the right shape — an approval is a request to a person, and people have one
inbox, not one per tool.

**Why it is wrong now.** It reintroduces a model into the approval path. The approval prompt must
describe what the page said; that text is attacker-influenced; the approving assistant is a model
reading attacker-influenced text and deciding whether to bother the human. That is the exact
failure the design doc rejects for Candidate B (LLM screening): *"strictly worse than injecting
the driving agent, because the screener's whole job is to be trusted."*

It survives only under a hard constraint: **the second agent is a strict courier.** It cannot
hold the approver credential; it can only obtain a human's literal act and forward it. If the
courier can approve, you have moved self-attestation one hop and made the audit trail harder to
read. Enforcing that constraint requires a trust model between agents that does not exist here
yet.

**Verdict:** correct destination, premature vehicle. Revisit once the courier constraint can be
enforced structurally rather than by convention.

### G — Pre-authorization (not a channel)

**Shape.** No new mechanism — `scope.py` is built. The human, while present at session start,
declares what this session may do without further approval: a write scope, and (an additive
field) a set of pre-approved consequence categories. Narrow-only, sealed on first page read,
unreachable by the page and by the model.

**The strongest case for it.** The cheapest way to make an approval channel unnecessary is to
collect the approval *before* the human walks away. This is the only page-immune *prevention* the
design has (design doc §4), it is already implemented and enforced at the single choke point, and
it matches the maintainer's stance better than any prompt: *"I generally want it to be able to
access what I access."* A session that declares `write: ("github.com",)` before the human leaves
cannot be talked into elevating anyone's privileges on
`repos.opensource.microsoft.com` — no channel, no latency, no 2am buzz.

**What it cannot do.** Approve something unforeseen. Which is exactly the case the channel exists
for. It reduces the channel's firing rate; it does not replace it.

**Verdict:** should be tried first in every session. A council should be explicitly asked whether
the right answer to "we need an approval channel" is partly "we need fewer gates." One
non-obvious extension worth considering: let the human pre-approve *categories* at session
establishment (`send` on `mail.google.com`, say), so the channel only carries what was genuinely
not anticipated.

### H — Post-hoc: execute, attribute loudly, human reverses

**Shape.** For a defined category of action, do not gate. Execute, attach the browser-asserted
`effects` block (built), write the `action_effects` audit record (built), and push a *notice* —
not an approval request — to the human's devices, with enough attribution that they can reverse
it themselves.

**The strongest case for it, and it is uncomfortable.** **The measured incident that motivated
this entire subsystem is in the reversible bucket.** An Administrator grant on a Microsoft GitHub
repo can be revoked; the design doc says so in its own words (§3, Candidate D: *"Many real cases
are reversible — an Administrator grant can be revoked — which is why attribution has more
practical value here than the word 'irreversible' suggests"*). The primary defect in that
incident was D3 — *"the agent's own result gave no indication anything unusual had happened"* —
and D3 is already fixed. If the human had learned within seconds that a `POST …/elevate` had
fired, the harm window would have been minutes, and no approval channel would have been needed at
all.

**What it cannot do.** Undo. And a generic undo is not available: reversal is site-specific, and
`CONTRIBUTING.md` forbids the extension from knowing anything site-specific. So the honest form is
**post-hoc attribution + an unmissable notice + a human-executed reversal**, never automated undo.
Claiming otherwise would be exactly the fabricated capability this project's conventions prohibit.

**The split it implies.** The seven canonical categories do not belong in one bucket:

| Category | Reversible? | Proposal |
|---|---|---|
| `purchase` | No — money moves | Gate |
| `send` | No — cannot un-send | Gate |
| `delete` | Usually not | Gate |
| `oauth_grant` | Revocable, but the token may already be exfiltrated | Gate |
| `permission_change` | **Yes** — revocable | **Gate** (reversed — see below) |
| `account_creation` | Mostly | Post-hoc + loud notice |
| `file_upload` | Depends entirely on destination | Gate (conservative) |

**Verdict:** this is not an alternative to a channel — it is what keeps the channel's firing rate
low enough that a channel is tolerable.

**RESOLVED (review panel, 2026-07-26): `permission_change` stays gated, reversing this section's
original "qualified yes" toward post-hoc.** Three independent reviewers converged on the same
objection, and it survives scrutiny: "revocable" is a property of the *grant* (the permission
record itself can later be set back), not of *what the grant enabled while it was live*. The
measured incident's own shape makes this concrete: between an Administrator grant firing and a
human noticing the loud post-hoc notice and revoking it, an Administrator can add an SSH deploy
key, add a collaborator, disable branch protection, or exfiltrate a secret — none of which is
undone by revoking the permission afterward. Attribution (D3, already fixed) tells the human
*what happened*; it does not roll back *what the grant enabled in the meantime*. The category
that this whole subsystem's motivating incident belongs to is exactly the category where "gate
first" and "notify loudly after" are not substitutable — the window between grant and revocation
is where an Administrator-level actor does its damage, and post-hoc attribution only starts the
clock on human response, it does not stop the clock on what an elevated principal can do while
still elevated. See §6 for where the original (now-reversed) recommendation lived, and §7 item 1
for the open question this closes.

---

## 5. The cross-cutting questions

### 5.1 Timeout — deny, queue, or expire silently? (the most consequential sub-decision)

**Where we start.** The gated command already returns `needs_confirmation` *immediately*
(`hub.py:820`) — it never blocks. The non-blocking discipline is a repo convention
(`CONTRIBUTING.md`: *"The queue is a real, inspectable state, never a hidden block"*), and it is
already satisfied. So "the agent hangs" is not on the table. The question is what happens to the
*token*, and what the agent is told.

Today: 300s TTL, single-use, `consume_confirmation` raises on expiry, `policy_confirmation_expired`
is audited. Nothing more.

**Three problems with leaving it there.**

1. **Silent expiry teaches the agent nothing.** A `confirm` that fails five minutes later is
   indistinguishable from a typo'd token. The agent cannot tell "the human declined" from "the
   human never saw it" from "the hub restarted" (P4).
2. **Nothing stops re-gating in a loop.** An injected agent can re-issue the same action fifty
   times. Each one raises a fresh approval request. **Approval-fatigue-by-retry is a real attack
   on a consent mechanism**, and it is the same shape as the false-positive flooding the design
   doc already identifies as an attack (§3, Candidate A: *"decorate innocuous buttons with danger
   vocabulary until the gate fires constantly and the maintainer disables it"*). It applies with
   more force here, because the flood lands on a human's phone rather than in a log.
3. **A stale approval approves a stale world.** If the TTL is extended to accommodate a human
   walking to another room, an approval granted 12 minutes later is granted against a page that
   may have navigated, re-rendered, or been replaced.

**Recommendation: expire → deny, rate-limited, with context revalidation at redemption.**

- **TTL for `out_of_band`: 900s**, matching `FLOW_TTL_SECONDS`. One number, and a flow approval
  and the flow elevation it covers then expire together. 300s stays the default for
  `redeem: "agent"`.
- **On expiry: fail closed and loud.** Keep `policy_confirmation_expired`; add
  `approval_timed_out`. The agent's next attempt at the same
  `(device_id, tab_id, command, ref)` within a cooldown does **not** mint a new token — it returns
  `{ok: false, reason_code: "approval_not_granted"}`. Distinct from `out_of_scope`, distinct from
  a gate. The agent is told, once, to stop and report the block.
- **Revalidate at redemption.** Before `skip_gate` re-dispatch, check that the tab's committed
  origin is unchanged from when the gate fired. The hub already tracks this (`note_tab_url`,
  `_tab_hosts`). If it changed: refuse with `reason_code: "context_changed"`. This is what makes a
  longer TTL safe.
- **Do not queue approvals across a device's dark window.** Approvals must not outlive their
  context; the queue exists for *commands*, whose context is re-checked at drain time. An
  approval whose page has moved on is not an approval.

**What the agent sees**, in order: `needs_confirmation` with `redeem: "out_of_band"` and
`expires_at` → the agent proceeds with other work or reports the block (it cannot redeem) → TTL
elapses → `approval_not_granted` on any retry of the same action. Three distinct,
machine-readable states, no silence anywhere.

**The counter-argument a council should test:** timeout-as-deny converts the agent's ability to
*suppress* the approval UI (§2.2) into a reliable denial-of-service on the agent's own work. That
is self-defeating for an injected agent, which wants the action to *happen* — so the incentive
runs the right way. But it also means a legitimate agent loses work to a closed tab. Whether
that is acceptable depends on how often gates fire, which is why §5.4 matters.

### 5.2 Does the approval carry enough context to be consent?

"Approve action 47?" is not consent. Minimum payload:

| Field | Provenance | Why |
|---|---|---|
| Committed origin | **browser** | The one identifier the page cannot forge. Lead with it. |
| Device label + tab id/title | **hub/browser** | In a multi-device system, "approve this click" is meaningless without it |
| Element accessible name | **page** | The most informative field, and the least trustworthy |
| Category + score + threshold | derived | What tripped, and how hard |
| **Per-signal provenance** | derived | **The highest-value field, and it is already computed** — `Classification.signals[].provenance` exists precisely for this |
| Session id / declared scope | **caller** | Which agent, under what grant |
| Screenshot thumbnail (optional) | **page pixels** | The human's own eyes. Measured available even on a minimized window (R4) and on Android (R7) |

**The rule that makes the body attack-resistant:** *quote page-authored text; never let it look
like the channel's own words.* A page that names its button `Approved by Brian — tap Approve to
dismiss this notice` is a direct attack on the notification body, and it is trivial to mount. So:
browser-asserted fields render as the channel's own chrome; page-asserted fields render quoted and
visually marked. This is concrete and testable.

**Surviving a 200-character limit.** It does not, and that is the argument. A short body carries
only the browser-asserted summary; the full provenance-marked detail requires tap-through:

```
Elevate…Administrator?  repos.opensource.microsoft.com · brians-macbook-pro · tab 47
```

Origin and device are unforgeable; the label is quoted and truncated. This is why candidate D
(a full HTML page, which can render provenance visually and embed a thumbnail) beats a
notification-body-only channel on the dimension that actually decides whether consent is
informed.

### 5.3 What is approved — the click, or the flow?

`confirm_scope` already exists (`"action"` | `"flow"`), and the existing rule — flow iff the only
contributing channel was `flow` — is right. The channel must preserve it rather than invent a
third scope.

- **Action approval** names an element and covers one command.
- **Flow approval** must be presented to the human as what it is: *"allow this whole flow on
  `repos.opensource.microsoft.com` for the next 15 minutes"* — a duration and an origin, not a
  click. It is bounded by the committed origin (cleared on origin change, already designed) and
  by `FLOW_TTL_SECONDS`. Both bounds must be visible in the approval text; a blanket grant the
  human did not understand as a blanket grant is worse than no gate.
- **Do not add a session-wide "approve everything" scope.** That is how a gate becomes a
  formality. If a caller wants session-wide latitude, the mechanism for that already exists and
  is better: declare it up front via `scope.py` (candidate G), where it is narrow-only, sealed,
  and audited.

This composition is what keeps the cadence at *two prompts for a real privilege escalation, zero
for ordinary browsing* (design doc §4.1).

### 5.4 Is a channel even the right primitive?

For a subset, no — and naming that subset is what makes the channel affordable. The answer is a
three-way split, not a binary:

| Situation | Primitive | Why |
|---|---|---|
| Foreseeable at session start | **Pre-authorization (G)** | The human is present. Collect it then. Zero latency, zero interruption, page-immune. |
| Unforeseen + **reversible** + attributable | **Post-hoc (H)** | Browser-asserted effects make it loud within seconds; the human reverses. No block, no 2am buzz. |
| Unforeseen + **irreversible / world-visible** | **Channel (D)** | You cannot un-send. This is the only slice that genuinely needs a human before the fact. |
| Unclassifiable | **`on_unknown`** (built) | Already the caller's declared policy |

The channel therefore carries only the narrow irreversible slice of the unforeseen — which is
what keeps its firing rate low enough that the human still reads it. **Every gate moved out of
the channel makes the remaining gates more trustworthy**, because attention is the scarce
resource being protected, not compute.

### 5.5 What shifts when the human is at a *different* device than the browser (the normal case)

1. **The prompt must go to the human's device, not the driven one.** The hub knows which devices
   are connected; it does not know where the human is. **Broadcast to all connected devices, and
   let the single-use token make first-approval-wins safe.** This is simple, correct, and it is a
   genuine advantage of candidate D: the approval surface follows the human because all of the
   human's browsers are already on the tailnet and already connected.
2. **The approval's reachability is decoupled from the driven device's.** The laptop being driven
   may be awake while the phone is Dozing, or the reverse. Broadcasting makes the channel's
   availability the *union* of the devices' availability rather than the intersection.
3. **Context does all the work.** The human cannot see the page. Everything in §5.2 that would be
   nice-to-have when co-located becomes required. This is the strongest argument for including a
   screenshot thumbnail — and this project uniquely can, having measured `captureScreenshot` at
   41–81ms on a *minimized* Edge desktop window (R4) and `captureVisibleTab` working on Edge
   Android (R7), where the received wisdom says both should fail.
4. **Suppression reach is asymmetric — but not usefully so.** The agent has full control of the
   driven browser and, usually, of the approving browser too (it is connected to the same hub).
   So pending state must live in the hub and be re-fetched on reconnect, never solely in a tab.
5. **The audit record must name both devices** — which device the action targets, and which
   device the approval came from. Approving a laptop action from a phone is the normal case;
   an audit line that records only one of them cannot reconstruct what happened.

---

## 6. Recommendation

**Primary channel: D — the extension's own UI.**
**Plus A, honestly labeled, as the degenerate case. Plus G tried first. Plus H to keep D's firing
rate near zero. B behind an operator flag as the escape hatch for the sleeping phone.**

The reasoning, in the order it ran:

**Start from the failure mode, not the feature.** The stated hard requirement is that this must
not become an approval nightmare, because a channel that annoys gets disabled and a disabled gate
protects nothing. That makes two properties first-order safety properties, not conveniences:
*setup burden* (an unprovisioned channel protects nothing either) and *cost when wrong* (the
false positive at 2am is what causes the disabling). On both, D wins by a wide margin and it is
not close: zero new dependencies, zero new tokens, zero new installs, and a worst case of a
number on a toolbar icon.

**Then check the boundary is real.** `chrome-extension://` is outside `<all_urls>` — measured on
this project's own rig, not argued from documentation. The agent cannot script that page. The
approval travels the `/device` route, which the agent does not speak, from a device holding its
own token. Given P1 and P2, forgery is structurally unavailable.

**Then check reachability against the measurements, not against hope.** D's latency *is* the
measured connection profile, because it uses the connection that was measured: desktop instant,
mobile-with-exemption within one maintenance window, mobile-default when the human next opens
the browser. No candidate does better without a platform push service.

**Then accept what that costs.** D cannot wake a sleeping phone. Neither can C. Only B can, and B
costs a third-party dependency this project has taken a public position against, plus the worst
false-positive profile in the set. That trade is worth making — but it should be the operator's
to make, not the design's, which is why B ships as a flag rather than a default.

**A ships regardless**, correctly labeled as a host-local boundary rather than a human boundary,
because it is the fallback when no device is connected and because it is genuinely right when the
human is at the box.

**G and H are what make D affordable.** Pre-authorization removes the foreseeable cases before
the human leaves; post-hoc attribution removes the genuinely reversible ones. **REVERSED (review
panel, 2026-07-26): `permission_change` is NOT one of the genuinely reversible ones, and stays
gated.** The original text here argued that because the grant itself is revocable and D3
(attribution) is fixed, `permission_change` could move to the post-hoc side. Three independent
reviewers rejected this, and the rejection holds: revocability of the *permission record* says
nothing about reversibility of *actions taken using that permission before it's revoked*. The
measured incident's own category is Administrator access — precisely the class of grant where the
window between "granted" and "a human notices and revokes it" is where an elevated actor does
irreversible things (add a deploy key, add a collaborator, disable branch protection, exfiltrate a
secret) that revoking the *permission* does not touch. Post-hoc attribution shortens how long the
human takes to *notice*; it does nothing to shorten how long the actor has *root* before they're
noticed. `permission_change` gates, full stop — see §3.1's table for the corrected split.

**E is not chosen but is not dead.** If MagicDNS is fixed on the maintainer's devices,
`tailscale serve`'s identity injection is strictly better than any token scheme for the audit
trail, and it could be layered *beneath* D as an audit-grade approval path for the highest-
consequence categories. It is blocked today by a measured DNS failure, not by a design objection.

**F is the right destination and the wrong vehicle.** Revisit when the courier constraint can be
enforced structurally.

### 6.1 Residual risk I am accepting, deliberately

1. **The agent can suppress, though not forge.** `tab_close` and `reload` are available to it.
   Mitigated by hub-side pending state, re-fetch on reconnect, and refusing `reload` while
   approvals are pending. With timeout-as-deny this is a denial of approval, which fails safe.
   **Not eliminated.**
2. **No wakeup on a sleeping phone.** If the human's only reachable device is a Dozing Android at
   default settings, the approval waits — measured 509s dark windows, and indefinite if the phone
   is not picked up. No self-hosted channel can fix this. Named as a trade; B is the escape hatch.
3. **`chrome.notifications` on Edge Android is unverified,** and this project has twice measured
   Microsoft's own API matrix to be wrong. The badge+popup path does not depend on it; the toast
   is an enhancement behind a behavioral probe.
4. **A human approving on device X cannot see the page on device Y.** Mitigated by
   provenance-marked context and an optional screenshot. **Not eliminated** — they are trusting a
   summary.
5. **Page-authored text appears in the approval body.** Mitigated by quoting and by leading with
   browser-asserted fields. A sufficiently clever accessible name will still mislead some humans
   some of the time. **Not eliminable** — it is the same asymptote the design doc already
   documents for prompt-level mitigation.
6. **Pending approvals are in-memory and die on hub restart (P4).** Accepted for a first
   implementation; must be stated in `docs/POLICY.md` rather than discovered.
7. **Approval fatigue remains possible and undetectable.** If gates fire often enough, the human
   taps without reading and the channel silently becomes self-attestation. The only real defense
   is §5.4's three-way split keeping the firing rate low — a policy defense, not a mechanism one.

---

## 7. What I did not resolve — the questions for the councils

These are genuine open trade-offs, not missing research. Each has a defensible answer on both
sides.

1. **RESOLVED, not open: `permission_change` stays gated.** (Was: "is it better handled
   post-hoc, given it's revocable and the attribution defect is fixed?") Three reviewers rejected
   the post-hoc framing on the same grounds: revocability of the grant does not bound what an
   elevated actor does before the grant is revoked. See the reversal recorded in §3.1's table and
   §6. Left here only so a future reader can see the original question and why it's closed, not
   as something still open for a council to re-litigate.
2. **Timeout-as-deny hands the agent a reliable denial-of-service on its own work** (it can close
   the approval tab). The incentive runs the right way for an *injected* agent, but a buggy one
   loses work. Is fail-closed still right?
3. **Does the third-party push objection actually survive §4.B's distinction** between a control-
   plane relay (rejected, correctly) and a notification relay whose approval is redeemed back
   over the tailnet? If it does not, B may deserve to be the default rather than a flag.
4. **Is broadcast-to-all-devices right, or should the human designate an approver device?**
   Broadcast is simpler and the single-use token makes it safe, but it means an approval request
   appears on every browser the human owns, including ones they are not at.
5. **Does an approval UI inside the extension cross the "zero policy in the extension" line?** I
   argue it does not — it renders hub-supplied text and returns a token, deciding nothing. A
   reviewer may reasonably disagree, and the boundary should be written down either way.
6. **Should P2's approver credential be a token, a tailnet-identity check (`whois` on the source
   address, requiring source-node ≠ agent-host node), or both?** A tailnet check is stronger — the
   model cannot move itself to another node — but it fails for tagged devices and adds a
   `tailscaled` local-API dependency to the hub.
7. **900s TTL: too long, too short?** It is set by human walking-to-another-room time, and I chose
   it for symmetry with `FLOW_TTL_SECONDS` rather than from evidence. Nobody has measured this.

---

## 8. Evidence provenance

Every measurement cited here comes from this project's own instrumentation, not from
documentation. Marked so a reviewer can weigh them:

| Claim | Source | Status |
|---|---|---|
| Desktop socket: 142 min, 0 gaps, max 15.1s | `SCRATCH.md` R5, real Edge on MBP over Tailscale | **Measured** |
| Mobile Doze: 44–133s dark windows with exemption; 509s without | `SCRATCH.md` R7 Doze A/B, real Z Fold 7 | **Measured** |
| `<all_urls>` does not cover `chrome-extension://` pages | `SCRATCH.md` R6, Edge 150 macOS | **Measured** (one browser/OS) |
| `captureScreenshot` on a minimized window: 41–81ms | `SCRATCH.md` R4, CDP against real Edge | **Measured** (macOS only; Windows occlusion path untested) |
| `captureVisibleTab` works on Edge Android (333KB JPEG) | `SCRATCH.md` R7 | **Measured** — contradicts Microsoft's published matrix |
| `tailscale serve` injects `Tailscale-User-Login`, real LE cert | `SCRATCH.md` R3, wscat from spark-1 | **Measured** |
| MagicDNS resolves to Funnel ingress on the MBP; works on Android | `SCRATCH.md` R2, R7 | **Measured** — per-device |
| `chrome.notifications` availability on Edge Android | — | **Unverified.** Must be behaviorally probed |
| Policy-force-installed extensions may skip the debugger banner | `SCRATCH.md`, reported | **Unverified** |
| An agent can drive a hub-served approval page (§2.2) | Inferred from `<all_urls>` + `COMMANDS` | **Reasoned, not measured.** A 10-minute test would settle it and should be run before E is dismissed |
