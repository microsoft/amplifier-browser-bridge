# Install -- Amplifier Browser Bridge (Edge extension)

You are reading this because you downloaded a zip and are about to sideload a
browser extension. **Read this whole document before you start** -- this
install is harder than a typical "unzip and load unpacked" extension, because
this extension does not work by itself. It is one of three pieces that all
have to be running and correctly configured together:

1. **The hub** -- a small Python program that runs somewhere reachable (your
   own machine, or a machine on your tailnet). It is the thing an AI agent
   actually talks to.
2. **This extension** -- runs inside Microsoft Edge, connects OUT to the hub,
   and is what actually reads/clicks/types on your behalf.
3. **A shared token** -- a password-like secret both sides must agree on, so
   that only your own hub and your own extension can talk to each other.

If you skip any of the three, nothing will work, and the failure will look
like "the extension does nothing" rather than a clear error -- so follow the
steps in order.

## Why this install is longer than a normal extension

Those three pieces are not incidental complexity -- they are the product.
Running your own hub, holding your own token, and reaching it over your own
Tailscale network is what makes the agent reach your browser across **your own
devices, with nothing else in the path**. The nearest alternative you could
pick instead, [`browser-relay`](https://github.com/reliefeai/browser-relay)
(MIT), is genuinely easier to start: it routes through a public relay server
and authenticates with a bearer token its own README describes as *"a
capability -- anyone with it can control this browser."* You are trading a
longer setup for no third party in the path and no long-lived bearer
credential crossing the public internet.

If that is not a trade you want, that project is the honest alternative. This
install will not get shorter, because the length is the property.

## Who should install this

This software lets an AI agent read and act inside your **real, logged-in**
Edge browser -- on this machine, or on a completely different device, over
your own [Tailscale](https://tailscale.com) network. It is not a sandboxed or
disposable browser; it is the browser you actually use, with your actual
sessions. Before installing, read
[SECURITY.md](https://github.com/bkrabach/amplifier-browser-bridge/blob/main/SECURITY.md)
in the source repository -- it explains, plainly, what protects you and what
does not.

## What you need before you start

- **Microsoft Edge**, desktop (Windows, macOS, or Linux). This extension does
  not support other browsers. **Edge on Android is experimental and is not
  installable this way** -- see "Android" at the end of this document before
  you try.
- **A place to run the hub.** The hub is a Python program (`amplifier-browser-bridge
  hub`). This can be the same machine running Edge, or a different machine on
  your tailnet -- both work, but if hub and browser are on different devices
  you need Tailscale connecting them (see below).
- **[Tailscale](https://tailscale.com)**, installed and signed in, if you want
  the hub and the browser on different devices. If hub and browser are the
  *same* machine, Tailscale is optional -- `127.0.0.1` (loopback) is enough.
- **Python 3.12 or newer, and [`uv`](https://docs.astral.sh/uv/getting-started/installation/)**,
  on whichever machine will run the hub. `uv` is how you install the hub in
  Step 1.
- **`git`**, to clone the hub's source. The hub is **not** on PyPI yet, so
  there is no `pip install` shortcut -- see Step 1.

## Step 1 -- Install and start the hub

The hub is not inside this zip -- it is a separate Python package. It is
**not published to PyPI yet**, but the repo is public, so `uv` can install it
straight from GitHub -- no local clone required. On the machine that will run
it:

```bash
uv tool install git+https://github.com/bkrabach/amplifier-browser-bridge@main
amplifier-browser-bridge init
```

No GitHub account, token, or SSH key is needed -- the repo is public. (If you
prefer a local clone -- e.g. to inspect the source first -- that also works:
`git clone` the repo, `cd` into it, then `uv tool install .` instead of the
`git+https://...` line above.)

> **Do not use `uv tool install amplifier-browser-bridge` or
> `pip install amplifier-browser-bridge`.** Neither works today; the name is
> not on PyPI, and both fail with a package-not-found error. When a release is
> published, that one-liner will replace the `git+https://...` command above.

Run from a real terminal, `init` walks you through the rest of this step
itself: it generates a token, offers to install and start the hub as a
background service, and confirms the hub actually came up before moving on.
Answer `Y` (or just press Enter) at the prompt:

```
  1. Start the hub as a background service (recommended -- survives logout and reboot).
     (auto-detected this machine's Tailscale IP via `tailscale ip -4`: 100.x.y.z)
     Install and start it now? (amplifier-browser-bridge service install --host 100.x.y.z --port 8900) [Y/n]:
     Installed and started the amplifier-browser-bridge service (linux).
     Confirmed: hub reachable at ws://100.x.y.z:8900/agent
```

If you'd rather run the hub yourself in a terminal you keep open (it stops
the moment you close that terminal or press Ctrl-C), answer `n` -- `init`
prints the exact command instead of installing anything:

```bash
AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE=~/.config/amplifier-browser-bridge/tokens.json \
  amplifier-browser-bridge hub --host <the address init printed> --port 8900
```

**Running this from a script, CI, or anything without a real terminal
attached** (no tty)? `init` detects that automatically and never prompts --
it prints the classic four manual steps (service install, load the
extension, pair, doctor) instead, same as every earlier release, so nothing
hangs waiting on input that will never arrive. Pass `--non-interactive` to
force that behavior even from a real terminal, or `--yes` to auto-accept the
service install without a terminal attached (it still won't block waiting
for you to load the extension -- see Step 3).

Leave the hub running. If it stops, the extension will show a disconnected
state and nothing will work until you restart it -- this is exactly what the
service path above avoids having to think about. See "Running the hub as a
service" below for the full `service` command surface, what platforms it
supports, and how it handles a rotated token or a changed Tailscale IP.

### Running the hub as a service

`amplifier-browser-bridge service` manages a **systemd --user** unit (Linux)
or a **launchd** user agent (macOS) for the hub -- never a system-wide
service, never root/sudo.

```bash
amplifier-browser-bridge service install --host <host> --port 8900   # install + start
amplifier-browser-bridge service status                              # installed? running?
amplifier-browser-bridge service stop                                 # stop without uninstalling
amplifier-browser-bridge service start                                # start it again
amplifier-browser-bridge service restart                              # e.g. after rotating the token
amplifier-browser-bridge service uninstall                             # stop + remove entirely
amplifier-browser-bridge service logs                                  # tail its logs
```

- **Rotating the token** (`amplifier-browser-bridge init --force`) only
  needs `amplifier-browser-bridge service restart` -- the token FILE PATH is
  baked into the service, not its contents, so a restart alone picks up the
  new value.
- **This machine's Tailscale IP changing** (or wanting a different port)
  needs `amplifier-browser-bridge service install` re-run (safe to repeat).
- **`amplifier-browser-bridge doctor` knows about the service**: a hub
  installed as a service but currently stopped is reported as exactly that,
  with the fix (`amplifier-browser-bridge service start`), instead of a bare
  connection failure.
- **Windows is not implemented in this release.** There is no systemd/launchd
  equivalent this command drives on Windows yet -- run
  `amplifier-browser-bridge hub ...` directly (see the foreground command
  above), or wrap it yourself as a real Windows service (Task Scheduler set
  to run at log on, or NSSM/WinSW). `amplifier-browser-bridge service ...`
  fails loud and says exactly this on Windows rather than doing nothing
  silently.
- **macOS (launchd) is implemented but could not be exercised end-to-end from
  this project's Linux-only development environment** -- every unit/plist
  write and `launchctl` call is unit-tested, but real verification on a Mac
  is still outstanding. The Linux (`systemd --user`) path has been verified
  end-to-end: installed, confirmed running and reachable, stopped (`doctor`
  correctly reported it), restarted, and uninstalled (unit file confirmed
  gone) -- see the source repository's README.md for the full transcript.

## Step 2 -- Load this extension into Edge

**If you got here from a link, skip to step 2 below** -- you already have the
zip. If you're reading this from inside a zip you downloaded some other way
(e.g. a GitHub release), the hub's own `GET /setup` page is now the primary
way to get this file: on the machine running Edge (which may or may not be
the same machine as the hub), visit `http://<hub host>:<hub port>/setup` and
click the desktop download button there. That page exists specifically so
you don't need a path on the hub machine's filesystem -- it downloads the
zip onto the SAME machine Edge is running on.

Either way, from here the steps are identical:

1. **Unzip** this archive somewhere stable on your machine (e.g.
   `~/Extensions/amplifier-browser-bridge/`). The folder must stay where it
   is -- Edge loads the extension from this location every time it starts,
   the same way it loads any unpacked extension.
2. Open `edge://extensions/`.
3. Toggle **Developer mode** on (bottom-left of the page).
4. Click **Load unpacked** and select the folder you unzipped.
5. The extension's options page should open automatically on first install.
   If it doesn't, click the extension's toolbar icon (its only UI) -- that
   also opens the options page.

**Chromium cannot install an extension directly from a zip file -- there is
no one-click install.** The `/setup` page does not pretend otherwise; the
win it provides is that the file arrives on the browser's own machine, not
that unzipping and "Load unpacked" go away.

## Step 3 -- Configure the extension

If you're following `init`'s guided flow (a real terminal, no `--non-interactive`),
it prints a single link -- in step 2 above -- that already has a **pairing code**
built into it (minted right after the hub comes up, not later):

```
  2. Add this browser -- open this link ON THE BROWSER YOU WANT TO ADD (any
     device on your tailnet, not necessarily this machine). The pairing code is
     already included -- valid 600s, expires 12:46:34:
       http://100.124.126.19:8900/setup#pair=FS55M-H87XV@100.124.126.19:8900&exp=1786301234
```

Opening that link (on the browser being paired) shows the same code with a live
countdown. Usually you don't need to do anything else: the moment the
extension's options page opens (Step 2 above), it scans open tabs and the
clipboard for that same code and redeems it on its own -- the options page's
step 2 flips straight to "Connected" with no paste required. If it doesn't
connect within a few seconds, open **"It didn't connect on its own"** on the
setup page (or **"Enter a code by hand"** on the options page) and paste the
code manually, then click **Pair**. Either way, the Hub URL and a
freshly-minted token are fetched automatically -- there is nothing else to
copy by hand. Once the hub sees the connection, `init` continues on its own --
there is no "did you finish?" prompt to answer. If nothing connects within a
few minutes, `init` falls back to asking once; if the code expires before
then, it also prints the exact command to mint a fresh one.

**Configuring manually instead** (piped/non-interactive `init`, or you just
prefer typing both values in yourself)? On the options page opened in Step 2,
open **"Manual setup"**:

- **Hub URL**: paste the URL `init` printed (`ws://<host>:<port>/device`).
- **Token**: paste the token `init` printed.
- Click **Save**.

The extension connects to the hub immediately after saving (either path). The
toolbar icon and the options page's status line tell you whether it connected.

**If hub and browser are on different devices**, the Hub URL must use your
Tailscale IP address (e.g. `100.x.y.z`), not `127.0.0.1` and not a Tailscale
MagicDNS name (`something.tailnet.ts.net`) -- MagicDNS name resolution was
measured to be unreliable on some devices in this project's own testing; a
plain IP address (Tailscale calls this an "IP literal") is the one thing that
worked everywhere it was tested. Find your hub machine's Tailscale IP with
`tailscale ip -4` on that machine.

## Step 4 -- Confirm it worked

On the machine running the hub:

```bash
AMPLIFIER_BROWSER_BRIDGE_TOKEN=<the token from Step 1> \
  amplifier-browser-bridge doctor --hub-url ws://127.0.0.1:8900/agent
```

`doctor` runs six checks in order -- the token file (`token_store`), other
token-like files sitting beside it (`token_file_siblings`), this machine's
network exposure and Tailscale ACL posture (`network_exposure`), hub
reachability (`hub_reachable`), token match (`token_match`), and whether a
browser has ever connected (`device_connected`). It stops at the first broken
link and marks everything downstream `skipped`, so you get one thing to fix
rather than a wall of red. A healthy setup ends with `[ok] device_connected`.

**If you run `doctor` before finishing Steps 2 and 3, the last line will read
`[FAIL] device_connected: no browser device has ever connected to this hub`
and the command will exit non-zero. That is expected, not a malfunction** --
it is `doctor` telling you the browser half isn't done yet, and naming the
step. Everything above that line showing `[ok]` means the hub half is
correct.

Once `doctor` confirms a connection, an agent can issue commands, e.g.:

```bash
amplifier-browser-bridge devices
amplifier-browser-bridge tabs <device_id>
```

## The "started debugging this browser" banner is expected

At some point Edge will put a bar across the top of your browser reading
*"Amplifier Browser Bridge started debugging this browser"*, with a **Cancel**
button. **Nothing is wrong.** That is the browser announcing -- in a place no
extension can fake or hide -- that the agent just escalated to a level of
control that deserves announcing. This project deliberately does not suppress
it.

What to expect:

- It appears **only** for the two commands that genuinely need it: a
  `trusted` (real, non-synthetic) click/type/key, or a screenshot of a tab
  that is not in the foreground. Ordinary reading, clicking, and navigating
  raise nothing.
- It appears on **every tab in every window**, not just the tab the agent is
  working in -- so a bar can show up on the page you are personally reading.
- It goes away on its own, roughly **25 seconds** after the agent stops.
- **Cancel** cuts off that level of access immediately, across all tabs. It
  always works, and nothing in this software can intercept it.

Where those numbers come from -- and the parts of this that are *not*
confirmed for Edge specifically -- is written out in
[`docs/DEBUGGER_BANNER.md`](https://github.com/bkrabach/amplifier-browser-bridge/blob/main/docs/DEBUGGER_BANNER.md).

## What this extension can do (permissions)

This is not a narrow, single-purpose extension -- it is a general remote-control
surface for your browser, and its permissions reflect that honestly rather than
minimizing it. See
[`docs/permission-justifications.md`](https://github.com/bkrabach/amplifier-browser-bridge/blob/main/docs/permission-justifications.md)
in the source repository for the full, long-form reasoning behind each one
(`<all_urls>`, `chrome.debugger`, and the persistent connection to the hub in
particular). In short:

| Permission | Why it's there |
|---|---|
| `<all_urls>` (host permission) | The agent can be asked to act on any site you browse to -- this is the maintainer's own explicit design goal ("I generally want it to be able to access what I access"), not an oversight. |
| `scripting`, `tabs`, `webNavigation` | Read page content, dispatch clicks/typing, know what tabs/URLs exist. |
| `debugger` | Optional escalation for trusted (non-synthetic) input events and screenshotting a tab that isn't in the foreground. Not used unless a command specifically asks for it. |
| `storage` | Stores the Hub URL/token you enter in Step 3, and this device's own identifier. |
| `downloads`, `alarms`, `tabGroups` | Support specific commands (download claiming, keepalive, tab-group-aware addressing). |
| `clipboardRead` | Lets the Settings page auto-fill a pairing code from your clipboard when no already-open pairing tab is found, so pairing needs no manual copy/paste. Never sent anywhere; only used if it looks like this project's own pairing code shape. |

There is **no telemetry and no third party** -- the only network connection
this extension makes is the WebSocket to the hub you configured in Step 3,
which you are running.

## Updating

When a new version arrives:

1. Download and unzip the new version over the existing folder (or to a
   fresh folder, then update `edge://extensions/`'s "Load unpacked" path).
2. In `edge://extensions/`, click the reload icon under the extension, or use
   the extension's own `reload` command once connected
   (`amplifier-browser-bridge reload <device_id>`).

Your Hub URL and token are **not** stored in any file this update touches --
they live in the extension's `chrome.storage.local`, keyed to its install
path, and survive an update untouched.

## Uninstalling

1. In `edge://extensions/`, click **Remove** on the extension.
2. Optionally delete the unzipped folder.
3. Stop the hub:
   - If you installed it as a service (recommended path above):
     `amplifier-browser-bridge service uninstall` -- stops it and removes the
     systemd/launchd unit entirely.
   - If you ran it directly in a terminal: Ctrl-C (or however you started
     it) if you no longer need it running.

## Android (experimental -- these instructions do not apply)

**Everything above describes Edge on the desktop. Android support is
experimental, and the steps above will not work there.** If you came here
hoping to put this on a phone, read this section before spending time on it:

- **Edge Android stable cannot install this extension.** Edge for Android
  gained extension support in March 2025, but only for a **small,
  Microsoft-curated set** of extensions -- roughly two dozen, chosen by
  Microsoft. **This extension is not on that list**, and no public process for
  getting onto it is documented.
- **Sideloading requires Edge Canary or Beta on Android**, through a hidden
  developer-options flow: Settings -> About Microsoft Edge -> tap the build
  number **5 times** -> Developer Options -> **"Extension install by crx"**.
  Microsoft does not document this flow publicly; it is known from community
  reporting and this project's own testing. Whether it is genuinely
  *exclusive* to Canary/Beta is inferred, not confirmed by Microsoft.
- **The flow is not "unzip and load unpacked".** It needs a packed `.crx`
  built by `scripts/package-android.sh`, served as a `.bin` file and renamed
  to `.crx` on the phone -- because Chromium intercepts `.crx` downloads and
  Edge Android silently discards the file. "Extension install by crx" also
  requires a **local file path**, not a URL.
- **A battery-optimization exemption is a requirement, not a tip.** Without
  it, the phone goes unreachable whenever the screen is off.
- **The Android file is a live credential to your browser, and it does not
  rotate.** Because Edge Android has no reachable options page, the hub URL
  and token are baked *inside* the `.crx`. Anyone who obtains that file can
  install it and connect to your hub. Delete it from the phone once the
  extension is connected, never forward it (not even to troubleshoot), and if
  it leaves your control, **treat it as compromised**. Revocation is
  all-or-nothing: rotating the hub token invalidates every phone *and* every
  desktop you have configured, and each must be redone by hand.
- **This extension's own code has never been confirmed running on a real
  Android device.** The platform behaviors were measured on real hardware with
  a separate throwaway probe extension, not with this project's code.

The full runbook, including the honest list of what remains unproven, is
[`docs/ANDROID.md`](https://github.com/bkrabach/amplifier-browser-bridge/blob/main/docs/ANDROID.md)
in the source repository.

## Trouble?

| Symptom | Likely cause |
|---|---|
| Options page shows a calm blue "Not paired yet" | Expected on a fresh install -- pair with a hub or enter a Hub URL/token and click Save. This is NOT an error state. |
| Options page shows a red "auth rejected" / "unreachable" message | A real problem: the hub rejected this device's token, or nothing answered at the configured address -- re-pair, or check the hub is running and reachable. |
| `doctor` fails at `hub_reachable` | The hub process isn't running, or the Hub URL/port doesn't match what you started it with. |
| `doctor` fails at `token_match` | The token in the extension's options page doesn't match the hub's token file. Re-copy it from `init`'s output, or re-run `init --force` to rotate and re-enter the new one. |
| `doctor` fails at `device_connected` | The extension is running but has never reached this specific hub -- check the Hub URL is correct and, for cross-device setups, that both devices are actually on the same tailnet (`tailscale status` on both). |
| Options page URL uses a MagicDNS name and nothing connects | Switch to the Tailscale IP literal (`tailscale ip -4`) instead -- see Step 3. |
| Extension connects, then goes dark for a long time on a phone/mobile device | Battery-optimization settings are suspending the browser in the background -- see `docs/ANDROID.md` in the source repository for the exact onboarding steps this project's own testing found necessary. |

For anything not covered here, file an issue at
<https://github.com/bkrabach/amplifier-browser-bridge/issues>.
