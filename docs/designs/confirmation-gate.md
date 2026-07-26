# Design: The Confirmation Gate — deciding when an action deserves a human

**Status:** draft for review. **Update (2026-07-26, same day): §15 step 6 / §16.1's "still
deferred" out-of-band redemption channel is CANCELLED, not deferred** — see
`docs/designs/approval-channel-options.md` §0 for the decision and evidence, and §17 below for
what changed in this codebase as a result (the `redeem: "out_of_band"` name, used throughout this
document as originally written, is renamed to `redeem: "unredeemable"` in the actual
implementation — this document's historical prose keeps the original name where it is describing
what was proposed/built at the time, for an accurate record).
**Date:** 2026-07-26
**Supersedes:** the gate half of `docs/POLICY.md` §3 and `policy.GATE_RULES`
**Leaves untouched:** the denylist (`docs/POLICY.md` §2), the kill switch (§5), the audit log (§6)

This document is both the design and the implementation specification. Part I is the design
(problem, candidates, recommendation, contracts). Part II is the builder-facing spec.

---

# Part I — Design

## 1. The measured failure

Live, on the maintainer's real browser, driving `repos.opensource.microsoft.com`:

1. `click` on a button labeled **"Next"** → advanced a just-in-time privilege-elevation flow.
   No gate fired.
2. `click` on a button labeled **"Elevate bkrabach to Administrator"** → **granted real
   Administrator access to a Microsoft GitHub repository.** No gate fired. The result envelope
   returned `{ok: true, result: {ref: "...", tag: "button"}}` — nothing in it indicated that
   anything had happened at all.

The `permission_change` rule that should have caught #2 (`policy.py:430-442`):

```python
GateRule(
    category="permission_change",
    commands=frozenset({"click"}),
    label_patterns=_re(
        r"\bgrant\b",
        r"\bchange permission\b",
        r"\bupdate permission\b",
        r"\bmake public\b",
        r"\bmake private\b",
    ),
    url_patterns=_re(r"/settings/permissions", r"/security/permissions"),
    combine="all",
)
```

It failed **twice over**. The label matched none of the five phrases. And even had it matched,
`combine="all"` additionally required the URL to contain `/settings/permissions` — which the JIT
elevation flow's URL does not. The rule was structured so that the strongest available evidence
("Elevate … to Administrator") could not fire it alone.

### 1.1 Three separable defects, not one

The instinct is to call this a vocabulary problem. It is not. Reading the code end-to-end, the
gate has three independent failures, and fixing only the first fixes nothing:

**D1 — Classification.** Detection runs on `(command, label, url)` where `label` is the
element's accessible name. Label text is a weak signal for reasons that are structural, not
tunable: the same word carries different consequence ("Continue" dismisses a cookie banner and
completes a purchase); the consequential button is routinely the blandest ("Next", "Submit",
"OK"); labels are localized, templated, icon-only, or `aria-label`-only. Word lists chase a
tail that has no end.

**D2 — Enforcement.** A fired gate returns `{status: "needs_confirmation", confirmation_token}`.
The token is redeemed by sending a `confirm` message on the hub's `/agent` WebSocket
(`hub.py:546`). That path is reachable only by the agent. **There is no human anywhere in it.**
A prompt-injected model that gets gated simply redeems its own token and proceeds. The gate is
an attestation, not an approval.

Worse, as shipped: `grep -rn confirm src/amplifier_browser_bridge/{cli,client,mcp_server}.py`
returns **zero** matches. No CLI command, no client method, no MCP tool can redeem a token. Today
a gate that fires is an *unrecoverable dead end* for every shipped agent surface. That is the
maximum possible false-positive cost, which is precisely the pressure that gets a safety control
switched off.

**D3 — Attribution.** A `click` returns `{ref, tag}`. Nothing about what the click *did*. The
audit log records `command=click, ref=f0.e93`. Neither the agent, nor the human reviewing the
log later, nor any downstream check can tell an elevation from a scroll. The maintainer's
observation — *"the agent's own result gave no indication anything unusual had happened"* — is
not a symptom of D1. It is its own defect, and it is the cheapest of the three to fix.

## 2. The lemma that decides the architecture

Sort every signal available to this system by **who authors it**.

| Provenance | Examples | Adversarial page can… |
|---|---|---|
| **Page-asserted** | accessible name, `aria-label`, `role`, `href`, `<form>` `method`/`action`, `formaction`, headings, `<title>`, `input[type]`, DOM structure, class/id | …set all of it to anything. Suppress danger words. Fabricate safety words. Label the elevate button "Cancel". |
| **Browser-asserted** | committed tab URL/origin, network requests the browser actually issued (method, URL, initiator), navigation commits + transition type, downloads started, tabs/windows created | …**cause** spurious events (decoy POSTs). **Cannot suppress** a real one. Cannot forge the observation channel. |
| **Caller-asserted** | the session's declared write scope, `on_unknown` handling, unattended flag | …not touch it at all. It never crosses the page boundary. |

Three consequences follow, and they are not negotiable:

> **Lemma.** Every signal available *before* an action executes, other than caller-asserted
> constraints, is authored by the page the gate exists to guard against.

**(a) Any page-derived pre-execution classifier is advisory.** Not "currently weak, improvable" —
advisory *by construction*. Enriching it from label-only to label + form + href + ARIA + heading
raises the floor against ordinary pages and does nothing against an adversarial one. It is worth
doing. It is not a boundary, and the system must stop describing it as one.

**(b) Page-immune prevention must be caller-asserted.** The only pre-execution constraint an
adversarial page cannot reach is one declared through a channel the page's content never enters.

**(c) Page-immune detection must be browser-asserted, and is therefore post-hoc.** Browser
observations have an asymmetry that is exactly right for a safety signal: a page can add noise
but cannot hide a request it actually made. No false negatives from page control. The cost is
that you learn after the fact.

The current gate sits entirely in category (a), where no amount of work produces a boundary. The
design must build legs in (b) and (c).

---

## 3. Candidate designs

Five candidates. A, B, C, D are alternative answers to "how does the system decide?"; E answers
"and then what?", which A–D all presuppose and none of them supplies.

### Candidate A — Deterministic multi-signal pre-classification

Keep the pre-execution gate; replace label-only matching with a scored fusion of everything the
extension can read from the DOM at resolve time: accessible name, `role`, `tag`, `href` plus a
cross-origin flag, enclosing `<form>`'s `method` and `action` plus cross-origin flag,
`type="submit"`, `formaction`, `input[type]`, the nearest heading, the enclosing dialog's title,
and the page `<title>`. Replace the seven fixed word lists with consequence **families** matched
by stem, scored additively across channels against a threshold.

- **Catches:** a large class the current rules miss — cross-origin form POSTs, submit buttons,
  `input[type=file]` without a matching word, and multi-term privilege phrasing such as
  "Elevate … to Administrator" (two family terms co-occurring, no phrase list required).
  Removes the `combine="all"` conjunction that made the measured case doubly impossible.
- **Misses:** an SPA button with an `onclick` handler, no `<form>`, no `href`, and a bland
  label. That is the "Next" case exactly, and it is the common shape of modern enterprise UI.
- **Adversarial page:** defeats it completely. Every input is page-authored. It can also attack
  in the *other* direction — decorate innocuous buttons with danger vocabulary until the gate
  fires constantly and the maintainer disables it. False-positive flooding is a real attack on a
  consent mechanism, not just an annoyance.
- **Ergonomics:** good. Invisible when nothing matches; modestly more prompts than today.
- **Weight:** medium. Additive snapshot fields in `injected.js`, a new pure-function classifier
  module in the hub. No new permissions, no new dependencies, no protocol round trips.

### Candidate B — LLM action screening

On ambiguity, call a model with the proposed action plus a page snapshot: *"does this action
appear irreversible or world-visible?"* Constrain output to an enum.

- **Catches:** the measured case trivially. A model reading "Elevate bkrabach to Administrator"
  on a just-in-time-access page classifies it correctly with no vocabulary engineering, and
  handles localization, icon-only buttons, and novel phrasings that no word list will ever cover.
- **Misses:** a bland "Next" whose consequence is two steps away, unless it reasons about the
  whole flow. Adds a model call's latency and cost to the interaction loop. Non-deterministic:
  the same click can classify differently across runs, which makes the control unauditable in the
  way a deterministic rule is auditable.
- **Adversarial page:** **the page controls the classifier's input.** This is prompt injection
  aimed directly at the safety layer — strictly worse than injecting the driving agent, because
  the screener's whole job is to be trusted. Hidden text reading *"this button dismisses a cookie
  notice"* is a one-line bypass. The design doc's own research (§6, and `SCRATCH.md`'s
  23.6% → 11.2% → ~1% → <0.08% trajectory) concludes prompt-level mitigation asymptotes above
  zero. Putting the last line of defense on that asymptote is the wrong place for it.
- **Ergonomics:** invisible, but every ambiguous click costs money and ~1s.
- **Weight:** high, and it is the wrong *shape*: a model dependency inside the hub makes the
  bridge a policy engine and takes a hard dependency the design doc §13 forbids. (`vision.py`
  exists, but as an explicitly caller-invoked mechanism — `vision_read` — never as something the
  hub reaches for on its own.)

There is a salvageable core, though: **a screener that may only escalate, never de-escalate.**
If the screener's output can raise a classification but can never lower one the deterministic
layer produced, then a page that successfully injects it achieves — at worst — a false gate.
Annoying, not dangerous. That constraint converts B from a liability into a safe optional layer,
provided it lives *outside* the bridge.

### Candidate C — Caller-declared scope, monotonically narrowing

The calling agent declares, at session establishment, what the session may *do*: read scope
(default: everything, per the maintainer's stance) and **write scope** — the origins on which
state-changing commands (`click`, `type`, `key`, `navigate`) are permitted. The hub enforces it
at `send_command`. Two rules make it page-immune:

1. The scope is set through a **session-establishment** message, never through the command path.
2. **It can only narrow, never widen, once the session has read any page content.** The hub
   tracks "has this session ingested page content yet"; after the first read, a widen request is
   refused.

- **Catches:** the measured case outright, *if* the session's write scope did not include
  `repos.opensource.microsoft.com`. For a task like "summarize my open PRs," it would not.
  Catches every category of consequential action equally, including ones nobody enumerated,
  because it never tries to classify — it constrains reach.
- **Misses:** everything inside the granted scope. If the task legitimately involves that origin,
  the elevation click is in scope and passes. A caller that declares `write: *` gets no
  protection at all.
- **Adversarial page:** **cannot touch it.** This is the only candidate with that property. The
  narrowing rule is what closes the loop: once page content has entered the agent's context, the
  agent can no longer talk itself into a wider grant, because the hub will not accept one.
- **Ergonomics:** one decision per session, not per click — but it is a real cost and it points
  at the "approval nightmare" the maintainer ruled out. Mitigated by defaulting to broad and
  letting the *caller's harness* (not the human) narrow — e.g. an unattended Amplifier run
  narrows itself to the origins named in its task.
- **Weight:** medium. A session concept in the hub (which today has none), a scope object, a
  check in `evaluate`.

### Candidate D — Browser-observed effects: attribution first, gating second

Stop predicting. Observe. For every state-changing command, the extension records what the
browser actually did in a bounded window after dispatch — non-GET network requests and their
target origins, navigation commits and transition types, downloads started, tabs opened — and
returns them as an `effects` block on the result. These are browser-asserted (§2): the page
cannot suppress them.

Then two uses, in order of value:

1. **Attribution (post-hoc).** The result, and the audit log, now say *"this click issued
   `POST https://…/api/…/elevate`"* instead of *"clicked ref f0.e93"*. Directly fixes D3.
2. **Flow elevation (pre-hoc, for the *next* action).** A tab in which an action was observed to
   be state-changing enters an **elevated-consequence context**. Subsequent state-changing
   commands in that tab are gated until the committed origin changes or a flow confirmation is
   redeemed. This is how a bland "Next" becomes catchable — not by its label, but by the observed
   character of the flow it sits in.

- **Catches:** every consequence that crosses the network, with no vocabulary, no model, and no
  page cooperation. Makes the measured elevation *loud* even in the case where nothing prevented
  it.
- **Misses:** prevention of the first consequential action in a fresh flow — by construction.
  Purely client-side steps that issue no request. Requests the page routes through an already-open
  WebSocket (the handshake is visible; the frames are not). And "irreversible" means exactly that:
  attribution does not undo a sent email. (Many real cases *are* reversible — an Administrator
  grant can be revoked — which is why attribution has more practical value here than the word
  "irreversible" suggests.)
- **Adversarial page:** can add decoy effects to trigger false elevation; cannot hide a real one.
  The failure mode is noise, not silence. That is the correct direction for a safety signal.
- **Ergonomics:** the attribution half costs the agent nothing — it is additional data on results
  it already receives. The flow-elevation half costs one confirmation per consequential flow,
  which is the cheapest gating cadence available.
- **Weight:** medium-high, and it has a **permission cost** that must be decided explicitly:

  | Tier | Mechanism | New permission | Sees | Blind to |
  |---|---|---|---|---|
  | `cdp` | CDP `Network.requestWillBeSent` | none (`debugger` already held) | everything | Android; requires attach → yellow banner |
  | `webrequest` | `chrome.webRequest` (observe-only) | **`webRequest`** | all requests incl. XHR/fetch | requests with `tabId: -1` (service-worker-initiated) |
  | `navigation` | `chrome.webNavigation.onCommitted` + `downloads.onCreated` + `tabs.onCreated` + post-action `tabs.get` | `webNavigation` (already in desktop manifest; **absent from `manifest.android.json`**) | navigations, form submits, downloads, new tabs | **all XHR/fetch mutations** — i.e. the SPA case |
  | `none` | — | — | — | everything |

  `navigation` is free but blind to exactly the case that matters most on modern enterprise apps.
  `webrequest` costs one manifest permission (store-review surface, Android re-sideload prompt)
  and is the only tier that covers SPA mutations without a banner.

### Candidate E — Out-of-band redemption (the enforcement fix)

Orthogonal to A–D, and required by all of them. A confirmation token becomes redeemable through a
channel the driving model cannot reach:

- `redeem: "agent"` — current behavior. Honestly relabeled as **self-attestation**: it forces the
  agent to make a second, explicit, separately-audited decision, which has real value against
  *accidental* action and none against an injected one.
- `redeem: "out_of_band"` — the token is redeemable only via a distinct hub endpoint that the
  agent's protocol route does not expose (`abb approve <token>` on the human's own machine, or
  the extension's own options page). The agent receives the token's existence, never a way to
  spend it.

- **Catches:** nothing by itself. It is what makes anything A–D catches actually *mean* something.
- **Misses:** the human is on another device and may be asleep. `out_of_band` on an unattended
  run converts a gate into a hang. Mitigated by the existing non-blocking discipline: the gated
  command is *not* queued — it returns `needs_confirmation` immediately, and the agent proceeds
  with other work or reports the block.
- **Adversarial page:** unreachable.
- **Ergonomics:** high cost when it fires. Which is the argument for firing rarely and precisely,
  i.e. for A + C + D being good.
- **Weight:** low-medium. A redemption channel and a per-session setting.

---

## 4. Recommendation

**Build A + C + D + E as one composed mechanism, and keep B outside the bridge as an
escalate-only hook.**

The reasoning, in the order it actually ran:

**Fixing D1 alone is not worth doing.** A better classifier that still hands its verdict to a
self-redeemed token, and still returns a result that says nothing about what happened, has moved
the failure from "no gate fired" to "a gate fired and the agent waved it through." Any
recommendation that does not touch D2 and D3 is treating the symptom.

**D3 is the highest value per unit of work, and nothing else in the system competes.** Effects
are browser-asserted, so they are the only page-immune signal available; they cost the agent
nothing; they make the audit log an actual attribution trail rather than a list of refs; and they
are the substrate that makes bland-label gating possible at all. If only one thing ships, it is
this.

**C is the only page-immune *prevention*, so it has to exist even though its default is
permissive.** Shipping the mechanism with a broad default costs nothing and honors the
maintainer's stance; the value is that an unattended caller now *has* a way to bind itself, held
outside the prompt, that a page cannot argue it out of. The narrow-only-after-first-read rule is
what makes it a boundary instead of a suggestion. This is the design doc's own capability-binding
argument (§6.2) applied to the write path, where the denylist already applies it to the read path.

**A is worth building, at its honest weight.** Most pages are not adversarial. A scored
multi-signal classifier catches far more of the ordinary bad case than word lists do, costs
nothing at runtime, and is fully deterministic and testable. It is labeled advisory throughout —
in the result shape, in the docs, in the audit events.

**B stays out of the bridge.** The bridge emits a structured, provenance-labeled action
descriptor; a caller that wants model screening runs it on that descriptor in its own layer. If
the hub ever supports a screening hook, it is configured by the *hub operator*, never by the
agent, and is constrained to escalate-only — so a page that injects the screener causes at worst
a spurious prompt. No model import, no API key, no policy in the mechanism layer.

**E must ship with the rest**, at minimum as: the `redeem` field on every gate, an actual
redemption path on the CLI/client/MCP surfaces (which do not exist today, making every gate a dead
end), and `out_of_band` available as a session setting.

### 4.1 How the composition catches the measured pair

| Action | Caught by | Mechanism |
|---|---|---|
| `click "Elevate bkrabach to Administrator"` | **A** | two `privilege`-family terms co-occur in the accessible name → score 3 ≥ threshold. No phrase list, no URL corroboration required — the `combine="all"` conjunction is gone. |
| | **D** | if it executes, the observed `POST …/elevate` is reported on the result and in the audit log — the "no indication anything happened" defect is closed regardless of whether the gate fired. |
| | **C** | denied outright if the session's write scope excludes that origin. |
| `click "Next"` | **D** (flow) | the tab is in an elevated-consequence context — either because a prior action there was observed to be state-changing, or because the page context matched a consequence family — so every state-changing command in it is gated until the flow is confirmed once. |

Two prompts for a real privilege escalation (entering the flow, then the act itself); zero
prompts for ordinary browsing. **Gate per flow, not per click** is what keeps this from being the
approval nightmare the maintainer ruled out.

### 4.2 Residual risk, accepted deliberately

Stated plainly, because the whole point of §2 is that some of this cannot be engineered away:

1. **An adversarial page defeats candidate A entirely, and can defeat D's flow-elevation
   trigger** where that trigger is page-context-derived. A page that renders a bland button, makes
   no request, and mutates server state via an already-open WebSocket is invisible to this design.
   Accepted: the compensating controls are C (which the page cannot reach) and the audit log.
2. **Effects are post-hoc.** The first consequential action in a fresh flow, with a bland label
   and no page-context signal, executes. Accepted: prevention of the genuinely unpredictable first
   action requires either a narrow scope (available, opt-in) or per-click approval (ruled out).
3. **`redeem: "agent"` is the default.** Chosen so gates are not dead ends on unattended runs.
   It provides no defense against an injected model. Accepted and labeled as such in every result
   and every doc. **Update, same day:** the alternative is not "one session setting away" as
   originally written here -- a human-approval channel was designed (`docs/designs/
   approval-channel-options.md`) and then cancelled outright (see that doc's §0). The only
   session-settable alternative is `redeem: "unredeemable"`, which does not add a channel; it
   converts the gate into a permanent, unrecoverable stop for a session declared unattended. If
   this residual risk (self-attestation, no real defense against injection) is unacceptable for a
   given task, the fix is a narrower `write` scope (Candidate C), not a different `redeem` value.
4. **False positives will rise.** Scoring plus flow elevation fires more often than today's word
   lists. Accepted because the cadence is per-flow, and because the alternative — a gate tuned so
   tight it misses "Elevate to Administrator" — is the failure we are fixing.
5. **A caller that declares `write: *` gets no scope protection.** The bridge cannot make a bad
   caller safe. It can only refuse to *be* the bad caller.
6. **Effect attribution is time-windowed, in a session where a human is also clicking.** A human
   click landing inside the agent's observation window is attributed to the agent. Mitigated by
   scoping to the acting tab and reporting an attribution confidence, not eliminated.

---

## 5. Where the mechanism/policy line falls

Design doc §13's test — *"could two reasonable agents, in two different situations, want
different behavior here?"* — applied line by line:

**The bridge supplies (mechanism):**

- The **action descriptor**: every signal it can observe about a proposed action, each tagged with
  its provenance (`page` / `browser` / `caller`). Raw facts, uniformly reported.
- The **classification**: a deterministic score, the categories that contributed, the individual
  signals that fired, the threshold, and the outcome (`clear` / `elevated` / `unknown`). Reported
  on every state-changing result, whether or not it gated.
- The **effects**: what the browser actually did, plus the collector `tier` so the caller knows
  what the block could and could not have seen. Honest degradation, never a silent gap.
- **Enforcement of the caller's declared scope**, held outside the command path, narrow-only.
- The **confirmation lifecycle**: single-use expiring tokens, and the redemption channels.
- **Audit** of every one of the above.

**The caller decides (policy):**

- The write scope, and whether to narrow it.
- `on_unknown`: what an unclassifiable action means for *this* session (§6).
- `redeem`: whether a human must approve, or the agent may self-attest.
- Whether to run a model screener on the descriptor, and which one.
- Whether to act on an `effects` block that reports something surprising.

**What the bridge deliberately does not do:** decide that a category is dangerous *for you*,
call a model on its own initiative, block an action because it could not classify it, or
substitute one mechanism for another when the first returns thin results.

The current design fails this test in one specific place, and that failure is the same class of
mistake as the frame-ranking heuristic the codebase already removed: `GATE_RULES` hardcodes a
seven-category taxonomy with tuned thresholds in the mechanism layer, and every caller gets that
one judgment. The fix is not to delete gating — it is to make the bridge report signal and
enforce *the caller's* declared handling of it, with a default that matches the maintainer's
stance. The category taxonomy survives as a **default classifier profile**, replaceable by
configuration, not as a fact baked into dispatch.

---

## 6. The unclassifiable case

`unknown` is a first-class outcome, distinct from `clear`. Today the two are conflated — a click
whose ref the hub never saw and a click whose label matched nothing both fall out of the loop at
`policy.py:816` as `PolicyDecision(status="allow")`, indistinguishably.

| Outcome | Meaning |
|---|---|
| `clear` | A descriptor was available. Signals were evaluated. Score below threshold. |
| `elevated` | Score at or above threshold. Gate fires. |
| `unknown` | **No usable descriptor at all.** The ref was never observed in a `snapshot`/`wait_for`; the cached hint was discarded as stale; the device's extension is too old to supply a descriptor; the page is canvas-rendered and yields no semantics. |

**Behavior:** `unknown` is governed by a caller-declared session setting, `on_unknown`, and is
**always reported** regardless of setting.

| `on_unknown` | Behavior | When a caller picks it |
|---|---|---|
| `"allow"` (**default**) | Command proceeds. Result carries `classification.status = "unknown"` and a `reason_code`. Audit event `policy_unclassified`. | Attended co-working. Matches the maintainer's stance; blocking here would fire on every un-snapshotted ref and get the gate disabled. |
| `"gate"` | Returns `needs_confirmation` with `category: null` and `classification.status: "unknown"`. | Unattended runs. The recommended setting when the human is on another device. |
| `"deny"` | Refused with `reason_code: "unclassifiable"`. | High-assurance callers. |

This is the fail-loud requirement satisfied correctly: **fail-loud means the state is visible,
not that the command is blocked.** The distinction between "we looked and it looks fine" and "we
could not look" survives all the way to the caller, the audit log, and the human — and what to do
about it is the caller's call, not the bridge's.

The bridge never silently treats `unknown` as `clear`. That silent conflation is the current
behavior and is the thing this section exists to end.

---

## 7. What the calling agent sees

All additions are additive to the existing envelopes in `docs/PROTOCOL.md`. No existing field
changes meaning or disappears.

### 7.1 Not gated — `classification.status: "clear"`

```json
{
  "v": 1, "id": "...", "type": "result", "ok": true,
  "result": {"ref": "f0.e93", "tag": "button"},
  "classification": {
    "status": "clear",
    "score": 1, "threshold": 3,
    "categories": [],
    "advisory": true,
    "signals": [
      {"channel": "label", "provenance": "page", "value": "Next", "matched": [], "weight": 0},
      {"channel": "page_context", "provenance": "page", "value": "Repos — Microsoft Open Source", "matched": ["access"], "weight": 1},
      {"channel": "url", "provenance": "browser", "value": "https://repos.opensource.microsoft.com/...", "matched": [], "weight": 0}
    ]
  },
  "effects": {
    "tier": "webrequest",
    "window_ms": 1500,
    "attribution": "time_window",
    "state_changing": true,
    "requests": [
      {"method": "POST", "url": "https://repos.opensource.microsoft.com/api/.../elevate",
       "type": "xmlhttprequest", "cross_origin": false}
    ],
    "navigations": [], "downloads": [], "tabs_opened": []
  }
}
```

`advisory: true` is not decoration. It is the contract statement that this block is derived from
page-controlled input and is not a security boundary. It is always `true` for page-provenance
signals.

### 7.2 Gated

```json
{
  "v": 1, "id": "...", "type": "result",
  "status": "needs_confirmation",
  "confirmation_token": "9f2c...hex",
  "category": "permission_change",
  "detected": {"category": "permission_change", "label_match": "privilege:2", "url_match": null},
  "classification": {
    "status": "elevated",
    "score": 3, "threshold": 3,
    "categories": ["permission_change"],
    "advisory": true,
    "signals": [
      {"channel": "label", "provenance": "page", "value": "Elevate bkrabach to Administrator",
       "matched": ["privilege:elevate", "privilege:administrator"], "weight": 3}
    ]
  },
  "redeem": "agent",
  "confirm_scope": "action",
  "expires_at": "2026-07-26T18:45:18.417996+00:00"
}
```

`category` and `detected` are retained verbatim for compatibility with existing consumers and
tests. `confirm_scope` is `"action"` (this one command) or `"flow"` (clears flow elevation for
this tab until its committed origin changes or the flow TTL elapses).

### 7.3 Classification failed — `unknown`

With `on_unknown: "allow"` (default), the command runs and reports:

```json
{
  "ok": true,
  "result": {"ref": "f0.e93", "tag": "button"},
  "classification": {
    "status": "unknown",
    "reason_code": "ref_not_observed",
    "advisory": true,
    "score": null, "threshold": 3, "categories": [], "signals": []
  },
  "effects": {"tier": "navigation", "state_changing": false, "...": "..."}
}
```

`reason_code` ∈ `ref_not_observed` · `hint_stale` · `descriptor_unavailable` ·
`device_capability_missing` · `no_page_semantics`.

With `on_unknown: "gate"`, the same information arrives as a `needs_confirmation` with
`category: null` and `classification.status: "unknown"`.

### 7.4 Refused by scope

```json
{
  "ok": false,
  "error": "write scope does not permit 'click' on origin https://repos.opensource.microsoft.com — session write scope: [https://github.com]",
  "reason_code": "out_of_scope"
}
```

**This error is specific, unlike the denylist's deliberately generic `DENY_REASON`, and the
asymmetry is intentional.** The denylist's vagueness protects an *invisibility* guarantee — naming
the category tells the agent a hidden tab exists and roughly what it is. Scope is the caller's own
declaration; telling it back leaks nothing and an unactionable error here would be strictly worse
than a specific one.

---

## 8. Migration

`policy.py` has four import sites, all verified:

| Site | Imports | Impact |
|---|---|---|
| `hub.py:67` | `PolicyEngine`, `PolicyError` | none — both retained, same names |
| `cli.py:28` | `Denylist`, `host_of` | none — both retained |
| `tests/test_policy.py:26` | `DEFAULT_CONFIRMATION_TTL_SECONDS`, `Denylist`, `PolicyEngine`, `PolicyError`, `host_matches_domain`, `host_of` | none — all retained |
| `tests/test_ref_hints.py:26` | `PolicyEngine` | none |

`GateRule`, `GATE_RULES`, and `FILE_UPLOAD_INPUT_TYPES` have **no importers outside `policy.py`**
(only prose references in `docs/POLICY.md`). They move to a new `classify.py` and are re-exported
from `policy.py` for one release.

**Additive, non-breaking:**

- `policy.py` keeps the denylist, tab-host/discarded/ref caches, `filter_tabs_result`, the
  confirmation lifecycle, and the kill switch. `PolicyEngine.evaluate` keeps its signature and
  return type.
- `PolicyDecision` gains optional fields (`classification`, `redeem`, `confirm_scope`,
  `reason_code`, `expires_at`), all defaulted. Existing construction sites are unaffected.
- All seven category names survive: `purchase` · `send` · `delete` · `oauth_grant` ·
  `file_upload` · `account_creation` · `permission_change`.
- Wire protocol: `needs_confirmation` keeps `confirmation_token`, `category`, `detected`.
- Extension: new snapshot node fields are additive; `combine_frames.mjs`, `frame_refs.mjs`, and
  `ref_registry.mjs` are untouched.

**Breaking, and named honestly — two existing tests encode semantics this design removes:**

| Test | Why it breaks | Replacement assertion |
|---|---|---|
| `test_gate_permission_change_requires_both_signals` (`test_policy.py:522`) | `combine="all"` is replaced by scoring. This conjunction is *the bug*: it is why "Elevate … to Administrator" could not fire the rule on its own. | Rewrite as `test_permission_change_weak_label_alone_does_not_gate` — a single weak family term ("access") still scores below threshold, preserving the original protective intent by a different mechanism. |
| `test_gate_oauth_grant_requires_both_label_and_url` (`test_policy.py:486`) | same | Rewrite as `test_lone_allow_label_does_not_gate` — a bare "Allow" (cookie banner) still does not gate; "Allow" *plus* an OAuth authorize URL still does. |

One test keeps passing but changes meaning and needs a companion:
`test_ordinary_click_with_no_signal_is_not_gated` (`test_policy.py:538`) still does not gate under
`on_unknown: "allow"` — add `test_click_with_no_descriptor_reports_unknown_not_clear` asserting
`classification.status == "unknown"`.

**Operational migration:**

- Extension: `manifest.json` gains `webRequest` if tier `webrequest` is adopted;
  `manifest.android.json` gains `webNavigation` (currently absent) and `webRequest`. Both are
  install-prompt changes requiring a re-sideload on Android. Devices running an older extension
  report no effects capability and their results carry `effects.tier: "none"` — honest
  degradation, never a silent gap.
- Docs: `docs/POLICY.md` §3 is replaced by a pointer here. `docs/PROTOCOL.md` gains the
  `classification`/`effects` blocks and the session-establishment message.
  `docs/DECISION_GUIDE.md` gains a "the action I want to take might be consequential" row.
  Per `CONTRIBUTING.md`, these ship in the same PR as the code.

---

## 9. Honest limits — what this does not protect against

1. **A page that wants the agent to click something.** Every page-asserted signal is forgeable.
   An adversarial page renders a bland button, issues no observable request, and mutates state
   over an already-open WebSocket. Nothing here sees it. The only defenses are caller scope and
   the audit log.
2. **The first consequential action in a fresh flow.** Effects are post-hoc; flow elevation needs
   a prior observation or a page-context match.
3. **A prompt-injected agent under `redeem: "agent"`.** It redeems its own token. Default for
   ergonomic reasons. **Update, same day:** there is no channel-based alternative -- `redeem:
   "unredeemable"` (renamed from `out_of_band`) does not add a channel, it removes the ability to
   redeem the token at all; see `docs/designs/approval-channel-options.md` §0. The only way to
   reduce this risk for a given task is a narrower `write` scope.
4. **Server-side consequences behind an idempotent-looking GET.** A `GET /admin/promote?u=x` is
   indistinguishable from a page load at the effects layer.
5. **Requests the collector cannot attribute.** `chrome.webRequest` reports `tabId: -1` for
   service-worker-initiated requests; WebSocket *frames* are invisible (only the handshake is
   observed). Tier `navigation` is blind to all XHR/fetch.
6. **Concurrent human activity.** This is a co-working system by design. A human click inside the
   agent's observation window is attributed to the agent. Reported as attribution confidence,
   not solved.
7. **Canvas-rendered pages.** No DOM semantics → `unknown` on every action. The same structural
   limit `docs/DECISION_GUIDE.md` documents for `read`.
8. **Anything on Android requiring CDP.** `chrome.debugger` is genuinely absent; tier `cdp` is
   desktop-only. Also: `chrome.debugger` attach was measured to fail on M365-origin tabs inside an
   enterprise tenant — that limit applies here too.
9. **A caller that declares broad scope, or a hub operator who disables the classifier.** The
   bridge supplies mechanism. It cannot supply judgment on the caller's behalf — which is the
   point of §5, and also its cost.
10. **`scope.py` (Candidate C) is now implemented** (a later PR than the one that originally wrote
    this section). Session establishment, narrow-only/seal-on-first-read enforcement, and
    write-scope denial are wired into `PolicyEngine.evaluate`/`Hub.send_command` and surfaced on
    the CLI (`abb session-establish`/`abb session-narrow`), the MCP server
    (`browser_establish_session`/`browser_narrow_scope`), and the Amplifier tool module. This is
    the one page-immune *prevention* this design promises (§4: "C is the only page-immune
    prevention"). **Still deferred:** `redeem: "out_of_band"`'s actual redemption channel (`abb
    approve`, §15 step 6 — `redeem` can be *declared* `"out_of_band"` on a session, but nothing
    yet redeems a token that way) and the operator-configured screening hook (Candidate B). Also
    deferred: enforcing `SessionScope.read` against any command — the field exists and
    participates fully in `narrow()`'s validation, but `PolicyEngine.evaluate` only ever consults
    `write` (matching this document's own §12 decision flow, which never mentions a read-scope
    check). See the implementing PR's report for the exact stopping point.
11. **Cases 2-3 of the §14.1 regression suite are proven against a fixture built from an OBSERVED
    URL shape and title template, not a fully-reproduced incident page.** A read-only `snapshot`
    of the maintainer's real, already-connected browser (a DIFFERENT repository than the
    incident, taken without clicking or otherwise changing any state) recorded:

        url:   https://repos.opensource.microsoft.com/orgs/microsoft/repos/amplifier-app-wiki-weaver/jit
        title: microsoft/amplifier-app-wiki-weaver repository | Microsoft Open Source

    This confirms, as OBSERVED fact rather than guesswork, that the flow URL has the shape
    `.../orgs/{org}/repos/{repo}/jit` and the page title follows the template `{org}/{repo}
    repository | Microsoft Open Source`. `tests/test_gate_elevation.py`'s `FLOW_URL`/`PAGE_TITLE`
    now use this observed shape (with a neutral placeholder org/repo — the real names carry no
    test-relevant signal and this project is headed for public release).

    **Two facts remain genuinely unverified**, and nothing in the test suite claims them: whether
    the elevation click itself issues an observable non-GET request (case 2's trigger is still a
    synthetic `ObservedRequest`, not a captured one), and the page's exact heading (`<h1>`/`<h2>`)
    structure (case 3's heading text is still synthetic). Both require actually clicking through
    the JIT-elevation flow, which the operational instructions for the PR that captured the
    URL/title explicitly prohibited (read-only observation only, no state changes to the
    maintainer's live hub/browser). **What would settle this:** drive the real flow once, with the
    effects collector enabled, and capture (a) the `effects` block from clicking through the
    flow's first step, and (b) the real `headings` from a `snapshot` taken on the bland-labeled
    page. Until then, the measured, verbatim-label case (case 1, `ELEVATE_LABEL` itself, plus the
    now-observed URL/title shape) is the most-verified-against-reality regression case; cases 2-3
    remain mechanism proofs on an observed-shape fixture.

---

# Part II — Implementation specification

## 10. Modules

| Path | Status | Purpose |
|---|---|---|
| `src/amplifier_browser_bridge/classify.py` | **new** | Pure classification. No I/O, no state, no `chrome`/network. `ActionDescriptor` → `Classification`. |
| `src/amplifier_browser_bridge/scope.py` | **new** | `SessionScope`: caller-declared write scope + `on_unknown` + `redeem`, with narrow-only enforcement. |
| `src/amplifier_browser_bridge/effects.py` | **new** | `EffectsReport` dataclasses + the hub-side parser for the extension's effects payload. |
| `src/amplifier_browser_bridge/policy.py` | **modify** | Keeps denylist / caches / confirmation lifecycle / kill switch. Delegates gate decisions to `classify` + `scope`. Re-exports `GateRule`/`GATE_RULES` for one release. |
| `src/amplifier_browser_bridge/hub.py` | **modify** | Session establishment; attach `classification`/`effects` to results; flow-elevation state; `confirm` redemption channels. |
| `src/amplifier_browser_bridge/client.py` | **modify** | `confirm()`, `establish_session()`. |
| `src/amplifier_browser_bridge/cli.py` | **modify** | `abb confirm <token>`. (`abb approve <token>` / `abb pending` were planned for the out-of-band channel here; that channel is CANCELLED -- see §17 -- so these were never built and will not be.) |
| `src/amplifier_browser_bridge/mcp_server.py` | **modify** | `browser_confirm` tool; surface `classification`/`effects` in tool results. |
| `extension/action_descriptor.mjs` | **new** | Pure. Element → descriptor fields. Zero `chrome.*`. Companion `.test.mjs`. |
| `extension/effects_collector.mjs` | **new** | Pure. Event accumulation + windowing + state-changing determination. Zero `chrome.*`. Companion `.test.mjs`. |
| `extension/injected.js` | **modify** | `snapshot()` nodes gain descriptor fields; new `describe(ref)` command. |
| `extension/background.js` | **modify** | Wire `chrome.webRequest`/`webNavigation`/`downloads`/`tabs` listeners into `effects_collector`; behavioral capability probe for the effects tier. |
| `extension/manifest.json`, `manifest.android.json` | **modify** | Permissions (§8). |

## 11. Interfaces

### 11.1 `classify.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal

Provenance = Literal["page", "browser", "caller", "external"]
Status = Literal["clear", "elevated", "unknown"]
ReasonCode = Literal[
    "ref_not_observed",
    "hint_stale",
    "descriptor_unavailable",
    "device_capability_missing",
    "no_page_semantics",
]

DEFAULT_THRESHOLD: int = 3


@dataclass(frozen=True)
class ActionDescriptor:
    """Everything known about a proposed action, before it executes.

    Fields sourced from the DOM are page-asserted and therefore forgeable
    (see design doc section 2). `url` and `origin` come from the hub's own
    observation of the tab (policy._tab_hosts) and are browser-asserted.
    """

    command: str
    # --- page-asserted ---
    label: str | None = None
    role: str | None = None
    tag: str | None = None
    input_type: str | None = None
    href: str | None = None
    href_cross_origin: bool | None = None
    form_method: str | None = None  # "get" | "post" | None
    form_action: str | None = None
    form_cross_origin: bool | None = None
    is_submit: bool | None = None
    page_title: str | None = None
    nearest_heading: str | None = None
    dialog_title: str | None = None
    # --- browser-asserted ---
    url: str | None = None
    origin: str | None = None
    # --- derived from prior browser-asserted effects (hub-supplied) ---
    flow_elevated: bool = False
    flow_elevated_by: str | None = None  # "observed_effect" | "page_context"

    @property
    def has_any_page_semantics(self) -> bool: ...


@dataclass(frozen=True)
class Signal:
    channel: str  # "label" | "page_context" | "url" | "element" | "flow" | "screen_hook"
    provenance: Provenance
    value: str | None
    matched: tuple[str, ...]
    weight: int


@dataclass(frozen=True)
class Classification:
    status: Status
    score: int | None
    threshold: int
    categories: tuple[str, ...]
    signals: tuple[Signal, ...]
    advisory: bool = True
    reason_code: ReasonCode | None = None

    def to_wire(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ClassifierProfile:
    """The replaceable default. Loadable from the same policy.json the
    denylist uses (key: "classifier"), so the taxonomy is configuration,
    not a fact baked into dispatch (design doc section 13)."""

    threshold: int = DEFAULT_THRESHOLD
    families: dict[str, tuple[str, ...]] = field(default_factory=...)
    phrases: dict[str, tuple[str, ...]] = field(default_factory=...)
    url_patterns: dict[str, tuple[str, ...]] = field(default_factory=...)

    @staticmethod
    def load(path: str | Path | None = None) -> ClassifierProfile: ...


def classify(
    descriptor: ActionDescriptor,
    profile: ClassifierProfile,
    *,
    extra_signals: tuple[Signal, ...] = (),
) -> Classification: ...
```

**Scoring table** (`classify` is a pure sum over channels, then compare to `threshold`):

| Channel | Condition | Weight |
|---|---|---|
| `label` | ≥2 distinct family terms in the accessible name | 3 |
| `label` | exactly 1 family term | 1 |
| `label` | an exact high-confidence phrase (the existing tuned phrase lists) | 3 |
| `url` | committed URL path matches a category URL pattern | 2 |
| `page_context` | ≥2 family terms across `page_title` + `nearest_heading` + `dialog_title` | 2 |
| `page_context` | exactly 1 family term | 0 |
| `element` | `input_type == "file"` | 3 |
| `element` | `is_submit` and `form_method == "post"` | 2 |
| `element` | `form_cross_origin` or `href_cross_origin` | 1 |
| `flow` | `flow_elevated` is true | 3 |
| `screen_hook` | external hook returned an escalation | 3 |

Weights are additive; multiple channels stack. Threshold 3.

**Family lexicon** (`privilege`, the family that fixes the measured case):
`elevate` · `elevation` · `escalate` · `administrator` · `admin` · `owner` · `maintainer` ·
`privilege` · `sudo` · `root` · `role` · `permission` · `permissions` · `grant` · `revoke` ·
`collaborator` · `member` · `access` · `just-in-time` · `jit`

Matching is word-boundaried, case-insensitive, and stem-tolerant (`permission`/`permissions`
count once as one term). The 1-term/2-term split is what keeps common words honest: `role` alone
scores 1 and does not gate; `elevate` + `administrator` scores 3 and does.

**Required behavior for `unknown`:** if `not descriptor.has_any_page_semantics` — no label, no
role, no tag, no page context — `classify` returns `status="unknown"`, `score=None`, and a
`reason_code`. It must **never** return `clear` for an absent descriptor. This is the fail-loud
requirement (§6) and is the single most important assertion in the module's tests.

### 11.2 `scope.py`

```python
@dataclass
class SessionScope:
    session_id: str
    read: Literal["*"] | tuple[str, ...] = "*"
    write: Literal["*"] | tuple[str, ...] = "*"  # origins, e.g. "https://github.com"
    on_unknown: Literal["allow", "gate", "deny"] = "allow"
    redeem: Literal["agent", "unredeemable"] = "agent"  # renamed from "out_of_band" -- see §17
    unattended: bool = False
    _sealed: bool = False  # set True on first page-content ingest

    def permits_write(self, origin: str | None) -> bool: ...
    def narrow(self, **kwargs: Any) -> None:
        """Apply a strictly-narrowing update. Raises ScopeError on any widening
        attempt, and on ANY change once `_sealed` is True."""

    def seal(self) -> None: ...
```

**The load-bearing rule:** `narrow()` rejects widening always, and rejects *all* changes once
sealed. `Hub` calls `seal()` the first time a session receives page content
(`read`/`snapshot`/`vision_read`/`tabs` result). After that point a prompt-injected agent cannot
widen its own grant — this is the write-path analogue of the capability binding `policy.py`
already applies to the read path.

`write: "*"` is the default. The mechanism exists; the policy is the caller's.

**Constraints on `narrow()`:** `write` may only go from `"*"` to a tuple, or to a strict subset of
the existing tuple. `on_unknown` may only move `allow → gate → deny`. `redeem` may only move
`agent → unredeemable` (renamed from `out_of_band` -- see §17). `unattended` may only go
`False → True`.

### 11.3 `effects.py`

```python
EffectsTier = Literal["cdp", "webrequest", "navigation", "none"]


@dataclass(frozen=True)
class ObservedRequest:
    method: str
    url: str
    type: str | None  # webRequest resourceType
    cross_origin: bool


@dataclass(frozen=True)
class ObservedNavigation:
    url: str
    transition_type: str | None  # "form_submit" | "link" | "reload" | ...
    origin_changed: bool


@dataclass(frozen=True)
class EffectsReport:
    tier: EffectsTier
    window_ms: int
    attribution: Literal["time_window", "none"]
    requests: tuple[ObservedRequest, ...] = ()
    navigations: tuple[ObservedNavigation, ...] = ()
    downloads: tuple[str, ...] = ()
    tabs_opened: tuple[int, ...] = ()

    @property
    def state_changing(self) -> bool:
        """True if ANY of: a non-GET/HEAD request; a navigation whose
        transition_type is 'form_submit'; a download started; a tab opened.
        Browser-asserted throughout -- see design doc section 2."""

    @staticmethod
    def from_wire(payload: dict[str, Any] | None) -> EffectsReport: ...
    def to_wire(self) -> dict[str, Any]: ...
```

An absent payload parses to `EffectsReport(tier="none", window_ms=0, attribution="none")`. Never
`None`, never omitted from the result — a caller must always be able to distinguish "observed
nothing" from "could not observe."

### 11.4 `policy.py` changes

```python
class PolicyEngine:
    def __init__(
        self,
        audit: AuditLog,
        denylist: Denylist | None = None,
        gate_rules: tuple[GateRule, ...] = GATE_RULES,  # retained; deprecated
        confirmation_ttl: float = DEFAULT_CONFIRMATION_TTL_SECONDS,
        profile: ClassifierProfile | None = None,  # new
    ) -> None: ...

    def evaluate(
        self,
        target: Target,
        command: str,
        args: dict[str, Any],
        *,
        skip_gate: bool = False,
        scope: SessionScope | None = None,  # new, defaults to permissive
    ) -> PolicyDecision: ...

    # New observation intake, symmetric with note_snapshot/note_ref:
    def note_effects(
        self, device_id: str, tab_id: int | None, effects: EffectsReport, url: str | None
    ) -> None:
        """Browser-asserted. If effects.state_changing, mark (device_id, tab_id)
        flow-elevated with reason 'observed_effect'. Cleared when the tab's
        committed ORIGIN changes (note_tab_url), or by a redeemed
        confirm_scope='flow' token, or after FLOW_TTL_SECONDS."""

    def note_page_context(
        self, device_id: str, tab_id: int | None, url: str | None, title: str | None, headings: list[str]
    ) -> None:
        """Page-asserted, weak. Marks flow-elevated with reason 'page_context'
        when >=2 family terms appear. Fed from snapshot/read results."""


@dataclass
class PolicyDecision:
    status: Literal["allow", "deny", "gate"]
    reason: str | None = None
    category: str | None = None
    token: str | None = None
    detected: dict[str, Any] | None = None
    # --- new, all optional ---
    classification: Classification | None = None
    redeem: Literal["agent", "unredeemable"] = "agent"  # renamed from "out_of_band" -- see §17
    confirm_scope: Literal["action", "flow"] = "action"
    reason_code: str | None = None
    expires_at: float | None = None
```

`FLOW_TTL_SECONDS = 900` (15 min). Flow elevation is per `(device_id, tab_id)`.

`STATE_CHANGING_COMMANDS = frozenset({"click", "type", "key", "navigate"})` — the set that scope
enforcement and effects collection apply to.

### 11.5 Extension

`snapshot()` nodes gain (additive to `{ref, role, name, tag, value?, input_type?}`):

```js
{ href, href_cross_origin, form_method, form_action, form_cross_origin,
  is_submit, nearest_heading, dialog_title }
```

`snapshot()`'s top-level result gains `page_title` (already has `title` — reuse it) and
`headings: string[]` (the `h1`/`h2` text, capped at 8 entries, 200 chars each).

New command `describe` (`PAGE_WORLD_COMMANDS`): `args.ref` → the full descriptor for one element.
Exists for the `unknown` path — a caller that gets `reason_code: "ref_not_observed"` can obtain a
descriptor without a full re-snapshot. It never auto-fires; the caller invokes it (design doc §13:
no silent escalation).

`background.js` effects collection:
- Behavioral probe at startup determines the tier; reported in the `hello` capability set as
  `effects_tier`. Probe by *invoking* (`chrome.webRequest.onBeforeRequest.addListener` inside
  try/catch), never `typeof` — Edge Android ships present-but-nonfunctional APIs.
- Around each `STATE_CHANGING_COMMANDS` dispatch: open a collection window on the acting tab,
  execute, hold the window `EFFECTS_WINDOW_MS = 1500` after the command's own result, close,
  attach `effects` to the result envelope.
- Listeners are filtered to the acting tab id. Cross-tab noise is dropped.
- **Never call `chrome.sidePanel.getLayout()`** (crashes Edge Android). Unrelated but restated
  because this touches the capability probe.

## 12. Decision flow

`Hub.send_command` remains the single choke point. Order inside `PolicyEngine.evaluate`:

```
1. kill switch active?                      -> deny  (unchanged, checked first)
2. resolve url_context / host               (unchanged)
3. denylist match (with discarded-auth exception)?  -> deny  (unchanged)
4. command in STATE_CHANGING_COMMANDS?
      no  -> allow, classification=None          # read/tabs/screenshot are not classified
5. scope.permits_write(origin)?
      no  -> deny, reason_code="out_of_scope", specific error text
6. skip_gate (post-confirmation re-dispatch)?
      yes -> allow  (unchanged; denylist above already ran)
7. build ActionDescriptor:
      caller args.label / args.input_type  (caller-supplied always wins)
      -> hub ref-hint cache (_resolve_ref_hint, existing staleness guard)
      -> hub page-context cache
      -> hub _tab_hosts for url/origin (browser-asserted)
      -> flow_elevated from note_effects / note_page_context
8. classify(descriptor, profile)
9. status == "unknown":
      on_unknown "allow" -> allow, classification attached, audit policy_unclassified
      on_unknown "gate"  -> gate,  category=None
      on_unknown "deny"  -> deny,  reason_code="unclassifiable"
10. status == "elevated" -> gate
      confirm_scope = "flow" if the ONLY contributing channel was `flow`,
                      else "action"
      redeem = scope.redeem
      audit policy_gated with full signal detail
11. otherwise -> allow, classification attached
```

Response path, in `Hub._ingest_result` (extending the existing method):

```
result envelope arrives
  -> existing: attach/detach, tabs filtering, note_tab_url, note_snapshot, note_ref
  -> new: EffectsReport.from_wire(env.get("effects"))
          policy.note_effects(device_id, tab_id, report, url)
          audit "action_effects" when report.state_changing
  -> new: policy.note_page_context(...) from snapshot/read results
  -> new: scope.seal() on first page-content result for this session
  -> attach classification (carried on the in-flight command) + effects to the
     returned envelope
```

**Ordering constraint:** `note_effects` must run *before* the envelope is handed to the pending
future, so the *next* command in that tab already sees `flow_elevated`.

## 13. New audit events

Add to `audit.py`'s event table:

| Event | When |
|---|---|
| `policy_unclassified` | `classify` returned `unknown`; includes `reason_code` and the `on_unknown` handling applied |
| `policy_scope_denied` | a state-changing command was refused by session write scope |
| `policy_scope_narrowed` | a session narrowed its scope; includes before/after |
| `policy_scope_sealed` | first page content ingested; the session can no longer widen |
| `action_effects` | a command's observed effects, when `state_changing` is true. **This is the attribution record — the fix for D3.** Includes method+URL of every non-GET request. |
| `flow_elevated` | a tab entered elevated-consequence context; includes the trigger (`observed_effect` / `page_context`) |
| `flow_cleared` | flow elevation cleared; includes reason (`origin_change` / `confirmed` / `ttl`) |

`policy_gated` gains the full `classification.to_wire()` payload, so the audit log records *why*,
not just *that*.

## 14. Success criteria

### 14.1 The regression test — `tests/test_gate_elevation.py`

This file exists specifically to prove the measured failure is fixed, and must not be merged into
`test_policy.py`. Both the consequential button and its bland sibling must be caught.

```python
ELEVATE_LABEL = "Elevate bkrabach to Administrator"  # measured, verbatim
BLAND_LABEL = "Next"  # measured, verbatim
FLOW_URL = "https://repos.opensource.microsoft.com/..."  # see 14.2
```

Required cases:

1. `test_elevate_to_administrator_gates_on_label_alone`
   A `click` on a ref whose cached snapshot node is
   `{name: ELEVATE_LABEL, role: "button", tag: "button"}`, on `FLOW_URL`, with **no** URL-pattern
   match and **no** flow elevation → `decision.status == "gate"`,
   `"permission_change" in classification.categories`, `score >= threshold`.
   *This is the exact configuration that failed in production.* Asserting "no URL match" is what
   proves the `combine="all"` conjunction is genuinely gone.

2. `test_bland_next_gates_when_flow_elevated_by_observed_effect`
   Drive the real sequence through the hub with the existing `FakeDeviceSocket` pattern:
   `snapshot` → `click(ref_a)` whose result carries
   `effects: {tier: "webrequest", requests: [{method: "POST", ...}]}` → `click(ref_next)` where
   `ref_next`'s label is `BLAND_LABEL` → second click returns `needs_confirmation` with
   `confirm_scope == "flow"`. **Browser-asserted trigger; the label contributes nothing.**

3. `test_bland_next_gates_when_flow_elevated_by_page_context`
   Same, but elevation comes from a `snapshot` result whose `title`/`headings` carry ≥2
   `privilege`-family terms. Page-asserted trigger — the test must also assert
   `classification.advisory is True`.

4. `test_flow_confirmation_covers_subsequent_bland_clicks_but_not_the_elevate_click`
   Redeem the flow token → the next `BLAND_LABEL` click is allowed → an `ELEVATE_LABEL` click
   **still gates**, because its score reaches threshold without the `flow` channel.
   *This is the anti-approval-nightmare proof: one prompt to enter the flow, one for the act.*

5. `test_elevate_click_effects_are_reported_and_audited`
   Even with the gate bypassed (`skip_gate=True`, i.e. post-confirmation), the result carries an
   `effects` block naming the POST, and an `action_effects` audit event is written.
   *Directly asserts the "no indication anything happened" defect is closed.*

6. `test_cookie_banner_allow_does_not_gate`
   `{name: "Allow", role: "button"}` on an ordinary URL, no flow elevation → `allow`.
   The false-positive floor. If this fails, the gate will be disabled and protect nothing.

7. `test_no_descriptor_is_unknown_not_clear`
   `click` on a never-snapshotted ref → `classification.status == "unknown"`,
   `reason_code == "ref_not_observed"`, and under default `on_unknown="allow"` the command
   proceeds. Under `on_unknown="gate"` the same input returns `needs_confirmation`.

8. `test_out_of_scope_click_is_denied_with_specific_error`
   Session narrowed to `write=("https://github.com",)`; a `click` on `FLOW_URL` →
   `ok: false`, `reason_code == "out_of_scope"`, and the error names the origin.

9. `test_sealed_session_cannot_widen_scope`
   Narrow → ingest a `read` result → attempt to widen → `ScopeError`, audit
   `policy_scope_sealed`.

### 14.2 Required evidence before merge

Cases 1, 6, 7, 8, 9 are fully determined by the labels the maintainer measured and need no
external validation.

Cases 2, 3, 4, 5 depend on facts about the real page that **have not been verified in this
design** and must not be assumed:

- the exact `FLOW_URL`,
- whether the elevation click issues an observable non-GET request (determines whether case 2's
  trigger is real or only synthetic),
- the page's actual `<title>`/`h1` text (determines whether case 3's trigger fires in production).

The builder must capture these from the live page — a `snapshot` and a `click` with the effects
collector enabled — and record the capture in the PR as evidence. If the elevation click turns out
to issue **no** observable request, case 2's synthetic fixture still proves the mechanism, but the
design doc's §9 limits list must be amended to say so, and case 3 becomes the only live trigger
for the bland-label path. **Do not paper over this with a fixture that asserts a fact nobody
observed.**

### 14.3 Other gates

- `pytest tests/` and `pytest modules/tool-browser-bridge/tests/` pass, with the two rewritten
  tests from §8 and their replacements.
- `ruff format --check .`, `ruff check .`, and `pyright` clean (run `pyright` from a shell with
  this repo's `.venv` active — see `CONTRIBUTING.md`).
- `node --test extension/*.test.mjs` passes, including new
  `action_descriptor.test.mjs` and `effects_collector.test.mjs`.
- `node --input-type=module --check` passes for every modified extension file.
- `classify.py` has **zero** imports from `hub`, `policy`, `aiohttp`, or any model SDK. It is a
  pure function module and its tests run with no fixtures.
- `docs/POLICY.md`, `docs/PROTOCOL.md`, and `docs/DECISION_GUIDE.md` updated in the same PR
  (`CONTRIBUTING.md`: "these documents are the contract, not commentary on it").
- The extension carries **zero** site knowledge and zero policy: `action_descriptor.mjs` and
  `effects_collector.mjs` contain no domain names, no category names, and no thresholds. All
  scoring lives in the hub.

## 15. Build order

Each step is independently shippable and independently valuable.

1. **Effects collection + attribution (D3).** Extension collector, `effects.py`, `effects` on
   every state-changing result, `action_effects` audit. **Highest value per unit of work; the
   only page-immune signal; ship first even if nothing else does.**
2. **`classify.py` + descriptor enrichment (D1).** Scoring, families, `unknown` as a real state.
   Retires `GATE_RULES` behind a re-export.
3. **Confirmation redemption surfaces (D2, part 1).** `abb confirm`, `client.confirm()`,
   `browser_confirm` MCP tool. Without this, every gate is still a dead end.
4. **Flow elevation.** Wires 1 and 2 together; makes bland labels catchable.
5. **`scope.py` (C).** Session establishment, narrow-only, `on_unknown`, seal-on-first-read.
   **Done** — see §9 item 10. `write`-scope enforcement is wired into `PolicyEngine.evaluate`
   and surfaced on all three agent surfaces (CLI/MCP/tool module). `read`-scope enforcement
   remains open (mechanism present, not yet a consumer -- see `scope.py`'s own docstring).
6. **~~`redeem: out_of_band` (D2, part 2). `abb approve`, the separate redemption channel.~~
   CANCELLED (2026-07-26, same day).** A live experiment showed the strongest candidate channel
   could be driven by the very agent it needed to exclude via `chrome.debugger`, and the simpler
   fix (narrow the session via step 5, already done) was available the whole time. See
   `docs/designs/approval-channel-options.md` §0 for the decision, the evidence, and what would
   reopen it. `redeem: "out_of_band"` is renamed `redeem: "unredeemable"` throughout the
   implementation (§17) -- it remains a real, permanent session setting; it is simply no longer a
   placeholder for a channel that was going to exist.
7. **Optional operator-configured screening hook (B).** Escalate-only, out-of-process, off by
   default. Only if a real consumer appears — otherwise skip it (design doc §13; `KERNEL_PHILOSOPHY`
   two-implementation rule).

Steps 1–3 are the critical path: after them, the measured failure is loud even when it is not
prevented, and a fired gate is no longer a dead end.

---

## 16. Post-implementation hardening (six-lens review panel, 2026-07-26)

A six-lens review panel returned one FAIL and five CONCERNs against the shipped implementation.
This section records the FAIL and the three unambiguous findings that were fixed, plus two
findings investigated to a partial/documented stopping point.

### 16.1 FAIL — the self-attestation hole (closed)

Four independent reviewers hit the same live exploit: `PendingConfirmation` had no `redeem`
field, and `_handle_agent_confirm` never checked one, so a session that declared
`redeem: "out_of_band"` (the name at the time -- renamed `"unredeemable"`, §17) was, in practice,
redeemable through the exact same `/agent` route as `redeem: "agent"` — the agent could mint the
token and confirm it itself, one layer below the incident this whole design exists to prevent.

**Fix:** `redeem` is now carried on `PendingConfirmation` (set from `scope.redeem` at the moment
a gate fires — this was ALSO missing: `PolicyDecision.redeem` was never actually populated from
`scope.redeem` anywhere in `evaluate()`, so the wire-level field silently stayed `"agent"`
regardless of what the session declared) and enforced at `PolicyEngine.consume_confirmation`'s new
`via` parameter. `Hub._handle_agent_confirm` calls `consume_confirmation(token, via="agent")` —
the only redemption route this codebase has, or ever will have (§17) — so a `redeem:
"unredeemable"` token is refused there, unconditionally, with a specific error naming why. A
wrong-channel attempt does NOT consume the token (no `used`, no delete) — marking it "used" would
claim a redemption that never happened; it stays unused until it naturally expires. Both the
refusal and the (new) audit event (`policy_confirmation_wrong_channel`) are unconditional,
regardless of who is asking — this is what also resolves the CLI finding below.

**Consequence, stated plainly and intentionally:** a `redeem: "unredeemable"` confirmation cannot
be redeemed AT ALL, by any route, ever. That is correct, deliberate, fail-closed behavior, and — as
of §17 — it is also the *permanent* behavior, not a placeholder for a channel that was going to be
built: a dedicated out-of-band channel was designed in full (`docs/designs/
approval-channel-options.md`) and then explicitly cancelled. The alternative ("leave a path open
because the real channel isn't built yet") was exactly the hole being closed here, and it remains
closed regardless of whether a channel is ever coming. See `tests/test_redeem_channel.py`.

### 16.2 The CLI confirm command (kept, relabeled, structurally barred)

Three reviewers converged: a CLI command with a shell on the hub host is not out-of-band with
respect to the agent, and shipping it without the enforcement above would look like coverage
where none exists. Resolution: **kept** `abb confirm` (removing it would leave `redeem: "agent"`
gates — the majority case — with no redemption surface at all, regressing D2). It is now
explicitly documented (see `cli.py`'s `confirm` command docstring) as a **host-local operator
convenience**, honestly labeled as out-of-band with respect to the *protocol*, not the *host* —
and it is **structurally barred** from redeeming `unredeemable` confirmations by the same
`consume_confirmation(..., via="agent")` enforcement in §16.1, because `abb confirm` reaches the
identical `Hub._handle_agent_confirm` route an agent's own `confirm` call reaches. This is code
enforcement, not a docstring promise — see `tests/test_redeem_channel.py`'s
`test_hub_agent_confirm_route_rejects_unredeemable_token_end_to_end`.

### 16.3 `permission_change` demotion reversed

`docs/designs/approval-channel-options.md` had floated moving `permission_change` to the post-hoc
side (§4 Candidate H, §6, §7 item 1), reasoning that the grant is revocable and attribution (D3)
is already fixed. Three reviewers rejected this: revocability of the *permission record* says
nothing about reversibility of *what an elevated actor does while holding it*. The measured
incident's own category (Administrator access) is exactly the class where the window between
grant and revocation is where the damage happens (deploy keys, collaborators, branch protection,
secret exfiltration) — none of which is undone by later revoking the permission. `permission_change`
**stays gated**. The reversal and full reasoning are now recorded in
`docs/designs/approval-channel-options.md` §3.1's table, §6, and §7 item 1, specifically so a
future reader does not re-derive the same (wrong) conclusion. No code changed for this item — the
demotion was only ever a design-doc proposal, never implemented in `classify.py`/`policy.py`.

### 16.4 Flow-elevation lifetime bound

New finding (tester-breaker F5): `FLOW_TTL_SECONDS` was a purely idle-gap bound — it resets on
every triggering observation (`note_effects`/`note_page_context`), so a flow kept alive by
ordinary, continuing activity (exactly the shape of a multi-step enterprise wizard — and the
measured incident's own URL is same-origin across every step, so origin-change never clears it
either) never actually timed out, no matter how long the episode had been running. A human
approving one low-stakes action early in a long desktop session (measured, `SCRATCH.md` R5: 142
minutes, zero gaps) could have that approval's *ambient flow context* still contributing a `flow`
signal to classification arbitrarily late in the same session.

**Fix:** `FLOW_MAX_LIFETIME_SECONDS = 1800.0` (30 min, 2x `FLOW_TTL_SECONDS`) — an ABSOLUTE cap
measured from when the episode started (`started_at`), independent of how recently it was last
touched (`at`). `PolicyEngine._touch_flow` refreshes `at` on every observation but preserves
`started_at` for a still-live episode; `_flow_state`'s read path checks both bounds and expires on
whichever fires first. Both timestamps are hub-clock (`time.time()`), never page-supplied, so a
page can cause MORE triggering observations but cannot manufacture more elapsed time — this is
deliberately NOT page-authored, unlike everything `classify.py`'s label/page_context channels
score (§2's lemma). Once the absolute cap lapses, the tab is not permanently barred from flow
elevation — the very next triggering observation starts a genuinely new episode with its own fresh
cap. See `policy.py`'s `FLOW_MAX_LIFETIME_SECONDS` comment and `tests/test_flow_lifetime.py`
(including the minute-1-approve / minute-140-exploit shape, reproduced with a deterministic fake
clock).

### 16.5 F6 — session sealing serialization point (addressed)

Finding: nothing named the point at which two commands sharing a `session_id`, dispatched before
the first's response lands, are guaranteed to observe a consistent view of that session's
seal state — a second command's `evaluate()` could run concurrently with the first command's
`_ingest_result` (where `_maybe_seal_session` actually runs), and see pre-seal scope.

**Fix:** `Hub` now holds a per-`session_id` `asyncio.Lock` (`_session_locks`), acquired by
`send_command` for the full evaluate-through-dispatch span whenever a `session_id` is given
(`contextlib.nullcontext()` — no lock, no behavior change — when one isn't). This is the hub's one
named serialization point for session-scoped state: a second command for the same session cannot
begin `PolicyEngine.evaluate` until the first command's full round trip — including seal-on-
first-read — has completed. Commands with no `session_id` are entirely unaffected (fully
concurrent, matching every pre-`scope.py` call site); commands under *different* sessions are also
unaffected (independent locks). The cost is that two commands under the SAME session_id can no
longer pipeline concurrently — an acceptable trade for a security property, and consistent with
`scope.py`'s existing session-establishment discipline (one decision per session, not per click).
See `tests/test_hub.py`'s `test_concurrent_commands_on_same_session_serialize_through_the_session_lock`.

### 16.6 F7 — classifier label extraction gaps (investigated, not fixed this pass)

Finding: the D1 classifier fix (`classify.py`) addressed the `combine="all"` boolean conjunction
bug, but not label *extraction* itself. Four independent gaps were named, each capable of starving
BOTH the label and page_context channels simultaneously (since both score off the same
`descriptor.label`/`page_title`/`nearest_heading`/`dialog_title` text):

1. **`aria-label`-only buttons** with no visible text content.
2. **CSS `::before`/`::after` generated content** contributing to the rendered/visible label a
   human sees but absent from any DOM text node or attribute.
3. **Homoglyph substitution** (e.g. Cyrillic `А` U+0410 in `Аdmin` vs Latin `A` U+0041) — defeats
   word-boundaried regex matching outright, and is NOT fixed by Unicode NFKC normalization (the
   two characters are in different Unicode blocks entirely; NFKC does not fold cross-script
   confusables).
4. **Same-origin iframes** whose accessible-name-bearing content never reaches the top frame's
   descriptor unless the caller explicitly used `args.all_frames=true`/`frame_id` targeting.

**Status: investigated, not implemented this pass.** Gaps 1 and 4 are real but bounded and
tractable (an accessible-name computation that checks `aria-label` before falling back to text
content is a scoped fix in `injected.js`'s accessible-name resolution, and is very likely already
partially covered — needs verification against the actual accessible-name algorithm before
claiming a fix). Gap 3 (homoglyphs) requires either a confusable-skeleton normalization pass
(Unicode TR39) or a mixed-script detector — nontrivial, and risks false positives against
genuinely multilingual UIs if done carelessly. Gap 2 (CSS-generated content) is arguably
unfixable from `injected.js`'s DOM-only vantage point without a full computed-style + pseudo-
element text read, which is a meaningfully larger change. Per this task's instruction to "stop at
a coherent boundary" rather than leave something half-wired: no code changed for F7. This section
exists so the four gaps are recorded precisely enough that a follow-up pass can pick them up
without re-deriving them, and so nobody assumes D1's `combine="all"` fix silently covers them too.
