# Amplifier Browser Bridge

Amplifier Browser Bridge lets an AI agent running on one device observe and drive a user's
real, logged-in Microsoft Edge browser on another device -- over the user's own Tailscale
tailnet. The agent is a second operator sharing a live browsing session, not a robot driving a
disposable browser it launched for itself.

No **vendor** product does this today: Claude in Chrome, Edge Copilot Mode, Gemini in Chrome, and
Playwright MCP's extension mode all run the control plane on the machine the human is sitting
at. Driving the browser on a phone from a workstation across the room -- or across the country --
is the gap this project fills among vendor-shipped tools.

**This claim narrows to exclude one real prior-art project:** [`browser-relay`](https://github.com/reliefeai/browser-relay)
(MIT) already does cross-device browser control today, via a public Cloudflare Worker relay
authenticated by a bearer Device ID that its own README calls *"a capability -- anyone with it
can control this browser."* This project does not claim to be first; its honest differentiator
is replacing that public relay with the user's own Tailscale tailnet -- no third-party relay in
the path, no long-lived bearer capability traveling the public internet, device-level network
ACLs instead of a single shared secret. See `docs/designs/browser-bridge.md` section 9 for the
full positioning against `browser-relay`, Playwright MCP, and every vendor tool above.

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

- **The per-device shared token is the load-bearing boundary for most deployments** -- not the
  tailnet. Tailscale's own default ACL policy allows every device on your tailnet to reach every
  other device on every port; unless you've written a restrictive ACL (starting point:
  [docs/tailscale-acl-example.hujson](docs/tailscale-acl-example.hujson)), the tailnet is not a
  meaningful boundary by itself. `amplifier-browser-bridge doctor` reports this and your bind-address exposure
  on every run.
- `amplifier-browser-bridge hub` binds `127.0.0.1` by default (safe, loopback-only); `amplifier-browser-bridge init`
  auto-detects this machine's Tailscale IP as a cross-device-capable default and warns loudly if
  a wildcard bind (`0.0.0.0`) is ever chosen instead.
- A per-device shared token is a second, narrower boundary on top of tailnet identity, because
  tailnet identity is per-*device*, not per-*application*. Token comparison is constant-time.
  One token controls every device connected to a hub unless you hand-provision per-device ones
  (`auth.py`'s `TokenStore` supports this; `init` does not auto-provision them yet).
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

You need [`uv`](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.12 or
newer. **This package is not on PyPI yet** (see "Status" above -- no packaged release), so the
only install path that works today is from a local clone.

Every command below was run verbatim on a clean machine with no prior configuration -- see
"Verified clean-room install" below for the full transcript, captured from a real run.

```bash
# 1. Clone. The repo is public; no credentials needed.
git clone https://github.com/bkrabach/amplifier-browser-bridge.git
cd amplifier-browser-bridge

# 2. Install (a real, non-editable install -- NOT `uv pip install -e .`).
#    Once this package is published to PyPI, `uv tool install amplifier-browser-bridge`
#    will work from anywhere and steps 1-2 collapse into that one command. It does not
#    work yet.
uv tool install .

# 3. First-run setup: generates a hub token, stages the extension into a stable directory,
#    and prints the exact remaining manual steps.
amplifier-browser-bridge init
```

`amplifier-browser-bridge init` prints something like:

```
Generated new hub token (stored in ~/.config/amplifier-browser-bridge/tokens.json).
Staged extension -> ~/.local/share/amplifier-browser-bridge/extension

Remaining steps (manual -- Edge has no CLI for these):

  1. Start the hub:
       AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE=~/.config/amplifier-browser-bridge/tokens.json amplifier-browser-bridge hub --host 100.x.y.z --port 8900
       (auto-detected this machine's Tailscale IP via `tailscale ip -4`: 100.x.y.z)

  2. Load the extension:
       edge://extensions -> enable Developer mode -> Load unpacked ->
       select: ~/.local/share/amplifier-browser-bridge/extension

  3. Configure it:
       Click the extension's toolbar icon (its only UI) to open the options page.
       Hub URL: ws://100.x.y.z:8900/device
       Token:   <the generated token, printed above>
       Click Save.

  4. Confirm it worked:
       AMPLIFIER_BROWSER_BRIDGE_TOKEN=<token> amplifier-browser-bridge doctor --hub-url ws://127.0.0.1:8900/agent
```

**A1 fix (security review finding):** `amplifier-browser-bridge hub` used to default to `--host 0.0.0.0` --
binding every network interface the machine has (home Wi-Fi, hotel Wi-Fi, a corporate LAN), not
just the Tailscale tailnet this project's threat model assumes -- and `init` printed that default
back as the recommended command. `hub` now defaults to `--host 127.0.0.1` (loopback only); `init`
auto-detects this machine's own Tailscale IP (`tailscale ip -4`) as shown above so the printed
command stays cross-device-capable without a silent wildcard bind. If Tailscale can't be
detected, `init` falls back to `127.0.0.1` and says so explicitly -- cross-device use then
requires passing `--hub-host <your tailnet IP>` yourself. Passing (or auto-falling to) a
wildcard host anywhere prints a specific, named warning listing exactly what it exposes. See
[SECURITY.md](SECURITY.md) for the full accounting.

Follow those four steps -- step 2 (loading an unpacked extension) is a genuinely manual step;
Edge has no CLI or API for it. Then issue a command:

```bash
amplifier-browser-bridge devices
amplifier-browser-bridge tabs <device_id>
amplifier-browser-bridge snapshot <device_id>/<tab_id>
amplifier-browser-bridge click <device_id>/<tab_id> <ref>
```

**`amplifier-browser-bridge doctor` diagnoses a stuck setup.** It runs six checks in dependency
order -- the token file, other token-like files sitting next to it, this machine's network
exposure and Tailscale ACL posture, hub reachability, token match, and whether a device has
ever connected. It stops at the first broken link and marks everything downstream `skipped`
with a reason, so you see ONE actionable thing to fix rather than a wall of failures.

Immediately after `init`, before you have loaded the extension into Edge, the expected result
is five `[ok]` and one `[FAIL]` on `device_connected`. **That final `[FAIL]` is not a
malfunction -- it is `doctor` telling you the setup is incomplete and naming the step you have
not done yet.** `doctor` exits non-zero in this state, which is correct: the chain is not
finished. This is the real output of that run (`network_exposure`'s message is long; it is
reproduced in full here because it is the check people most often assume is boilerplate):

```
[ok]   token_store: auth enabled; token file: /root/.config/amplifier-browser-bridge/tokens.json
[ok]   token_file_siblings: no other token-like files found alongside /root/.config/amplifier-browser-bridge/tokens.json
[ok]   network_exposure: this doctor invocation targets a loopback host ('127.0.0.1'). Note: this
       check can only see what host YOU pointed doctor at -- it cannot prove the running hub
       process isn't ALSO bound to a wider address (e.g. started with --host 0.0.0.0). Confirm
       separately how the hub you're diagnosing was actually started. could not detect a Tailscale
       IP on this machine (`tailscale ip -4` unavailable or failed). [...Tailscale default-ACL
       disclosure continues; see docs/tailscale-acl-example.hujson...]
[ok]   hub_reachable: hub reachable at ws://127.0.0.1:8900/agent
[ok]   token_match: token accepted by hub
[FAIL] device_connected: no browser device has ever connected to this hub. Load the extension
       unpacked (edge://extensions -> Developer mode -> Load unpacked), click its toolbar icon,
       and set the Hub URL/token on the options page.
Error: one or more checks failed -- see above.
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

- **Rotating a token** means running `amplifier-browser-bridge init --force` (regenerates it) and re-pasting it into
  the options page -- no tracked file to edit.
- **Updating the extension** (re-running `amplifier-browser-bridge init` after a `git pull`) re-copies the JS/HTML/
  manifest files into the same staging directory, which never touches `chrome.storage.local` --
  Chrome/Edge ties that storage to the extension's install path, not to file contents. Verified:
  see "Update survives configuration" below.
- **An unconfigured extension fails loud**, never silently: no hub URL saved means no WebSocket
  connection is even attempted. The toolbar icon shows a red badge, the options page says "Not
  configured", and the browser console logs exactly what's missing.

### Enabling auth

Auth is disabled by default in dev and this is loudly logged by the hub. `amplifier-browser-bridge init` generates a
real token and writes it to the hub's token file; pass `AMPLIFIER_BROWSER_BRIDGE_TOKEN` (matching what's on the
extension's options page) to the CLI, MCP server, or `amplifier-browser-bridge doctor`. See `docs/PROTOCOL.md`
("Authentication") for the full resolution order.

### Verified clean-room install

Run 2026-08-08 on a **throwaway Ubuntu 24.04 container with nothing pre-installed** -- no copy
of this package, no `~/.config/amplifier-browser-bridge/`, no repo checkout, no GitHub
credentials, no SSH key, no `~/.netrc`, no `~/.gitconfig`. Only `git`, `curl`, Python 3.12, and
`uv` were present, which is what a stranger's machine looks like. The repo was cloned
anonymously over HTTPS and the Quickstart above was then executed **verbatim**, in order, with
nothing substituted:

```console
$ git clone https://github.com/bkrabach/amplifier-browser-bridge.git
Cloning into 'amplifier-browser-bridge'...
$ cd amplifier-browser-bridge && git rev-parse HEAD
8605536c3e8ceb2f126adff2fcb0e514e625a71b

$ uv tool install .
Resolved 13 packages in 222ms
   Building amplifier-browser-bridge @ file:///root/amplifier-browser-bridge
Downloading aiohttp (1.7MiB)
 Downloaded aiohttp
      Built amplifier-browser-bridge @ file:///root/amplifier-browser-bridge
Prepared 11 packages in 319ms
Installed 13 packages in 4ms
 + aiohappyeyeballs==2.7.1
 + aiohttp==3.14.3
 + aiosignal==1.4.0
 + amplifier-browser-bridge==0.1.0 (from file:///root/amplifier-browser-bridge)
 + attrs==26.1.0
 + click==8.4.2
 + frozenlist==1.8.0
 + idna==3.18
 + multidict==6.7.1
 + propcache==0.5.2
 + typing-extensions==4.16.0
 + websockets==17.0.1
 + yarl==1.24.5
Installed 2 executables: amplifier-browser-bridge, amplifier-browser-bridge-mcp

$ amplifier-browser-bridge init
Generated new hub token (stored in /root/.config/amplifier-browser-bridge/tokens.json).
Staged extension -> /root/.local/share/amplifier-browser-bridge/extension

Remaining steps (manual -- Edge has no CLI for these):

  1. Start the hub:
       AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE=/root/.config/amplifier-browser-bridge/tokens.json amplifier-browser-bridge hub --host 127.0.0.1 --port 8900
       (could not detect a Tailscale IP -- `tailscale ip -4` is unavailable or failed -- defaulting to 127.0.0.1, which is NOT reachable from another device; for cross-device use, re-run with --hub-host <this machine's tailnet IP>)

  2. Load the extension:
       edge://extensions -> enable Developer mode -> Load unpacked ->
       select: /root/.local/share/amplifier-browser-bridge/extension

  3. Configure it:
       Click the extension's toolbar icon (its only UI) to open the options page.
       Hub URL: ws://127.0.0.1:8900/device
       Token:   9a971fad524311b42dc81956c8d162ae
       Click Save.

  4. Confirm it worked:
       AMPLIFIER_BROWSER_BRIDGE_TOKEN=9a971fad524311b42dc81956c8d162ae amplifier-browser-bridge doctor --hub-url ws://127.0.0.1:8900/agent

$ ls /root/.local/share/amplifier-browser-bridge/extension/
args_bool.mjs        config_validate.mjs   frame_refs.mjs   options.html
background.js        download_claim.mjs    injected.js      options.js
bundled_config.mjs   effects_collector.mjs manifest.json    ref_registry.mjs
combine_frames.mjs   fetch_utils.mjs
```

Steps 1 and 4 of `init`'s printed instructions were then run **exactly as printed** -- no host,
port, or URL edited:

```console
$ AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE=/root/.config/amplifier-browser-bridge/tokens.json amplifier-browser-bridge hub --host 127.0.0.1 --port 8900
amplifier-browser-bridge hub listening on ws://127.0.0.1:8900/device (extensions) and ws://127.0.0.1:8900/agent (agents); audit log -> ./amplifier-browser-bridge-audit.jsonl

$ AMPLIFIER_BROWSER_BRIDGE_TOKEN=9a971fad524311b42dc81956c8d162ae amplifier-browser-bridge doctor --hub-url ws://127.0.0.1:8900/agent
[ok]   token_store: auth enabled; token file: /root/.config/amplifier-browser-bridge/tokens.json
[ok]   token_file_siblings: no other token-like files found alongside /root/.config/amplifier-browser-bridge/tokens.json
[ok]   network_exposure: this doctor invocation targets a loopback host ('127.0.0.1'). [...]
[ok]   hub_reachable: hub reachable at ws://127.0.0.1:8900/agent
[ok]   token_match: token accepted by hub
[FAIL] device_connected: no browser device has ever connected to this hub. Load the extension unpacked (edge://extensions -> Developer mode -> Load unpacked), click its toolbar icon, and set the Hub URL/token on the options page.
Error: one or more checks failed -- see above.
```

That trailing `[FAIL]` is the correct and expected end state for this transcript: no browser was
ever attached, because the container has no Edge in it. The token shown was generated inside the
throwaway container and died with it.

**The `init` -> `doctor` host agreement is load-bearing and was specifically checked.** An
earlier release printed a hub command binding a tailnet address while printing a `doctor`
command aimed at loopback, so following the printed steps verbatim always failed at
`hub_reachable`. Above, with no Tailscale present, `init` fell back to `127.0.0.1`, said so
explicitly, and printed a `doctor` command targeting that same `127.0.0.1` -- the two agree.

Before this fix, the `amplifier-browser-bridge init` step above raised `ExtensionSourceNotFoundError` on a
non-editable install -- the wheel didn't contain `extension/` at all (see
`[tool.hatch.build.targets.wheel.force-include]` in `pyproject.toml` and
`tests/test_packaging.py`, which builds a real wheel and asserts every file `amplifier-browser-bridge init` needs
is actually inside it).

**Separately, and earlier (2026-07-26), the browser half was verified in its own container** --
the clean-room run above has no browser in it at all, so it proves the install and CLI chain,
not the extension. In that earlier run the extension was loaded via Playwright's headless
Chromium (`--load-extension=./extension --user-data-dir=./profile
--remote-debugging-port=<port>`). Verified via CDP's `/json/list`:

- The service worker registered (`chrome-extension://<id>/background.js`).
- **The options page opened automatically** on first install (`onInstalled` ->
  `chrome.runtime.openOptionsPage()`), with no manual click needed to discover it exists.
- Filling in the Hub URL and token fields and clicking Save (the exact UI a real user drives)
  persisted both values into `chrome.storage.local`, confirmed by reading it back directly:
  `{"amplifier_browser_bridge_hub_url": "ws://127.0.0.1:8910/device", "amplifier_browser_bridge_hub_token": "<set>"}`.
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
not be completed in that specific sandbox; every other step -- install, `amplifier-browser-bridge init`, `amplifier-browser-bridge hub`,
`amplifier-browser-bridge doctor`'s diagnostic chain, extension load, options-page auto-open, and config persistence
via the real UI -- was verified with real commands and real output as shown above.

### Update survives configuration (verified)

To prove the fix for the original bug (editing `extension/config.js` and copying it over a
running install silently wiped the working token), the clean-room extension directory was
re-staged in place -- `amplifier-browser-bridge init` re-run with the same `--dest` after a source file changed,
simulating a `git pull` + reinstall:

```console
$ amplifier-browser-bridge init --dest ./extension --token-file ./tokens.json ...
Reusing existing hub token (stored in ./tokens.json).   # <- NOT regenerated
Staged extension -> ./extension                          # <- same path, files updated in place
```

The already-loaded browser profile's `chrome.storage.local` was re-checked afterward (same
profile directory, extension reloaded from the now-updated staging directory) and found
**unchanged**: `amplifier_browser_bridge_hub_url`, `amplifier_browser_bridge_hub_token`, and the extension's own generated `amplifier_browser_bridge_device_id`
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
`amplifier-browser-bridge cmd` / `browser_poll` to check on it later. A tool call that silently hangs for two minutes
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
| Kill switch | A hub-level stop-all that halts new dispatch and rejects every queued command immediately -- reachable via `amplifier-browser-bridge kill-switch engage\|disengage\|status` (A4 fix: previously library-API-only, `Hub.engage_kill_switch()`, with no CLI or wire-protocol path to it) |
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
`write` scope up front (`amplifier-browser-bridge session-establish`) so the action is denied outright, rather than
relying on a gate that a human will never actually see.

## Platform support

**Edge desktop is the supported platform. Edge Android is EXPERIMENTAL.** Read the box below
before reading the table -- the table describes what the *platform* can do, not how easy it is
to get this extension onto it.

> ### Android support is experimental
>
> - **You cannot install this on Edge Android stable.** Edge for Android gained extension
>   support in March 2025 (v134), but only for a **small, Microsoft-curated set** of extensions
>   -- roughly two dozen, chosen by Microsoft. **This extension is not on that list**, and there
>   is no application process documented for getting onto it.
> - **Sideloading requires Edge Canary or Beta on Android**, via a hidden developer-options
>   flow: Settings -> About Microsoft Edge -> tap the build number 5 times -> Developer Options
>   -> "Extension install by crx". Microsoft does not document this flow anywhere public; it is
>   known from community reporting and from this project's own testing. Whether it is genuinely
>   *exclusive* to Canary/Beta is inferred, not confirmed by Microsoft.
> - **The install is awkward on purpose-of-the-platform grounds, not ours.** The artifact must
>   be served as `.bin` and renamed to `.crx` on the phone, because Chromium intercepts `.crx`
>   downloads and Edge Android silently discards the file. A battery-optimization exemption is
>   an onboarding *requirement*, not a tip.
> - **This extension's own code has never been confirmed running on a real Android device.** The
>   Android platform behaviors below were measured on real hardware with a *separate throwaway
>   probe extension*, not with this project's code. See
>   [docs/ANDROID.md](docs/ANDROID.md)'s "What remains unproven" for the exact line between what
>   was proven and what was inferred.
>
> Treat every "Yes" in the Edge Android column as "the platform supports this", not as "this
> extension has been observed doing this on your phone."

| Capability | Edge desktop (Windows / macOS / Linux) | Edge Android (experimental) |
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

Beyond the CLI shown above, the same lib is exposed as an MCP server (`amplifier-browser-bridge-mcp`, for any
MCP-speaking client) and an Amplifier tool module (`modules/tool-browser-bridge/`, composed via
`bundle.md`). Both are thin adapters -- all logic lives in the Python lib. See
[docs/AGENT_SURFACES.md](docs/AGENT_SURFACES.md) for how to run and configure each, and for the
proof that both work end-to-end against a real hub.

```bash
uv pip install -e ".[mcp]"
amplifier-browser-bridge-mcp   # runs over stdio, the default every MCP client speaks
```

## Testing

```bash
# Run from the repo root. `uv pip install` REQUIRES an active virtualenv -- with none
# active it refuses with "No virtual environment found" and exits non-zero, and the
# `pytest` lines then die with "command not found". These two lines are not optional.
uv venv
source .venv/bin/activate

# Root package -- 412 tests.
uv pip install -e . pytest pytest-asyncio "mcp<2"
pytest tests/

# Amplifier tool module -- 14 tests. amplifier-core is a PEER dependency the
# module deliberately does not declare (see its pyproject.toml), so install it here.
uv pip install -e ./modules/tool-browser-bridge amplifier-core
pytest modules/tool-browser-bridge/tests/
```

Verified 2026-08-08 by extracting this exact block from `README.md` and running it with
`bash`, from a fresh anonymous clone, in a container with no venv active.

Each part of that first command is load-bearing; dropping any one of them produces a
failure that looks like a broken repo rather than a missing package:

| Omit | What actually happens |
|---|---|
| `pytest-asyncio` | **32 failed, 380 passed** -- every `async def` test errors with "async def functions are not natively supported" |
| `mcp<2` | `pytest tests/` aborts during collection: `tests/test_mcp.py` -> `mcp_server.py` -> `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` |
| `amplifier-core` | `modules/tool-browser-bridge/tests/` aborts during collection: `ModuleNotFoundError: No module named 'amplifier_core'` |

**Two traps worth naming explicitly:**

- **There is no `[dev]` extra.** `pyproject.toml` declares exactly one optional
  dependency group, `mcp`. `uv pip install -e ".[dev]"` does not fail -- it prints
  `warning: ... does not have an extra named 'dev'` and **exits 0 having installed
  nothing**, so the next command dies with 32 collection errors.
- **`.[mcp]` does not currently work either.** The declared floor is `mcp>=1.6`, which
  today resolves to `mcp` 2.0.0 -- a release that removed `mcp.server.fastmcp`, the
  exact symbol `src/amplifier_browser_bridge/mcp_server.py:35` imports. Until that
  floor is capped in `pyproject.toml`, pass `"mcp<2"` explicitly (1.29.0 resolves and
  passes). This affects `amplifier-browser-bridge-mcp` at runtime too, not just tests.

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
