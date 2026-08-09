"""Self-serve onboarding page: platform detection + HTML rendering for the
hub's `GET /setup` route (see hub.py).

## Why this exists

`cli.py`'s `init` used to print a *filesystem path* on the hub machine
(`select: /home/<user>/.local/share/amplifier-browser-bridge/extension`) as
the instruction for loading the extension into Edge. That instruction is
only correct when Edge happens to run on the same machine as the hub -- and
this project's whole reason for existing is the opposite case: the hub on a
Linux box, Edge on a MacBook or an Android phone. On those machines the
printed path does not exist, is not reachable, and cannot be typed into
"Load unpacked". This module (and the single hub route that serves it) is
the fix: an onboarding page reachable, by URL, from any browser on the
tailnet -- the browser being paired fetches its own install artifact from
the hub, instead of the operator trying to hand it a path on someone else's
filesystem.

It also folds in the Android sideload path (previously
`scripts/serve-android-setup.py`, a separate ~10KB hand-rolled HTTP server on
its own port). Two design/product council reviews of this project both
flagged desktop and Android onboarding as "two unbridged mechanisms nobody
reconciled" -- one page, platform-aware, replacing both.

## What this module is NOT responsible for

This is pure presentation logic -- no filesystem access, no network calls,
no knowledge of the hub's device registry or audit log. Splitting it out
this way (the same judgment call muxplex's own `setup_page.py` makes for its
own CA-install onboarding page -- see that module's docstring) keeps it
trivially unit-testable and regeneratable in isolation from hub.py's
routing/auth concerns.

## Security note (echoes muxplex's `setup_page.py` posture)

`detect_platform` returns ONLY one of a fixed, closed set of labels (never
the raw User-Agent string), and `render_setup_page` never echoes any
request-supplied text into the HTML it returns -- there is nothing here for
a hostile `User-Agent` header to inject into.

The pairing code is deliberately **never threaded through this function at
all**. It travels only in the URL fragment (`#pair=<code>`), which browsers
never transmit to a server (fragments are stripped before the request is
sent) -- this page reads it back out of `location.hash` via inline
client-side JS after the page has already loaded. See hub.py's module-level
comment on these routes for the full authentication-circularity reasoning,
and `pairing.py`'s module docstring for the ticket design this displays.
"""

from __future__ import annotations

__all__ = ["detect_platform", "render_setup_page"]

# Deliberately a two-way split (desktop vs. android), not muxplex's four-way
# OS split (android/ios/macos/windows) -- this project's desktop instructions
# are IDENTICAL across Windows/macOS/Linux (see README.md's platform table);
# only Android needs a materially different flow (packed CRX, `.bin` rename
# trap, battery-optimization exemption). iOS is an explicit non-goal
# (README.md "Non-goals" -- Microsoft documents no extension API for it) so
# it is not a platform label here at all; an iOS UA falls through to
# "desktop" only in the sense that it gets the default label, not because
# desktop instructions apply -- the page does not claim iOS works.
_PLATFORMS = ("desktop", "android")


def detect_platform(user_agent: str) -> str:
    """Classify a User-Agent header into "android" or "desktop" (the default).

    Args:
        user_agent: The raw `User-Agent` request header (may be empty).

    Returns:
        "android" or "desktop".

    Example:
        >>> detect_platform("Mozilla/5.0 (Linux; Android 14; Pixel 8)")
        'android'
        >>> detect_platform("")
        'desktop'
        >>> detect_platform("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5)")
        'desktop'
    """
    if "Android" in (user_agent or ""):
        return "android"
    return "desktop"


_STYLE = """
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 16px 72px; background: #0D1117; color: #E6EDF3;
    font: 16px/1.55 -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  }
  main { max-width: 640px; margin: 0 auto; }
  h1 { font-size: 1.4rem; margin: 0 0 4px; }
  h2 {
    font-size: 0.85rem; margin: 28px 0 8px; color: #8B949E;
    text-transform: uppercase; letter-spacing: .07em;
  }
  .subtitle { color: #8B949E; margin: 0 0 20px; }
  .honesty {
    background: #161B22; border: 1px solid #30363D; border-radius: 8px;
    padding: 14px 16px; margin-bottom: 20px; font-size: 0.95rem;
  }
  .honesty b { color: #E6EDF3; }
  .exp {
    background: #241A13; border-left: 3px solid #d2691e; border-radius: 0 8px 8px 0;
    padding: 13px 15px; margin: 0 0 16px; font-size: 0.92rem;
  }
  .exp b { color: #ffb066; }
  .exp ul, .exp ol { padding-left: 20px; margin: 8px 0 0; }
  .exp li { margin: 6px 0; }
  .sec {
    background: #241318; border-left: 3px solid #b4402a; border-radius: 0 8px 8px 0;
    padding: 12px 14px; margin: 14px 0; font-size: 0.88rem; color: #d8c2c2;
  }
  .sec ul { padding-left: 20px; margin: 8px 0 0; }
  a.dl {
    display: block; background: #2F81F7; color: #fff; text-decoration: none;
    padding: 14px; border-radius: 8px; text-align: center; font-weight: 600;
    font-size: 1.02rem; margin: 12px 0;
  }
  a.dl.disabled { background: #30363D; color: #8B949E; pointer-events: none; }
  ol { padding-left: 22px; margin: 8px 0; }
  li { margin: 8px 0; }
  code {
    background: #21262D; padding: 0.15em 0.4em; border-radius: 4px; font-size: 0.92em;
    word-break: break-all;
  }
  details {
    background: #161B22; border: 1px solid #30363D; border-radius: 8px;
    margin: 10px 0; padding: 2px 14px;
  }
  summary { cursor: pointer; padding: 12px 0; font-weight: 600; }
  .pair-box {
    background: #0f2417; border: 1px solid #235c34; border-radius: 8px;
    padding: 16px; margin: 12px 0; text-align: center;
  }
  .pair-code {
    font-size: 1.3rem; font-weight: 700; letter-spacing: .03em; user-select: all;
    word-break: break-all; color: #7ee787;
  }
  .pair-hint { color: #8B949E; font-size: 0.85rem; margin-top: 8px; }
  .muted { color: #8B949E; font-size: 0.9rem; }
  #pair-section-none, #pair-section-found { display: none; }
"""

# Reused near-verbatim from the now-retired `scripts/serve-android-setup.py`
# (see that history for provenance) -- this is the hard-won knowledge from
# real device testing (docs/ANDROID.md), not new copy.
_ANDROID_TRAPS_HTML = """
<div class="exp">
  <b>&#9888; Android support is EXPERIMENTAL.</b> Edge on the desktop is the
  supported platform. This is a sideload with sharp edges.
  <ul>
    <li><b>This will not work on Edge Android stable.</b> Stable supports only a
        small, Microsoft-curated set of extensions &mdash; about two dozen. This
        extension is not on that list, and there is no documented way to get onto
        it.</li>
    <li><b>You need Edge Canary or Beta</b>, and the hidden developer-options flow
        in step 2 below. Microsoft does not document this flow publicly.</li>
    <li><b>This extension's own code has never been confirmed running on a real
        Android device.</b> Platform behavior was measured with a separate
        throwaway probe extension -- see docs/ANDROID.md, "What remains
        unproven".</li>
  </ul>
</div>
"""


def _desktop_section(open_attr: str) -> str:
    return f"""
<details data-platform="desktop"{open_attr}>
<summary>Desktop Edge (Windows / macOS / Linux)</summary>
<div class="honesty">
  <b>What this does and does not do.</b> Chromium cannot install an extension
  directly from a zip file -- there is no one-click install here. What changes
  is that the file is now downloaded <b>onto this machine</b>, the one running
  the browser, instead of you needing to find a path on the hub's filesystem.
  You still unzip it and still pick the folder yourself in the next step.
</div>
<a class="dl" href="/setup/extension.zip" download>Download extension (.zip)</a>
<ol>
  <li>Unzip the downloaded file somewhere stable on this machine.</li>
  <li>Open <code>edge://extensions</code>.</li>
  <li>Toggle <b>Developer mode</b> on (bottom-left).</li>
  <li>Click <b>Load unpacked</b> and select the unzipped folder.</li>
  <li>The extension's Settings page should open automatically. If not, click
      its toolbar icon.</li>
</ol>
</details>
"""


def _android_section(open_attr: str, *, artifact_available: bool) -> str:
    if artifact_available:
        download_html = (
            '<a class="dl" href="/setup/android-extension.bin" download>'
            "Download extension (.bin)</a>"
            '<div class="muted">Downloads as <code>.bin</code> on purpose &mdash; '
            "Chromium intercepts <code>.crx</code> downloads and Edge Android "
            "silently discards the file. Rename it to <code>.crx</code> in "
            "<b>My Files</b> before installing.</div>"
        )
    else:
        download_html = (
            '<a class="dl disabled" href="#" aria-disabled="true">'
            "No build available on this hub yet</a>"
            '<div class="muted">The operator needs to run '
            "<code>scripts/package-android.sh</code> and point this hub at the "
            "result (<code>--android-artifact</code>) before this download works. "
            "See docs/ANDROID.md.</div>"
        )
    return f"""
<details data-platform="android"{open_attr}>
<summary>Android (experimental)</summary>
{_ANDROID_TRAPS_HTML}
{download_html}
<ol>
  <li>Once downloaded, open <b>My Files &rarr; Downloads</b> and rename the file
      so it ends in <code>.crx</code> instead of <code>.bin</code>.</li>
  <li>Edge Canary &rarr; <b>Settings</b> &rarr; <b>About Microsoft Edge</b>.</li>
  <li>Tap the <b>build number 5 times</b> &mdash; this unlocks Developer Options.</li>
  <li>Back &rarr; <b>Developer Options</b> &rarr; <b>Extension install by crx</b>
      &mdash; this requires a local file, not a URL.</li>
  <li>Pick the renamed <code>.crx</code> file.</li>
</ol>
<div class="exp" style="border-color:#b4842a;background:#2a2213;">
  <b>Battery exemption is required, not a tip.</b> Settings &rarr; Apps &rarr;
  Edge Canary &rarr; Battery &rarr; <b>Unrestricted</b>, and remove it from
  "sleeping apps". Measured: 509s dark without this; ~85s with it.
</div>
<div class="sec">
  <b>&#9888; This file is a live credential to this browser, and it does not
  rotate.</b> Anyone who gets it can connect to the hub as this device.
  Delete it from Downloads once connected; never forward it, not even to
  troubleshoot. If it leaves your control, rotate the hub token
  (<code>amplifier-browser-bridge init --force</code>) and rebuild.
</div>
</details>
"""


_PAIR_SCRIPT = """
<script>
(function () {
  var hash = (window.location.hash || "").replace(/^#/, "");
  var params = new URLSearchParams(hash);
  var code = params.get("pair");
  var found = document.getElementById("pair-section-found");
  var none = document.getElementById("pair-section-none");
  if (code) {
    document.getElementById("pair-code-value").textContent = code;
    found.style.display = "block";
  } else {
    none.style.display = "block";
  }
})();
</script>
"""


def render_setup_page(*, platform: str, host: str, port: int, android_artifact_available: bool) -> str:
    """Render the full `GET /setup` HTML document.

    Args:
        platform: "desktop" or "android" (from `detect_platform`) -- controls
            which section is pre-expanded. An unrecognized value is treated
            as "desktop".
        host: The hub's bind host, for display only (e.g. in the manual
            `pair` command shown when no code is present in the URL).
        port: The hub's bind port, for display only.
        android_artifact_available: Whether `GET /setup/android-extension.bin`
            currently has a real file to serve (mirrors the hub's own
            availability check).

    Returns:
        A complete, self-contained HTML document (inline `<style>`/`<script>`,
        no external stylesheet/framework dependency -- this page must render
        correctly even with no other network access than the hub itself).
    """
    if platform not in _PLATFORMS:
        platform = "desktop"

    desktop_open = " open" if platform == "desktop" else ""
    android_open = " open" if platform == "android" else ""

    pair_cmd = f"AMPLIFIER_BROWSER_BRIDGE_HUB_URL=ws://{host}:{port}/agent amplifier-browser-bridge pair"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Amplifier Browser Bridge &mdash; install</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
<h1>Amplifier Browser Bridge</h1>
<p class="subtitle">Add this browser to the hub at {host}:{port}.</p>

<div class="honesty">
This is a <b>sideload</b>, not a store install. Downloading the extension here
saves you finding a path on the hub's own filesystem -- you still unzip it and
still pick the folder yourself in Edge. See "Why isn't this page locked down?"
near the bottom before you wonder about it.
</div>

<h2>1 &mdash; Get the extension</h2>
{_desktop_section(desktop_open)}
{_android_section(android_open, artifact_available=android_artifact_available)}

<h2>2 &mdash; Pair with this hub</h2>
<div id="pair-section-found" class="pair-box">
  <div class="pair-code" id="pair-code-value"></div>
  <div class="pair-hint">Open the extension's toolbar icon &rarr; Settings &rarr;
  paste this under "Pair with a hub" &rarr; click Pair. Single-use, short-lived --
  if it stops working, ask whoever sent you this link for a fresh one.</div>
</div>
<div id="pair-section-none" class="pair-box" style="border-color:#30363D;background:#161B22;">
  <div class="muted">No pairing code was included in this link.</div>
  <div class="pair-hint">On the hub machine, run:<br><code>{pair_cmd}</code><br>
  then paste the printed code into the extension's Settings &rarr;
  "Pair with a hub", or ask for a link that already has one embedded
  (<code>#pair=&lt;code&gt;</code>).</div>
</div>
{_PAIR_SCRIPT}

<details>
<summary>Why isn't this page (or the extension download) locked down with a token?</summary>
<div class="muted">
<p>A browser that has never been configured holds no hub token yet -- it cannot
authenticate to fetch the thing that would give it one. Gating this page behind
the same token everything else uses would make it unreachable by the one
browser that needs it.</p>
<p>What this page actually exposes to anyone who can reach this address (i.e.
anyone already on your tailnet, the same boundary every other hub route relies
on): this project's own source code -- already public on GitHub, not a secret --
and generic install instructions. It never exposes the long-lived hub token,
never lists connected devices, and never lets a visitor run a command. The
pairing code above, when present, is a short-lived (minutes), single-use ticket
whose only power is bootstrapping one new device's connection -- see
<code>pairing.py</code>'s documented ticket design. The Android download (when
available) does carry a live credential by design -- see the warning in that
section -- under the identical tailnet-only exposure the old standalone
Android server always had; nothing here widens that.</p>
</div>
</details>
</main>
</body>
</html>
"""
