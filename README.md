# Amplifier Browser Bridge

Amplifier Browser Bridge lets an AI agent running on one device observe and drive a user's
real, logged-in Microsoft Edge browser on another device -- over the user's own Tailscale
tailnet. The agent is a second operator sharing a live browsing session, not a robot driving a
disposable browser it launched for itself.

No vendor product does this today: Claude in Chrome, Edge Copilot Mode, Gemini in Chrome, and
Playwright MCP's extension mode all run the control plane on the machine the human is sitting
at. Driving the browser on a phone from a workstation across the room -- or across the country --
is the gap this project fills. See `docs/designs/browser-bridge.md` section 9 for the full
positioning against each of these.

## Status

**Early. Phases 1-3 of a 6-phase build order are committed; this is not yet a finished product.**

| Phase | What it covers | Status |
|---|---|---|
| 1. Hub + extension, one device, one tab | `snapshot` -> `click` -> `read`, proves the pipe | Done |
| 2. Addressing | Multi-device, multi-window, multi-tab targeting | Done |
| 3. Tier model + queue | Non-blocking dispatch to intermittently-connected devices | Done |
| 4. Agent surfaces | Python lib, CLI, MCP server, Amplifier tool module | Done |
| 5. Policy engine | Denylist, confirmation gates, audit log | Done |
| 6. CDP escalation | Trusted input events, background-tab screenshots, soft-detach | Not started |

What that means concretely:

- **Proven end-to-end**: the wire protocol, hub dispatch and queueing, the CLI, the MCP server,
  the Amplifier tool module, and the policy engine all have passing automated tests
  (`pytest tests/`, `pytest modules/tool-browser-bridge/tests/`) and, for the agent surfaces, a
  documented real run against a live hub (`docs/AGENT_SURFACES.md`, "Verified end-to-end").
- **Measured on real hardware, not assumed**: every load-bearing transport and platform
  constraint in the design doc -- MV3 service worker lifetime, Android Doze behavior, background
  tab screenshot support, MagicDNS reliability -- was measured against real Edge installs on
  macOS and Android, not taken from documentation (`docs/designs/browser-bridge.md` section 2).
- **Not yet built**: `chrome.debugger` (CDP) escalation for trusted input events and any-tab
  screenshot capture on desktop. Today's injection-only implementation covers the full command
  vocabulary, but synthetic input is not `isTrusted`, and `screenshot` only works on the tab
  that is already active.
- **Not yet published**: this repository has no packaged release, no CI history, and has not
  been submitted to the Edge Add-ons store. Everything above is verified in-repo, not in
  production use.

## Security posture

This software controls a user's real, authenticated browser session. Before evaluating or
deploying it, read [SECURITY.md](SECURITY.md) for the full threat model. In brief:

- The hub has no public listener. It is reachable only from devices already inside the
  operator's Tailscale tailnet; Tailscale ACLs are the outer authorization boundary.
- A per-device shared token is a second, narrower boundary on top of tailnet identity, because
  tailnet identity is per-*device*, not per-*application*.
- Consent is enforced structurally, at a single choke point in the hub (`PolicyEngine.evaluate`),
  never by prompting the model to behave -- a prompt-injected agent can *want* a different
  target, it cannot *address* one policy has not permitted.
- Everything the agent does is written to an audit log. See [docs/POLICY.md](docs/POLICY.md) for
  exactly what the denylist and confirmation gates do and do not catch -- both sections end with
  an honest list of known gaps.

## Quickstart

```bash
# 1. Install
uv pip install -e .

# 2. Run the hub (auth disabled by default in dev -- this is loudly logged)
abb hub --host 0.0.0.0 --port 8900

# 3. Point the extension at the hub, then load it unpacked in Edge
#    - edit extension/config.js: set HUB_URL to the hub's tailnet IP (never MagicDNS -- see
#      docs/designs/browser-bridge.md section 4 for why)
#    - edge://extensions -> Developer mode -> Load unpacked -> select extension/

# 4. Confirm the device connected, then issue a command
abb devices
abb tabs <device_id>
abb snapshot <device_id>/<tab_id>
abb click <device_id>/<tab_id> <ref>
```

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the full command vocabulary and target-addressing
format, and the section below on connecting to a hub token-protected deployment.

### Enabling auth

Set `ABB_HUB_TOKEN` before starting the hub, match it in `extension/config.js`'s `HUB_TOKEN`,
and pass `ABB_TOKEN` to the CLI or MCP server. See `docs/PROTOCOL.md` ("Authentication") for the
full resolution order.

## Connectivity tiers

A command's behavior depends on how reachable the target device currently is. This is a
first-class concept the agent surface exposes honestly, not an implementation detail:

| Tier | Reality | What the agent sees |
|---|---|---|
| `live` | Desktop, or any device with an open connection | Command executes immediately; ordinary `{ok, result}`/`{ok:false, error}` |
| `intermittent` | Mobile with battery-optimization exemption; disconnected but seen recently | Returns immediately: `{status: "queued", tier, last_seen, queue_position}` -- drains in roughly 45-135s, self-healing |
| `dormant` | Mobile without the exemption, or a device never seen | Same queued shape -- drains whenever the device next reconnects, which may be much longer |

**A command to a non-live device never blocks.** It returns instantly with a queued status; use
`abb cmd` / `browser_poll` to check on it later. A tool call that silently hangs for two minutes
is indistinguishable from a broken system -- this project treats that as unacceptable.

**On Android, the battery-optimization exemption is an onboarding requirement, not a tip.**
Measured behavior: with the exemption granted, a locked/backgrounded device reconnects within
roughly two minutes; without it, the socket can stay dark for 8+ minutes with no self-recovery.
See `docs/designs/browser-bridge.md` sections 2 and 5 for the underlying measurements.

## Consent model

Default posture is **broad read access, narrow exceptions** -- reflecting the explicit design
requirement that the agent see what the user sees, without a per-tab approval flow for every
read or click:

| Mechanism | What it does |
|---|---|
| Denylist | A small, hand-maintained set of sensitive host categories (financial, healthcare, identity providers, password managers) are made **invisible** to the agent -- they never appear in a `tabs` listing, not merely refused when directly addressed |
| Confirmation gates | A fixed set of irreversible/world-visible actions (purchase, send, delete, OAuth grant, file upload, account creation, permission change) require an explicit `confirm` call before dispatch; everything else runs unprompted |
| Kill switch | A hub-level stop-all that halts new dispatch and rejects every queued command immediately |
| Audit log | Every command, policy decision, and result is recorded, so broad default access has a compensating "the human can review everything after the fact" control |

Read [docs/POLICY.md](docs/POLICY.md) in full before relying on this for anything beyond the
threat model it targets -- it documents, plainly, what the denylist cannot see and which
confirmation gates have no real detection signal wired up yet in this phase.

## Platform support

| Capability | Edge desktop (Windows / macOS / Linux) | Edge Android |
|---|---|---|
| Read/write DOM, element click/type dispatch | Yes | Yes |
| Screenshot the active tab | Yes | Yes |
| Screenshot a background/minimized tab | Yes (measured on macOS; Windows untested) | No -- active tab only |
| `chrome.windows`, `chrome.tabs` | Yes | Yes (Microsoft's own docs incorrectly say no -- see design doc section 2) |
| `chrome.tabGroups` | Yes | No |
| `chrome.debugger` (CDP): trusted input events, network interception | Not yet implemented (Phase 6) | Not available on this platform at all |

Non-goals, by explicit design decision: browsers other than Microsoft Edge, and iOS (Microsoft
documents no extension API surface for it today). See `docs/designs/browser-bridge.md`
"Non-goals" for the full list and rationale.

## Repository layout

```
docs/designs/browser-bridge.md   the design doc -- read this first
docs/PROTOCOL.md                 the wire protocol
docs/POLICY.md                   the consent/policy model
docs/AGENT_SURFACES.md           the MCP server and Amplifier tool module
bundle.md                        Amplifier bundle composing the tool-browser-bridge module
src/amplifier_browser_bridge/    the lib: protocol, addressing, tiers, hub, client, CLI, mcp_server
modules/tool-browser-bridge/     the Amplifier tool module (thin adapter over the lib)
extension/                       the MV3 browser extension (one build, all platforms)
tests/                           unit tests for everything testable without a live browser
```

## Agent surfaces

Beyond the CLI shown above, the same lib is exposed as an MCP server (`abb-mcp`, for any
MCP-speaking client) and an Amplifier tool module (`modules/tool-browser-bridge/`, composed via
`bundle.md`). Both are thin adapters -- all logic lives in the Python lib. See
[docs/AGENT_SURFACES.md](docs/AGENT_SURFACES.md) for how to run and configure each, and for the
proof that both work end-to-end against a real hub.

```bash
uv pip install -e ".[mcp]"
abb-mcp   # runs over stdio, the default every MCP client speaks
```

## Testing

```bash
uv pip install -e ".[dev]"          # or: uv pip install -e . pytest
pytest tests/                        # root package
pytest modules/tool-browser-bridge/tests/   # Amplifier tool module
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, engineering conventions this project
holds contributors to, and how to load the extension and run the hub locally. See
[SECURITY.md](SECURITY.md) to report a vulnerability, and [SUPPORT.md](SUPPORT.md) for what's
in and out of scope for issues.

## Further reading

- [docs/designs/browser-bridge.md](docs/designs/browser-bridge.md) -- the design of record: goals, measured evidence, architecture, transport, positioning
- [docs/PROTOCOL.md](docs/PROTOCOL.md) -- the wire protocol, message shapes, command vocabulary
- [docs/POLICY.md](docs/POLICY.md) -- the consent model in full, including its honest limits
- [docs/AGENT_SURFACES.md](docs/AGENT_SURFACES.md) -- MCP server and Amplifier tool module, with verified end-to-end proof

## License

MIT. See [LICENSE](LICENSE).
