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

## Who should install this

This software lets an AI agent read and act inside your **real, logged-in**
Edge browser -- on this machine, or on a completely different device, over
your own [Tailscale](https://tailscale.com) network. It is not a sandboxed or
disposable browser; it is the browser you actually use, with your actual
sessions. Before installing, read
[SECURITY.md](https://github.com/microsoft/amplifier-browser-bridge/blob/main/SECURITY.md)
in the source repository -- it explains, plainly, what protects you and what
does not.

## What you need before you start

- **Microsoft Edge**, desktop (Windows, macOS, or Linux). This extension does
  not support other browsers.
- **A place to run the hub.** The hub is a Python program (`amplifier-browser-bridge
  hub`). This can be the same machine running Edge, or a different machine on
  your tailnet -- both work, but if hub and browser are on different devices
  you need Tailscale connecting them (see below).
- **[Tailscale](https://tailscale.com)**, installed and signed in, if you want
  the hub and the browser on different devices. If hub and browser are the
  *same* machine, Tailscale is optional -- `127.0.0.1` (loopback) is enough.
- **A way to run the hub program.** See "Installing and starting the hub"
  below -- it is a separate download from this zip.

## Step 1 -- Install and start the hub

The hub is not inside this zip -- it is a separate Python package. On the
machine that will run it:

```bash
uv tool install amplifier-browser-bridge   # or: pip install amplifier-browser-bridge
amplifier-browser-bridge init
```

`init` generates a token, prints the exact remaining commands (including
which host/port to bind), and tells you whether it auto-detected a Tailscale
IP. Follow its printed instructions to actually start the hub -- something
like:

```bash
AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE=~/.config/amplifier-browser-bridge/tokens.json \
  amplifier-browser-bridge hub --host <the address init printed> --port 8900
```

**Write down two things `init` prints**, you will need them in Step 3:

- The **Hub URL** (something like `ws://100.x.y.z:8900/device` --
  cross-device -- or `ws://127.0.0.1:8900/device` -- same-machine only).
- The **token** (a long random string).

Leave the hub running. If it stops, the extension will show a disconnected
state and nothing will work until you restart it.

## Step 2 -- Load this extension into Edge

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

## Step 3 -- Configure the extension

On the options page that opened in Step 2:

- **Hub URL**: paste the URL `init` printed in Step 1
  (`ws://<host>:<port>/device`).
- **Token**: paste the token `init` printed in Step 1.
- Click **Save**.

The extension connects to the hub immediately after saving. The toolbar icon
and the options page's status line tell you whether it connected.

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

`doctor` checks the token file, hub reachability, token match, and whether a
browser has ever connected -- and stops at the first broken link with a
specific, actionable message. A healthy setup ends with the device showing
as connected. If it doesn't, `doctor`'s own output tells you which of the
four links is broken.

Once `doctor` confirms a connection, an agent can issue commands, e.g.:

```bash
amplifier-browser-bridge devices
amplifier-browser-bridge tabs <device_id>
```

## What this extension can do (permissions)

This is not a narrow, single-purpose extension -- it is a general remote-control
surface for your browser, and its permissions reflect that honestly rather than
minimizing it. See
[`docs/permission-justifications.md`](https://github.com/microsoft/amplifier-browser-bridge/blob/main/docs/permission-justifications.md)
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
3. Stop the hub process (Ctrl-C, or however you started it) if you no longer
   need it running.

## Trouble?

| Symptom | Likely cause |
|---|---|
| Toolbar icon shows a red badge / "Not configured" | Hub URL or token was never saved -- open the options page (click the toolbar icon) and re-enter them. |
| `doctor` fails at `hub_reachable` | The hub process isn't running, or the Hub URL/port doesn't match what you started it with. |
| `doctor` fails at `token_match` | The token in the extension's options page doesn't match the hub's token file. Re-copy it from `init`'s output, or re-run `init --force` to rotate and re-enter the new one. |
| `doctor` fails at `device_connected` | The extension is running but has never reached this specific hub -- check the Hub URL is correct and, for cross-device setups, that both devices are actually on the same tailnet (`tailscale status` on both). |
| Options page URL uses a MagicDNS name and nothing connects | Switch to the Tailscale IP literal (`tailscale ip -4`) instead -- see Step 3. |
| Extension connects, then goes dark for a long time on a phone/mobile device | Battery-optimization settings are suspending the browser in the background -- see `docs/ANDROID.md` in the source repository for the exact onboarding steps this project's own testing found necessary. |

For anything not covered here, file an issue at
<https://github.com/microsoft/amplifier-browser-bridge/issues>.
