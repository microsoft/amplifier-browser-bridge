# Amplifier Browser Bridge

Lets an AI agent on one device observe and drive the user's real, logged-in Microsoft
Edge browser on another device -- over the user's own Tailscale network. The agent is a
second operator in a live browsing session, not a robot driving a disposable browser.

Read `docs/designs/browser-bridge.md` first: the full architecture, and the measured
evidence behind every constraint (transport, connectivity tiers, capability model). Read
`docs/PROTOCOL.md` for the wire protocol this package implements.

## Status: Phase 1

This phase proves the core pipe end-to-end: hub, extension, addressing, and the Python
lib/CLI. Not yet built (see the design doc's build order, §10): the MCP server, the
Amplifier tool module, the policy/consent engine, and CDP escalation.

## Layout

```
docs/designs/browser-bridge.md   -- the design (read this first)
docs/PROTOCOL.md                 -- the wire protocol
docs/AGENT_SURFACES.md           -- the MCP server and Amplifier tool module (agent surfaces)
bundle.md                        -- Amplifier bundle composing the tool-browser-bridge module
src/amplifier_browser_bridge/    -- the lib: protocol, addressing, tiers, hub, client, CLI, mcp_server
modules/tool-browser-bridge/     -- the Amplifier tool module (thin adapter over the lib)
extension/                       -- the MV3 browser extension (one build, all platforms)
tests/                           -- unit tests for everything testable without a live browser
```

## Agent surfaces: MCP server and Amplifier tool module

Beyond the CLI above, the lib is also exposed as an MCP server (`abb-mcp`, any
MCP-speaking client) and an Amplifier tool module (`modules/tool-browser-bridge/`,
composed via `bundle.md`). See `docs/AGENT_SURFACES.md` for how to run and
configure each, and the proof that both work end-to-end.

## Running the hub

```bash
uv pip install -e .
abb hub --host 0.0.0.0 --port 8900
```

Auth is disabled by default in dev (loudly logged). To enable it, set `ABB_HUB_TOKEN`
(and match it in `extension/config.js`'s `HUB_TOKEN`, and pass `ABB_TOKEN` to the CLI).

## Loading the extension

Edit `extension/config.js` so `HUB_URL` points at your hub's tailnet IP (never a
MagicDNS name -- see the design doc §4 for why), then load `extension/` as an unpacked
extension in Edge (`edge://extensions` -> Developer mode -> Load unpacked).

## CLI

```bash
abb devices                              # list connected/known devices
abb tabs <device_id>                     # list tabs on a device
abb snapshot <device_id>/<tab_id>        # accessibility-style snapshot with element refs
abb read <device_id>/<tab_id>            # visible text of a tab
abb click <device_id>/<tab_id> <ref>     # click an element by ref
abb navigate <device_id>/<tab_id> <url>  # navigate a tab
abb cmd <target> <command> --arg k=v     # escape hatch for any vocabulary command
```

See `docs/PROTOCOL.md` for the full command vocabulary and target addressing format.

## Testing

```bash
uv pip install -e ".[dev]"  # or: uv pip install -e . pytest
pytest tests/
```
