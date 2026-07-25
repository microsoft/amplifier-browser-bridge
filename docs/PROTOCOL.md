# Wire Protocol

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
    "scripting": true
  },
  "protocol_version": 1,
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

Sent by the hub every 20s to any connected device as a keepalive. The extension replies with a
`heartbeat`. Measured to hold a desktop MV3 service worker alive indefinitely (165 min, zero
gaps in the underlying probe work -- see design doc §2).

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
      "connected": true,
      "tier": "live",
      "last_seen": "2026-07-25T18:03:11.482+00:00",
      "queue_length": 0
    }
  ]
}
```

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
see docs/POLICY.md for the full category list and the honest limits of detection):
```json
{
  "v": 1,
  "id": "...",
  "type": "result",
  "status": "needs_confirmation",
  "confirmation_token": "9f2c...hex",
  "category": "delete",
  "detected": {"category": "delete", "label_match": "\\bdelete\\b", "url_match": null}
}
```
The command was **not** dispatched to the device. Re-submit it via `confirm` (below) with the
same `confirmation_token` to execute it, or let the token expire (5 minutes by default) to
abandon it.

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
| `snapshot` | injected.js | Accessibility-style tree, stable `eN` refs |
| `read` | injected.js | Full visible text of the page |
| `click` | injected.js | `args.ref` |
| `type` | injected.js | `args.ref`, `args.text` |
| `key` | injected.js | `args.ref` (optional), `args.key` |
| `scroll` | injected.js | `args.x`, `args.y` |
| `back` / `forward` | injected.js | `history.back()`/`forward()` |
| `wait_for` | injected.js | `args.selector`, `args.timeout_ms`; polls, never sleeps blindly |
| `wait_text` | injected.js | `args.text`, `args.timeout_ms`; polls, never sleeps blindly |
| `tabs` | background.js (`chrome.tabs.query`) | optionally scoped by `target.window_id` |
| `tab_open` | background.js (`chrome.tabs.create`) | target is device-only; `args.url`, `args.active` (default background) |
| `tab_close` | background.js (`chrome.tabs.remove`) | |
| `tab_activate` | background.js (`chrome.tabs.update`) | the one command that's explicitly *allowed* to steal focus, because it was asked to |
| `screenshot` | background.js (`chrome.tabs.captureVisibleTab`, or CDP `Page.captureScreenshot` -- see CDP section below) | Injection-only by default: only works if the target tab is already active. Pass `args.capture_hidden: true` to auto-escalate to CDP for any-tab/hidden capture. |
| `attach` | background.js (`chrome.debugger.attach`) | Phase 4: explicit CDP attach for a tab. See CDP section below. |
| `detach` | background.js (`chrome.debugger.detach`) | Phase 4: explicit CDP detach for a tab. |

Commands are partitioned into `PAGE_WORLD_COMMANDS` (dispatched into `injected.js` running in
the page's isolated world) and `BROWSER_LEVEL_COMMANDS` (handled directly by
`background.js` against `chrome.tabs`/`chrome.windows`/`chrome.debugger`, which are not
reachable from page context). See `protocol.py` for the exact partition.

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

## Authentication

Two layers (design doc §4):

1. **Tailscale ACLs** -- the outer boundary, configured outside this codebase.
2. **Per-device shared token** -- `token` field on `hello` (device route) and on every agent
   request. See `auth.py` for the resolution order (env var -> token file -> disabled/dev-mode).
   Real tokens are never committed; the extension's `config.js` ships an obviously-fake
   placeholder that must be changed to match whatever the hub operator configured.
