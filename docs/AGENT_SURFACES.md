# Agent surfaces: MCP server and Amplifier tool module

Design doc section 3.3 names four agent-surface levels; the lib and CLI shipped in
Phase 1. This doc covers the two Phase 2 surfaces -- both are thin adapters over
the same lib (`client.py`, `addressing.py`, `tiers.py`); neither implements any
new logic.

**Hand-verified 2026-08-15 (counted directly against the code, not assumed):**
the native Amplifier tool module registers **30** tools; the MCP server
registers **28**. They are not byte-identical sets -- the native module has
three the MCP server does not (`browser_reload`, `browser_setup`,
`browser_setup_status`), and the MCP server has one the native module does
not (`browser_confirm`). Every other name below is shared by both, named
`browser_<command>` (mirroring Playwright MCP's vocabulary, design doc
section 9), including `browser_archive` (D2, browser-state archive),
`browser_archive_convert` (the new MHTML-to-markdown conversion step -- see
"Browser-state archive: MHTML -> markdown conversion" below), and
`browser_update_extension` (the version-skew story -- see "Extension
update (Tier 0/1/2)" below). A prior revision of this doc claimed "29 and
27" -- that count was already stale before this update (it predated
`browser_archive_convert` being added to both surfaces); both numbers here
were re-counted directly from
`amplifier_module_tool_browser_bridge/__init__.py`'s `_build_tools()` and
`mcp_server.py`'s `@mcp.tool()` decorators, the authoritative, current lists,
not carried forward from the prior claim.

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
| `browser_archive` | (composed: `windows`/`tabs`/`page_state`/`mhtml`/`nav_history`/profile-data commands) | D2, browser-state archive -- capture browser state at a chosen depth (L0-L5), write payloads to disk, return a MANIFEST (never the payload) -- see "Browser-state archive" below | both |
| `browser_archive_convert` | (no wire command -- pure local conversion over an existing archive on disk) | Convert a `browser_archive` output's captured MHTML pages into markdown, AFTER THE FACT -- see "Browser-state archive: MHTML -> markdown conversion" below | both |
| `browser_update_extension` | (composed: restage + `reload` + polled `list_devices`) | verify-or-guide extension update -- see "Extension update (Tier 0/1/2)" below | both |
| `browser_setup` | (native, in-process `init` equivalent) | first-run/re-run setup, no CLI on PATH required | **native module only** |
| `browser_setup_status` | (native, in-process `doctor` equivalent) | diagnose the setup chain | **native module only** |

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

`modules/tool-browser-bridge/` wraps the same lib as 28 Amplifier tools: the 26 in the
table above, plus `browser_setup` and `browser_setup_status` (native-module-only --
in-process first-run/re-run setup and diagnostics, no CLI on PATH required; see
`auto_setup.py` and the README's "Recommended: install via the Amplifier bundle").
Every tool follows the `mount()` Iron Law (`creating-amplifier-modules`
skill): each tool is registered via `await coordinator.mount("tools", tool, name=tool.name)`.

## Browser-state archive (D2)

`browser_archive` is the ONE agent-facing tool for the browser-state archive
capability (`docs/PROTOCOL.md`'s "Browser-state archive" section, `archive.py`'s
`run_archive`). It composes ten wire commands (`windows`, `page_state`, `mhtml`,
`nav_history`, `history_list`, `bookmarks_list`, `sessions_list`, `top_sites`,
`reading_list`, `cookies_list`) into a depth ladder (L0 through L5, cheapest to
deepest), writes every captured payload straight to a timestamped directory on
disk, and returns a MANIFEST -- paths, counts, byte sizes, per-tab/profile status,
failures -- never the payloads themselves. None of the ten wire commands it
composes is its own agent-facing tool in this phase; adding one that returned a
raw MHTML document or a full `outerHTML` dump would recreate the exact
context-truncation failure `browser_tabs` hit (see "browser_tabs: pagination,
filtering, and summary mode" above).

**Depth ladder** (each level a strict superset of the one below): `L0` windows/
tab-groups/tabs inventory (no tab wake, no page contact) -> `L1` + visible text
per tab -> `L2` + DOM/forms/storage/scroll per tab -> `L3` + screenshots per tab
-> `L4` + MHTML per tab (requires the `debugger` capability; requesting L4/L5 on
a device without it fails loud immediately, never silently degrading to a lower
depth) -> `L5` + navigation history per tab, and browser-wide profile data.

**No-wake guarantee**: a tab flagged `discarded`/`asleep` in the L0 inventory is
SKIPPED for L1+ capture (recorded in the manifest, not silently dropped) unless
`wake=true` is explicitly passed -- at real-world scale (hundreds of tabs) most
are discarded, and waking one destroys real, unsaved in-page state.

**Cookies are opt-in**: `include_cookies` defaults to `False` and is never implied
by requesting L5 (or any depth) -- a caller must explicitly opt in even at
maximum archive depth (`docs/permission-justifications.md` section 6).

**Manifest honesty**: `manifest["status"]` is `"ok"` only when nothing failed or
was skipped -- `"ok_with_skips"` or `"ok_with_failures"` otherwise, and
`manifest["failures"]` lists every failure/skip at the top level, never buried.
`manifest["summary"]` never collapses the INVENTORY axis (what actually exists in
the browser -- `windows_inventoried`/`tab_groups_inventoried`/`tabs_inventoried`,
populated at every depth including L0) into the CAPTURE axis (what had page
content pulled down -- `tabs_capture_attempted`/`tabs_captured`/`tabs_skipped`/
`tabs_failed`, legitimately all `0` at L0 by design; `profile`, `None` below L5).
An L0 archive of 735 real tabs reports `tabs_inventoried: 735` alongside
`tabs_captured: 0` -- never `tabs_inventoried: 0`, which would misread as "nothing
was archived" when 735 tabs are sitting on disk.

**Transport failures fail one capture, never the whole run**: a real page's
`mhtml` capture can be large enough to exceed a WebSocket's message-size limit
(`docs/PROTOCOL.md`'s "WebSocket message-size ceiling" section) -- or hit any
other connection-level failure mid-archive. Every per-capture/per-profile-item
wire call this tool composes goes through one choke point (`archive.py`'s
`_safe_command`) that turns that failure into an ordinary recorded capture
failure, exactly like an explicit `{"ok": false, ...}` result -- the run
continues to the next tab, and every tab already captured (and already written
to disk) survives in the final manifest. Before this fix, an uncaught transport
failure partway through a run aborted `run_archive` entirely -- discarding
every already-captured tab's manifest entry, since `manifest.json` is written
only once, at the very end.

Per-tab status is likewise not binary: `"ok"` (every attempted capture succeeded),
`"partial"` (some succeeded, some failed -- e.g. a browser error page where
CDP-based captures like `mhtml`/`screenshot`/`nav_history` succeed even though
JS-injection captures like `text`/`dom` cannot run at all), or `"failed"` (every
attempted capture failed); `"skipped"` (no-wake guarantee) remains a distinct
fourth state. `summary["tabs_partial"]` counts partial tabs explicitly, and a run
containing any partial tab is never reported as plain `"ok"` -- a partial tab
always adds at least one entry to `manifest["failures"]`.

A `tab_id` named in `tab_ids` that no longer exists in the live inventory (closed
between the caller reading it and this call -- or never existed at all) is a FIFTH
per-tab state, `"not_found"` -- distinct from `"ok"`/`"partial"`/`"failed"`/`"skipped"`.
Observed live: an archive requesting 4 tab_ids reported `tabs_capture_attempted: 3`
with entries for only 3 of the 4 -- the vanished tab appeared in no `tabs` entry, no
`failures` entry, no `skipped` record, nothing. `manifest["tabs"][tab_id]` now gets a
synthetic `{"status": "not_found", "reason": ...}` entry for it, **at every depth,
including L0** -- this accounting is computed once against the live inventory and
does not depend on any per-tab capture running (a prior version of this fix
computed it only alongside L1+ capture, so an L0 request for a nonexistent `tab_id`
still silently reported plain `"ok"`). This is benign (not a capture
failure -- there is nothing left to capture) so it never adds to
`manifest["failures"]`, but it is never folded into plain `"ok"` either:
`summary["tabs_not_found"]` counts it, `summary["tabs_capture_attempted"]` excludes
it (a vanished tab was never actually attempted), and `manifest["status"]` becomes
`"ok_with_skips"` -- the same bucket `"skipped"` tabs use, since both are benign,
non-failure gaps. The top-level `manifest["requested_tab_ids_not_found"]` list is a
convenience summary of the same ids.

## Browser-state archive: MHTML -> markdown conversion

`browser_archive_convert` is the ONE agent-facing tool for converting an existing
`browser_archive` output's captured MHTML pages into markdown, AFTER THE FACT
(`mhtml_convert.py`, `archive_convert.py`'s `run_archive_convert`). It does no
browser interaction at all -- pure local CPU work over `.mhtml` files a prior
`browser_archive` call (at depth L4 or deeper) already wrote to disk. This is a
distinct, later, OPT-IN step: it never runs automatically as part of
`browser_archive`, mirroring the mechanism/policy split `browser_vision_read`
already establishes for calling an external vision model.

**Why MHTML, not `outer_html`**: measured live, on the same real tabs, the
JS-injection capture route (`read`/`page_state`) failed on 7 of 7 real tabs
(including a browser error page it could not touch at all), timing out at both
90s and 120s budgets; the CDP-based route (`mhtml`/`screenshot`/`nav_history`)
succeeded 3 of 3 on those same tabs. MHTML is the only reliably obtainable
full-page capture in this system, so it is the only conversion source.

**Two outputs, never one**: every converted tab gets BOTH `page.extracted.md`
(trafilatura's main-content extraction -- scores F1 0.924 on trafilatura's own
published benchmark, vs. 0.667 for a raw-HTML do-nothing baseline) and
`page.full_page.md` (a deliberately unfiltered whole-page conversion via
html2text). Main-content extraction quality swings 0.42-0.93 by page type
(WCXB benchmark), and 47% of real pages are non-articles where an extractor can
silently delete the content a caller wanted -- the full-page output makes a bad
extraction recoverable rather than lossy.

**Assets are content-addressed sidecars, never inlined**: every non-HTML MIME
part (images/CSS/fonts) is written to a SHARED `archive_dir/assets/` directory,
named `<sha256-of-bytes><ext>` -- shared across every tab converted into the
same archive, so identical assets (a shared logo/icon/font) dedupe rather than
duplicating per page. The HTML's own `Content-Location`/`cid:` asset references
are rewritten to the sidecar's relative path BEFORE conversion.

**Known limitations, never silently mangled**: a table with merged cells
(`colspan`/`rowspan`) cannot be expressed as a markdown pipe table -- a FORMAT
limitation, not a tooling gap. Affected tables are named explicitly in
`result["tabs"][tab_id]["tables_with_merged_cells"]` (table index + a short text
preview) rather than silently producing wrong-looking output with no
explanation. A page containing more than one `text/html` body (an
`<iframe>`-heavy page captured as separate frame documents) is the documented
hard case this converter does not attempt to merge -- that tab's entry reports
`{"status": "failed", "error": ...}` naming every frame found, rather than
silently converting only the first frame as if it were the whole page.

**Not-captured accounting mirrors `browser_archive`'s own fix**: a `tab_ids` id
with no `page.mhtml` on disk (archived below L4, or a typo) gets a
`{"status": "not_captured", ...}` entry -- counted in
`summary["tabs_not_captured"]`, moving `manifest["status"]` to `"ok_with_skips"`
-- rather than silently absent from `manifest["tabs"]`, the same discipline
`browser_archive`'s own `"not_found"` per-tab state applies to a vanished
`tab_id`.

**Manifest, never the payload**: like `browser_archive`, this returns only
paths, byte counts, per-tab status, and warnings -- never the markdown text
itself. A converted page can be many KB of markdown; returning it as this
tool's return value would recreate the exact context-truncation failure
`browser_archive` itself exists to avoid.

## Extension update (Tier 0/1/2)

`browser_update_extension` is the ONE agent-facing tool for the version-skew story
(`docs/PROTOCOL.md`'s "Tier 0 handshake"/"Extension self-reload" sections,
`update_extension.py`'s `run_update_extension`, `skew.py`, `build_stamp.py`). It
never guesses whether this device's unpacked extension lives on the same machine
as the hub or a genuinely remote one (unreliable to detect -- a network mount can
look local); instead it always restages a fresh build (the same
`setup.stage_extension` function `amplifier-browser-bridge init` uses) and sends
`reload`, then VERIFIES the result by re-reading the device's self-reported
command set AND build stamp after it reconnects:

- **Already current**: the device already reports every command this hub knows
  (`skew.SkewReport.in_sync`) **AND** its build stamp matches this hub's current
  build (`build_stamp.BuildFreshness.current`) -- a no-op, `{"ok": true,
  "already_current": true, "updated": false}`. Requiring BOTH closes a real gap:
  a device can be command-complete and still stale -- a bug/UI/security fix that
  adds or removes zero commands (this repo's own commits `6175ce4`/`cc140c5`) was
  previously invisible to `skew` alone, and `already_current` was reported as
  `true` for a genuinely outdated browser. See `build_stamp.py`'s module docstring
  for the full incident.
- **Automatic update verified (Tier 1)**: reload succeeded, the device reconnected
  (a NEW `connected_at`, not the stale pre-reload connection), and its command set
  OR its build stamp genuinely changed -- `{"ok": true, "updated": true, ...}`.
  Either axis changing is sufficient proof the restage reached this device's real
  extension files (a command-only change legitimately leaves the build stamp as
  the only other thing that moved, and vice versa).
- **Automatic update unverifiable, guided path (Tier 2)**: reload succeeded and the
  device reconnected, but its command set AND build stamp are BOTH UNCHANGED --
  this hub's restage did not reach wherever the browser's real extension files
  live (most likely a different machine). Reported as `{"ok": false, "reason":
  "no_verified_change", "guided": {"download_url": ..., "instructions": ...}}` --
  never a false "done." `download_url` is this hub's own `GET /setup/extension.zip`
  (`extension_zip.py`), derived from the SAME host the calling client is already
  using to reach the hub, so it resolves from wherever a human actually needs to
  open it -- including a different machine than the hub itself.
- **Bootstrap limit**: the device never acknowledges `reload` at all -- its
  extension predates self-service reload entirely, a one-time manual step that
  cannot be routed around. Also guided, `reason: "reload_unsupported"`.
- **Not live / never reconnected**: fails loud (`reason: "device_not_live"` or
  `"reconnect_timeout"`) -- never silently treated as success, and never verified
  against a stale registry record.

A device that has NEVER reported a command set or a build stamp at all (every
extension shipped before this feature) still gets the automatic path attempted --
seeing either go from unreported to a real, populated value after reload IS the
verification signal that the update worked.

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
