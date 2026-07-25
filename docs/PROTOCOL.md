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
| `/agent` | CLI / lib / (later) MCP server | agent -> hub | `list_devices`, `command`, `poll` |
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
each one calls. `debugger` is unconditionally `false` in this phase -- the manifest does not
request the `debugger` permission at all (injection-only is the default posture; CDP escalation
is a later phase).

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
| `screenshot` | background.js (`chrome.tabs.captureVisibleTab`) | **injection-only limitation**: only works if the target tab is already active (no CDP this phase) -- fails loud rather than activating the tab to comply |

Commands are partitioned into `PAGE_WORLD_COMMANDS` (dispatched into `injected.js` running in
the page's isolated world) and `BROWSER_LEVEL_COMMANDS` (handled directly by
`background.js` against `chrome.tabs`/`chrome.windows`, which are not reachable from page
context). See `protocol.py` for the exact partition.

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
