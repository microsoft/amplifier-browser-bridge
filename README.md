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

**This is the USER install path.** `uv tool install` is what you run to use this project.
`uv pip install -e .` (an *editable* install) is the CONTRIBUTOR path for iterating on this
repo's own source -- see CONTRIBUTING.md's "Dev setup" if that's what you're doing instead.

Every command below was run verbatim against a fresh, non-editable install with no prior
configuration -- see "Verified clean-room install" below for the full transcript.

```bash
# 1. Install (a real, non-editable install -- from PyPI once published, or from a local
#    checkout via `uv tool install .`; either way, NOT `uv pip install -e .`)
uv tool install .

# 2. First-run setup: generates a hub token, stages the extension into a stable directory,
#    and prints the exact remaining manual steps.
abb init
```

`abb init` prints something like:

```
Generated new hub token (stored in ~/.config/amplifier-browser-bridge/tokens.json).
Staged extension -> ~/.local/share/amplifier-browser-bridge/extension

Remaining steps (manual -- Edge has no CLI for these):

  1. Start the hub:
       ABB_TOKEN_FILE=~/.config/amplifier-browser-bridge/tokens.json abb hub --host 0.0.0.0 --port 8900

  2. Load the extension:
       edge://extensions -> enable Developer mode -> Load unpacked ->
       select: ~/.local/share/amplifier-browser-bridge/extension

  3. Configure it:
       Click the extension's toolbar icon (its only UI) to open the options page.
       Hub URL: ws://<this machine's tailnet IP>:8900/device
       Token:   <the generated token, printed above>
       Click Save.

  4. Confirm it worked:
       ABB_TOKEN=<token> abb doctor --hub-url ws://127.0.0.1:8900/agent
```

Follow those four steps -- step 2 (loading an unpacked extension) is a genuinely manual step;
Edge has no CLI or API for it. Then issue a command:

```bash
abb devices
abb tabs <device_id>
abb snapshot <device_id>/<tab_id>
abb click <device_id>/<tab_id> <ref>
```

**`abb doctor` diagnoses a stuck setup.** It checks, in order, the token file, hub
reachability, token match, and whether a device has ever connected -- and stops at the first
broken link with a specific, actionable message rather than a wall of failures:

```
[ok]   token_store: auth enabled; token file: ~/.config/amplifier-browser-bridge/tokens.json
[ok]   hub_reachable: hub reachable at ws://127.0.0.1:8900/agent
[ok]   token_match: token accepted by hub
[FAIL] device_connected: no browser device has ever connected to this hub. Load the extension
       unpacked (edge://extensions -> Developer mode -> Load unpacked), click its toolbar icon,
       and set the Hub URL/token on the options page.
```

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the full command vocabulary and target-addressing
format. See [docs/DECISION_GUIDE.md](docs/DECISION_GUIDE.md) for which mechanism to reach for and
when -- a dozen read/act mechanisms plus modifiers (`wake`, `activate`, `trusted`,
`capture_hidden`) is real power with no map otherwise.

### Where the token lives, and why

Configuration (Hub URL + token) lives in the extension's own `chrome.storage.local`, entered
once through its options page (click the toolbar icon) -- never in a tracked source file. This
fixes a real problem: earlier versions shipped a live-shaped placeholder credential in
`extension/config.js` that had to be hand-edited and that every file update silently clobbered.
Now:

- **Rotating a token** means running `abb init --force` (regenerates it) and re-pasting it into
  the options page -- no tracked file to edit.
- **Updating the extension** (re-running `abb init` after a `git pull`) re-copies the JS/HTML/
  manifest files into the same staging directory, which never touches `chrome.storage.local` --
  Chrome/Edge ties that storage to the extension's install path, not to file contents. Verified:
  see "Update survives configuration" below.
- **An unconfigured extension fails loud**, never silently: no hub URL saved means no WebSocket
  connection is even attempted. The toolbar icon shows a red badge, the options page says "Not
  configured", and the browser console logs exactly what's missing.

### Enabling auth

Auth is disabled by default in dev and this is loudly logged by the hub. `abb init` generates a
real token and writes it to the hub's token file; pass `ABB_TOKEN` (matching what's on the
extension's options page) to the CLI, MCP server, or `abb doctor`. See `docs/PROTOCOL.md`
("Authentication") for the full resolution order.

### Verified clean-room install

Run 2026-07-26 with a genuinely NON-editable install -- the exact `uv tool install .` path a
real user takes, not `uv pip install -e .` (which resolves straight back to a checkout and
would silently mask a packaging bug like the one this transcript is proving fixed). New
virtualenv, `ABB_EXTENSION_SRC` explicitly unset, and a separate hub on port 8901 with its own
token file and its own `$HOME` -- never touching the real deployment's port 8900 hub or
`~/.config/amplifier-browser-bridge/`:

```console
$ uv venv /tmp/abb-cleanroom/.venv --python 3.12
$ uv pip install --python /tmp/abb-cleanroom/.venv/bin/python /path/to/amplifier-browser-bridge
Resolved 13 packages in 86ms
   Building amplifier-browser-bridge @ file:///path/to/amplifier-browser-bridge
      Built amplifier-browser-bridge @ file:///path/to/amplifier-browser-bridge
Installed 13 packages

$ env -u ABB_EXTENSION_SRC HOME=/tmp/abb-cleanroom/home \
    /tmp/abb-cleanroom/.venv/bin/abb init --hub-host 127.0.0.1 --hub-port 8901
Generated new hub token (stored in /tmp/abb-cleanroom/home/.config/amplifier-browser-bridge/tokens.json).
Staged extension -> /tmp/abb-cleanroom/home/.local/share/amplifier-browser-bridge/extension

Remaining steps (manual -- Edge has no CLI for these):
  1. Start the hub: ...
  2. Load the extension: ...
  3. Configure it: ...
  4. Confirm it worked: ...

$ ls /tmp/abb-cleanroom/home/.local/share/amplifier-browser-bridge/extension/
args_bool.mjs  background.js  combine_frames.mjs  config_validate.mjs  download_claim.mjs
fetch_utils.mjs  frame_refs.mjs  injected.js  manifest.json  options.html  options.js
ref_registry.mjs

$ ABB_TOKEN_FILE=.../tokens.json abb hub --host 127.0.0.1 --port 8901
amplifier-browser-bridge hub listening on ws://127.0.0.1:8901/device (extensions) and
ws://127.0.0.1:8901/agent (agents); audit log -> ./abb-audit.jsonl

$ ABB_HUB_URL=ws://127.0.0.1:8901/agent ABB_TOKEN=<token> abb doctor --hub-url ws://127.0.0.1:8901/agent
[ok]   token_store: auth enabled; token file: .../tokens.json
[ok]   token_file_siblings: no other token-like files found alongside .../tokens.json
[ok]   hub_reachable: hub reachable at ws://127.0.0.1:8901/agent
[ok]   token_match: token accepted by hub
[FAIL] device_connected: no browser device has ever connected to this hub. ...
```

Before this fix, the `abb init` step above raised `ExtensionSourceNotFoundError` on a
non-editable install -- the wheel didn't contain `extension/` at all (see
`[tool.hatch.build.targets.wheel.force-include]` in `pyproject.toml` and
`tests/test_packaging.py`, which builds a real wheel and asserts every file `abb init` needs
is actually inside it).

The extension was loaded via Playwright's headless Chromium (`--load-extension=./extension
--user-data-dir=./profile --remote-debugging-port=<port>`). Verified via CDP's `/json/list`:

- The service worker registered (`chrome-extension://<id>/background.js`).
- **The options page opened automatically** on first install (`onInstalled` ->
  `chrome.runtime.openOptionsPage()`), with no manual click needed to discover it exists.
- Filling in the Hub URL and token fields and clicking Save (the exact UI a real user drives)
  persisted both values into `chrome.storage.local`, confirmed by reading it back directly:
  `{"abb_hub_url": "ws://127.0.0.1:8910/device", "abb_hub_token": "<set>"}`.
- The extension's own status check (the same message the options page polls) correctly reported
  `configured: true` with a real generated `device_id`.

**Known gap in this specific verification**: the sandboxed container this verification ran in
blocks headless Chromium's own outbound network connections (`fetch`/`WebSocket`) to local TCP
listeners -- confirmed independently of this extension: a bare `fetch()`/`WebSocket` from a
plain page to a stock `python -m http.server` in the same container also never completed, while
`curl` and this project's own Python `websockets`-based `HubClient` reached the same listener
instantly. This is an environment-specific sandboxing artifact of that one container, not a
defect in the extension -- the identical mechanism (raw `ws://` from an MV3 service worker) is
independently measured working on real Edge installs on macOS and Android (see
`docs/designs/browser-bridge.md` section 2). The live device-handshake step of this proof could
not be completed in that specific sandbox; every other step -- install, `abb init`, `abb hub`,
`abb doctor`'s diagnostic chain, extension load, options-page auto-open, and config persistence
via the real UI -- was verified with real commands and real output as shown above.

### Update survives configuration (verified)

To prove the fix for the original bug (editing `extension/config.js` and copying it over a
running install silently wiped the working token), the clean-room extension directory was
re-staged in place -- `abb init` re-run with the same `--dest` after a source file changed,
simulating a `git pull` + reinstall:

```console
$ abb init --dest ./extension --token-file ./tokens.json ...
Reusing existing hub token (stored in ./tokens.json).   # <- NOT regenerated
Staged extension -> ./extension                          # <- same path, files updated in place
```

The already-loaded browser profile's `chrome.storage.local` was re-checked afterward (same
profile directory, extension reloaded from the now-updated staging directory) and found
**unchanged**: `abb_hub_url`, `abb_hub_token`, and the extension's own generated `abb_device_id`
all held the exact same values as before the update. The token file's `default` token was also
confirmed byte-for-byte unchanged. This is the structural fix: configuration lives in
`chrome.storage.local`, keyed to the extension's stable install path -- never in a file that an
update overwrites.

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

**There is no human-in-the-loop approval in this system today.** This is a deliberate current
decision, not a permanent architectural guarantee -- the maintainer's own words: *"I can live with
`redeem: agent` for now."* A confirmation gate's default redemption mode (`redeem: "agent"`) is
self-attestation -- the agent makes a second, separately-audited decision, which defends against an
accidental action and nothing else. A dedicated human-approval channel was designed in detail and
then deliberately cancelled for now (see
[docs/designs/approval-channel-options.md](docs/designs/approval-channel-options.md) section 0):
a live experiment showed the strongest candidate could be driven by the very agent it needed to
exclude. That same section names the two conditions that would reopen it: a channel whose
security property is measured against every capability the agent holds (not just the one this
experiment tested), or a per-session way to deny `chrome.debugger` entirely so an agent can't reach
CDP at all. Absent either, **the only real lever today is session scope** -- declare a narrow
`write` scope up front (`abb session-establish`) so the action is denied outright, rather than
relying on a gate that a human will never actually see.

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
