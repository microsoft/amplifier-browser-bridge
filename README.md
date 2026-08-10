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
| 6. CDP escalation | Trusted input events, background-tab screenshots, soft-detach | Built, never run against a real Edge browser |

What that means concretely:

- **Proven end-to-end**: the wire protocol, hub dispatch and queueing, the CLI, the MCP server,
  the Amplifier tool module, and the policy engine all have passing automated tests
  (`uv run pytest tests/`, `uv run pytest modules/tool-browser-bridge/tests/` -- see "Testing"
  below) and, for the agent surfaces, a documented real run against a live hub
  (`docs/AGENT_SURFACES.md`, "Verified end-to-end").
- **Measured on real hardware, not assumed**: every load-bearing transport and platform
  constraint in the design doc -- MV3 service worker lifetime, Android Doze behavior, background
  tab screenshot support, MagicDNS reliability -- was measured against real Edge installs on
  macOS and Android, not taken from documentation (`docs/designs/browser-bridge.md` section 2).
- **Built but never exercised on a real browser**: `chrome.debugger` (CDP) escalation for
  trusted input events and any-tab screenshot capture on desktop. The full path exists and is
  unit-tested (`cdp.py`, `hub.py`'s `_ensure_cdp_attached`, `background.js`'s `cdpAttach`,
  `tests/test_cdp.py`): a `trusted` or `capture_hidden` arg escalates that one tab on demand,
  and the hub soft-detaches after 20s idle so the debugger banner clears. What has *not*
  happened is a run of it against a real Edge install -- so the banner behavior in
  [docs/DEBUGGER_BANNER.md](docs/DEBUGGER_BANNER.md) is derived from Chromium source, not
  observed here. Without either arg, dispatch stays injection-only: synthetic input is not
  `isTrusted`, and `screenshot` only reaches the tab that is already active. CDP network
  interception is not implemented at all.
- **Not yet published**: this repository has no packaged release, no CI history, and has not
  been submitted to the Edge Add-ons store. Everything above is verified in-repo, not in
  production use.

## Security posture

This software controls a user's real, authenticated browser session. Before evaluating or
deploying it, read [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) for the full threat model.
In brief:

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

### Why the setup is this long

Three of the steps below -- run your own hub, hold your own token, address it by your own
Tailscale IP -- exist for exactly one reason: the connection between the agent and your browser
runs over **your own network, with nothing in the path but your own devices**. The nearest
alternative you could pick instead, [`browser-relay`](https://github.com/reliefeai/browser-relay)
(MIT), is genuinely faster to start: it routes through a public Cloudflare Worker relay and
authenticates with a bearer Device ID its own README calls *"a capability -- anyone with it can
control this browser."* That is the whole trade -- a longer setup, in exchange for no
third-party relay in the path, no long-lived bearer capability crossing the public internet, and
device-level ACLs instead of one shared string that travels.

If that trade is not one you want to make, `browser-relay` is the honest recommendation. This
setup is not going to get shorter, because the length *is* the property.

**This is the USER install path.** `uv tool install` is what you run to use this project.
`uv pip install -e .` (an *editable* install) is the CONTRIBUTOR path for iterating on this
repo's own source -- see CONTRIBUTING.md's "Dev setup" if that's what you're doing instead.

You need [`uv`](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.12 or
newer. **This package is not on PyPI yet** (see "Status" above -- no packaged release), but the
repo is public, so `uv tool install` can install straight from GitHub -- no local clone
required. (An explicit local-clone path is also shown further below for anyone who wants to
inspect the source first, or who needs an editable checkout -- see CONTRIBUTING.md's "Dev
setup" for that.)

Every command below was run verbatim on a clean machine with no prior configuration -- see
"Verified clean-room install" below for the full transcript, captured from a real run.

```bash
# 1. Install straight from GitHub (public repo, no credentials needed, no local clone
#    required). Verified working 2026-08-08 -- see "Verified clean-room install" below.
#    Once this package is published to PyPI, `uv tool install amplifier-browser-bridge`
#    (no `git+`, no URL) will work too and this collapses to that one command.
uv tool install git+https://github.com/microsoft/amplifier-browser-bridge@main

# 2. This is the ONLY other command you need to run. From a real terminal it walks
#    you through everything else -- installing the hub, then pairing the extension.
amplifier-browser-bridge init
```

Prefer a local clone (e.g. to read the source, or to pin a specific commit)?

```bash
git clone https://github.com/microsoft/amplifier-browser-bridge.git
cd amplifier-browser-bridge
uv tool install .        # a real, non-editable install -- NOT `uv pip install -e .`
amplifier-browser-bridge init
```

### `init` is the one command -- it walks you through the rest

Earlier releases had `init` print four manual steps (`service install`, load the extension,
`pair`, `doctor`) that you had to know existed and run in order. `init` now runs the first and
third of those *for you*, interactively, and stops for exactly the one step that is genuinely
manual (Edge has no CLI for loading an unpacked extension). This is real output, from a real run
on this project's own dev machine, with nothing pre-configured (see "Verified interactive `init`
end-to-end" below for the full, unedited transcript this was captured from):

```
$ amplifier-browser-bridge init
Generated new hub token (stored in /home/user/.config/amplifier-browser-bridge/tokens.json).
Staged extension -> /home/user/.local/share/amplifier-browser-bridge/extension

  1. Start the hub as a background service (recommended -- survives logout and reboot).
     (auto-detected this machine's Tailscale IP via `tailscale ip -4`: 100.124.126.19)
     Install and start it now? (amplifier-browser-bridge service install --host 100.124.126.19 --port 8900) [Y/n]:
     Installed and started the amplifier-browser-bridge service (linux).
     Confirmed: hub reachable at ws://100.124.126.19:8900/agent

  2. Add this browser -- open this link ON THE BROWSER YOU WANT TO ADD (any
     device on your tailnet, not necessarily this machine). The pairing code is
     already included -- valid 600s, expires 12:47:31:
       http://100.124.126.19:8900/setup#pair=FS55M-H87XV@100.124.126.19:8900&exp=1754750851
     It downloads the extension, walks through 'Load unpacked', then Settings -> Pair
     (code pre-filled). Same machine as this hub? Skip straight to Settings -> Pair:
       edge://extensions -> enable Developer mode -> Load unpacked -> select: /home/user/.local/share/amplifier-browser-bridge/extension
     Code expired, or pairing a different browser? amplifier-browser-bridge pair

  Waiting for the browser to connect... (checking every 2s; code expires in ~9m59s; will ask after 4m00s if nothing connects)
  Connected: device 3f1c...  (edge-macos, MacIntel) -- continuing automatically.

  3. Confirming...
[ok]   token_store: auth enabled; token file: /home/user/.config/amplifier-browser-bridge/tokens.json
[...]
All checks passed. Try: amplifier-browser-bridge devices
```

**That `devices` command works, unmodified, with no environment variables set.** `init` doesn't
just print the resolved host (`100.124.126.19` above) -- it PERSISTS it, at the moment it's
decided, to `~/.config/amplifier-browser-bridge/hub_location.json`. Every command that doesn't
take its own `--hub-url` (a bare `devices`, the MCP server, the Amplifier tool module) reads that
file back as its default, instead of each independently falling through to a hardcoded
`ws://127.0.0.1:8900/agent` -- which is what used to make `init`'s own suggested next command
crash with `ConnectionRefusedError` on a cross-device setup. An explicit `AMPLIFIER_BROWSER_BRIDGE_HUB_URL`
(or `--hub-url`) always overrides the persisted value; re-running `init --hub-host <ip>` or
`service install --host <ip>` corrects a stale one -- never by hand-editing the file. See
`amplifier-browser-bridge doctor`'s `hub_location` check to see what's currently persisted.

Answering `[Y/n]` with Enter (the default) at the ONE remaining prompt installs a real
systemd/launchd service and confirms the hub is actually reachable. From there `init` hands you
a single link that already carries the pairing code, then **watches for the browser to actually
connect and continues on its own** -- no second or third prompt to answer. Four things worth
calling out about that sequence:

- **The pairing code is minted right after the hub comes up, and the link leads.** The setup
  URL you're handed in step 2 already has the code embedded in its fragment (`#pair=...`,
  never sent to the hub -- see `docs/PROTOCOL.md`'s Pairing section) -- there is no separate
  trip back to this terminal to fetch a code once you're on the browser being paired.
- **`init` watches, it doesn't ask.** Once the link is printed, `init` polls the hub for a live
  device the same way `doctor` does, printing a visible waiting line (never a silent hang) with
  a running countdown against both the poll interval and the code's own TTL. The moment the hub
  sees the browser connect, `init` prints that and moves straight to the final `doctor` check --
  no "did you finish yet?" prompt.
- **If nothing connects within a few minutes, it falls back honestly** to a single "Still there?"
  confirmation rather than waiting forever (never a silent jump-cut past you either). Say `n` and
  it prints the exact `doctor` command to check whenever you're ready.
- **Piped, scripted, or run without a terminal attached** (CI, a digital twin, a redirected
  command), `init` never prompts and never installs anything beyond the token and the staged
  extension -- it prints the classic four-step block instead, exactly like every earlier release.
  Pass `--non-interactive` to force that from a real terminal too, or `--yes` to auto-accept the
  service install (never blocking on the browser step) even without a terminal attached.

**A1 fix (security review finding):** `amplifier-browser-bridge hub` used to default to `--host 0.0.0.0` --
binding every network interface the machine has (home Wi-Fi, hotel Wi-Fi, a corporate LAN), not
just the Tailscale tailnet this project's threat model assumes -- and `init` printed that default
back as the recommended command. `hub` now defaults to `--host 127.0.0.1` (loopback only); `init`
auto-detects this machine's own Tailscale IP (`tailscale ip -4`) as shown above so the printed/
installed command stays cross-device-capable without a silent wildcard bind. If Tailscale can't be
detected, `init` falls back to `127.0.0.1` and says so explicitly -- cross-device use then
requires passing `--hub-host <your tailnet IP>` yourself. Passing (or auto-falling to) a
wildcard host anywhere prints a specific, named warning listing exactly what it exposes. See
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) for the full accounting.

**Running the hub as a service (recommended).** This is what `init` offers to install for you --
see "Running the hub as a service" below for the full command surface (`start`/`stop`/`restart`/
`status`/`logs`/`uninstall`), what gets baked into the unit, how token rotation and Tailscale
IP changes are handled, and the real install-to-uninstall transcript this was verified against.
The foreground `hub` command shown when you decline the offer still works exactly as before, for
quick local testing or when a service manager isn't available (see that section's platform table).

Once pairing is confirmed (`init`'s last step runs `doctor` automatically and tells you), issue a
command:

```bash
amplifier-browser-bridge devices
amplifier-browser-bridge tabs <device_id>
amplifier-browser-bridge snapshot <device_id>/<tab_id>
amplifier-browser-bridge click <device_id>/<tab_id> <ref>
```

### The "started debugging this browser" banner is the system working

At some point Edge will put a bar across the top of your browser reading *"Amplifier Browser
Bridge started debugging this browser"* with a **Cancel** button. **That is expected -- it is
the browser announcing, in a place no extension can fake or hide, that the agent just escalated
to a level of control that deserves announcing.** This project does not suppress it, though a
mechanism exists to.

- It appears **only** when a command genuinely needs CDP: `trusted` input (an `isTrusted: true`
  click/type/key) or `capture_hidden` (screenshotting a tab that is not in the foreground).
  Ordinary `snapshot`/`read`/`click`/`navigate` raise nothing.
- It covers **every tab in every window** of that profile, not just the tab being driven -- so
  an agent working in a background tab puts a bar on the tab you are personally reading.
- It clears on its own roughly **25 seconds** after the agent stops: this hub soft-detaches CDP
  after 20s idle, and Chromium removes the banner 5s after the last detach.
- **Cancel** detaches every CDP session this extension holds. It is a real kill switch for CDP
  that nothing in this project can intercept.

Scope and lifetime above are read out of current Chromium source and are **not documented by
Google or Microsoft anywhere**; this project has also never observed its own banner on a real
Edge install. [docs/DEBUGGER_BANNER.md](docs/DEBUGGER_BANNER.md) names the exact source file
for each claim and lists, plainly, everything about it that remains unverified -- including one
in-repo field note that contradicts the source reading.

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

### Running the hub as a service

`amplifier-browser-bridge service` runs the hub as a **systemd --user** unit on Linux, or a
**launchd** user agent on macOS, so it survives logout and reboot instead of living in a
terminal you have to keep open -- while never running as root or needing sudo (a browser
remote-control hub running as root would be indefensible).

```bash
amplifier-browser-bridge service install --host <host> --port 8900   # install + start (as init prints)
amplifier-browser-bridge service status                              # installed? active? raw status too
amplifier-browser-bridge service stop                                 # stop without uninstalling
amplifier-browser-bridge service start                                # start it again
amplifier-browser-bridge service restart                              # e.g. after rotating the token
amplifier-browser-bridge service uninstall                             # stop + remove the unit entirely
amplifier-browser-bridge service logs                                  # tail the service's own logs
```

**What gets baked into the unit, and why.** Host, port, and the token file's PATH are baked in
as explicit `hub --host/--port/--token-file` **command-line arguments**, never environment
variables -- systemd --user and launchd services do **not** inherit the installer's shell
environment, so anything ambient (an exported env var) silently vanishes for a service-mode
process. Passing these as arguments instead of relying on inherited env sidesteps that class of
bug entirely, rather than working around it. The audit log defaults to
`~/.local/share/amplifier-browser-bridge/hub-audit.jsonl` when running as a service (not the
foreground hub's `./amplifier-browser-bridge-audit.jsonl`, which is meaningless for a process with no
particular working directory).

**Token rotation vs. host/port changes -- these need different responses:**

- **Rotating the token's contents** (`amplifier-browser-bridge init --force`) needs only
  `amplifier-browser-bridge service restart`. The token FILE PATH is what's baked into the unit,
  not its contents, so a restart alone picks up the new value -- no reinstall.
- **This machine's Tailscale IP changing** (or wanting a different port) needs
  `amplifier-browser-bridge service install` to be re-run (safe -- it's idempotent). A stale IP
  baked into an old unit does not fail silently: if the address is no longer assigned to any
  interface on this machine, the bind itself fails and `Restart=on-failure` keeps retrying,
  loudly, rather than pretending to be up -- `amplifier-browser-bridge service status` /
  `amplifier-browser-bridge doctor` both surface this.

**`doctor` knows about the service.** A hub that's installed as a service but currently stopped
is reported as exactly that -- `[FAIL] service_status: ... the hub is NOT running. Start it
with amplifier-browser-bridge service start` -- with the network checks below it marked
`skipped`, instead of a bare, unexplained connection failure. This check only asserts a failure
for the service on the SAME machine `--hub-url` targets (loopback, or this machine's own
detected Tailscale IP) -- pointed at a different host, it says so and stays informational.

**Platform support:**

| Platform | Mechanism | Status |
|---|---|---|
| Linux | `systemd --user` | Implemented, verified end-to-end on this repo's own dev machine (see below) |
| macOS | `launchd` (`~/Library/LaunchAgents`) | Implemented; the shape of every unit/plist write and every `launchctl` call is unit-tested, but **launchd itself cannot be exercised from this Linux-only development environment -- BLOCKED, not asserted working, until verified on a real Mac** |
| Windows | -- | **Not implemented in this release.** There is no systemd/launchd equivalent this module drives on Windows yet. Run `amplifier-browser-bridge hub ...` directly, or wrap it yourself as a real Windows service (Task Scheduler set to run at log on, or NSSM/WinSW) -- see [INSTALL.md](INSTALL.md)'s Windows section. `amplifier-browser-bridge service ...` fails loud and names this explicitly rather than silently doing nothing. |

**Verified end-to-end on Linux, 2026-08-08** (real `systemd --user`, no mocks, on this repo's
own development machine -- not a container, since the point was to prove real systemd
integration, which containers frequently can't provide):

```console
$ amplifier-browser-bridge service install --host 100.124.126.19 --port 8900
Created symlink /home/.../.config/systemd/user/default.target.wants/amplifier-browser-bridge.service → /home/.../.config/systemd/user/amplifier-browser-bridge.service.
Installed and started the amplifier-browser-bridge service (linux).
  Unit: /home/.../.config/systemd/user/amplifier-browser-bridge.service
  Hub URL for the extension: ws://100.124.126.19:8900/device
  Token file: /home/.../.config/amplifier-browser-bridge/tokens.json

Check it: amplifier-browser-bridge service status
Confirm it worked: amplifier-browser-bridge doctor --hub-url ws://100.124.126.19:8900/agent

$ systemctl --user status amplifier-browser-bridge --no-pager
● amplifier-browser-bridge.service - Amplifier Browser Bridge hub
     Loaded: loaded (.../amplifier-browser-bridge.service; enabled; preset: enabled)
     Active: active (running) since Sat 2026-08-08 18:32:16 PDT; 4s ago
   Main PID: 1093765 (amplifier-brows)
             └─1093765 .../bin/python .../bin/amplifier-browser-bridge hub --host 100.124.126.19 --port 8900 --token-file /home/.../tokens.json --audit-log /home/.../hub-audit.jsonl
Aug 08 18:32:17 spark-1 amplifier-browser-bridge[1093765]: amplifier-browser-bridge hub listening on ws://100.124.126.19:8900/device (extensions) and ws://100.124.126.19:8900/agent (agents); audit log -> /home/.../hub-audit.jsonl

$ AMPLIFIER_BROWSER_BRIDGE_TOKEN=<token> amplifier-browser-bridge doctor --hub-url ws://100.124.126.19:8900/agent
[ok]   service_status: service installed and active (unit: .../amplifier-browser-bridge.service).
[ok]   hub_reachable: hub reachable at ws://100.124.126.19:8900/agent
[ok]   token_match: token accepted by hub
[FAIL] device_connected: no browser device has ever connected to this hub. [...]
# (expected -- no browser was attached in this test; every prior check passed)

$ amplifier-browser-bridge service stop
Stopped the amplifier-browser-bridge service.
$ ss -tln | grep 8900   # nothing -- port released
$ AMPLIFIER_BROWSER_BRIDGE_TOKEN=<token> amplifier-browser-bridge doctor --hub-url ws://100.124.126.19:8900/agent
[FAIL] service_status: service is installed but NOT active (...) -- the hub is NOT running. Start it with `amplifier-browser-bridge service start` [...]
[skip] hub_reachable: skipped (service not running)
[skip] token_match: skipped (service not running)
[skip] device_connected: skipped (service not running)

$ amplifier-browser-bridge service start
Started the amplifier-browser-bridge service.
$ ss -tln | grep 8900   # LISTEN 100.124.126.19:8900 -- back up

$ amplifier-browser-bridge service uninstall
Removed "/home/.../.config/systemd/user/default.target.wants/amplifier-browser-bridge.service".
Removed the amplifier-browser-bridge service.
$ systemctl --user status amplifier-browser-bridge
Unit amplifier-browser-bridge.service could not be found.
```

Also confirmed: the running hub process's own environment (`/proc/<pid>/environ`) contains
**zero** `AMPLIFIER_BROWSER_BRIDGE_*` variables -- host, port, token file, and audit log all
arrived purely via the unit's explicit command-line arguments, exactly as designed, with no
dependence on anything that happened to be exported in the installer's shell. This is the
concrete proof that the service survives a fresh login (or a reboot with
[`loginctl enable-linger`](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html)
enabled for this user, so the user's systemd instance -- and this unit under it -- starts even
with nobody logged in) rather than merely "working while my shell happens to still be open."

### Verified clean-room install

Run 2026-08-08 on a **throwaway Ubuntu 24.04 container with nothing pre-installed** -- no copy
of this package, no `~/.config/amplifier-browser-bridge/`, no repo checkout, no GitHub
credentials, no SSH key, no `~/.netrc`, no `~/.gitconfig`. Only `git`, `curl`, Python 3.12, and
`uv` were present, which is what a stranger's machine looks like. The repo was cloned
anonymously over HTTPS and the Quickstart above was then executed **verbatim**, in order, with
nothing substituted:

```console
$ git clone https://github.com/microsoft/amplifier-browser-bridge.git
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
     On the browser being paired (open this URL THERE -- any device on your
     tailnet, not necessarily this machine):
       http://127.0.0.1:8900/setup
     Same machine as this hub? Skip the download and use the staged copy directly:
       edge://extensions -> enable Developer mode -> Load unpacked -> select: /root/.local/share/amplifier-browser-bridge/extension

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
[ok]   network_exposure: targets loopback ('127.0.0.1') -- cannot prove the hub isn't ALSO bound wider.
         this check can only see what host YOU pointed doctor at [...]
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

### Verified interactive `init` end-to-end (guided flow, auto-advance)

Run 2026-08-09 on this project's own Linux dev machine, in a real PTY (`terminal_inspector`, not
`click.testing`), on a real Tailscale tailnet, against a real, temporarily-repurposed
`systemd --user` service (test host/port/token; restored to the original production
config immediately afterward -- see `docs/designs/browser-bridge.md`'s onboarding-v2 notes for the
restore procedure). **Loading the extension into a real Edge browser is BLOCKED in this
environment -- no Edge binary exists here.** That gap is stated plainly, not glossed over: the
"device connects" half below is driven by a small throwaway script that performs the exact two
network calls the real extension's `options.js`/`background.js` make (`POST /pair/redeem`, then
`hello` over the `/device` WebSocket) -- proving the HUB-side auto-advance mechanism for real,
without asserting anything about the extension UI this environment cannot run.

```console
$ amplifier-browser-bridge init --hub-host 100.124.126.19 --hub-port 18900 --token-file /tmp/onboarding-e2e/tokens.json --dest /tmp/onboarding-e2e/extension
Generated new hub token (stored in /tmp/onboarding-e2e/tokens.json).
Staged extension -> /tmp/onboarding-e2e/extension

  1. Start the hub as a background service (recommended -- survives logout and reboot).
     Install and start it now? (amplifier-browser-bridge service install --host 100.124.126.19 --port 18900) [Y/n]: y
     Installed and started the amplifier-browser-bridge service (linux).
     Confirmed: hub reachable at ws://100.124.126.19:18900/agent

  2. Add this browser -- open this link ON THE BROWSER YOU WANT TO ADD (any
     device on your tailnet, not necessarily this machine). The pairing code is
     already included -- valid 600s, expires 12:46:34:
       http://100.124.126.19:18900/setup#pair=B3FT6-20TTG@100.124.126.19:18900&exp=1786304794
     It downloads the extension, walks through 'Load unpacked', then Settings -> Pair
     (code pre-filled). Same machine as this hub? Skip straight to Settings -> Pair:
       edge://extensions -> enable Developer mode -> Load unpacked -> select: /tmp/onboarding-e2e/extension
     Code expired, or pairing a different browser? amplifier-browser-bridge pair

  Waiting for the browser to connect... (checking every 2s; code expires in ~10m00s; will ask after 4m00s if nothing connects)
    ...still waiting (16s elapsed; auto-detect gives up in 223s)
    ...still waiting (32s elapsed; auto-detect gives up in 207s)
    ...still waiting (48s elapsed; auto-detect gives up in 191s)
  Connected: device 9ef19996-c4ae-4341-a373-d2103e5f413c (unknown, unknown) -- continuing automatically.

  3. Confirming...
[ok]   token_store: auth enabled; token file: /tmp/onboarding-e2e/tokens.json
[ok]   token_file_siblings: no other token-like files found alongside /tmp/onboarding-e2e/tokens.json
[ok]   network_exposure: targets '100.124.126.19' -- confirm this is your tailnet IP, not something wider.
         this machine's own Tailscale IP: 100.124.126.19.
         Tailscale's default ACL allows every device on your tailnet to reach every port on every other device -- unless you've written a restrictive ACL of your own (https://login.tailscale.com/admin/acls), the per-device token is your real security boundary, not the tailnet. Starting-point ACL: docs/tailscale-acl-example.hujson.
[ok]   service_status: service installed and active (unit: /home/bkrabach/.config/systemd/user/amplifier-browser-bridge.service).
[ok]   hub_reachable: hub reachable at ws://100.124.126.19:18900/agent
[ok]   token_match: token accepted by hub
[ok]   device_connected: 1 device(s) live: ['9ef19996-c4ae-4341-a373-d2103e5f413c']

All checks passed. Try: amplifier-browser-bridge devices
```

**No prompt was answered between step 1 and the final result above -- there is only ONE `[Y/n]`
in the entire transcript** (the service-install offer). The device-connect step, driven in a
second terminal partway through the "still waiting" lines (`POST /pair/redeem` against the code
above, then a `hello` over `ws://.../device`), was observed by the watch loop within one poll
cycle after it landed -- `init` printed `Connected: ...` and moved straight into `doctor` on its
own. `label`/`platform` show as `"unknown"` because the throwaway script's `hello` (unlike the
real extension's) doesn't send those fields -- expected and immaterial to what this proves (the
hub-side observation and auto-continue, not the extension's own payload contents).

**`label`/`platform: unknown` is the one honest gap in this specific proof, not a defect:** a real
Edge extension's `hello` always includes both (`background.js`'s `connect()`), so a real pairing
would show e.g. `(edge-macos, MacIntel)` here instead.

This is the direct fix for the exact hassle reported after a real run: previously this same
juncture asked two separate `[Y/n]` prompts ("Loaded, and its Settings page is open?" / "Entered
the code and clicked Pair?") and only minted the pairing code after the first of them -- forcing a
trip back to this terminal to fetch it. Here the link in step 2 already carries the code, and
nothing after step 1 requires a human answer at all when the browser actually connects in time.

The non-interactive fallback and the decline path were verified too, in the same real session.
Piped/non-interactive (`--non-interactive`, forced here even though this WAS a real terminal, to
prove the flag overrides tty detection) reuses the existing token (idempotent) and prints exactly
the classic four-step block:

```console
$ amplifier-browser-bridge init --non-interactive
Reusing existing hub token (stored in /home/bkrabach/.config/amplifier-browser-bridge/tokens.json).
Staged extension -> /home/bkrabach/.local/share/amplifier-browser-bridge/extension

Remaining steps (manual -- Edge has no CLI for these):

  1. Start the hub as a background service (recommended -- survives logout and reboot):
       amplifier-browser-bridge service install --host 100.124.126.19 --port 8900
       (auto-detected this machine's Tailscale IP via `tailscale ip -4`: 100.124.126.19)
     [... steps 2-4, identical to the block shown earlier in this README ...]
```

And declining the service offer prints the exact foreground command, then falls through to that
same classic block -- nothing is silently skipped:

```console
$ amplifier-browser-bridge init
Reusing existing hub token (stored in /home/bkrabach/.config/amplifier-browser-bridge/tokens.json).
Staged extension -> /home/bkrabach/.local/share/amplifier-browser-bridge/extension


  1. Start the hub as a background service (recommended -- survives logout and reboot).
     (auto-detected this machine's Tailscale IP via `tailscale ip -4`: 100.124.126.19)
     Install and start it now? (amplifier-browser-bridge service install --host 100.124.126.19 --port 8900) [Y/n]: n

Remaining steps (manual -- Edge has no CLI for these):

  1. Start the hub as a background service (recommended -- survives logout and reboot):
       amplifier-browser-bridge service install --host 100.124.126.19 --port 8900
       (auto-detected this machine's Tailscale IP via `tailscale ip -4`: 100.124.126.19)

     Or run it directly in this terminal instead (stops when the terminal closes):
       AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE=/home/bkrabach/.config/amplifier-browser-bridge/tokens.json amplifier-browser-bridge hub --host 100.124.126.19 --port 8900
     [... steps 2-4 follow, identical to the block shown earlier in this README ...]
```

**What's proven vs. what's BLOCKED:** everything on this machine -- token generation, extension
staging, real `systemd --user` install/start/status, real hub reachability confirmation, real
ticket minting and the fixed `pair` invocation, the non-interactive and decline fallbacks, and the
final `doctor` diagnostic -- is real output from real commands. Redeeming the pairing code inside
an actual Edge browser is **BLOCKED**: this environment has no Edge binary. That single step is
the same one every other verification pass in this README names as unprovable here (see "Known
gap in this specific verification" above) -- nothing new is being asserted about it.

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
> - **The Android artifact is a live credential to your browser, and it does not rotate.** To
>   work around Edge Android having no reachable options page, the build bakes the hub URL and
>   token *inside* the `.crx`. Anyone who obtains that file can install it and connect to your
>   hub. There is no way to revoke one artifact -- the only lever is rotating the hub token
>   (`init --force`), which invalidates every phone **and** every desktop you configured, each
>   of which then has to be redone. Tell anyone you hand the file to, including a tester:
>   **if it leaves your control, treat it as compromised.** See
>   [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md)'s
>   "One credential, two trust models" and "Where a live credential ends up after install".
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
| `chrome.debugger` (CDP): trusted input events, hidden-tab capture | Built, never run against a real Edge browser (raises a [browser-wide banner](docs/DEBUGGER_BANNER.md) while attached) | Not available on this platform at all |
| `chrome.debugger` (CDP): network interception | Not implemented | Not available on this platform at all |

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
# Run from the repo root. No venv activation, no manual install step: `pyproject.toml`'s
# [dependency-groups] dev declares everything this needs (pytest, pytest-asyncio, ruff,
# pyright, the `mcp` extra, amplifier-core, and this repo's own two packages), and
# `uv run` resolves + syncs it automatically on first use. This is the exact same
# command `.github/workflows/ci.yml` runs, so a green run here means CI is green too.

# Root package -- 412 tests.
uv run pytest tests/ -v

# Amplifier tool module -- 14 tests.
uv run pytest modules/tool-browser-bridge/tests/ -v
```

Verified 2026-08-09 by extracting this exact block from `README.md` and running it with
`bash`, from a fresh anonymous clone, with no venv active and nothing pre-installed.

**Why there is no "if you omit X, you get error Y" table here anymore:** every earlier version
of this section told a contributor to hand-type a `uv pip install <list of packages>` command
before running `pytest` -- and that list lived in three places at once (here, `CONTRIBUTING.md`,
and `.github/workflows/ci.yml`), each free to drift from the others. That drift already happened
in practice: dropping `pytest-asyncio` from the hand-typed list produced **32 failed, 380
passed**, every `async def` test failing with "async def functions are not natively supported"
(see `.github/workflows/ci.yml` git history for where this was first caught). Declaring the
dependency set exactly once, in `pyproject.toml`, and having every consumer (`uv run` locally,
CI) read from that one declaration removes the hand-typed list -- and the drift -- entirely.
There is no longer a set of packages a contributor or CI can partially install.

**One trap still worth naming:** `dev` is a [dependency group](https://peps.python.org/pep-0735/)
(`[dependency-groups]`), not a `[project.optional-dependencies]` extra. `uv pip install -e
".[dev]"` does not fail loud -- it prints `warning: ... does not have an extra named 'dev'` and
exits 0 having installed nothing. Use `uv run <command>` (as above) or `uv sync --group dev`
(to materialize `.venv` explicitly) instead.

## Contributing

> [!NOTE]
> This project is not currently accepting external contributions, but we're actively working toward opening this up. We value community input and look forward to collaborating in the future. For now, feel free to fork and experiment!

Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Further reading

- [docs/designs/browser-bridge.md](docs/designs/browser-bridge.md) -- the design of record: goals, measured evidence, architecture, transport, positioning
- [docs/PROTOCOL.md](docs/PROTOCOL.md) -- the wire protocol, message shapes, command vocabulary
- [docs/POLICY.md](docs/POLICY.md) -- the consent model in full, including its honest limits
- [docs/AGENT_SURFACES.md](docs/AGENT_SURFACES.md) -- MCP server and Amplifier tool module, with verified end-to-end proof
- [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) -- this project's own threat model: what protects you and what does not
- [CONTRIBUTING.md](CONTRIBUTING.md) -- dev setup, engineering conventions, loading the extension and running the hub locally
- [SECURITY.md](SECURITY.md) -- how to report a vulnerability (MSRC, the one channel that exists); [SUPPORT.md](SUPPORT.md) -- this project has no issue tracker and no support channel

## License

MIT. See [LICENSE](LICENSE).

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
