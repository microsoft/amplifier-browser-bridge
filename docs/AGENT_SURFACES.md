# Agent surfaces: MCP server and Amplifier tool module

Design doc section 3.3 names four agent-surface levels; the lib and CLI shipped in
Phase 1. This doc covers the two Phase 2 surfaces -- both are thin adapters over
the same lib (`client.py`, `addressing.py`, `tiers.py`); neither implements any
new logic.

**Verified 2026-08-08 (grep-counted directly against the code, not assumed):**
the native Amplifier tool module registers **25** tools; the MCP server also
registers **25**. They are not byte-identical sets -- they differ by exactly
one tool each: the native module has `browser_reload` (the MCP server does
not), and the MCP server has `browser_confirm` (the native module does not).
Every other name below is shared by both, named `browser_<command>`
(mirroring Playwright MCP's vocabulary, design doc section 9). Earlier
revisions of this doc claimed "twenty-two" tools and this repo's `bundle.md`
claimed "sixteen" -- both were stale as of this correction; see
`amplifier_module_tool_browser_bridge/__init__.py`'s `_build_tools()` and
`mcp_server.py`'s `@mcp.tool()` decorators for the authoritative, current
lists.

| Tool | Command | Notes | Surface |
|---|---|---|---|
| `browser_devices` | `list_devices` | Entry point -- call first | both |
| `browser_tabs` | `tabs` | Entry point -- call second, to get `tab_id` values; each entry carries `discarded`/`status`; PAGED by default (`limit`/`offset`), filterable (`window_id`/`url_contains`/`title_contains`), and has a `summary` mode -- see "browser_tabs: pagination, filtering, and summary mode" below | both |
| `browser_snapshot` | `snapshot` | Accessibility-style tree with element `ref`s; optional `wake`/`activate` (see Discarded tabs, docs/PROTOCOL.md) | both |
| `browser_read` | `read` | Full visible text across all frames; optional `wake`/`activate` | both |
| `browser_click` | `click` | `ref`, optional `session_id` | both |
| `browser_type` | `type` | `ref`, `text`, optional `session_id` | both |
| `browser_key` | `key` | `key`, optional `ref`, `session_id` | both |
| `browser_scroll` | `scroll` | `x`, `y` | both |
| `browser_navigate` | `navigate` | `url`, optional `session_id` | both |
| `browser_tab_open` | `tab_open` | device-only target; `url`, `active` (default background) | both |
| `browser_tab_close` | `tab_close` | | both |
| `browser_tab_activate` | `tab_activate` | the one command allowed to steal focus | both |
| `browser_screenshot` | `screenshot` | pixels only, no model call; `capture_hidden`, `frame_id`, `multi_page` | both |
| `browser_vision_read` | (composed: `screenshot` + vision-model extraction) | TEXT extracted from pixels via a configured vision provider | both |
| `browser_wait_for` | `wait_for` | `selector`, `timeout_ms` | both |
| `browser_wait_text` | `wait_text` | `text`, `timeout_ms` | both |
| `browser_fetch_bytes` | `fetch_bytes` | device-only target; fetch a URL from the extension's own (cookied) context | both |
| `browser_grab_image` | `grab_image` | fetch a URL from the PAGE's own script context (defeats Referer/hotlink protection) | both |
| `browser_downloads_list` | `downloads_list` | device-only target; baseline for `since_id` | both |
| `browser_download` | `download` | device-only target; triggers `chrome.downloads.download` | both |
| `browser_wait_download` | `wait_download` | device-only target; poll for a completed download | both |
| `browser_poll` | (agent-only `poll`) | check on / retrieve a previously queued command | both |
| `browser_establish_session` | (agent-only) | create a session with a declared write scope (confirmation-gate.md) | both |
| `browser_narrow_scope` | (agent-only) | narrow an existing session's scope -- never widens | both |
| `browser_reload` | `reload` | device-only target; self-service extension reload (see docs/PROTOCOL.md) | **native module only** |
| `browser_confirm` | (agent-only) | redeem a single-use confirmation-gate token | **MCP server only** |

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

## browser_tabs: pagination, filtering, and summary mode

Real-world finding: on the maintainer's own device (~728 open tabs), an unpaged `browser_tabs`
result was ~640KB -- large enough to truncate mid-response before it ever reached an agent's
context window, silently destroying whatever the agent was trying to do with it. The hub still
returns every tab in one `tabs` command result (see docs/PROTOCOL.md's "Agent-facing tabs
pagination (not a wire change)" -- this is deliberately NOT a wire-protocol change); both agent
surfaces shape that full result before handing anything back, via the shared, pure-logic
`amplifier_browser_bridge.paging.shape_tabs_response` (no I/O, fully unit-tested in isolation --
see `tests/test_paging.py`). This is the single home for the logic; neither surface reimplements
it.

**Paged by default.** `limit` (default 100) and `offset` (default 0) -- pass `limit=0` to opt back
into the old, unpaged full listing. The response's `result` always reports `total` (every tab on
the device, unfiltered), `matched` (how many passed any filters), `returned` (this page's size),
`offset`, `limit`, and `has_more`, so a caller can tell "3 tabs matched my filter" from "3 tabs
exist" and page correctly without guessing.

**Filter before paging** with `window_id` (exact match), `url_contains`, and/or `title_contains`
(both case-insensitive substrings). These are applied as a POST-FETCH filter over the full,
unfiltered `tabs` result -- not forwarded to the wire-level `target.window_id` the device sees --
which is what lets `total` stay an honest, device-wide count even when a filter is in effect.

**Summary mode is the cheap first call against a profile of unknown size.** Pass `summary=true` to
get ONLY per-window tab counts, totals, and how many tabs are discarded/asleep, with no tab list at
all -- useful for deciding how to narrow (which window, which url/title substring) before paying
for a full listing.

A `{"status": "queued", ...}` or `{"ok": false, ...}` `tabs` response is passed through by
`shape_tabs_response` completely untouched -- never paged, filtered, or reshaped -- consistent with
this document's tier pass-through guarantee above.

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
| `AMPLIFIER_BROWSER_BRIDGE_HUB_URL` | see below | Hub's agent-route WebSocket URL |
| `AMPLIFIER_BROWSER_BRIDGE_TOKEN` | unset | Per-device/agent shared token, if hub auth is enabled |
| `AMPLIFIER_BROWSER_BRIDGE_MCP_TRANSPORT` | `stdio` | `stdio`, `sse`, or `streamable-http` |

**`AMPLIFIER_BROWSER_BRIDGE_HUB_URL`'s default is resolved, not hardcoded** (`hub_location.py`):
1. this env var, if set -- always wins;
2. the hub location `amplifier-browser-bridge init`/`amplifier-browser-bridge service install`
   persisted the last time either one decided where the hub lives (`~/.config/amplifier-browser-bridge/hub_location.json`);
3. `ws://127.0.0.1:8900/agent`, if nothing has ever been persisted.

In practice: if you've already run `init` on this machine (even just to stage the extension --
you don't need to have installed the service), the MCP server defaults to the SAME hub `init`
told you about, with no env var required. Set the env var explicitly to point at a different hub
than the one persisted here.

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
`ClientSession` + `stdio_client`, launching `amplifier-browser-bridge-mcp` as a subprocess). This
transcript is historical (captured when the server exposed 16 tools) and is preserved verbatim as
real evidence -- it is not a claim about today's tool count. See the table above for the current,
verified-2026-08-08 count of 25.

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

`modules/tool-browser-bridge/` wraps the same lib as 27 Amplifier tools: the 25 in the
table above, plus `browser_setup` and `browser_setup_status` (native-module-only --
in-process first-run/re-run setup and diagnostics, no CLI on PATH required; see
`auto_setup.py` and the README's "Recommended: install via the Amplifier bundle").
Every tool follows the `mount()` Iron Law (`creating-amplifier-modules`
skill): each tool is registered via `await coordinator.mount("tools", tool, name=tool.name)`.

### Adding the bundle

See `bundle.md` at the repo root -- it is now a real, loadable Amplifier
bundle (not just documentation) that composes foundation plus
`behaviors/browser-bridge.yaml`, which wires this tool module in. See that
file for the exact `includes:`/`tools:` YAML stanzas for each composition
pattern (whole published bundle, behavior-only, or tool-module-only; a
published git source, or a local relative path for development against a
checkout of this repo).

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
mounting a tool module into a session). This transcript is historical
(captured when the module registered 16 tools) and preserved verbatim as real
evidence -- not a claim about today's count; see the table above for the
current, verified-2026-08-08 count of 25:

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
