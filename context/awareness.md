# Browser Bridge: addressing, tiers, queued results

25 `browser_*` tools (verified 2026-08-08 in both the native module and the
MCP server; they differ by one tool each -- native has `browser_reload`, MCP
has `browser_confirm`. This repo's earlier "sixteen"/"twenty-two" tool-count
claims were stale -- see `docs/AGENT_SURFACES.md` for the corrected list.)

**Entry point, every time:** `browser_devices()` for `device_id`s and their
tier, then `browser_tabs(device_id)` for `tab_id`s, then
`browser_snapshot(device_id, tab_id)` for element `ref`s to pass to
`browser_click`/`browser_type`/`browser_key`. No implicit "current" device
or tab -- every call addresses one explicitly.

**The hazard you must not misread:** a command to a non-`live` device returns
immediately as `{"status": "queued", "command_id": ..., "tier": ...,
"queue_position": ...}`, not `{"ok": ...}`. This is a normal, actionable
result -- never an empty success, never an error. Call
`browser_poll(device_id, command_id)` later for the eventual result
(`intermittent` devices typically drain in ~1-2 min; `dormant` devices drain
whenever they next reconnect).

**Configuration is required:** the tool module reads
`AMPLIFIER_BROWSER_BRIDGE_HUB_URL` (default `ws://127.0.0.1:8900/agent`) and
`AMPLIFIER_BROWSER_BRIDGE_TOKEN` (default unset) from the session's
environment, not from this bundle. Left unset, calls target an unauthed
localhost hub and do nothing useful unless one happens to be running there.
Point `HUB_URL` at the hub's Tailscale IP -- never a MagicDNS name.

Full vocabulary/protocol: `docs/AGENT_SURFACES.md`, `docs/DECISION_GUIDE.md`,
`docs/PROTOCOL.md`.
