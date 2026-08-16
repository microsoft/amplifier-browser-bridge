# Wire Protocol

See [docs/DECISION_GUIDE.md](DECISION_GUIDE.md) for WHICH mechanism to reach for and when --
this document defines each command's exact semantics; that one is the decision layer on top.

This is the contract everything else in this repo depends on. `src/amplifier_browser_bridge/protocol.py`
is the Python-side source of truth for the names below (message types, command vocabulary,
capability keys); `extension/background.js` mirrors the same shapes by hand in JS (there is no
shared codegen in this phase -- keep the two in sync manually when either changes).

## Envelope

Every message on both routes is a single JSON object with this shape:

```json
{
  "v": 1,
  "id": "3b1b2e2a-...-uuid4",
  "type": "hello | heartbeat | result | event | command | ping | list_devices | poll | devices | error",
  "...": "type-specific fields"
}
```

- `v` -- protocol version. `1` in this phase.
- `id` -- correlation id (uuid4). A request and its eventual response/result share the same `id`.
- `type` -- the message's discriminator, from one of the vocabularies below.

## Two routes, two vocabularies

The hub exposes two WebSocket routes on the same port:

| Route | Who connects | Direction | Vocabulary |
|---|---|---|---|
| `/device` | Browser extension (dials **out**) | ext -> hub | `hello`, `heartbeat`, `result`, `event` |
| | | hub -> ext | `command`, `ping`, `error` |
| `/agent` | CLI / lib / (later) MCP server | agent -> hub | `list_devices`, `command`, `poll`, `confirm` |
| | | hub -> agent | `devices`, `result`, `error` |

The extension always dials **out** to the hub -- it never listens on an inbound port. This is
what lets it work behind NAT, survive network roaming, and require zero port-forwarding setup
on the browser's device (design doc §3.1).

## WebSocket message-size ceiling

Every leg of a round trip -- the extension's `/device` connection, the hub's `/agent` route, and
the agent/CLI client's own connection -- enforces the SAME explicit ceiling on a single message:
`MAX_WS_MESSAGE_BYTES` (`protocol.py`), **64MiB**.

Neither WebSocket library this codebase depends on was ever asked, on this protocol's behalf, how
big a single message may be -- each defaulted to its own generic value: `websockets` (the client's
library, `client.py`) defaults `max_size` to 2**20 (1MB); `aiohttp` (the hub's library, both
`web.WebSocketResponse()` routes in `hub.py`) defaults `max_msg_size` to 4MB. This protocol
legitimately carries payloads that exceed both -- a real page's MHTML capture (`mhtml`,
`Page.captureSnapshot`) inlines every stylesheet, font, and image the page references, and
routinely lands well past 1MB, sometimes past 4MB, for a genuinely heavy real-world page
(github.com, huggingface.co). Real-world finding: archiving four such pages at MHTML depth (see
"Browser-state archive" below) died with `websockets`' own `sent 1009 (message too big) frame
exceeds limit of 1048576 bytes` -- the CLIENT tripping its unset (so default) 1MB cap while
receiving the hub's relayed `mhtml` result.

The fix is one explicit, shared, BOUNDED constant applied to all three legs -- `websockets.connect
(..., max_size=MAX_WS_MESSAGE_BYTES)` in `client.py`, and `web.WebSocketResponse(...,
max_msg_size=MAX_WS_MESSAGE_BYTES)` on both hub routes -- so no leg silently enforces a smaller
limit than another, and a payload that clears one hop only to be rejected by the next is not a
failure mode this protocol has to reason about. Deliberately NOT unbounded: an unlimited
per-message size is an unlimited per-command memory allocation on both the hub and the client, for
a payload size the caller cannot predict or cap themselves.

A capture that still manages to exceed even this raised ceiling -- or hits any other
transport-level failure -- surfaces as an ordinary `HubError` to a direct `HubClient` caller. The
browser-state archive orchestrator (`archive.py`) goes one step further: every per-capture/
per-profile-item wire call goes through a single choke point (`_safe_command`) that turns a
`HubError` into an ordinary `{"ok": false, ...}` capture failure recorded in the archive manifest,
so ONE oversized or otherwise unreachable page fails only that capture -- never the whole archive
run. See "Browser-state archive" below and `archive.py`'s module docstring ("No silent partial
success").

---

## Device protocol (extension <-> hub)

### `hello` (ext -> hub)

Sent once, immediately after the WebSocket opens. Establishes device identity and capabilities.

```json
{
  "v": 1,
  "id": "...",
  "type": "hello",
  "device_id": "5e9f... (persisted uuid4, generated once, stored in chrome.storage.local)",
  "profile_id": "a1c2... (persisted uuid4, best-effort -- see addressing.py docstring)",
  "label": "edge-macos",
  "platform": "MacIntel",
  "capabilities": {
    "storage": true,
    "windows": true,
    "tab_groups": true,
    "debugger": false,
    "capture_visible_tab": true,
    "downloads": true,
    "alarms": true,
    "scripting": true,
    "history": true,
    "bookmarks": true,
    "sessions": true,
    "top_sites": true,
    "reading_list": true,
    "cookies": true
  },
  "protocol_version": 1,
  "commands": ["snapshot", "click", "type", "...", "reload"],
  "manifest_version": "0.5.0",
  "build_stamp": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "token": "shared secret, validated against the hub's TokenStore"
}
```

`label` is a coarse, human-friendly platform hint derived from the user agent
(`edge-macos` / `edge-windows` / `edge-linux` / `edge-android` / `edge-unknown`) -- not an
identity, just a display aid.

Every capability is the result of a **behavioral probe**: a real invocation in a try/catch,
never a `typeof` check. See `extension/background.js`'s `probeCapabilities()` for exactly what
each one calls. As of Phase 4, `debugger` is a real probe (`chrome.debugger.getTargets()` in a
try/catch) -- `true` on desktop builds using `manifest.json` (which requests the `debugger`
permission), `false` on Android builds using `manifest.android.json` (which deliberately omits
it -- `chrome.debugger` is genuinely absent on Edge Android; see design doc §2/§7) or on any
device where the API throws.

**`capture_visible_tab`/`scripting` can under-report `false` here** if no real tab existed yet
at connect time (a fresh browser launch can have zero tabs). See `capabilities_update` below for
the correction path -- don't treat a `false` here as final if the device has only just connected.

**`commands` and `manifest_version` (Tier 0 handshake -- the version-skew story).** Before this
existed, there was zero version awareness of the extension anywhere -- `protocol_version` was a
hardcoded constant nothing ever compared against anything, and version strings were hand-
maintained and had already drifted (`pyproject.toml` said `0.1.0`, `manifest.json` said `0.4.0`,
same repo, same day, no CI). `commands` fixes this by making the extension self-describe *what
it can actually do*, rather than a version number that can lie or decay: it is the literal,
current contents of `extension/background.js`'s `SUPPORTED_COMMANDS` set -- mirrored by hand
from this file's own `COMMANDS`, guarded against drift by
`tests/test_extension_command_parity.py`, the same discipline already applied to
`PAGE_WORLD_COMMANDS`.

The hub (`skew.py`) diffs a device's self-reported `commands` against its own `COMMANDS`
**bidirectionally**: `device_behind` (the extension is missing commands the hub knows -- the
common case, an unupdated install) and `hub_behind` (the device reports commands the hub
doesn't recognize -- the hub itself is older than the extension) are reported separately,
because they need different fixes. A device that omits `commands` entirely -- every extension
shipped before this feature, including whatever is connected to a hub the moment it's upgraded
-- is not treated as "supports nothing"; it is a distinct, definitively-stale state
(`SkewReport.known = False`), surfaced in every `devices`/`browser_devices` listing (see
"Reporting command-set skew to an agent" below) so an agent can see staleness *before* ever
failing a command. See `update_extension.py`'s module docstring for how the
`browser_update_extension` tool uses this to verify-or-guide an update.

`manifest_version` (`chrome.runtime.getManifest().version`) travels alongside `commands` purely
as **diagnostic color** -- useful for a human glancing at a `devices` listing -- and is **never**
consulted for skew detection; only the self-reported command set is authoritative, for exactly
the reason above (it cannot drift the way a version string already has).

**`build_stamp` (build-freshness handshake -- the sibling story `commands` does NOT cover).**
`commands` answers "what can this device DO" -- capability skew. It does NOT answer "IS this
device running the CURRENT build": a change that adds or removes zero commands (a bug fix, a UI
fix, a security fix -- this repo's own commits `6175ce4`/`cc140c5` touched only `options.js`) is
completely invisible to it. Proven live: pointing `browser_update_extension` at a real,
genuinely-outdated browser that had already caught up on commands returned `already_current:
true` -- false. `build_stamp` closes this gap: it is a SHA-256 over the bytes of every file this
build actually ships (`extension/background.js`'s `computeBuildStamp()`, hashing the same file
set `setup.py`'s `_EXTENSION_FILES` stages/zips -- see `build_stamp.py`'s module docstring for the
exact byte layout). Nobody has to remember to bump it, and unlike `manifest_version`, it cannot
lie: if a single byte of a single shipped file changes, the stamp changes.

The hub (`build_stamp.py`) compares a device's self-reported `build_stamp` against
`current_build_stamp()` -- this hub's own currently-loaded extension source, hashed the same way.
A device that omits `build_stamp` entirely -- every extension shipped before this feature -- is
not treated as "unknown, assume current"; it is a distinct, definitively-stale state
(`BuildFreshness.known = False`), the same discipline `commands` already applies. See "Reporting
build freshness to an agent" below, and `update_extension.py`'s module docstring for how
`browser_update_extension` now requires BOTH `commands` and `build_stamp` to check out before
reporting `already_current`.

A command the hub's own `COMMANDS` recognizes but a device has **positively** reported it
doesn't support (`known=True`, command absent from `commands`) now fails **fast, at the hub**,
before ever reaching the device:

```json
{
  "ok": false,
  "error": "'some_new_command' is not in this device's reported command set -- the EXTENSION is behind this hub. Update the extension (browser_update_extension tool, or see INSTALL.md's \"Updating\" section), then retry.",
  "reason_code": "device_command_unsupported"
}
```

This replaces the previous, useless failure mode: dispatching the command anyway and letting
`background.js`'s own if-chain fall through to a bare `{"ok": false, "error": "unsupported
command: X"}` with no hint the extension was stale. **Deliberately, this fast-fail does NOT
apply when a device's command set is unknown** (`known=False`, every currently-connected
pre-Tier-0 extension the moment this ships) -- see `skew.py`'s module docstring for why: it
would also block `reload` itself for every one of those devices, breaking the very mechanism
the automatic-update path depends on. An unknown-command-set device dispatches exactly as
before this feature existed.

### `capabilities_update` (ext -> hub)

```json
{
  "v": 1,
  "id": "...",
  "type": "capabilities_update",
  "device_id": "5e9f...",
  "capabilities": {"capture_visible_tab": true, "scripting": true}
}
```

Sent whenever the extension re-probes and finds a capability differs from what it last told the
hub -- see `background.js`'s `maybeReprobe()`, triggered on `chrome.tabs.onActivated`/`onUpdated`
(a real tab becoming available) and as a periodic fallback on the existing keepalive alarm. The
hub **merges** the reported keys into the device's existing capability set (`Hub.
_handle_device_message`'s `capabilities_update` branch) -- a partial update does not clobber
capabilities it didn't mention. This exists because a capability set that under-reports is worse
than none: an agent will route around a capability (e.g. `capture_visible_tab`) that actually
works, simply because it was told `false` once at `hello` time before any tab existed.

### `heartbeat` (ext -> hub)

```json
{"v": 1, "id": "...", "type": "heartbeat", "device_id": "5e9f...", "seq": 42}
```

Sent every 15s by the extension, and also in reply to any `ping` from the hub. Updates
`last_seen` on the device's registry record, which feeds the tier computation (see below).

### `command` (hub -> ext)

```json
{
  "v": 1,
  "id": "cmd-uuid",
  "type": "command",
  "command": "snapshot",
  "target": {"tab_id": 7, "window_id": 3},
  "args": {}
}
```

`target` never includes `device_id` on this route -- the device is implicit in *which socket*
the command arrives on. `command` must be one of the names in [Command vocabulary](#command-vocabulary).

### `result` (ext -> hub)

```json
{"v": 1, "id": "cmd-uuid", "type": "result", "device_id": "5e9f...", "ok": true, "result": {"...": "..."}}
```

or, on failure:

```json
{"v": 1, "id": "cmd-uuid", "type": "result", "device_id": "5e9f...", "ok": false, "error": "stale or unknown element ref: e7"}
```

**Fail loud, always.** Every command produces exactly one of these two shapes. No silent
fallbacks, no synthetic/guessed results, no partial-success ambiguity.

### `event` (ext -> hub)

Reserved for unsolicited notifications (e.g. a future phase pushing `tab_created` /
`tab_closed` without being asked). Not emitted by anything in this phase; the shape is
defined so the vocabulary doesn't need a breaking change to add it later:

```json
{"v": 1, "id": "...", "type": "event", "device_id": "5e9f...", "event": "tab_created", "data": {"tab_id": 12}}
```

### `ping` (hub -> ext)

```json
{"v": 1, "id": "...", "type": "ping"}
```

Sent by the hub every 20s to any connected device as a keepalive (`Hub.keepalive_sweep`,
`hub.py`). The extension replies with a `heartbeat` -- the same `background.js` code path its
own 15s timer already uses (`onMessage`'s `ping` branch), so no extension-side change was
needed to wire this up. Measured to hold a desktop MV3 service worker alive indefinitely (165
min, zero gaps in the underlying probe work -- see design doc §2).

App-level, deliberately, not aiohttp's transport-level `heartbeat=` option
(`_handle_device_ws` explicitly sets `heartbeat=None`) -- a raw WS ping/pong frame never
updates `DeviceRecord.last_seen`, which would leave tier inference (`tiers.py`) and dead-socket
detection answering "is this connection alive?" from two uncorrelated clocks. Staying app-level
keeps one number, `last_seen`, driving both.

**Detection, not just distrust.** If a connected device stays silent -- no heartbeat, result,
or event, despite this ping -- for `LIVE_SILENCE_TIMEOUT_SECONDS` (60s, `tiers.py`; the same
threshold `compute_tier` already uses to demote a stale-but-open socket out of `Tier.LIVE`),
the hub proactively closes the connection rather than merely waiting for the OS to eventually
notice (which airplane mode, and similar abrupt radio-off events, may never do -- see
`tiers.py`'s module docstring). This is recorded as a `keepalive_timeout_closing` audit entry,
followed by the normal `device_disconnected` unbind once the closed connection's own handler
loop observes the close. Any command already queued for that device is unaffected -- the queue
lives on the device record, survives the unbind, and drains automatically on the device's next
`hello`.

---

## Agent protocol (CLI/lib <-> hub)

### `list_devices` (agent -> hub) / `devices` (hub -> agent)

Request:
```json
{"v": 1, "id": "...", "type": "list_devices", "token": "..."}
```

Response:
```json
{
  "v": 1,
  "id": "...",
  "type": "devices",
  "devices": [
    {
      "device_id": "5e9f...",
      "profile_id": "a1c2...",
      "label": "edge-macos",
      "platform": "MacIntel",
      "capabilities": {"...": "..."},
      "protocol_version": 1,
      "commands": ["snapshot", "click", "...", "reload"],
      "manifest_version": "0.5.0",
      "build_stamp": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "skew": {
        "known": true, "in_sync": false,
        "device_behind": ["a_new_command"], "hub_behind": [],
        "summary": "the EXTENSION is behind this hub -- missing ['a_new_command']; update the extension (browser_update_extension)"
      },
      "build_freshness": {
        "known": true, "current": false,
        "device_stamp": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "hub_stamp": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "summary": "this extension's build stamp does not match the hub's current build -- the shipped files differ from what this hub would install (a bug fix, UI change, or other update not reflected in the command set). Update the extension (browser_update_extension)."
      },
      "connected": true,
      "connected_at": "2026-07-25T18:03:11.480+00:00",
      "tier": "live",
      "last_seen": "2026-07-25T18:03:11.482+00:00",
      "queue_length": 0
    }
  ]
}
```

### Reporting command-set skew and build freshness to an agent (Tier 0 handshake)

Every `devices`/`browser_devices` entry above carries `commands` (the device's raw
self-reported set, or `null` if it has never reported one -- see the `hello` section's
"`commands` and `manifest_version`"), `manifest_version` (diagnostic only), `build_stamp` (the
device's raw self-reported content hash, or `null` if it has never reported one -- see the
`hello` section's "`build_stamp`"), `connected_at` (the moment THIS connection was established --
distinct from `last_seen`, which a mere heartbeat on an already-live connection also bumps), and
TWO comparisons against this hub's own current state:

- `skew` (`skew.py`'s `SkewReport.to_summary()`) -- the hub's own comparison of that device's
  `commands` against its current `COMMANDS`. `skew.known = false` means the device has never
  reported a command set at all: a definitively stale extension, not a crash and not "unknown."
  `skew.summary` is `null` only when `in_sync` is `true`; otherwise it is a one-line,
  human-readable message naming exactly which side is behind and what to do about it. Answers
  "what can this device DO."
- `build_freshness` (`build_stamp.py`'s `BuildFreshness.to_summary()`) -- the hub's own
  comparison of that device's `build_stamp` against `current_build_stamp()` (this hub's own
  currently-loaded extension source, hashed the same way). `build_freshness.known = false` means
  the device has never reported a build stamp at all: a definitively stale build, the same
  discipline `skew.known` already applies. `build_freshness.current` can be `false` even when
  `skew.in_sync` is `true` -- a device can be command-complete and still running a stale build (a
  bug/UI/security fix that touched zero commands). Answers "IS this device running the CURRENT
  build."

Both are deliberately surfaced in the device listing itself -- an agent (or a human running
`amplifier-browser-bridge devices`) can see staleness on EITHER axis *before* ever failing a
command against it, not only after.

### `command` (agent -> hub) / `result` (hub -> agent)

Request:
```json
{
  "v": 1,
  "id": "...",
  "type": "command",
  "command": "click",
  "target": {"device_id": "5e9f...", "window_id": 3, "tab_id": 7, "ref": "e12"},
  "args": {"ref": "e12"},
  "token": "..."
}
```

Response -- immediate execution (device is `live`):
```json
{"v": 1, "id": "...", "type": "result", "ok": true, "result": {"ref": "e12", "tag": "button"}}
```

Response -- device is not `live` (see [Tiers](#the-three-tier-connectivity-model)):
```json
{
  "v": 1,
  "id": "...",
  "type": "result",
  "status": "queued",
  "command_id": "cmd-uuid",
  "tier": "intermittent",
  "last_seen": "2026-07-25T17:58:02.001+00:00",
  "queue_position": 1
}
```

**This is the load-bearing non-blocking guarantee**: a command targeting a non-live device
returns *immediately* with a queued status. It never silently blocks waiting for the device to
reconnect -- design doc §5: "A tool call that hangs for two minutes is indistinguishable from a
broken system."

Response -- the target failed a **denylist** check (see docs/POLICY.md):
```json
{"v": 1, "id": "...", "type": "result", "ok": false, "error": "target is not accessible under current policy"}
```
The reason text is deliberately generic -- it never names the matched category or domain. A
denied tab must stay invisible; naming *why* a target was refused reveals as much as showing it
in a `tabs` listing would. Full detail (category, matched domain) goes to the audit log only.

Response -- the command matched a **confirmation gate** (an irreversible/world-visible action;
see `docs/designs/confirmation-gate.md` for the classification mechanism, the scoring table, and
the honest limits of detection -- this supersedes docs/POLICY.md §3's older label+URL model):
```json
{
  "v": 1,
  "id": "...",
  "type": "result",
  "status": "needs_confirmation",
  "confirmation_token": "9f2c...hex",
  "category": "delete",
  "detected": {"category": "delete", "score": 3, "matched": ["delete"]},
  "classification": {
    "status": "elevated", "score": 3, "threshold": 3, "categories": ["delete"],
    "advisory": true, "reason_code": null,
    "signals": [{"channel": "label", "provenance": "page", "value": "Delete",
                 "matched": ["delete"], "weight": 3}]
  },
  "redeem": "agent",
  "confirm_scope": "action",
  "expires_at": "2026-07-26T18:45:18.417996+00:00"
}
```
The command was **not** dispatched to the device. Re-submit it via `confirm` (below) with the
same `confirmation_token` to execute it, or let the token expire (5 minutes by default) to
abandon it.

**`classification`** is attached to every `STATE_CHANGING_COMMANDS` (`click`/`type`/`key`/
`navigate`) result -- gated or not, `unknown` or `clear` or `elevated` -- never only on the gated
path. `advisory: true` is not decoration: every signal here except the `url`/`flow` channels is
page-asserted and therefore forgeable (design doc §2) -- never treat this block as a security
boundary on its own. `status: "unknown"` (distinct from `"clear"`) means no page semantics were
observable at all (an unobserved ref, a stale hint, a canvas-rendered page) -- see
`reason_code`, one of `ref_not_observed` · `hint_stale` · `descriptor_unavailable` ·
`device_capability_missing` · `no_page_semantics`.

**`effects`** is also attached to every `STATE_CHANGING_COMMANDS` result -- what the browser
actually did (non-GET requests, navigations, downloads, tabs opened) in a bounded window after
dispatch. This is **browser-asserted**, not page-asserted: a page can add decoy effects but
cannot suppress a real one.
```json
{"effects": {"tier": "webrequest", "window_ms": 1500, "attribution": "time_window",
             "state_changing": true,
             "requests": [{"method": "POST", "url": "https://.../elevate",
                           "type": "xmlhttprequest", "cross_origin": false}],
             "navigations": [], "downloads": [], "tabs_opened": []}}
```
`tier` is one of `cdp` · `webrequest` · `navigation` · `none` -- honest degradation, never a
silent gap: `tier: "none"` means nothing could be observed, not that nothing happened. A tab
observed to be `state_changing` enters **flow elevation** (design doc §11.4): subsequent
state-changing commands in that tab gate until its committed origin changes, a flow-scoped
confirmation is redeemed (`confirm_scope: "flow"` above), or 15 minutes of no new triggering
observation elapse -- this is how a bland-labeled button (e.g. "Next" in a multi-step flow)
becomes catchable without any label signal of its own.

**Flow elevation has TWO independent bounds** (design doc §16.4, fix for review-panel finding F5):
the 15-minute idle-gap bound above resets on every new triggering observation, so a flow kept
"warm" by ordinary, continuing activity would otherwise never end. A second, ABSOLUTE bound
(`FLOW_MAX_LIFETIME_SECONDS`, 30 minutes) is measured from when the flow episode started and does
NOT reset on continued activity -- it ends the episode regardless of how recently it was last
touched. Once it lapses, the very next triggering observation starts a genuinely new episode with
its own fresh 30-minute cap; a tab is never permanently excluded from flow elevation.

### `confirm` (agent -> hub) / `result` (hub -> agent)

Executes a command that previously returned `needs_confirmation`:

```json
{"v": 1, "id": "...", "type": "confirm", "confirmation_token": "9f2c...hex", "token": "..."}
```

(Note the two different `token` fields: `token` is the hub's own auth token, same as every other
agent request; `confirmation_token` is the single-use policy token from the gated response --
they are unrelated and deliberately spelled differently to avoid confusion.)

Response is whatever the original command would have returned had it not been gated (an
immediate `result`, a `queued` status, or -- if the target became denylisted in the meantime --
another denial; a confirmation only bypasses the *gate*, never the denylist). A second `confirm`
with the same `confirmation_token` fails with `{"ok": false, "error": "confirmation token already
used"}`; one submitted after the token's TTL fails with `{"ok": false, "error": "confirmation
token expired"}`.

**Channel enforcement (fix for a live self-attestation hole, `docs/designs/confirmation-gate.md`
§16.1):** `confirm` on this route is `redeem: "agent"` self-attestation, and ONLY redeems a
confirmation whose session declared `redeem: "agent"` (the default). A confirmation whose
session declared `redeem: "unredeemable"` is refused here unconditionally --
`{"ok": false, "error": "...requires human approval... redeem='unredeemable'..."}` -- because this
is the ONLY redemption route this protocol has, or ever will have (see below), and it is reachable
by the agent's own process. This is enforced in `PolicyEngine.consume_confirmation`, not advisory:
a wrong-channel attempt does not consume the token (marking it "used" would claim a redemption that
never happened), and is separately audited as `policy_confirmation_wrong_channel`.

**There is no human-approval channel in this system today.** This is a deliberate current
decision, not a permanent architectural guarantee. A dedicated out-of-band channel (the
extension's own UI, pushed over the `/device` socket) was designed in detail in
`docs/designs/approval-channel-options.md` and then **cancelled for now**: a live CDP experiment
showed the strongest candidate could be driven by the very agent it needed to exclude (see that
doc's cancellation note, section 0), and the simpler fix -- a narrower session `write` scope,
already built via `establish_session`/`narrow_scope` below -- was available the whole time. That
same section names what would reopen the decision: a channel whose security property is measured
against every capability the agent holds (not just the one this experiment tested), or a
per-session way to deny `chrome.debugger` entirely. `redeem: "unredeemable"` is the honest name for
what declaring it does today: a session that declares it is saying "this session is unattended; a
gate here is a permanent stop, not a wait." Its gated actions can never be confirmed, by any route
that exists right now. **If an action must not happen unattended, the way to prevent it today is
to not grant it in the session's write scope** (see `establish_session` below), not to rely on a
gate firing and a human approving it.

### `establish_session` (agent -> hub) / `result` (hub -> agent)

Creates a brand-new session with a caller-declared **write scope** (`scope.py`, `docs/designs/
confirmation-gate.md` section 11.2, Candidate C) -- the only pre-execution signal an adversarial
page cannot touch at all (design doc section 2's lemma). All fields are optional and default to
fully permissive (matching every caller that predates sessions):

```json
{
  "v": 1, "id": "...", "type": "establish_session",
  "read": "*", "write": ["github.com"], "on_unknown": "allow", "redeem": "agent",
  "unattended": false, "allow_self_attested_escalation": false, "token": "..."
}
```

```json
{
  "v": 1, "id": "...", "type": "result",
  "ok": true, "session_id": "9f2c...hex",
  "scope": {"session_id": "9f2c...hex", "read": "*", "write": ["github.com"],
            "on_unknown": "allow", "redeem": "agent", "unattended": false,
            "allow_self_attested_escalation": false, "sealed": false}
}
```

**`allow_self_attested_escalation` (FIX 3, product review panel) defaults to `false`.** Even
when `write` covers the origin, an action `classify.py` scores into `ESCALATION_CATEGORIES`
(today, just `permission_change` -- the measured incident's own category) is forced to
`redeem: "unredeemable"` regardless of write scope and regardless of this session's own
`redeem` value, unless this field was explicitly `true` here at establishment. `write` is an
origin allowlist; it never implies "and may also self-attest its own escalations there" -- see
`docs/POLICY.md` §3.1 for the full reasoning and its honest limits. Like every other scope
field, this narrows one-way (`true -> false`) via `narrow_scope` and can never be turned back on
for an existing session.

The hub **always** mints a fresh `session_id` (a `uuid4`) and never accepts a caller-supplied
one -- this is what stops `establish_session` from ever being replayed against an existing
(possibly already-sealed) session to reset its scope back to broad. `write`/`read` entries are
bare hostnames (subdomain-inclusive, e.g. `"github.com"` also matches `"gist.github.com"` --
the same matching `policy.py`'s denylist uses), not scheme-qualified origin URLs.

Pass the returned `session_id` as an optional `session_id` field on a `command` request
(below) to enforce this session's write scope against that command. Omitting `session_id`
entirely keeps the existing, fully-permissive default every pre-`scope.py` caller already gets.

**A5 fix (security review finding): this state is now visible, not silent.** Omitting
`session_id` used to leave a caller with no way to tell, from the response alone, that its
command ran with no page-immune write restriction at all. The deliberate choice remains
unchanged -- the maintainer's own stance is broad access by default (`docs/POLICY.md` section
1), so the default itself was not narrowed -- but every `STATE_CHANGING_COMMANDS` result reached
without a `session_id` now carries a `scope_warning` string field (`hub.py`'s
`SCOPE_UNSCOPED_WARNING`), the same way `classification` is attached to every such result
whether or not it gated:

```json
{"v": 1, "id": "...", "type": "result", "ok": true, "result": {"...": "..."},
 "scope_warning": "no session_id supplied -- this command ran under the pre-existing, fully-permissive implicit write scope ..."}
```

A caller that always establishes a session and passes its `session_id` never sees this field.

### `narrow_scope` (agent -> hub) / `result` (hub -> agent)

Narrows an **existing** session's scope -- never widens:

```json
{"v": 1, "id": "...", "type": "narrow_scope", "session_id": "9f2c...hex", "write": ["github.com"], "token": "..."}
```

Only the fields present in the request are touched. Rules, enforced field-by-field and
validated atomically (a call that narrows three fields correctly and gets the fourth wrong
changes nothing at all):

- `write`/`read`: `"*"` -> any finite list, or a **strict subset** of the current list. Never
  back to `"*"`, never a superset, never a disjoint set.
- `on_unknown`: `allow -> gate -> deny` only (may skip directly from `allow` to `deny`).
- `redeem`: `agent -> unredeemable` only.
- `unattended`: `false -> true` only.

```json
{"v": 1, "id": "...", "type": "result", "ok": false,
 "error": "write may only narrow to a STRICT subset of the current grant ['github.com'], got ['github.com', 'contoso.com'], which is not a strict subset"}
```

**Once a session has ingested any page content** (a `read`/`snapshot`/`tabs` result -- see
`command`'s response section below), the hub **seals** it, and every subsequent `narrow_scope`
call for that `session_id` -- including a further-narrowing one -- is rejected outright:

```json
{"v": 1, "id": "...", "type": "result", "ok": false,
 "error": "session '9f2c...hex' is sealed (it has already ingested page content) -- scope can no longer be changed at all, narrowing included"}
```

This is the property that actually matters (`scope.py`'s module docstring): a prompt-injected
instruction can only exist inside page content the agent has already read, which means the
session has already sealed by the time such an instruction could possibly reach the model.
There is no sequence of calls, starting from a session that has read anything, that ends with a
wider grant than it started with.

A session's scope survives its device disconnecting/reconnecting -- it is hub-process state,
torn down only by hub restart (like the confirmation-token and flow-elevation state in
`policy.py`), not by any one device's `/device`-route connection lifecycle. Mobile devices drop
and re-attach by design (the three-tier connectivity model below); a scope that evaporated on
reconnect would defeat its own purpose.

### `poll` (agent -> hub) / `result` (hub -> agent)

Used to check on (or retrieve the eventual result of) a previously queued command:

```json
{"v": 1, "id": "...", "type": "poll", "device_id": "5e9f...", "command_id": "cmd-uuid", "token": "..."}
```

Response is one of three shapes, depending on where the command is in its lifecycle:

```json
{"v": 1, "id": "...", "type": "result", "status": "queued", "queue_position": 1, "tier": "intermittent"}
{"v": 1, "id": "...", "type": "result", "status": "pending"}
{"v": 1, "id": "...", "type": "result", "ok": true, "result": {"...": "..."}}
```

### `error` (hub -> agent, either route)

```json
{"v": 1, "id": "...", "type": "error", "error": "unauthorized"}
```

Sent for malformed requests, unknown request types, auth failures, and any unhandled
exception while processing an agent request (the hub never crashes an agent's connection over
one bad request -- see `hub.py`'s `_handle_agent_ws`).

---

## Addressing

```
device_id / profile_id / window_id / tab_id  ->  element_ref
```

See `addressing.py` for the canonical `Target` dataclass and CLI-friendly string format
(`device_id`, `device_id/tab_id`, `device_id/window_id/tab_id`, each optionally suffixed with
`#ref`). `profile_id` does not appear in the CLI string form in this phase -- each `device_id`
today corresponds to exactly one browser profile (the extension install *is* the profile, from
the APIs available to it), so the hub resolves `profile_id` from the device's own `hello`
rather than requiring the caller to specify it separately.

## Command vocabulary

Deliberately mirrors Playwright MCP's tool names -- models already expect these:

| Command | Executed via | Notes |
|---|---|---|
| `snapshot` | injected.js | Accessibility-style tree, frame-qualified `f<frameId>.eN` refs. Top frame only by default; pass `args.all_frames: true` to gather from **every frame** instead (see "Frames" below). Every node (and every `frames` manifest entry) carries a `generation` -- see "Snapshot generations and ref staleness" below. |
| `read` | injected.js | Full visible text. Top frame only by default; pass `args.all_frames: true` to gather **every frame's** content uniformly instead (see "Frames" below). |
| `click` | injected.js | `args.ref`; routes to the exact frame the ref was qualified with |
| `type` | injected.js | `args.ref`, `args.text`; routes to the exact frame the ref was qualified with |
| `key` | injected.js | `args.ref` (optional), `args.key`; routes to the exact frame ONLY when `args.ref` is given -- a ref-less key press runs against the top frame (frameId 0) |
| `scroll` | injected.js | `args.x`, `args.y`; top frame only |
| `back` / `forward` | injected.js | `history.back()`/`forward()`; top frame only |
| `wait_for` | injected.js | `args.selector`, `args.timeout_ms`; polls, never sleeps blindly; top frame only -- see "Frames" below |
| `wait_text` | injected.js | `args.text`, `args.timeout_ms`; polls, never sleeps blindly; top frame only -- see "Frames" below |
| `tabs` | background.js (`chrome.tabs.query`) | optionally scoped by `target.window_id`; each entry now also carries `discarded`/`status` -- see "Discarded tabs" below. **Unchanged by the agent-facing pagination fix below**: the wire-level `result` is still the full, unfiltered list of every tab on the device -- see "Agent-facing tabs pagination (not a wire change)". |
| `tab_open` | background.js (`chrome.tabs.create`) | target is device-only; `args.url`, `args.active` (default background) |
| `tab_close` | background.js (`chrome.tabs.remove`) | |
| `tab_activate` | background.js (`chrome.tabs.update`) | the one command that's explicitly *allowed* to steal focus, because it was asked to |
| `screenshot` | background.js (`chrome.tabs.captureVisibleTab`, or CDP `Page.captureScreenshot` -- see CDP section below) | Injection-only by default: only works if the target tab is already active. Pass `args.capture_hidden: true` to auto-escalate to CDP for any-tab/hidden capture. Returns `base64` image bytes. `args.frame_id` crops to one frame's on-screen region (requires `capture_hidden`); `args.multi_page: true` scrolls and captures repeatedly (`args.max_pages`, `args.scroll_selector`, `args.page_delay_ms`) -- see "Frame-targeted and multi-page screenshot capture" below. |
| `attach` | background.js (`chrome.debugger.attach`) | Phase 4: explicit CDP attach for a tab. See CDP section below. Also see "Discarded tabs": attaching implicitly wakes a discarded tab. |
| `detach` | background.js (`chrome.debugger.detach`) | Phase 4: explicit CDP detach for a tab. |
| `reload` | background.js (`chrome.runtime.reload()`) | Self-service extension reload. Target is device-only. See "Extension self-reload" below. |
| `fetch_bytes` | background.js (extension-context `fetch`, `credentials: "include"`) | `args.url`, `args.max_bytes` (optional). Target is device-only. See "Content-extraction mechanisms" below. |
| `grab_image` | background.js (`chrome.scripting.executeScript` into the page's `MAIN` world) | `args.url`, `args.max_bytes` (optional). Requires `target.tab_id`. See "Content-extraction mechanisms" below. |
| `downloads_list` | background.js (`chrome.downloads.search`) | `args.limit` (optional, default 20). Target is device-only. |
| `download` | background.js (`chrome.downloads.download`) | `args.url`, `args.filename` (optional). Target is device-only. |
| `wait_download` | background.js (`chrome.downloads.search`, polled) | `args.download_id` XOR `args.since_id` (+ optional `args.pattern`), `args.timeout_ms`. Target is device-only. See "Content-extraction mechanisms" below. |
| `windows` | background.js (`chrome.windows.getAll` + `chrome.tabGroups.query`) | Target is device-only. Full window metadata (incl. `incognito`) and tab-group metadata. No tab_id/page contact -- see "Browser-state archive" below. |
| `page_state` | injected.js | Per-tab `outer_html` (capped, honest `truncated` flag), `forms` (field values -- password values always excluded), `local_storage`/`session_storage`, `scroll`. Top frame only by default (same narrower limitation as `scroll`/`wait_for`); `args.frame_id` targets one known frame. |
| `mhtml` | background.js (CDP `Page.captureSnapshot`) | Unconditionally CDP-requiring -- no injection-only alternative exists. Returns the raw MHTML document as `result.data` (a string, not base64). |
| `nav_history` | background.js (CDP `Page.getNavigationHistory`) | Unconditionally CDP-requiring, same as `mhtml`. |
| `history_list` | background.js (`chrome.history.search`) | `args.query` (default `""`), `args.max_results` (default 1000), `args.start_time` (ms epoch, optional). Target is device-only. |
| `bookmarks_list` | background.js (`chrome.bookmarks.getTree`) | Target is device-only. Returns a flattened list, not the nested tree. |
| `sessions_list` | background.js (`chrome.sessions.getRecentlyClosed` + `getDevices`) | `args.max_results` (default 25). Target is device-only. |
| `top_sites` | background.js (`chrome.topSites.get`) | Target is device-only. |
| `reading_list` | background.js (`chrome.readingList.query`) | Target is device-only. |
| `cookies_list` | background.js (`chrome.cookies.getAll`) | `args.url`/`args.domain` (optional filters). Target is device-only. **Not gated at this wire-command level** -- a direct caller gets cookies like any other command; the opt-in gate lives at the archive orchestrator level (see below). |

Every `PAGE_WORLD_COMMAND` (`snapshot`, `read`, `click`, `type`, `key`, `scroll`, `back`, `forward`,
`wait_for`, `wait_text`) also accepts an optional `args.wake` -- see "Discarded tabs" below -- and
an optional `args.activate` -- see "Foregrounding a tab for DOM injection (`args.activate`)" below.
Every command accepts an optional `args.timeout_s` -- see "Command timeout" below.

### Agent-facing tabs pagination (not a wire change)

Real-world finding: on the maintainer's own device (~728 open tabs), an unpaged `tabs` result was
~640KB -- large enough to truncate mid-response before it ever reached an agent's context window.
The hub -> agent wire transfer that produces that payload is cheap (machine to machine); what's
expensive is a payload that size entering an LLM's context.

**This device protocol is deliberately unchanged.** `background.js`'s `listTabs()` still returns
every tab on the device in one `result` list, exactly as documented in the table above -- there is
no `args.limit`/`args.offset`/`args.window_id` filtering added to the `tabs` command, and
`protocol.py`/`extension/background.js` gained no new fields for this. Adding wire-level paging
would grow the hand-synced `protocol.py` <-> `extension/background.js` surface this document's own
header warns is kept in sync manually, for no benefit -- the wire transfer was never the expensive
part.

Instead, pagination, filtering, and a cheap summary mode live entirely in the agent-facing tool
layer, one level up from this protocol: `amplifier_browser_bridge.paging.shape_tabs_response` (a
pure function, no I/O) reshapes the hub's full `tabs` response before either agent surface
(`mcp_server.py`'s `browser_tabs` tool, or the Amplifier tool module's `browser_tabs`) hands
anything back to the calling agent. See `docs/AGENT_SURFACES.md` for the resulting tool-level
behavior (paging defaults, filters, summary mode). A `{"status": "queued", ...}` or `{"ok": false,
...}` `tabs` response is passed through by that layer completely untouched, per this document's
existing tier pass-through and fail-loud guarantees.

### Frames

At real-world scale a substantial fraction of a page's actual content can live inside an
`<iframe>`, not the top frame -- the motivating case: a SharePoint/M365 policy page whose
document *body* renders inside an embedded viewer frame, while the top frame carries only nav
chrome, a title, and metadata. Before this fix, `read`/`snapshot` only ever injected into the
top frame (`injected.js`'s own module docstring documented this honestly as a limitation, not a
silent gap) -- `chrome.scripting.executeScript` was never called with `allFrames: true`, so an
embedded document was completely invisible to `read`.

**How it works now:** `read` and `snapshot` inject `injected.js` into, and dispatch against,
**every frame** `chrome.scripting.executeScript({allFrames: true})` can reach (the extension
holds `<all_urls>`, so this includes cross-origin frames, not just same-origin ones). Frames
Chrome could not inject into at all -- sandboxed without `allow-scripts`, an opaque-origin
`data:`/`about:blank` frame, or one removed mid-call -- are simply absent from the result set;
Chrome does not report *why* a frame is missing, so the extension cannot always name the exact
reason, but it never silently pretends a declared child frame doesn't exist: any `<iframe>`/
`<frame>` element a successfully-injected frame's own DOM declares, that produced no result
itself, is reported in `unconfirmed_frames` (an array of `src` URLs) on the combined result.

**Combine strategy for `read`** (see `extension/combine_frames.mjs`'s module docstring and
docs/designs/browser-bridge.md's "Mechanism, not policy" section for the full rationale):
return **every frame's** content, uniformly, in `frames` (ordered by `frame_id` ascending --
predictable, not ranked). Each entry carries `frame_id`/`url`/`title`/`chars` (the real,
untruncated length) and `text` (capped per frame at `READ_FRAME_TEXT_CAP`, 50,000 characters,
with an honest `truncated` flag -- a payload-size bound, never a content pick). Top-level
`url`/`title` identify the tab itself (frame 0's own metadata -- deterministic identity, not a
content judgment), alongside `frame_count` and `unconfirmed_frames`.

This replaces an earlier design that ranked frames by character count and returned only the
"richest" frame's text as *the* result (with the rest demoted to an `other_frames` manifest).
That was a policy decision -- "which frame's content does the caller want" -- baked into this
mechanism layer, and a bad one: live against a real SharePoint/Word Online policy page, the
richest frame by character count was an O365 auth/bootstrap iframe's inlined JS config blob
(3,608 characters), not the actual policy document body (108 characters -- Word Online renders
the document to `<canvas>`, so the DOM text is only viewer chrome; see "Content-extraction
mechanisms" below for why `read`/`snapshot` cannot reach that content at all). A char-count
heuristic cannot distinguish "verbose bootstrap JS" from "the document that matters" -- that
judgment belongs to the calling agent, which has context this layer does not. Blind
concatenation of every frame's text into one string was also considered and rejected: a real
page can have 20+ frames of nav chrome/ads/trackers around one substantive embedded document,
and concatenating them buries the useful content in noise. Returning every frame's content
**separately** avoids both failure modes: nothing is picked for the caller, and nothing is
buried.

```json
{
  "ok": true,
  "result": {
    "url": "https://.../PolicyProcedure.aspx",
    "title": "Policy Procedure",
    "frame_count": 5,
    "frames": [
      {"frame_id": 0, "url": "https://.../PolicyProcedure.aspx", "title": "Policy Procedure", "chars": 2874, "text": "... nav chrome ...", "truncated": false},
      {"frame_id": 3, "url": "https://.../viewer-frame", "title": "Policy Document", "chars": 108, "text": "PAGE 1 OF 5 | CONFIDENTIAL...", "truncated": false},
      {"frame_id": 5, "url": "https://o365.../bootstrap.js", "title": "", "chars": 3608, "text": "... auth bootstrap JS ...", "truncated": false}
    ],
    "unconfirmed_frames": []
  }
}
```

**Combine strategy for `snapshot`**: unlike `read`, there is no "pick the richest one" answer --
an interactive element an agent needs to click/type into can legitimately live in any frame, so
`nodes` from **every** frame are included, each `ref` qualified with the frame it came from (see
"Frame-qualified refs" below) and tagged with its own `frame_id`. A `frames` array reports a
per-frame manifest (`frame_id`/`url`/`title`/`node_count`) alongside `frame_count` and
`unconfirmed_frames`, for the same "let an agent reason about what it's looking at" reason `read`
reports `other_frames`.

**Frame-qualified refs:** because `injected.js` runs independently in every frame (each frame
gets its own `window.__amplifierBrowserBridge` with its own ref counter starting at `e1`), a bare `"e12"` in frame 0
and `"e12"` in frame 7 are NOT the same element. Every ref this system hands back is qualified as
`"f<frameId>.<ref>"` (e.g. `"f0.e12"`, `"f7.e3"`) -- see `extension/frame_refs.mjs` (pure,
independently unit-tested with `node --test`, no `chrome.*` dependency) for `qualifyRef`/
`parseQualifiedRef`. **This changes the ref format from prior phases** (previously a bare `"e12"`)
-- refs are already documented as valid only within the page load that produced them (they reset
on navigation), so this is not a new class of staleness, just a stricter, unambiguous shape.
`click`/`type`/`key` (when `args.ref` is given) parse the frame id back out and target
`chrome.scripting.executeScript`'s `frameIds: [frameId]` at the exact frame that owns the ref --
never frame 0 by default, never a guess. A ref whose frame no longer exists in the tab (navigated
away, reloaded, or removed) fails loud, naming the frame id, rather than a bare "stale ref".

**Documented narrower limitation:** `scroll`, `back`, `forward`, `wait_for`, `wait_text`, a
ref-less `key` press, and `page_state` (D2, browser-state archive) still operate on the top
frame (frameId 0) only by default, in this phase. A `wait_for`/`wait_text` selector or text that
only exists inside an iframe will not be found; `page_state` will not capture an embedded
document's own outerHTML/storage without an explicit `args.frame_id`. This is a scope decision,
not an oversight -- these are page/tab-level operations (or, for `key`, have no ref to resolve a
frame from) where multi-frame semantics are considerably less obviously correct (e.g. "scroll
which frame?"). `page_state` accepts `args.frame_id` (the same generic single-frame-targeting
branch `read`/`snapshot` use) to reach one known frame explicitly, without needing its own
MULTI_FRAME_COMMANDS combine strategy. Revisit if a real need for frame-scoped waits/page_state
emerges.

### Snapshot generations and ref staleness

**The bug this section fixes:** every `snapshot` regenerates a frame's ref table. Before this fix,
a ref from a *superseded* snapshot could still resolve -- if the same DOM node happened to still
be connected -- and `click`/`type`/`key` reported `{"ok": true, ...}` while doing nothing (or
acting on the wrong thing). Reproduced live: snapshot a page, note ref `f0.e93` for a button,
take a *second* snapshot (or run any other command), then click `f0.e93` -- it resolved and
reported success, but the click had no effect. That is the worst class of bug for this system: an
agent believes an action happened and proceeds on a false premise, in a system whose stated
discipline is fail-loud (design doc §8).

**How it's fixed:** every ref is bound to the *generation* of the snapshot that produced it, not
just to whether its underlying DOM node still exists. Each frame's own `window.__amplifierBrowserBridge` (one per
frame -- see "Frame-qualified refs" above) keeps a generation counter, starting at 0. Every
`snapshot` call increments it and re-stamps every ref it touches (new or already-known) with the
new value. **A ref only resolves while its stamped generation still equals that frame's CURRENT
generation** -- i.e., only refs from the *most recent* snapshot of a given frame (or from a
`wait_for` that ran since) are valid. A ref from any earlier snapshot is rejected outright, even
if it still points at a live, connected element -- see docs/designs/browser-bridge.md's
"Mechanism, not policy" section: silently accepting it because the element "still works" would be
exactly the silent-substitution mistake that section forbids. There is no automatic re-snapshot on
a stale ref -- the caller re-snapshots explicitly and gets a result reflecting the page's current
state, rather than the bridge guessing what it should point at instead.

Every `snapshot` result's nodes (and each `frames` manifest entry) carry a `generation` number so
a caller can see which pass produced a given ref:

```json
{"ok": true, "result": {
  "url": "https://example/admin",
  "nodes": [{"ref": "f0.e93", "role": "button", "name": "Revoke", "tag": "button", "generation": 3, "frame_id": 0}],
  "frame_count": 1,
  "frames": [{"frame_id": 0, "url": "https://example/admin", "title": "Admin", "node_count": 40, "generation": 3}],
  "unconfirmed_frames": []
}}
```

`click`/`type`/`key` fail loud with one of **four distinct, actionable causes** (see
`extension/ref_registry.mjs` -- the tested reference implementation of this algorithm --
and `injected.js`'s hand-synced inline copy, kept in sync the same way protocol.py/background.js
are):

1. **Unknown ref** -- never produced in the current page context. Either the ref string is simply
   wrong, or a navigation/reload happened since the snapshot that produced it (which destroys
   `window.__amplifierBrowserBridge` along with the rest of the page's JS context, wiping the ref table entirely):
   `"unknown element ref: f0.e93 -- never produced in the current page context. If a navigation or
   reload happened since you last took a snapshot, the ref table was reset -- take a fresh snapshot."`
2. **Stale ref (superseded generation)** -- this is the bug fix's core case: `"stale ref: e93 was
   captured by an earlier snapshot (generation 1); the most recent snapshot on this page is
   generation 2. Refs are only valid from the MOST RECENT snapshot -- take a fresh snapshot and use
   a ref from that result."`
3. **Disconnected (same generation)** -- the element left the DOM without any new snapshot
   happening: `"element for ref e93 is no longer attached to the page (removed from the DOM since
   it was captured, still generation 2) -- take a fresh snapshot."`
4. **Identity changed (same generation, still connected)** -- a cheap, additional safety net beyond
   the generation check: at resolve time, the element's current tag and accessible name are
   re-derived and compared against what was recorded when the ref was minted. A virtualized/
   recycled-list UI can reuse the exact same DOM node for different content between snapshots
   without ever disconnecting it -- an element "still there" under a still-valid-generation ref,
   but no longer semantically the one the caller inspected, is the same silent-failure class this
   whole section exists to prevent, just without a generation bump to catch it structurally:
   `"element for ref e93 no longer matches what was captured (expected tag=BUTTON name=\"Revoke\",
   now tag=BUTTON name=\"Approve\") -- the DOM node may have been reused for different content since
   the snapshot. Take a fresh snapshot."` (Trade-off, deliberately accepted: an element whose visible
   text legitimately changed between snapshot and action -- e.g. a live counter -- will also trip
   this check. Fail-loud-but-occasionally-conservative beats silently clicking the wrong thing.)

`wait_for`'s own found ref is minted the same way (stamped with whatever generation is currently
current, without bumping it) -- it stays valid until the *next* real `snapshot` on that frame
supersedes it, not forever.

**Shadow DOM piercing is unaffected**: `injected.js`'s `deepQueryAll()` still pierces open shadow
roots within each frame it runs in -- multi-frame traversal is orthogonal to (and composes with)
shadow-DOM traversal, not a replacement for it.

### Content-extraction mechanisms

`read`/`snapshot` only ever see text that's actually present in the DOM. Real-world finding,
live against the SharePoint policy page above: `Quarterly-Report.docx` is embedded in a
Word Online viewer, which renders the document to `<canvas>` -- the viewer frame's entire DOM
text is a page-chrome string (`"PAGE 1 OF 5 | CONFIDENTIAL\INTERNAL ONLY | ..."`), not the
document body. **There is no DOM text there to read, full stop.** Two mechanisms reach that
content instead, and this system deliberately does not pick one for the caller (see
docs/designs/browser-bridge.md's "Mechanism, not policy" section):

- **`fetch_bytes`** -- fetch a URL from the **extension's own context**, with
  `credentials: "include"`. This rides the user's real cookies for the target origin (the
  entire point of this project -- design doc §1), so it can retrieve a file the user is
  authenticated to see even though the page never rendered its bytes as DOM text.
- **`screenshot`** -- capture pixels (see the CDP section above for the hidden/background-tab
  case). Works even when there is no text to extract at all.
- **`vision_read`** (agent-surface only, not a wire command -- see "Vision-based extraction"
  below) -- capture pixels AND call a vision-capable LLM over them to extract text. A distinct
  mechanism from `screenshot`: `screenshot` never calls a model; `vision_read` always does.

```json
{"v": 1, "id": "...", "type": "command", "command": "fetch_bytes", "target": {"device_id": "..."}, "args": {"url": "https://.../Quarterly-Report.docx", "max_bytes": 10485760}, "token": "..."}
```

```json
{"ok": true, "result": {"url": "https://.../Quarterly-Report.docx", "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "byte_length": 47213, "base64": "UEsDBBQ..."}}
```

Refuses past `args.max_bytes` (default 25MB) with an actionable error naming the limit --
never silently truncates the bytes (a truncated `.docx`/`.pdf` is corrupt, not partially
useful). `args.max_bytes` raises the cap.

**`grab_image`** is a *distinct* mechanism, not a fallback `fetch_bytes` reaches for
automatically: it runs the fetch inside the **page's own `MAIN`-world script context**
(`chrome.scripting.executeScript({world: "MAIN"})`), so the request carries the page's real
`Referer` and cookie context -- exactly what defeats hotlink/Referer-checking protection that
an extension-context fetch would trip. Requires `target.tab_id` (there is no page context to
run the fetch in without a live page); `fetch_bytes` has no such requirement. Same
`args.url`/`args.max_bytes`/result shape as `fetch_bytes`. The caller picks whichever fits the
target; neither silently retries as the other on failure -- the error each returns explicitly
names the other as something to try instead (see "Discoverable alternatives, not automatic
escalation" below).

**`downloads_list`** / **`download`** / **`wait_download`** -- for a file the user reaches by
triggering a browser download (clicking a page's own Download control, or navigating a URL
that responds with `Content-Disposition: attachment`) rather than one linked as a fetchable
URL:

```json
{"v": 1, "id": "...", "type": "command", "command": "downloads_list", "target": {"device_id": "..."}, "args": {"limit": 20}, "token": "..."}
```
```json
{"ok": true, "result": {"downloads": [{"download_id": 41, "filename": "...", "url": "...", "state": "complete", "mime": "...", "byte_length": 12345, "start_time": "..."}], "max_download_id": 41}}
```

`download` triggers a download directly (`chrome.downloads.download()`) and returns **its own
definite** `download_id` -- no ambiguity, since the command itself started the download:

```json
{"v": 1, "id": "...", "type": "command", "command": "download", "target": {"device_id": "..."}, "args": {"url": "https://.../file.pdf"}, "token": "..."}
```
```json
{"ok": true, "result": {"download_id": 42, "url": "https://.../file.pdf"}}
```

`wait_download` polls (never sleeps blindly) for a completed download, in one of two mutually
exclusive modes:

- `args.download_id` -- wait for that **specific** download (from a prior `download` call).
- `args.since_id` (+ optional `args.pattern`, a filename regex) -- wait for a **NEW** download
  with an id strictly greater than the baseline. This is the baseline-max-id + filename-pattern
  approach lifted from the reference implementation this project supersedes (see the design
  doc's evidence base): a caller calls `downloads_list` **before** the action that triggers an
  indirect download (e.g. clicking a page's Download button) to capture `max_download_id` as
  the baseline, then `wait_download` with `since_id=<that baseline>`. This is the mechanism
  that guarantees a download the human started themselves, in the same window, is **never**
  mistaken for the agent's own -- id ordering, not "whatever's newest," is the guarantee.

```json
{"v": 1, "id": "...", "type": "command", "command": "wait_download", "target": {"device_id": "..."}, "args": {"since_id": 41, "pattern": "\\.docx$", "timeout_ms": 30000}, "token": "..."}
```
```json
{"ok": true, "result": {"download_id": 43, "filename": "Quarterly-Report.docx", "url": "https://ppc-word-view.../download", "mime": "application/vnd...", "byte_length": 47213, "state": "complete"}}
```

Neither `args.download_id` nor `args.since_id` given fails loud immediately (`"wait_download
requires args.download_id or args.since_id..."`) -- there is no silent default to "grab
whatever download is newest," since that default is exactly the unsafe behavior this baseline
scheme exists to prevent.

### Frame-targeted and multi-page screenshot capture

Two real-world findings motivated these: a SharePoint policy page's actual document body
renders inside a nested Word Online viewer `<iframe>` (not the top frame), and that document
is 5 pages -- a single viewport capture only ever shows page 1.

**`args.frame_id`** crops a `screenshot` to one frame's own on-screen region (an integer from a
prior `read`/`snapshot`'s `frames` entries), instead of the whole tab. Requires
`args.capture_hidden: true` -- cropping uses CDP's `Page.captureScreenshot` `clip` parameter,
which `chrome.tabs.captureVisibleTab` has no equivalent for. Computing the region requires
walking the frame's ANCESTOR chain (`chrome.webNavigation.getAllFrames` gives
`frameId`/`parentFrameId`/`url` -- the real containment hierarchy) and, at each level, finding
the `<iframe>`/`<frame>` element in the PARENT's own DOM whose `getBoundingClientRect()` gives
the on-screen offset/size. `window.frameElement` (read from inside the child frame) was
considered and rejected: it is `null` for a cross-origin child by design (a browser security
restriction, not a bug) -- exactly the SharePoint-embeds-officeapps.live.com case this exists
for. Fails loud (naming the frame id and what went wrong) if the frame can't be found in the
current tree, or its containing element can't be located in its parent.

**`args.multi_page: true`** scrolls the target frame's own scrollable element (or
`args.scroll_selector`, a CSS selector for a specific scrollable container) and captures at
each stop, until it detects the end of the scrollable region or hits `args.max_pages` (default
10, hard cap 50) -- whichever comes first. `args.page_delay_ms` (default 350) is the settle
delay between scrolling and capturing, for content that needs a moment to (re-)render.  Returns
a `pages` array (each with its own `base64`), plus `page_count`, `capped` (true if `max_pages`
was hit before the real end), and `stopped_reason` -- **honestly reported, never silently
returning a partial capture as if it were the complete document.**

Combine with `frame_id` to scroll and capture pages of a specific embedded viewer rather than
the whole tab.

### Vision-based extraction

Two mechanisms exist for turning pixels into something a caller can use, and this system
deliberately keeps them distinct rather than picking one automatically (design doc §13,
"Mechanism, not policy"):

1. **Return pixels** -- `screenshot` (above). No model call, ever. A vision-capable MCP client
   consumes the returned image content block directly with zero extra cost.
2. **Return text extracted from pixels** -- `vision_read`, an **agent-surface-only** operation
   (CLI `amplifier-browser-bridge vision-read`, MCP tool `browser_vision_read`, Amplifier tool `browser_vision_read`)
   -- **not a wire-protocol command**. It composes an ordinary `screenshot` call (with whatever
   `frame_id`/`multi_page`/etc. args the caller supplies) with a real call to an external
   vision-capable LLM (`src/amplifier_browser_bridge/vision.py`), and returns the extracted text.

**Why `vision_read` is not a hub/extension command:** the hub and extension's job is mechanism
-- reliable, capability-scoped pixel capture, with zero knowledge of what happens to the bytes
afterward. Calling an external model is a policy decision (which model, whether to spend the
latency/cost at all) that only the calling agent can make correctly. Baking a "if the capture
looks thin, try vision" heuristic into `screenshot` itself would be exactly the automatic,
silent-substitution mistake `read`'s original frame-ranking logic made (see the design doc's
worked example). Keeping the model call in a separate, explicitly-named Python-lib
composition means the hub/extension never import an LLM SDK or hold a model API key, and
`screenshot` remains fully useful with zero vision provider configured.

**Provider configuration:** no project-specific model/provider convention exists in this
standalone repo, so `vision.py` follows the same env-var-configured-provider pattern
documented in Amplifier's `image-vision` skill: `GOOGLE_API_KEY` / `ANTHROPIC_API_KEY` /
`OPENAI_API_KEY`, checked in that order (first one present wins), or `AMPLIFIER_BROWSER_BRIDGE_VISION_PROVIDER`
(`gemini`/`anthropic`/`openai`) to pin a specific one, with `AMPLIFIER_BROWSER_BRIDGE_VISION_MODEL` to override the
default model. **Fails loud** with a message naming exactly which environment variable(s)
would resolve it if none is configured -- never silently returns empty text.

```json
{"ok": true, "result": {
  "text": "...extracted text...",
  "vision_provider": "anthropic",
  "vision_model": "claude-3-5-sonnet-latest",
  "image_count": 5,
  "page_count": 5,
  "capped": false,
  "stopped_reason": "reached end of scrollable content",
  "frame_id": 862
}}
```

If the underlying `screenshot` capture is queued (non-`live` device) or fails, that shape is
returned unchanged -- the vision model is **never** called without a real captured image in
hand.

### Discoverable alternatives, not automatic escalation

When a command fails in a way another mechanism would solve, the **error text** names the
alternative -- it never retries the command a different way itself (see docs/designs/
browser-bridge.md's "Mechanism, not policy" section):

- A `read`/`snapshot` timeout mentions `args.all_frames=true` (if not already set) or
  `args.frame_id=<id>` (if it was) as something the caller may choose; a `click`/`type`/`key`
  timeout mentions `args.trusted=true` (CDP-backed input).
- A CDP-requiring command (`args.trusted`/`args.capture_hidden`) on a device without
  `chrome.debugger` (e.g. Edge Android) names the capability as unavailable **and** names what
  the device can still do instead (untrusted injected input; active-tab-only screenshot).
- `fetch_bytes` failing with an HTTP/fetch error mentions `grab_image` as the page-context
  alternative; `grab_image` failing mentions `fetch_bytes` back.

This is discoverability, not decision-making -- the calling agent still chooses.

### Discarded tabs

At real-world scale (hundreds of tabs open), Edge discards (unloads the renderer of) most
background tabs to reclaim memory. A discarded tab has no live page for
`chrome.scripting.executeScript` to inject into. Before this fix, a command against a discarded tab
surfaced Edge's own, genuinely misleading error: `"Cannot access contents of the page. Extension
manifest must request permission to access the respective host."` -- `<all_urls>` is granted; the
real cause is "there is no live renderer here right now," not a permissions problem.

- **`tabs` results now include `discarded` (bool) and `status` (`"loading"` / `"complete"` /
  `"unloaded"`)** on every entry, so a caller can see which tabs are live before acting on one.
- **A `PAGE_WORLD_COMMAND` against a discarded tab fails loud with a specific, actionable error**
  naming the real cause, instead of passing through Edge's misleading one.
- **Waking a discarded tab requires reloading it**, which destroys in-page state (unsaved form
  data, scroll position, ephemeral JS state) -- co-working etiquette (design doc §6.3) requires this
  be explicit, never a hidden side effect of an ordinary read. Pass `args.wake: true` to opt in; the
  extension reloads the tab, waits for it to finish loading, and only then runs the command. The
  result carries `"woke": true` and a `"wake_reason"` string so the caller can tell its own read
  triggered a reload rather than reading the tab's pre-existing state.
- **A tab the agent is actively engaged with is marked `autoDiscardable: false`** (best-effort,
  never fails the calling command) so Edge does not immediately re-discard a tab the agent just woke
  or acted on. This is applied per-tab, on deliberate engagement (any page-world command,
  `navigate`, `tab_activate`, `screenshot`, or CDP `attach`) -- never blanket-applied across an
  entire `tabs` listing.
- **CDP sidesteps this differently, not for free**: `chrome.debugger.attach()` on a discarded tab
  was observed (live, against a real discarded tab) to force Edge to instantiate a live renderer for
  it -- attach succeeded, and a subsequent plain (non-CDP) `read` immediately succeeded where it had
  failed before attaching, with **no explicit wake/reload call**. This is not free of the same
  state-loss caveat as an explicit `wake: true` -- a discarded tab has no renderer at all, so making
  one live is observably equivalent to a reload; it is simply automatic (a side effect of attaching)
  rather than opt-in. Any command that escalates to CDP (`trusted: true`, `capture_hidden: true`)
  attaches first via the hub's own pre-flight (`_ensure_cdp_attached`), so this happens before the
  real command ever reaches the device.

### Foregrounding a tab for DOM injection (`args.activate`)

Real-world finding: `snapshot` against a heavy enterprise SPA
(`repos.opensource.microsoft.com`'s Open Source Management Portal) timed out at 170s while the
tab was backgrounded, and completed in ~2s against the same tab immediately after activating it.
DOM injection/traversal on a large, fully-hydrated page is viable-when-foreground,
dead-when-background -- the tab needs to actually be compositing/rendering for
`chrome.scripting.executeScript` to complete promptly.

Every `PAGE_WORLD_COMMAND` accepts an optional `args.activate` (same tolerant coercion as
`args.wake`/`args.all_frames` -- see "Boolean argument coercion" below). **Default off.** When
truthy, `runInPage()` (background.js) activates the tab (`chrome.tabs.update(tabId, {active:
true})`) *before* any injection happens for that command, then proceeds normally. Like
`args.wake`, this is **never automatic** -- co-working etiquette (design doc §6.3) forbids
stealing the human's focus as a hidden side effect of an ordinary read, so this follows the exact
same precedent as `tab_activate`: the command was explicitly asked to steal focus, so it does,
and the result says so. Only acts (and only reports it) when the tab wasn't already active --
activating an already-active tab steals nothing and changes nothing, so nothing is reported:

```json
{"ok": true, "result": {"url": "...", "title": "...", "nodes": [...], "activated": true}}
```

**Discoverable, not automatic** (design doc's "Mechanism, not policy" section): when a
`PAGE_WORLD_COMMAND` times out and `args.activate` was NOT already set, the timeout error names
every real alternative rather than picking one -- for `read`/`snapshot`: `args.activate=true`
(fast, exact DOM, steals focus), the agent-surface-only `vision_read` (screenshot + a vision-model
call, no focus steal, no element refs), or raising `args.timeout_s`/narrowing with
`args.frame_id`; for `click`/`type`/`key`/others: `args.activate=true` or raising `args.timeout_s`.
See `Hub._timeout_hint` (hub.py) and "Command timeout" below.

### Extension self-reload

Unpacked extensions do not pick up file changes on disk automatically -- normally this requires a
human to click Reload in `edge://extensions` after every code update. The `reload` command
(`chrome.runtime.reload()`) makes this self-service from an already-connected agent surface:

```json
{"v": 1, "id": "...", "type": "command", "command": "reload", "target": {"device_id": "..."}, "args": {}, "token": "..."}
```

The extension acks (`{"ok": true, "result": {"reloading": true}}`) *before* actually reloading --
`chrome.runtime.reload()` terminates the service worker close to immediately, so the ack is sent
first and the reload is deferred briefly (~250ms) to give it time to flush over the websocket.

**The very first deployment of this command still requires one manual reload.** An extension has to
already be running code that understands the `reload` command before it can reload itself into a
version that understands it -- there is no way around that single bootstrap step. Every subsequent
iteration is self-service via `amplifier-browser-bridge reload <device_id>`.

**`browser_update_extension` (Tier 1 + Tier 2, `update_extension.py`) -- composed, not a wire
command**, the same way `vision_read`/`browser_archive` compose wire commands without being one
themselves. It restages a fresh build (`setup.stage_extension`, the same function
`amplifier-browser-bridge init` uses) and sends this `reload` command, but never reports success
on the ack alone -- `chrome.runtime.reload()` drops the websocket, so it polls `list_devices`
until this device reconnects with a NEW `connected_at` (a real, bounded timeout, never a bare
sleep-and-hope) and only then compares its self-reported `commands` (see the `hello` section's
"Tier 0 handshake") before and after. If the set changed, the automatic update reached this
device's real extension files -- verified, not assumed. If reload succeeded and the device
reconnected but its command set is unchanged, this hub's restage did not reach wherever that
browser actually loads its extension from (most likely a different machine) -- reported plainly,
with guided manual instructions and a real `GET /setup/extension.zip` download URL (see
`extension_zip.py`) rather than a false "done." See `update_extension.py`'s module docstring for
the full design and docs/AGENT_SURFACES.md for the tool's exact shape.

Commands are partitioned into `PAGE_WORLD_COMMANDS` (dispatched into `injected.js` running in
the page's isolated world) and `BROWSER_LEVEL_COMMANDS` (handled directly by
`background.js` against `chrome.tabs`/`chrome.windows`/`chrome.debugger`, which are not
reachable from page context). See `protocol.py` for the exact partition.

### Command timeout

`Hub._send_and_await` waits for a device's `result` before returning to the caller (or,
for a queued/non-`live` device, before a drained command's later `poll()` resolves).
Real-world finding that motivated this section: `read` against a heavy SPA
(`repos.opensource.microsoft.com`'s Open Source Management Portal) timed out at the prior
fixed `30.0s` default even though the tab was awake and reporting `status: "complete"` --
injection + shadow-DOM-piercing traversal on a large, fully hydrated single-page app can
genuinely take longer than 30s.

**Three coherent, non-overlapping layers** (each a different *scope*, not three ways to say the
same thing):

1. **Hub default** (`hub.py`'s `DEFAULT_COMMAND_TIMEOUT`, now **120.0s**, was 30.0s) -- applies
   to every command that doesn't override it. Configurable per hub process via
   `amplifier-browser-bridge hub --command-timeout <seconds>`.
2. **Per-command override** -- any command's `args` may include `timeout_s` (a float, seconds).
   This is a **hub-only** arg (see `protocol.py`'s `HUB_ONLY_ARGS`): `Hub.send_command` pops and
   validates it *before* building the `QueuedCommand` sent to the device -- it never reaches
   `injected.js`/`background.js` over the wire. Accepted range: `MIN_COMMAND_TIMEOUT` (1.0s) to
   `MAX_COMMAND_TIMEOUT` (600.0s); anything else fails loud with `{"ok": false, "error":
   "args.timeout_s must be between 1.0 and 600.0 seconds, got: ..."}` rather than being silently
   clamped. A command that queues on a non-`live` device carries its `timeout_s` override with it
   (`QueuedCommand.timeout`) and still honors it once drained later -- it does not silently revert
   to the hub default just because dispatch was deferred.
3. **CLI/MCP/tool-module surface**: the CLI exposes `--timeout <seconds>` on every
   tab/device-targeting subcommand (translates to `args.timeout_s`); the MCP server and the
   Amplifier tool module expose the same thing as a `timeout_s` parameter/property. One name
   (`timeout_s`/`--timeout`), one meaning, everywhere.

**On timeout, the error is actionable, not just "timeout"**:

```json
{
  "ok": false,
  "error": "timeout waiting 120.0s for device result on command 'read' (device=cb8d..., tab_id=1565892316). The page may still be loading or a heavy SPA may still be hydrating. Raise the limit for just this command with args.timeout_s=<seconds> (CLI: --timeout <seconds>; MCP tools: timeout_s param), up to 600.0s, or raise the hub's own default with `amplifier-browser-bridge hub --command-timeout <seconds>`."
}
```

**Interaction with `wait_for`/`wait_text`'s `timeout_ms`**: that field is a page-side polling
deadline (`injected.js` polls every 150ms up to `timeout_ms`), a completely different scope from
the hub's device-round-trip wait. If a caller raises `timeout_ms` past the hub's configured
`command_timeout`, the hub will give up on the round trip and return a timeout error *before* the
in-page poll itself finishes -- raise `--timeout`/`args.timeout_s` to at least
`timeout_ms / 1000` (plus margin) when using a long `wait_for`/`wait_text`.

**SPA hydration caveat for `wake`/`navigate`**: `chrome.tabs.onUpdated` reports `status:
"complete"` when the browser finishes loading the document and its static resources -- for a
heavy client-rendered SPA, this fires well before the app has finished hydrating and rendering
real content. A `wake: true` reload (see "Discarded tabs" above) that returns as soon as the tab
reports `status: "complete"` can hand back a mostly-empty shell page rather than the app's actual
content (observed live: a woken heavy-SPA tab returned only a loading-placeholder string). This
system does **not** attempt to detect "real" hydration completion -- there is no reliable,
site-agnostic signal for it. Callers driving a known-heavy SPA should follow a `wake`/`navigate`
with an explicit `wait_for`/`wait_text` targeting a selector or text string that only appears once
the app has actually rendered, rather than assuming the immediate `read`/`snapshot` reflects final
content.

### Optional policy-hint args

Three `args` keys are recognized by the hub's policy engine (policy.py) for gate detection:

| Key | Applies to | Meaning |
|---|---|---|
| `label` | `click`, `type` | Visible text / `aria-label` of the target element. |
| `page_url` | any command without its own `url` | The tab's current URL, for gate URL-pattern matching when the command itself doesn't carry one. |
| `input_type` | `click`, `type` | Element type hint, e.g. `"file"` for an `input[type=file]`. |

As of Phase 4, a caller does **not** need to populate these itself for `click`/`type` commands
targeting a `ref`: the hub resolves `label`/`input_type` from its own remembered
`snapshot`/`wait_for` observations for that ref (see policy.py's "Label hints are now wired"
section and `Hub._ingest_result`). A caller-supplied value always takes precedence over the
remembered one. `page_url` was already resolved this way (from the hub's `_tab_hosts` cache) in
earlier phases. Explicitly supplying any of these is still supported and still wins.

### Boolean argument coercion

Every boolean-intent arg in this system (`args.trusted`, `args.capture_hidden`,
`args.all_frames`, `args.wake`, `args.activate`, `args.multi_page`, `args.active` on
`tab_open`) is coerced with a shared, tolerant helper -- `truthy()` in
`src/amplifier_browser_bridge/args_bool.py` (Python side: hub/CLI) and
`extension/args_bool.mjs` (JS side: extension) -- rather than a strict `is True`/
`=== true` identity check. This exists because a caller-supplied value can arrive in
different native shapes depending on which surface sent it:

- The CLI's `cmd` escape hatch (`amplifier-browser-bridge cmd <target> screenshot --arg capture_hidden=true`)
  parses **every** `--arg key=value` as a plain string -- `"true"`, never the bool `True`.
- The MCP server / Amplifier tool module pass a real bool from their own typed parameters.
- A caller scripting the wire protocol directly could send a bare `1`/`0`.

A strict identity check silently treats the first and third cases as `False`. This was a
real, reported bug: `amplifier-browser-bridge cmd <target> screenshot --arg capture_hidden=true` sent the string
`"true"`; `cdp.py`'s `requires_cdp()` checked `args.get("capture_hidden") is True`, which is
`False` for a string; the hub never escalated to CDP, and the device failed loud with
"screenshot requires the target tab to already be active" -- despite the caller passing
exactly the flag meant to prevent that. `truthy()` recognizes real `True`/`true`
(JS)/`False`/`false`, the strings `"true"`/`"1"` (case-insensitive, whitespace-trimmed) as
true, and everything else (including `None`/missing/`"false"`/`0`) as false -- see
`tests/test_args_bool.py` and `extension/args_bool.test.mjs`, which enumerate every boolean
arg in the codebase and assert each accepts `True`, `"true"`, and `1`.

### CDP escalation args (Phase 4)

Two more `args` keys express **caller intent** for CDP-backed dispatch -- see
`cdp.requires_cdp` and design doc §7. Neither is a raw on/off switch for CDP itself; each
describes *what the caller needs*, and the hub decides how to satisfy it (auto-attaching,
never speculatively):

| Key | Applies to | Meaning |
|---|---|---|
| `trusted` | `click`, `type`, `key` | `true` requests `isTrusted: true` input events (`Input.dispatchMouseEvent`/`dispatchKeyEvent` via CDP) -- `injected.js`'s synthetic `dispatchEvent` calls cannot produce these. |
| `capture_hidden` | `screenshot` | `true` requests capture of a tab that may not be the active tab of a focused window (`Page.captureScreenshot` via CDP) -- `chrome.tabs.captureVisibleTab` cannot do this. |

When either is set and the hub determines CDP is genuinely required (`cdp.requires_cdp`), it:

1. Checks `record.capabilities["debugger"]` -- if falsy (e.g. Edge Android), returns
   `{"ok": false, "error": "capability unavailable on this device: ..."}` immediately. **No
   silent fallback** to the injection-only path.
2. If not already attached for that tab (`Hub.cdp`, a `CdpRegistry` -- see cdp.py), sends an
   internal `attach` command and waits for it to succeed before proceeding.
3. Sets a hub-internal `_cdp: true` flag on the command's `args` before dispatching it to the
   device -- this is the ONLY way `_cdp` reaches the wire; a caller-supplied `_cdp` in its own
   request is stripped by `Hub.send_command` before any of this runs (the same
   capability-binding discipline policy.py applies to denylisted targets applies here: the hub
   decides CDP usage from its own state, never from anything the caller asserts).

**Unconditionally CDP-requiring commands (browser-state archive, D2):** `mhtml` and
`nav_history` have no `args` key to opt in -- `cdp.requires_cdp` treats both as
CDP-requiring unconditionally (`_ALWAYS_CDP_COMMANDS`), because neither has an
injection-only alternative at all (`Page.captureSnapshot`/`Page.getNavigationHistory`
are CDP methods with no `chrome.scripting` equivalent). The same three-step sequence
above still applies: capability check first (fails loud, no silent fallback, on a
device without `debugger`), then attach, then dispatch.

### `attach` / `detach` (agent -> hub -> ext)

Explicit escalation, independent of any specific command -- useful for a caller that wants to
hold a CDP session open across several commands, or to detach proactively:

```json
{"v": 1, "id": "...", "type": "command", "command": "attach", "target": {"device_id": "...", "tab_id": 7}, "args": {}, "token": "..."}
```

Result carries `{"tab_id": 7, "attached": true}` (or `{"...": "...", "already": true}` if a
session was already held). `detach` is symmetric. Both flow through the normal command
choke point (policy + queueing) like any other command -- there is nothing gate-worthy about
attaching/detaching itself, though the *target* is still subject to the denylist like any
other command.

### Soft-detach on idle

The hub sweeps for CDP sessions idle past a configurable threshold (default ~10 minutes,
`Hub`'s `cdp_idle_seconds` constructor arg) and detaches them automatically -- design doc
§6.3: "so the banner clears while the human is just browsing." This runs as a background task
(`Hub.soft_detach_loop`, started via `build_app`'s `on_startup` hook) and requires no extension
changes: it is simply a `detach` command the hub sends on its own initiative, indistinguishable
on the wire from an agent-requested one (see `Hub.soft_detach_idle_tabs`, `args: {"reason":
"idle"}`, audited as `cdp_detached` with that reason).

The idle clock only resets on CDP-*requiring* activity (an attach, or a trusted
click/type/key, or a hidden-capture screenshot) -- not on ordinary commands against the same
tab. If the agent stops needing CDP specifically but keeps issuing plain clicks, the session
still soft-detaches on schedule.

### Unsolicited CDP detach (ext -> hub, `event`)

`chrome.debugger` can be detached without the hub ever asking: the human clicking Cancel on
the yellow banner, opening DevTools (which force-detaches every session on the target), or the
target tab crashing/being discarded. The extension reports this via the existing `event`
message type:

```json
{"v": 1, "id": "...", "type": "event", "device_id": "...", "event": "cdp_detached", "data": {"tab_id": 7, "reason": "canceled_by_user"}}
```

The hub updates `Hub.cdp`'s attach state immediately on receipt. The *next* CDP-requiring
command against that tab transparently re-attaches (design doc §8: "surface real errors;
recover by re-attaching where sensible") -- there is no special recovery step a caller needs
to take.

### Reporting CDP attach state to an agent

Two places (design doc §7: "report CDP attach state per tab so an agent can reason about it"):

- `devices` (`list_devices`): each device summary gains a `"cdp"` key -- `{tab_id: {"attached":
  bool, "attached_at": iso8601|null, "last_activity": iso8601|null, "last_detach_reason":
  str|null}}` for every tab this hub has ever attached to on that device.
- `tabs`: each entry in the result gains `"cdp_attached": bool` for its current state.

### Browser-state archive (D2) -- composed, not a wire command

The ten commands above this section (`windows`, `page_state`, `mhtml`, `nav_history`,
`history_list`, `bookmarks_list`, `sessions_list`, `top_sites`, `reading_list`,
`cookies_list`) are real wire-protocol commands, individually. But the agent-facing
**archive** capability -- "capture the state of a browser at a chosen depth, get back a
manifest" -- is not itself a wire command, the same way `vision_read` composes
`screenshot` + a vision-model call without being a wire command of its own (see "Vision-based
extraction" above).

`amplifier_browser_bridge.archive.run_archive` (hub-side Python, no extension changes) composes
these ten commands into a depth ladder (L0 windows/groups/tabs inventory through L5 MHTML +
navigation history + browser-wide profile data), writes every captured payload straight to a
timestamped directory on disk, and returns a **manifest** -- paths, counts, byte sizes, per-tab
status, failures -- never the payloads themselves. This is the direct fix for the same class of
bug the tabs-pagination fix (above) addressed: a raw MHTML document, a full outerHTML dump, or a
browser's entire history can each individually be many megabytes, and returning any of them as an
agent tool's return value would recreate the exact context-truncation failure `browser_tabs`
hit. See `archive.py`'s module docstring for the full depth-ladder contract (no-wake guarantee,
per-tab failure recording, impossible-depth fail-loud behavior) and `docs/AGENT_SURFACES.md` for
the single agent-facing tool (`browser_archive`) both surfaces expose for it.

`manifest["summary"]` never collapses the INVENTORY axis (what actually exists in the browser --
`windows_inventoried`/`tab_groups_inventoried`/`tabs_inventoried`, populated at every depth
including L0) into the CAPTURE axis (what had page content pulled down --
`tabs_capture_attempted`/`tabs_captured`/`tabs_skipped`/`tabs_failed`, legitimately all `0` at
L0 by design; `profile`, `None` below L5). An L0 archive of 735 real tabs reports
`tabs_inventoried: 735` alongside `tabs_captured: 0` -- a caller must never read the latter as
"nothing was archived" when the former says otherwise.

Per-tab status is not binary either: `manifest["tabs"][tab_id]["status"]` is `"ok"` only when
every attempted capture (`text`/`dom`/`screenshot`/`mhtml`/`nav_history`, whichever ran at this
depth) succeeded, `"failed"` only when every attempted capture failed, and `"partial"` -- a
third, middle state -- when some succeeded and some failed; `"skipped"` (no-wake guarantee)
remains a separate, fourth state. `summary["tabs_partial"]` counts partial tabs explicitly,
alongside `tabs_captured`/`tabs_skipped`/`tabs_failed`. Observed live: a tab on a browser error
page failed `text`/`dom` (JS injection cannot run on an error page) while `mhtml`/`screenshot`/
`nav_history` (CDP-based, an independent capture route -- see `archive.py`'s module docstring)
all succeeded, landing ~147KB of real artifacts on disk; reporting that tab `"failed"` (the
prior, binary behavior) discarded that fact. A `"partial"` (or `"failed"`) tab always
contributes at least one entry to `manifest["failures"]`, so a run containing any partial tab
is never reported as plain `"ok"`.

A `tab_id` named in `tab_ids` can also vanish (tab closed) between when the caller read the
tab inventory and when the archive ran -- or simply never have existed at all -- a FIFTH
per-tab state, `"not_found"`, distinct from `"ok"`/`"partial"`/`"failed"`/`"skipped"`. Observed
live: a caller requesting 4 tab_ids got manifest entries for only 3 -- the vanished tab
appeared in no `manifest["tabs"]` entry, no `manifest["failures"]` entry, and no `"skipped"`
record, so nothing told the caller their fourth tab was ever requested. `manifest["tabs"][tab_id]`
now always gets a `{"status": "not_found", "reason": ...}` entry for a vanished id, **at every
depth, including L0** -- this accounting is computed once from `tab_ids` against the live
inventory and does not depend on any per-tab capture running (an earlier version of this fix
computed it only alongside L1+ capture, so an L0 request for a nonexistent `tab_id` still
silently reported plain `"ok"`), so every id in `tab_ids` is accounted for one way or another
regardless of depth. This is benign (there is no tab left to capture, so it is
not a capture failure) and never adds to `manifest["failures"]`, but -- like `"skipped"`
tabs -- it is never silently folded into a plain `"ok"` run either: `summary["tabs_not_found"]`
counts it explicitly, `summary["tabs_capture_attempted"]` excludes it (a vanished tab was never
actually attempted), and `manifest["status"]` becomes `"ok_with_skips"`, the same bucket
`"skipped"` tabs use since both are benign, non-failure gaps. The top-level
`manifest["requested_tab_ids_not_found"]` list remains a convenience summary of the same ids.

#### Injection budget and explicit capture selection

Because the depth ladder is a strict superset, requesting L4 (MHTML) also runs L1 (`text`) and L2
(`dom`) first -- both JS-injection-based (`read`/`page_state`). Real-world finding: on heavy
hydrated SPAs (github.com, huggingface.co), the injection captures timed out at BOTH 90s and 120s
budgets, while the CDP-based captures (`mhtml`/`screenshot`/`nav_history`) succeeded on the SAME
tabs in seconds. For an archive spanning many tabs, every tab pays up to two full injection
timeouts before reaching the capture the caller actually wanted.

Two independent, optional knobs address this WITHOUT abandoning the strict-superset property as
the default:

- **`injection_timeout_s`** (`run_archive`/`browser_archive`): overrides `timeout_s` for JUST the
  injection-based captures (`text`/`dom`) -- CDP-based captures keep using `timeout_s` (or the hub
  default) unchanged. Bounds the wall-clock cost of a hung/heavy SPA's injection captures without
  ever reducing what the depth ladder attempts: `text`/`dom` are still attempted, they just fail
  fast (an ordinary `"failed"` capture) instead of consuming the full budget. `None` (the default)
  means no override -- behavior is byte-for-byte unchanged from before this argument existed.
- **`captures`** (`run_archive`/`browser_archive`): an explicit allow-list, from `CAPTURE_NAMES`
  (`"text"`, `"dom"`, `"screenshot"`, `"mhtml"`, `"nav_history"`), that narrows -- never widens --
  which per-tab captures actually run at this depth. `captures=["mhtml", "screenshot",
  "nav_history"]` with `depth="L4"` is a "CDP-only" archive: `text`/`dom` are never attempted at
  all for any tab, zero wall-clock cost, instead of attempted-then-bounded. A capture the depth
  ladder would otherwise have included, but that `captures` excludes, is recorded as
  `{"status": "skipped", "reason": ...}` -- the SAME status a no-wake tab skip or an opt-out
  `cookies_list` skip already uses -- never silently omitted, and excluded from the ok/failed
  accounting a tab's own rollup status computes (`archive.py`'s `_tab_status`), so a tab where
  every ATTEMPTED capture succeeded still reports plain `"ok"` even though some captures were
  configured out. `None` (the default) means no narrowing -- the pre-existing strict-superset
  behavior. `manifest["captures_requested"]` records exactly what was passed (or `None`), so a
  caller reading the manifest later can tell what was actually asked for.

  Validated PRE-FLIGHT (`archive.py`'s `_validate_captures`), before anything is captured: an
  empty list, an unrecognized name, or a `captures` set naming nothing reachable at the requested
  `depth` (e.g. `captures=["mhtml"]` with `depth="L1"`, which would silently capture nothing for
  every tab in the run) all raise `ArchiveError` immediately -- the same fail-loud posture as the
  "Impossible depth" check above, one level down.

A caller must never be able to silently get less than they asked for: a capture skipped by
`captures` is always distinct from one attempted-and-failed, exactly as `"skipped"`/`"not_found"`
already are at the tab level.

#### Queued commands and pacing (large archives)

`run_archive` is one caller among several that issues ordinary `command()` requests over the
`/agent` route -- so it is fully subject to the queued-command contract above ("This is the
load-bearing non-blocking guarantee"): a per-tab/profile-data command dispatched to a non-live
device returns `{"status": "queued", ...}` immediately. `run_archive` follows a queued response
with `poll` until it resolves to a real result -- or gives up after `poll_max_wait_s` (default
90s, checked every `poll_interval_s`, default 2s) and records an honest failure rather than
hanging the archive indefinitely. This closes a real-world gap: a prior version treated the
queued response ITSELF as the final, failed outcome and never called `poll` at all, so when a
126-tab archive's own command volume pushed the device off `live` partway through, ~200
commands the device went on to actually execute were all recorded as failures -- the work was
done; the answers were discarded. `manifest["pacing"]["queued_waits"]`/`["queued_wait_total_s"]`/
`["queued_timeouts"]` summarize how much of this happened during a given run.

The same real-world archive is also why `run_archive` paces its own CDP-requiring per-tab
dispatches (`mhtml`, `nav_history`, and `screenshot` when using `capture_hidden` -- see
"Unconditionally CDP-requiring commands" above): nothing previously limited how fast these were
fired, and doing so back-to-back with zero gap is what overwhelmed the device's extension in the
first place. `cdp_pace_s` (default 0.2s) enforces a floor on the interval between successive CDP
dispatches; independently, before each one, a cheap hub-local `list_devices()` tier check waits
(bounded exponential backoff, cap `cdp_backpressure_max_wait_s`, default 20s) for a
non-`live` device to recover before proceeding, rather than adding more commands to a device that
has already shown it cannot keep up. Both are advisory pacing, not a correctness gate -- the
queued/poll handling above is what guarantees a correct eventual outcome regardless of whether
pacing was enough. `run_archive`'s optional `on_progress` callback receives structured events
(`tab_done`, `queued_wait_started`/`_resolved`/`_gave_up`, `backpressure_waiting`/`_resumed`,
`archive_finished`) live as the run progresses -- a small sample (e.g. 5 tabs) never saturates
anything, so a live signal during a large run is worth more than a happy-path number measured on
a handful of tabs. See `archive.py`'s module docstring ("Queued means wait, not fail" and
"Pacing") for the full design and the measured incident both respond to.

## Browser-state archive: MHTML -> markdown conversion (composed, not a wire command)

Like the archive capability itself, converting a captured page's MHTML into markdown --
`amplifier_browser_bridge.mhtml_convert.convert_mhtml`, composed by `archive_convert.run_archive_convert`
-- is not a wire command; it does no browser interaction at all, and runs entirely against
`.mhtml` files a prior `run_archive` (at depth L4+) already wrote to disk. It is exposed as
exactly one agent-facing tool, `browser_archive_convert` (`docs/AGENT_SURFACES.md`'s "Browser-state
archive: MHTML -> markdown conversion" section), invoked as a distinct, later, OPT-IN step -- never
automatically as part of `run_archive`/`browser_archive` itself, the same mechanism/policy split
`vision_read`/`vision.py` establish for the vision-extraction feature.

**Why MHTML, not `outer_html`**: measured live, on the same real tabs, the JS-injection capture
route (`read`/`page_state` -> `outer_html`) failed on 7 of 7 real tabs -- including a browser
error page it could not touch at all -- timing out at both 90s and 120s budgets; the CDP-based
route (`mhtml`/`screenshot`/`nav_history`) succeeded 3 of 3 on those same tabs. Published
extraction-research benchmarks recommend converting from live-rendered `outer_html`, not a saved
MHTML snapshot -- but every one of those benchmarks measured server-rendered HTML, not a
post-hydration DOM fetched over a websocket from a remote browser (a configuration nobody has
benchmarked), and in THIS system `outer_html` is not reliably obtainable at all. MHTML is the
only full-page capture this converter can depend on.

`convert_mhtml` parses the MHTML document as ordinary MIME (Python's own `email` module -- MHTML
is RFC 2557 multipart/related, no custom parser needed) and produces, for each archived tab, TWO
markdown files: `page.extracted.md` (trafilatura's main-content extraction -- its own published
benchmark scores F1 0.924 against a raw-HTML do-nothing baseline of 0.667, and it emits markdown
directly, so this is an extract-then-convert pipeline, not convert-then-clean) and
`page.full_page.md` (a deliberately unfiltered whole-page conversion via html2text, so a bad
extraction -- main-content quality swings 0.42-0.93 by page type per the WCXB benchmark, and 47%
of real pages are non-articles -- is recoverable rather than lossy). Every non-HTML MIME part
(images/CSS/fonts) is written as a content-addressed sidecar (`<sha256-of-bytes><ext>`) under a
SHARED `archive_dir/assets/` directory -- shared across every tab converted into the same
archive, so identical assets dedupe -- and the HTML's own `Content-Location`/`cid:` asset
references are rewritten to the sidecar's relative path BEFORE conversion, never base64-inlined.

A table with merged cells (`colspan`/`rowspan`) cannot be expressed as a markdown pipe table -- a
FORMAT limitation, not a tooling gap this converter tries to work around. Affected tables are
named explicitly in the result's `tables_with_merged_cells` list (table index + a short text
preview) rather than silently producing wrong-looking output. A page containing more than one
`text/html` body (an `<iframe>`-heavy page captured as separate frame documents) is the documented
hard case this converter does not attempt to merge -- `convert_mhtml` raises `ConversionError`
naming every frame's `Content-Location` rather than silently converting only the first body as if
it were the whole page; `run_archive_convert` catches this per tab (recorded as that tab's
`{"status": "failed", "error": ...}`) so one unconvertible tab does not abort the whole archive's
conversion run, mirroring `run_archive`'s own no-abort-on-one-bad-tab behavior.

Like `browser_archive`, `browser_archive_convert` returns only a MANIFEST -- paths, byte counts,
per-tab status, warnings -- never the markdown text itself, written to
`archive_dir/conversion_manifest.json` as well as returned. A `tab_ids` id with no `page.mhtml` on
disk gets a `{"status": "not_captured", ...}` entry (counted in `summary["tabs_not_captured"]`,
moving `manifest["status"]` to `"ok_with_skips"`) rather than being silently absent from
`manifest["tabs"]` -- the same discipline `run_archive`'s own `"not_found"` per-tab state applies
to a vanished `tab_id` (see above).

## The three-tier connectivity model

| Tier | Meaning | Agent-visible behavior |
|---|---|---|
| `live` | Hub currently holds an open websocket to the device | Command executes now; response carries `ok`/`result` or `ok`/`error` |
| `intermittent` | Disconnected, but recently seen (< 150s) | Command is queued; response is `{status: "queued", ...}` immediately |
| `dormant` | Disconnected for >= 150s, or never seen | Same as intermittent -- queued, drains whenever the device reconnects |

Thresholds come directly from measured dark-window data (design doc §2, §5): mobile devices
with a battery-optimization exemption showed dark windows of 43-133s that always
self-healed; without the exemption, a 509s dark window with zero self-recovery was observed.
150s is padded above the self-healing ceiling. See `tiers.py` for the single function that
implements this.

**Queued is a real, inspectable state** (`queue_position`, `tier`, `last_seen` are all
returned), never a hidden block. When a device transitions back to `live` (a fresh `hello`),
the hub drains its queue in strict FIFO order (`queue.py`), dispatching each command exactly as
if it had just been requested, and results become retrievable via `poll`.

### `kill_switch_engage` / `kill_switch_disengage` / `kill_switch_status` (agent -> hub) / `result` (hub -> agent)

A4 fix (security review finding): `docs/POLICY.md` section 5 and README's consent table always
described a hub-level kill switch as an available control; until this pass it was reachable only
by an embedding application calling `Hub.engage_kill_switch()`/`disengage_kill_switch()` directly
in-process -- no wire message, and no CLI command, reached it.

```json
{"v": 1, "id": "...", "type": "kill_switch_engage", "token": "..."}
```
```json
{"v": 1, "id": "...", "type": "result", "ok": true, "kill_switch_active": true, "rejected_queued_commands": 3}
```

`kill_switch_engage` halts all future dispatch immediately and rejects (does not silently drop)
every command currently sitting in a per-device queue -- see `Hub.engage_kill_switch`'s
docstring for the honest limit: it does NOT recall a command already in flight to a device.
`kill_switch_disengage` restores normal dispatch (`{"ok": true, "kill_switch_active": false}`).
`kill_switch_status` reports the current state without changing it
(`{"ok": true, "kill_switch_active": bool}`). All three are ordinary token-checked agent-route
requests -- same path as `command`/`confirm`/`establish_session`. CLI: `amplifier-browser-bridge kill-switch
engage|disengage|status`.

## Authentication

Two layers (design doc §4):

1. **Tailscale ACLs** -- the outer boundary, configured outside this codebase.
2. **Per-device shared token** -- `token` field on `hello` (device route) and on every agent
   request. See `auth.py` for the resolution order (env var -> token file -> disabled/dev-mode).
   Real tokens are never committed; the extension's `config.js` ships an obviously-fake
   placeholder that must be changed to match whatever the hub operator configured.
