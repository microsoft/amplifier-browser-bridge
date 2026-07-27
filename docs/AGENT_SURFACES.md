# Agent surfaces: MCP server and Amplifier tool module

Design doc section 3.3 names four agent-surface levels; the lib and CLI shipped in
Phase 1. This doc covers the two Phase 2 surfaces -- both are thin adapters over
the same lib (`client.py`, `addressing.py`, `tiers.py`); neither implements any
new logic.

Both surfaces expose the same twenty-two tools, named `browser_<command>` (mirroring
Playwright MCP's vocabulary, design doc section 9):

| Tool | Command | Notes |
|---|---|---|
| `browser_devices` | `list_devices` | Entry point -- call first |
| `browser_tabs` | `tabs` | Entry point -- call second, to get `tab_id` values; each entry carries `discarded`/`status` |
| `browser_snapshot` | `snapshot` | Accessibility-style tree with element `ref`s; optional `wake` (see Discarded tabs, docs/PROTOCOL.md) |
| `browser_read` | `read` | Full visible text; optional `wake` |
| `browser_click` | `click` | `ref` |
| `browser_type` | `type` | `ref`, `text` |
| `browser_key` | `key` | `key`, optional `ref` |
| `browser_scroll` | `scroll` | `x`, `y` |
| `browser_navigate` | `navigate` | `url` |
| `browser_tab_open` | `tab_open` | device-only target; `url`, `active` (default background) |
| `browser_tab_close` | `tab_close` | |
| `browser_tab_activate` | `tab_activate` | the one command allowed to steal focus |
| `browser_screenshot` | `screenshot` | active-tab-only in this injection-only phase |
| `browser_wait_for` | `wait_for` | `selector`, `timeout_ms` |
| `browser_wait_text` | `wait_text` | `text`, `timeout_ms` |
| `browser_poll` | (agent-only `poll`) | check on / retrieve a previously queued command |
| `browser_reload` | `reload` | device-only target; self-service extension reload (see docs/PROTOCOL.md) |

See `docs/PROTOCOL.md` for the exact command semantics and `docs/designs/browser-bridge.md`
for the addressing model (`device_id` -> `window_id`/`tab_id` -> `ref`) and the
three-tier connectivity model (`live` / `intermittent` / `dormant`). See
`docs/DECISION_GUIDE.md` for WHICH of these tools to reach for and when -- a dozen
read/act mechanisms plus modifiers (`wake`, `activate`, `trusted`, `capture_hidden`) is
real power with no map otherwise.

## The one thing both surfaces must get right: tier pass-through

A command sent to a device that is not `live` returns **immediately** as
`{"status": "queued", "command_id": ..., "tier": ..., "last_seen": ...,
"queue_position": ...}` instead of `{"ok": ..., "result"/"error": ...}`. Both
adapters below hand this shape to the calling agent completely unmodified --
never flattened into an error, never blocked on, never silently retried. Every
tool description says so explicitly (not just the server-level instructions),
because an MCP client typically shows one tool's description in isolation.

## MCP server

Any MCP-speaking client -- Claude Desktop, an Amplifier bundle, a bare `mcp`
CLI/SDK session -- can drive the bridge with zero Amplifier dependency.

### Install and run

```bash
uv pip install -e ".[mcp]"   # installs the optional `mcp` dependency
amplifier-browser-bridge-mcp                       # runs over stdio (the default every MCP client speaks)
```

Environment variables (same ones the CLI uses):

| Variable | Default | Purpose |
|---|---|---|
| `AMPLIFIER_BROWSER_BRIDGE_HUB_URL` | `ws://127.0.0.1:8900/agent` | Hub's agent-route WebSocket URL |
| `AMPLIFIER_BROWSER_BRIDGE_TOKEN` | unset | Per-device/agent shared token, if hub auth is enabled |
| `AMPLIFIER_BROWSER_BRIDGE_MCP_TRANSPORT` | `stdio` | `stdio`, `sse`, or `streamable-http` |

### Pointing an MCP client at it

Any client that can launch a subprocess over stdio works. For example, in a
Claude Desktop-style `mcp_servers.json`:

```json
{
  "mcpServers": {
    "amplifier-browser-bridge": {
      "command": "amplifier-browser-bridge-mcp",
      "env": { "AMPLIFIER_BROWSER_BRIDGE_HUB_URL": "ws://<this machine's tailnet IP>:8900/agent" }
    }
  }
}
```

### Verified end-to-end (proof)

Run with a real hub (`amplifier-browser-bridge hub`) and a real MCP client (the `mcp` Python SDK's
`ClientSession` + `stdio_client`, launching `amplifier-browser-bridge-mcp` as a subprocess):

```
=== TOOL LIST ===
- browser_devices: List every known browser device (every device the hub has ever received a
- browser_tabs: List open tabs on a device, optionally scoped to one window_id. Use this
... (16 tools total)

=== CALL browser_devices() against a running hub with zero connected devices ===
{
  "ok": true,
  "devices": []
}
```

An empty device list is a valid, honest result -- it proves the surface works
end-to-end without needing a real Edge browser attached.

Tier pass-through, proven against a simulated non-live device (a raw WebSocket
client that said `hello` to the hub's `/device` route, then disconnected):

```
=== browser_snapshot(device_id='sim-phone-1', tab_id=1) -- device is NOT live ===
{
  "status": "queued",
  "command_id": "5822ba9a-3afa-4684-b2e5-2cb590b6d046",
  "tier": "intermittent",
  "last_seen": "2026-07-25T22:49:27.130880+00:00",
  "queue_position": 1
}

=== browser_poll(device_id='sim-phone-1', command_id=<above>) ===
{
  "status": "queued",
  "queue_position": 1,
  "tier": "intermittent"
}
```

The MCP tool call returned instantly with the queued/tier shape intact -- it did
not block, and it was not reported as an error.

## Amplifier tool module

`modules/tool-browser-bridge/` wraps the same lib as sixteen Amplifier tools,
following the `mount()` Iron Law (`creating-amplifier-modules` skill): each tool
is registered via `await coordinator.mount("tools", tool, name=tool.name)`.

### Adding the bundle

See `bundle.md` at the repo root for the exact `tools:` YAML stanza (a published
git source, or a local relative path for development against a checkout of this
repo).

### Local development note

This module's `pyproject.toml` declares `amplifier-browser-bridge` as a plain
dependency, with a `[tool.uv.sources]` override pointing at the repo root
(`../..`) for monorepo local development -- respected by `uv`, ignored by plain
`pip`. Until this repo is published somewhere `pip`/`uv` can fetch
`amplifier-browser-bridge` from directly (PyPI, or a git dependency), installing
this module standalone with `pip`/`uv` outside this repo requires either
`--no-deps` (relying on the sibling package already being installed, as it is in
this repo's own `.venv`) or updating the dependency to a git URL once one exists.

### Verified protocol compliance (proof)

Ran `amplifier_core.validation.tool.ToolValidator` directly against the module
directory (this is the same check Amplifier's module loader performs before
mounting a tool module into a session):

```
INFO protocol_compliance - Tool 'browser_devices' implements Tool interface
INFO tool_name - Tool has name: 'browser_devices'
INFO tool_description - Tool has description
INFO tool_input_schema - Tool.input_schema returns dict with 0 properties
INFO tool_execute - Tool.execute() has correct async signature
... (all 16 tools, all checks PASS)

PASSED
```

### Tests

```bash
cd modules/tool-browser-bridge
uv pip install -e . --no-deps   # sibling package already in the repo's .venv
python -m pytest tests/ -v      # 10 passed
```

## Known pyright false positive

Both `mcp_server.py` and the tool module import packages (`mcp`,
`amplifier_browser_bridge`, `amplifier_core`) that are correctly installed in
this repo's `.venv`, but the automated `python_check` tool in this environment
resolves imports against a different Python environment and reports
`reportMissingImports` for them. Verified independently:

```bash
$ source .venv/bin/activate
$ pyright --venvpath . modules/tool-browser-bridge/amplifier_module_tool_browser_bridge/__init__.py
0 errors, 0 warnings, 0 informations
```

`pyright` run directly against this repo's own `pyproject.toml` (which sets
`venvPath = "."`, `venv = ".venv"`) resolves every import cleanly. `ruff format`,
`ruff lint`, and the stub-detection check all pass clean under `python_check` as
well -- only cross-environment import resolution is affected.
