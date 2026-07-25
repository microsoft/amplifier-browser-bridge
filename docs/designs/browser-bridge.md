# Design: Cross-Device Agent ↔ Real-Browser Bridge

**Status:** draft for review
**Date:** 2026-07-25
**Working name:** `browser-bridge` (provisional — see Open Decisions)

---

## 1. What we're building

A system that lets an AI agent running on one device **observe and drive the user's real,
logged-in Microsoft Edge browsers on other devices**, over the user's own tailnet.

The agent is a **second operator in a live browsing session** — not a robot driving its own
disposable browser. The human is present, or will be shortly.

### Goals

1. Agent on device A drives Edge on devices B, C, D (macOS, Windows, Android).
2. Rides the user's real logged-in sessions. Never touches credentials.
3. Full steering: read, click, type, navigate, open/close tabs and windows.
4. Multiple devices, multiple profiles, multiple windows, many tabs — all addressable.
5. Acts on tabs that are not focused, without stealing focus.
6. **Broad access by default.** Not an approval nightmare.
7. Usable by Amplifier agents *and* by any MCP-speaking agent.
8. Publishable as a Microsoft open-source project.

### Non-goals

- Cross-browser support. **Edge only.** (Removes the MV3/WebExtension portability tax.)
- Headless/disposable browser automation. That's Playwright's job; see §9.
- iOS. Microsoft documents no extension API surface for it. Revisit when they do.
- Being a general-purpose scraping framework. Site knowledge lives in the caller.

---

## 2. Evidence base

Every load-bearing constraint below was **measured on the user's own hardware**
(2026-07-25), not taken from documentation. Full data in `../../SCRATCH.md` and
`probe-kit/results/`.

| Finding | Measurement |
|---|---|
| Plain `ws://` to a tailnet IP from an MV3 service worker | **Works.** Edge 150 macOS 407ms; Edge Canary Android 19ms |
| MagicDNS name resolution | **Unreliable per-device.** Works on Android, fails on macOS (resolves to public Funnel ingress) |
| `getBoundingClientRect()` on hidden/minimized tabs | **Never zero.** Real geometry in all states |
| `Page.captureScreenshot{fromSurface:true}` on minimized window | **Works, 41–81ms.** Does not hang |
| Element-level click dispatch on background tab | **Fires**, all states, both platforms |
| Desktop MV3 service worker lifetime | **660 heartbeats / 165 min / zero gaps** |
| Android service worker under screen-off | **Never evicted.** `restartCount: 0`, timer keeps ticking (throttled 15s→25.5s) |
| Android socket under screen-off, no battery exemption | Dies in ~8s. **509s dark, zero reconnects** |
| Android socket under screen-off, **with** battery exemption | 5 dark windows of **43–133s**, each self-reconnecting in **<2s** |
| `chrome.debugger` on Android | **Genuinely absent** |
| `chrome.windows` / `captureVisibleTab` on Android | **Present and working** — Microsoft's docs say otherwise |

Two documentation corrections worth carrying: Microsoft's API matrix wrongly lists
`chrome.windows` and the capture APIs as desktop-only. Both work on Edge Android.
**Feature detection must be behavioral, never `typeof`** — Edge Android also ships APIs that
are present but silently non-functional, and one (`sidePanel.getLayout()`) crashes the browser.

---

## 3. Architecture

```
┌─ device A: agent host (spark-1) ────────────────┐
│                                                  │
│   Agent(s) ──► Agent Surface (lib/CLI/MCP/tool)  │
│                        │                         │
│                        ▼                         │
│                 ┌─────────────┐                  │
│                 │     HUB     │                  │
│                 │ registry    │                  │
│                 │ queue/device│                  │
│                 │ policy      │                  │
│                 │ audit       │                  │
│                 └──────┬──────┘                  │
│                        │ ws://  (listens)        │
└────────────────────────┼─────────────────────────┘
                         │  tailnet (WireGuard)
        ┌────────────────┼────────────────┐
        │                │                │
   ┌────▼────┐      ┌────▼────┐      ┌────▼────┐
   │ Edge    │      │ Edge    │      │ Edge    │
   │ macOS   │      │ Win11   │      │ Android │
   │ (ext)   │      │ (ext)   │      │ (ext)   │
   └─────────┘      └─────────┘      └─────────┘
   dials OUT        dials OUT        dials OUT
```

Three components. Each is independently replaceable.

### 3.1 Extension (one build, all platforms)

MV3. Dials **out** to the hub — the browser device never needs an inbound port, works behind
NAT, survives roaming.

Responsibilities, and nothing more:
- Behavioral capability probe at startup → report a capability set to the hub
- Execute targeted commands against a specific tab
- Stay connected; reconnect with **single-flight + exponential backoff + jitter**
- Be **stateless and re-hydratable** — the hub is the source of truth for everything

The extension carries **zero site knowledge and zero policy**. Both live in the hub.

> The probe fired 3 `hello`s in 5 seconds on reconnect because an alarm timer, a backoff retry,
> and a startup handler all raced. In production that turns every laptop-lid-open into a
> reconnect storm. Single-flight is not optional.

### 3.2 Hub (on the agent host)

The hub is separate from the agent for three reasons, each load-bearing:

1. **Multiple agents.** The user wants "any agent," not just Amplifier. Device state can't
   live inside one agent session.
2. **The queue must outlive the agent.** Mobile devices are reachable in windows, not
   continuously. A command issued now may execute in 90 seconds.
3. **Policy must be outside the model's reach.** This is the structural defense against
   prompt injection — see §6.

Holds: device registry, per-device command queue, policy engine, audit log, correlation/routing.

### 3.3 Agent surface

Logic lives in **one home** — the Python lib. Everything else is a thin adapter.

| Level | Consumer | Build? |
|---|---|---|
| Python lib | the single home for all logic | yes |
| CLI | humans, scripts; everything shells out to it | yes |
| MCP server | **"or maybe just any agent"** | yes |
| Amplifier tool module | Amplifier agents | yes |
| `.dot` attractor pipelines | — | **no** — no named consumer |

---

## 4. Transport

**Plain `ws://<agent-host-tailnet-ipv4>:<port>`. No TLS.**

Justified by measurement, not preference:

- Plain `ws://` from an MV3 service worker to a `100.64.0.0/10` address **works** on both
  Edge desktop and Edge Android. This was the blocking unknown; no documentation answered it.
- Confidentiality and integrity are already provided by WireGuard.
- Adding TLS would require `tailscale cert`, which publishes machine names to Certificate
  Transparency logs, expires every 90 days, and requires the **MagicDNS name** — which we
  measured as broken on the user's MacBook.

**Address by tailnet IP literal, never MagicDNS.** MagicDNS worked on Android and failed on
macOS in the same tailnet at the same moment. IP literals are the only thing that works
everywhere.

`tailscale serve` remains an *optional* TLS path — it does pass WebSocket upgrades and injects
`Tailscale-User-Login` identity headers, which is free cryptographic per-user identity. Kept as
a documented option, not the default.

### Authentication

Two layers, because one is insufficient:

1. **Tailscale ACLs** — the outer boundary. Deny-by-default; pin which devices may reach the
   hub port at all.
2. **Per-device token** — because tailnet identity is per-**device**, not per-**application**.
   Without this, any other extension or local process on an authorized device reaches the hub
   with the same identity.

Keepalive: hub pings every 20s, extension heartbeats every 15s. Measured to hold a desktop
service worker alive indefinitely (165 min, zero gaps).

---

## 5. The three-tier connectivity model

This is not an implementation detail. It is a first-class concept the agent surface must
expose honestly.

| Tier | Reality | Agent-visible behavior |
|---|---|---|
| **live** | Desktop. Persistent connection. | Execute now |
| **intermittent** | Mobile + battery exemption. Dark 43–133s, self-heals <2s. | Queue; drains next window |
| **dormant** | Mobile, default settings. Dark 8.5min+, no self-recovery. | Queue; drains when device wakes |

**A command to a non-live device returns immediately** with `{status: "queued", tier,
last_seen, est_drain}`. It does **not** silently block. A tool call that hangs for two minutes
is indistinguishable from a broken system.

The battery-optimization exemption is an **onboarding requirement**, not a tip. It is the
difference between a device that is reachable within ~2 minutes and one that is unreachable
until touched. (The same conclusion the `fable-wa` project reached independently.)

Because the service worker never dies — only the socket does — queued state and identity
survive blackouts intact. Recovery is automatic.

---

## 6. Addressing and consent

### 6.1 Addressing

Every command carries an explicit target:

```
device_id / profile_id / window_id / tab_id  →  element_ref
```

This is the single biggest structural fix over the reference implementation, which had one
global "work tab" and no `tabId` parameter on any command. Without first-class addressing,
none of the multi-device / multi-profile / multi-window / selective-grant requirements are
reachable.

Element refs are stable within a snapshot, in the manner of Playwright MCP's `snapshot` and
agent-browser's `@e1` scheme.

### 6.2 Consent — broad by default, narrow by exception

The user's stated requirement is explicit: *"I generally want it to be able to access what I
access so that it can leverage/see what I've seen."* The design takes that seriously.

**Default: broad read.** Every tab the browser can see, the agent can see. No per-tab grants,
no per-session approval, no prompting to look at things.

**Denylist, not allowlist.** A small hand-maintained set of sensitive categories — financial,
healthcare, auth/OAuth consent screens, password managers. Denied tabs are **invisible**, not
merely unreadable: they do not appear in `tabs` output at all. (No public maintained list of
such domains exists; we maintain ~5 categories.)

**Confirmation gates ONLY on irreversible or world-visible actions:**
purchase/payment · send (mail, message, post) · delete · OAuth grant · file upload ·
account creation · permission change.

Everything else — reading, navigating, clicking, typing, opening and closing tabs — runs
free, fully audited.

**Capability binding is enforced in the hub, not in the prompt.** The agent names a target;
the hub validates that target against the current grant. A prompt-injected model can be made
to *want* a different tab. It cannot *address* one it was not granted. This is the structural
measure; prompt instructions are not a defense.

Revocation: disable the extension, or a hub-level stop-all. Both immediate.

### 6.3 Co-working etiquette

The agent shares a live session with a human. Rules that follow:

- **Never steal focus.** Never activate a tab merely to screenshot it — measurement confirms
  desktop CDP captures hidden and minimized windows directly, so activation is never required.
- **Never spawn windows or open tabs unasked.** Navigation reuses an attached tab.
- **Soft-detach CDP after idle** so the debugger banner clears while the human is just
  browsing.
- Full audit log; the human can see everything the agent did, after the fact.

---

## 7. Capability model

Probe behaviorally at startup. Report a capability set. Two observed profiles:

| | Desktop (Win/macOS/Linux) | Android |
|---|---|---|
| DOM read/write, element dispatch | yes | yes |
| Screenshots | **any tab**, incl. minimized (CDP) | **active tab only** (`captureVisibleTab`) |
| `chrome.windows` | yes | yes *(docs say no)* |
| `chrome.tabGroups` | yes | **no** |
| `chrome.debugger` / CDP | yes | **no** |
| Trusted input events (`isTrusted:true`) | yes, via CDP | **no** |
| Network interception, `Emulation.*` | yes, via CDP | **no** |

**The core vocabulary is uniform across both tiers.** Element-ref interaction and screenshots
work everywhere. CDP is an *enhancement* — trusted input, network interception, and
any-tab capture — not a requirement. One API surface, two capability tiers, honest degradation.

The one genuine asymmetry to document rather than hide: **screenshotting a non-active tab is
desktop-only.** On Android, "show me the page" means the tab the user is actually on. For
co-working on a phone that is acceptable; for unattended mobile driving it is a real limit.

### CDP: opt-in, not default

Attaching `chrome.debugger` triggers an unsuppressable yellow banner, conflicts with DevTools,
and its Cancel button detaches *every* debugger session. So: **injection-only by default;
escalate to CDP per-tab when trusted input or background capture is actually needed, and
soft-detach when idle.** This also keeps the default posture lower-risk for security review.

---

## 8. Failure discipline

- **Fail loud.** Every command returns `{ok, result}` or `{ok:false, error}`. No silent
  fallbacks, no synthetic results, no degraded-mode guessing.
- **Queued is a real state**, surfaced to the agent — never a hidden block.
- **Behavioral capability probes**, never `typeof` checks. Edge Android ships APIs that are
  present but non-functional; one crashes the browser.
- **Never call `chrome.sidePanel.getLayout()`** — confirmed browser-crashing bug on Edge
  Android (microsoft/MicrosoftEdge-Extensions#661).
- Poll-don't-sleep for waits (`waitFor`, `waitText`).

---

## 9. Positioning

**No vendor product does cross-device co-working.** Claude in Chrome, Edge Copilot Mode,
Gemini in Chrome, Comet, Codex for Chrome — in every case the model runs remotely but the
*control plane* is the extension on the machine the human is sitting at. Driving the browser
on a phone from a workstation is unclaimed.

**vs. Playwright MCP extension mode** (Microsoft's own): its every documented rationale is
authentication and environment fidelity — SSO reuse, extension-dependent pages, "automate
pages you already have open." The verb is *automate*; the human is assumed to have stepped
away. It has no notion of the tab the human is looking at, no handoff protocol, no
concurrent-actor arbitration, and no cross-device transport.

> Playwright MCP answers *"how does the agent get logged in?"*
> This answers *"how does the agent sit next to me while I work, from another machine?"*

Build separately. **Adopt its tool vocabulary** (`snapshot`/`click`/`type`/`navigate`/`tabs`/
`wait`) — it has effectively standardized the names models already expect. Free competence.

**vs. `browser-relay`** (MIT, closest prior art): near-identical purpose statement, but its
cross-device transport is a public Cloudflare Worker relay authenticated by a bearer Device ID
that its own README describes as *"a capability — anyone with it can control this browser."*
Replacing that with the user's own tailnet is the articulable improvement: no third-party
relay, no long-lived bearer capability on the wire, device-level ACLs, cryptographic identity.
It is desktop-only with no mobile leg.

---

## 10. Build order

Plumbing before polish. Prove each layer end-to-end before refining any of it.

1. **Hub + extension, one device, one tab.** `snapshot` → `click` → `read`. Proves the pipe.
2. **Addressing.** Multi-device, multi-window, multi-tab targeting. Proves the model that the
   reference implementation could not express.
3. **Tier model + queue.** Android in the loop; commands queue and drain. Proves mobile.
4. **Agent surface.** Lib → CLI → MCP → Amplifier tool.
5. **Policy engine.** Denylist, irreversible-action gates, audit log.
6. **CDP escalation.** Trusted input, background capture, soft-detach.

Steps 1–3 are the critical path. Everything else is additive.

---

## 11. Open decisions

1. **Name.** Must avoid "cowork" (collides with Anthropic's Claude Cowork).
2. **Denylist categories** — the concrete starting set.
3. **Irreversible-action list** — confirm the canonical set in §6.2.
4. **Distribution:** Edge Add-ons store vs. enterprise `ExtensionInstallForcelist` with a
   self-hosted update manifest. The latter bypasses store review entirely and is deployable
   via Intune. No precedent found either way for a browser-remote-control extension passing
   Edge Add-ons review.
5. **Android reach:** sideload-on-Canary (works today, proven) vs. applying to Microsoft's
   Edge Android allowlist (requires submitting QA test cases).

## 12. Known unknowns

- Occlusion behavior on **Windows** — different code path (`CalculateNativeWinOcclusion`) than
  macOS, untested.
- Whether policy-force-installed extensions skip the debugger banner. Single secondary source.
- Whether CRX sideload works on Edge Android **stable**, or Canary/Beta only.
- `document.hasFocus()` returned `true` even minimized and occluded — unexplained. Do not
  build focus-dependent logic on it.
- WebMCP (origin trial, Chrome 149) — an extension is the natural broker for exposing
  page-declared WebMCP tools to a *remote* agent, and nothing occupies that position. Read the
  W3C CG draft before designing against it.
