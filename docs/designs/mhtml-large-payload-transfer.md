# Follow-up: MHTML large-payload transfer is a single unstreamed `ws.send`

**Status:** Open design issue (not yet scheduled). Filed alongside the
`fix/cdp-wake-wait-for-load` work, which is a *separate* fix.

## Why this is tracked separately from the discard/wake fix

Live testing of `browser_archive` on a real device (macOS Edge over Tailscale,
freshly rebooted) surfaced **two distinct failure modes** on the same run.
They must not be conflated:

1. **Deep-discard wake race** — a discarded tab, cold-woken by the CDP capture
   path, fails screenshot (`-32603 Internal error`) and mhtml
   (`Detached while handling command`) because `cdpAttach()` did not wait for
   the renderer to finish loading before capturing. **This is fixed** by
   `fix/cdp-wake-wait-for-load` (CDP path now reuses `waitForTabAwake()`).

2. **MHTML large-payload transfer over a flaky link** — the subject of *this*
   note. It is NOT a discard problem: it reproduced on a fully-loaded,
   never-discarded tab.

## The observation

During the discard investigation, `mhtml` capture failed **device-wide** across
three consecutive single-tab runs, including a known-good, never-discarded tab
(`github.com/michaeljabbour/project-context`):

| run | tab | screenshot | mhtml |
|-----|-----|-----------|-------|
| 1 | onecli (discarded→navigated+loaded) | ok (402 KB) | failed — `device disconnected mid-command` |
| 2 | onecli (same, loaded) | ok (402 KB) | failed — `device disconnected mid-command` |
| 3 | project-context (known-good, loaded) | ok (397 KB) | failed — `could not reach hub … timed out` |

Screenshot (small JPEG payload) succeeded every time; mhtml (multi-MB text
payload) failed every time. The device had recently rebooted and the Tailscale
link was unstable. The pattern is consistent with a **large single-frame
transfer failing on a degraded connection**, while small payloads get through.

## The structural cause (read from code, not inferred)

The extension returns an entire captured MHTML document to the hub as **one
JSON-serialized WebSocket text frame**, via a single unstreamed send:

```js
// extension/background.js — send()
function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));   // whole multi-MB MHTML string, one shot
  }
}
```

- `Page.captureSnapshot` (`{format: "mhtml"}`) returns the full serialized MHTML
  as `result.data` (a string), which is embedded whole into the command result.
- There is **no application-layer chunking, streaming, or retry** of an
  individual capture's payload bytes. (The `cdp_chunk_size`/`_CdpPacer`
  machinery chunks the *tab list* the archive iterates over — not a single
  capture's bytes.)
- A 64 MB ceiling (`MAX_WS_MESSAGE_BYTES`) is enforced symmetrically on both
  ends and fails *cleanly* when exceeded — but that is a size cap, not a
  reliability mechanism. It does nothing for a payload that is under the cap yet
  large enough that one shot across a flaky link is fragile.

**Consequence:** even a fully-loaded, painted tab's mhtml capture can fail
purely from payload size × connection instability. On a good link this is
invisible; on a degraded link (VPN reconnect, post-reboot Tailscale, congested
network) a large-page mhtml has exactly one chance to cross the wire as a unit,
and a mid-transfer degradation surfaces as `device disconnected mid-command` or
a hub timeout — indistinguishable at the surface from a genuine device drop.

## Options (not yet decided)

- **Chunked/streamed capture transfer** — split a large capture payload into
  bounded application-level chunks with per-chunk acknowledgement + reassembly
  on the hub. Highest effort; fully addresses the fragility.
- **Retry-on-send-failure for capture payloads** — bounded retry of a failed
  large capture (re-issue `Page.captureSnapshot` and resend) once the socket
  recovers. Cheaper; helps transient drops, not sustained degradation.
- **Hub-side persistence with resumable transfer** — write capture bytes to a
  side channel rather than the command WebSocket. Larger architectural change.
- **Do nothing / document** — accept that large-page mhtml over a flaky link is
  best-effort and honestly reported as a per-capture failure. The archive
  already records it loud (per-tab `failed`, not silent).

## Reproduction notes for whoever picks this up

- Reproduces on a **degraded** connection; a healthy link hides it. To force it,
  archive a large GitHub repo landing page at L4 (`captures` including `mhtml`)
  while the link is unstable, or throttle the connection.
- Screenshot succeeding while mhtml fails on the *same* tab is the tell that
  this is a payload-size/transport issue, not a wake/render issue.
- Isolated from the discard fix: this note deliberately changes **no** transfer
  code. The `fix/cdp-wake-wait-for-load` branch leaves `send()` untouched.
