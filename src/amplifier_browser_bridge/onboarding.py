"""Self-serve onboarding pages: platform detection + HTML rendering for the
hub's `GET /setup` and `GET /setup/android` routes (see hub.py).

## Why this exists

`cli.py`'s `init` used to print a *filesystem path* on the hub machine
(`select: /home/<user>/.local/share/amplifier-browser-bridge/extension`) as
the instruction for loading the extension into Edge. That instruction is
only correct when Edge happens to run on the same machine as the hub -- and
this project's whole reason for existing is the opposite case: the hub on a
Linux box, Edge on a MacBook or an Android phone. On those machines the
printed path does not exist, is not reachable, and cannot be typed into
"Load unpacked". This module (and the hub routes that serve it) is the fix:
an onboarding page reachable, by URL, from any browser on the tailnet -- the
browser being paired fetches its own install artifact from the hub, instead
of the operator trying to hand it a path on someone else's filesystem.

## One ladder, two screens (docs/designs/onboarding-ux.md)

This page and the extension's `options.html` are **one three-step ladder**,
not two unrelated pages:

    Step 1  Install the extension     -> /setup
    Step 2  Connect it                -> /setup shows the code, options page resolves it
    Step 3  You're ready              -> options page

Both pages render the same step component off the same copy-pasted CSS
token block (see `_TOKENS_CSS` below and `docs/designs/onboarding-ux.md`
section 2 -- the pages cannot share a stylesheet by URL, so the token block
is duplicated byte-for-byte; `tests/test_shared_design_tokens.py` guards
against the two drifting apart). A `done` step collapses to one line --
that is the whole trick that keeps the page from growing as the user
progresses through it (see the design doc's step-component table).

## The redemption-polling fix (the bug this closes)

Before this pass, `/setup` never learned that its own pairing code had been
redeemed -- the tab kept showing a live countdown for a code the extension
had already used to pair. `_PAIR_SCRIPT` below now polls `POST /pair/status`
(pairing.py's `PairingStore.status`, a read-only, side-effect-free check) on
the same ~1s cadence as its own countdown tick, and flips the code area from
"waiting" to "Connected" the moment redemption is observed -- see
docs/designs/onboarding-ux.md section 5.2's "One behavioral requirement".

## The Android page split (a deliberate deviation from the design doc)

The design doc's section 5.3 describes Android as a `<details>` disclosure
under step 1. This module instead serves it as its own page, `/setup/android`,
linked from step 1. Rationale: a `<details>` element still ships its entire
subtree in the HTML response regardless of whether it is open -- the
credential warning, five install steps, battery-optimization note, and a
nested "Known limits" disclosure together make the Android content the
single heaviest thing on the page even collapsed. Splitting it out keeps
`/setup` itself short (matching the whole point of the redesign: the page
gets *shorter*, never longer) while giving the Android instructions their
own page to be as thorough as they need to be, for the minority of visitors
who actually need them. `/setup` links to it with one plain, on-path line;
nothing about Android competes for space with steps 1/2/3 on the primary
desktop path.

## What this module is NOT responsible for

This is pure presentation logic -- no filesystem access beyond what's handed
to it, no direct network calls, no knowledge of the hub's device registry or
audit log. Splitting it out this way keeps it trivially unit-testable and
regeneratable in isolation from hub.py's routing/auth concerns.

## Security note

`detect_platform` returns ONLY one of a fixed, closed set of labels (never
the raw User-Agent string), and neither render function ever echoes any
request-supplied text into the HTML it returns -- there is nothing here for
a hostile `User-Agent` header to inject into.

The pairing code is deliberately **never threaded through `render_setup_page`
at all**. It travels only in the URL fragment (`#pair=<code>`), which
browsers never transmit to a server (fragments are stripped before the
request is sent) -- this page reads it back out of `location.hash` via
inline client-side JS after the page has already loaded, and only THEN sends
it to `/pair/status` (a deliberate, narrow exception -- see `_PAIR_SCRIPT`'s
own comment for why that is not a regression of this invariant). See hub.py's
module-level comment on these routes for the full authentication-circularity
reasoning, and `pairing.py`'s module docstring for the ticket design this
displays.
"""

from __future__ import annotations

__all__ = ["detect_platform", "render_android_setup_page", "render_setup_page"]

# Deliberately a two-way split (desktop vs. android), not a four-way OS split --
# this project's desktop instructions are IDENTICAL across Windows/macOS/Linux
# (see README.md's platform table); only Android needs a materially different
# flow (packed CRX, `.bin` rename trap, battery-optimization exemption). iOS is
# an explicit non-goal (README.md "Non-goals" -- Microsoft documents no
# extension API for it) so it is not a platform label here at all; an iOS UA
# falls through to "desktop" only in the sense that it gets the default label,
# not because desktop instructions apply -- the page does not claim iOS works.
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


# ---------------------------------------------------------------------------
# Shared design tokens -- docs/designs/onboarding-ux.md section 2, pasted here
# BYTE-FOR-BYTE identical to the block inside extension/options.html's own
# <style>. tests/test_shared_design_tokens.py extracts the text between the
# BEGIN/END markers on both sides and asserts equality -- a change here
# without the matching change there fails that test, not silently drifts.
# ---------------------------------------------------------------------------
_TOKENS_CSS = """/* TOKENS:BEGIN -- docs/designs/onboarding-ux.md section 2 (byte-identical on both pages) */
:root {
  /* type -- 1.25 ratio, 16px base. Only 4 of the 5 appear on a given page. */
  --t-meta:   0.75rem;  /* 12px  countdown, device id, footnotes */
  --t-sm:     0.875rem; /* 14px  secondary line under a step title */
  --t-body:   1rem;     /* 16px  default */
  --t-title:  1.25rem;  /* 20px  step title, pairing code */
  --t-page:   1.5rem;   /* 24px  page title (once per page) */

  --lh-tight: 1.25;  /* titles */
  --lh-body:  1.5;   /* everything else */
  --w-normal: 400;
  --w-semi:   600;

  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;

  /* space -- 4px base, only these seven values */
  --s-1: 4px;  --s-2: 8px;  --s-3: 12px; --s-4: 16px;
  --s-6: 24px; --s-8: 32px; --s-12: 48px;

  --radius: 8px;
  --radius-sm: 4px;
  --measure: 34rem;   /* max line length, ~66ch -- both pages */

  /* color -- light */
  --surface:        #FFFFFF;
  --surface-raised: #F6F8FA;
  --border:         #D8DEE4;
  --ink:            #16202B;
  --ink-dim:        #5A6B7C;
  --ink-faint:      #7C8B99;

  --accent:         #0B57D0;   /* links, step-now marker */
  --accent-ink:     #FFFFFF;   /* text on --accent */

  --ok-bg:      #E6F4EA;  --ok-ink:      #0E5C2F;  --ok-line:      #7BC49A;
  --pending-bg: #EEF2F7;  --pending-ink: #33455C;  --pending-line: #B6C2D1;
  --alert-bg:   #FDECEA;  --alert-ink:   #8C1D18;  --alert-line:   #E0A19C;
  --caution-bg: #FFF4E0;  --caution-ink: #7A4E00;  --caution-line: #E0BE7B;
}

@media (prefers-color-scheme: dark) {
  :root {
    --surface:        #0D1117;
    --surface-raised: #161B22;
    --border:         #30363D;
    --ink:            #E6EDF3;
    --ink-dim:        #9BA7B4;
    --ink-faint:      #7A8794;

    --accent:         #58A6FF;
    --accent-ink:     #06131F;

    --ok-bg:      #0F2417;  --ok-ink:      #7EE787;  --ok-line:      #235C34;
    --pending-bg: #161C24;  --pending-ink: #9FB3C8;  --pending-line: #2C3947;
    --alert-bg:   #241318;  --alert-ink:   #FF9E92;  --alert-line:   #B4402A;
    --caution-bg: #241A13;  --caution-ink: #FFB066;  --caution-line: #B4842A;
  }
}
/* TOKENS:END */"""

# ---------------------------------------------------------------------------
# Shared step-component + primitive CSS -- docs/designs/onboarding-ux.md
# section 3. Not required to be byte-identical (only the token block is), but
# kept identical in practice on both pages by convention -- this is the CSS
# implementation of the ONE step component described there.
# ---------------------------------------------------------------------------
_COMPONENTS_CSS = """
* { box-sizing: border-box; }
body {
  margin: 0; padding: var(--s-6) var(--s-4) var(--s-12);
  background: var(--surface); color: var(--ink);
  font: var(--t-body)/var(--lh-body) var(--font);
}
main { max-width: var(--measure); margin: 0 auto; }
h1.page-title { font-size: var(--t-page); font-weight: var(--w-semi); line-height: var(--lh-tight); margin: 0 0 var(--s-1); }
p.page-subtitle { font-size: var(--t-body); color: var(--ink-dim); margin: 0 0 var(--s-4); }

/* ---- step component ---- */
.step { display: flex; gap: var(--s-3); margin: 0 0 var(--s-8); align-items: flex-start; }
.step:last-child { margin-bottom: 0; }
.step-marker {
  flex: 0 0 auto; width: 24px; height: 24px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: var(--t-sm); font-weight: var(--w-semi);
  background: var(--surface-raised); color: var(--ink-faint);
}
.step[data-state="now"] .step-marker { background: var(--accent); color: var(--accent-ink); }
.step[data-state="done"] .step-marker { background: var(--ok-bg); color: var(--ok-ink); }
.step[data-state="next"] .step-marker { background: var(--surface-raised); color: var(--ink-faint); }
.step-main { flex: 1 1 auto; min-width: 0; }
.step-title { font-size: var(--t-title); font-weight: var(--w-semi); line-height: var(--lh-tight); color: var(--ink); }
.step[data-state="next"] .step-title { color: var(--ink-faint); }
.step[data-state="done"] .step-title { color: var(--ink-dim); }
.step-context { font-size: var(--t-sm); color: var(--ink-dim); margin-top: var(--s-1); }
.step-body { margin-top: var(--s-4); }
.step-body > * + * { margin-top: var(--s-3); }
.step-result { font-size: var(--t-sm); color: var(--ink-dim); margin-top: var(--s-1); }
.step[data-state="next"] .step-body { display: none; }

/* ---- buttons ---- */
.btn-primary, .btn-quiet {
  display: inline-flex; align-items: center; justify-content: center;
  min-height: 44px; padding: var(--s-3) var(--s-6); border-radius: var(--radius);
  font-size: var(--t-body); font-weight: var(--w-semi); text-decoration: none;
  cursor: pointer; border: 1px solid transparent;
}
.btn-primary { background: var(--accent); color: var(--accent-ink); }
.btn-primary[aria-disabled="true"] { background: var(--surface-raised); color: var(--ink-faint); pointer-events: none; }
.btn-quiet { background: transparent; color: var(--ink); border-color: var(--border); }

/* ---- note block (alert / caution only) ---- */
.note { border-radius: var(--radius); border-left: 3px solid; padding: var(--s-3) var(--s-4); font-size: var(--t-sm); }
.note-alert { background: var(--alert-bg); color: var(--alert-ink); border-left-color: var(--alert-line); }
.note-caution { background: var(--caution-bg); color: var(--caution-ink); border-left-color: var(--caution-line); }

/* ---- disclosure ---- */
details.disclosure { margin-top: var(--s-3); }
details.disclosure > summary { font-size: var(--t-sm); color: var(--ink-dim); cursor: pointer; }
details.disclosure > summary::marker { color: var(--ink-faint); }
details.disclosure .disclosure-body { margin-top: var(--s-3); font-size: var(--t-sm); color: var(--ink-dim); }
details.disclosure ol, details.disclosure ul { padding-left: var(--s-6); }
details.disclosure li { margin: var(--s-2) 0; }

/* ---- code ---- */
code, .code-mono {
  font-family: var(--font-mono); background: var(--surface-raised);
  border-radius: var(--radius-sm); padding: 0.1em 0.4em; word-break: break-all;
}
.code-mono.code-title { display: block; font-size: var(--t-title); padding: var(--s-3) var(--s-4); text-align: center; user-select: all; }
.code-mono.code-title.struck { text-decoration: line-through; opacity: 0.6; }

ol.plain { padding-left: var(--s-6); margin: 0; }
ol.plain li { margin: var(--s-2) 0; }
.meta { font-size: var(--t-meta); color: var(--ink-faint); }
.back-link { font-size: var(--t-sm); color: var(--ink-dim); }
"""


def _step(
    number: str,
    title: str,
    *,
    state: str,
    step_id: str,
    context: str | None = None,
    body_html: str = "",
) -> str:
    """Render one step of the shared step component (design doc section 3).

    `number` is the glyph shown when NOT done (a digit); a `done` step always
    shows a check mark regardless of what's passed, since a completed step
    never shows its ordinal again.
    """
    marker = "&#10003;" if state == "done" else number
    context_html = f'<div class="step-context">{context}</div>' if context else ""
    return f"""<section class="step" data-state="{state}" id="{step_id}">
  <span class="step-marker">{marker}</span>
  <div class="step-main">
    <div class="step-title">{title}</div>
    {context_html}
    <div class="step-body">
{body_html}
    </div>
  </div>
</section>"""


_WHY_SAFE_HTML = """<details class="disclosure">
<summary>Why is this safe?</summary>
<div class="disclosure-body">
A browser that has never been configured holds no hub token yet -- it cannot
authenticate to fetch the thing that would give it one. This page hands out
only this project's own source code (already public on GitHub, not a secret)
and generic install instructions -- never the long-lived hub token, never the
connected-device list. Reachable only by whatever is already on your tailnet,
same as every other hub route. Everything happens directly between your agent
and this browser -- your own login, your own network, no third-party relay.
The pairing code below, when present, is a short-lived, single-use ticket
whose only power is bootstrapping one new device -- see <code>pairing.py</code>.
</div>
</details>"""

_WHY_NOT_ONE_CLICK_HTML = """<details class="disclosure">
<summary>Why isn't this one click?</summary>
<div class="disclosure-body">
Chromium can't install an extension straight from a zip file -- there's no
one-click path. This just gets the file onto the machine running the browser
instead of the hub's, so the next steps are the only ones left.
</div>
</details>"""


def _step1_body(*, platform: str) -> str:
    android_cue = ""
    android_link_class = "back-link"
    if platform == "android":
        android_cue = '<div class="step-context">Android needs different steps.</div>'
        android_link_class = "btn-quiet"
    return f"""<a class="btn-primary" href="/setup/extension.zip" download>Download extension (.zip)</a>
<ol class="plain">
  <li>Unzip it.</li>
  <li>Open <code>edge://extensions</code>, turn on Developer mode.</li>
  <li>Load unpacked &rarr; pick the unzipped folder.</li>
</ol>
<div class="step-context">Settings opens by itself.</div>
{_WHY_NOT_ONE_CLICK_HTML}
{android_cue}
<p><a class="{android_link_class}" href="/setup/android">Android (experimental) &rarr;</a></p>"""


def _step2_body(*, host: str, port: int) -> str:
    """The code area -- rendered server-side in its default ("waiting" /
    "no code in link") shape; `_PAIR_SCRIPT` mutates it client-side into the
    other states (paired / expired) once it reads `location.hash` and starts
    polling `/pair/status`. See docs/designs/onboarding-ux.md section 5.2 for
    the four-state table this implements.
    """
    pair_cmd = f"AMPLIFIER_BROWSER_BRIDGE_HUB_URL=ws://{host}:{port}/agent amplifier-browser-bridge pair"
    return f"""<div id="pair-none">
  <div class="step-context">Run <code>{pair_cmd}</code> on your computer, then paste the code into the extension's Settings.</div>
</div>
<div id="pair-found" style="display:none;">
  <div class="code-mono code-title" id="pair-code-value"></div>
  <button type="button" class="btn-quiet" id="pair-copy-btn">Copy</button>
  <div class="meta" id="pair-countdown"></div>
  <div class="step-context" id="pair-waiting-line" style="color:var(--pending-ink);">Waiting for the extension&hellip;</div>
  <details class="disclosure" id="pair-manual-disclosure">
    <summary>It didn't connect on its own</summary>
    <div class="disclosure-body">Open the extension's Settings, then paste the code under "Enter a code by hand".</div>
  </details>
  <div class="step-result" id="pair-result-line" style="display:none;"></div>
  <div class="step-result" id="pair-nothing-to-copy-line" style="display:none;">Nothing to copy &mdash; it found the code itself.</div>
  <div class="note note-alert" id="pair-expired-note" style="display:none;">
    This code expired.<br>Run <code>amplifier-browser-bridge pair</code> for a new one.
  </div>
</div>"""


# Client-side state machine for step 2's code area. Deliberately NOT told the
# code at render time (see module docstring) -- reads `location.hash` after
# load, same as before. New in this pass: `/pair/status` polling (see
# pairing.py's `PairingStore.status`) on the SAME interval as the pre-existing
# countdown tick, closing the bug where the tab kept counting down a code
# that had already been redeemed elsewhere.
_PAIR_SCRIPT = """
<script>
// Best-effort clipboard copy -- see this module's history: this page is served
// over plain http (the hub deliberately never terminates TLS), so
// navigator.clipboard requires a secure context and is simply undefined here.
// execCommand("copy") is the PRIMARY mechanism, not a fallback.
function copyText(text) {
  if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(function () { execCommandCopy(text); });
    return;
  }
  execCommandCopy(text);
}
function execCommandCopy(text) {
  var ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand("copy"); } catch (e) { /* best-effort */ }
  document.body.removeChild(ta);
}
(function () {
  var hash = (window.location.hash || "").replace(/^#/, "");
  var params = new URLSearchParams(hash);
  var code = params.get("pair");
  var exp = params.get("exp");
  var noneEl = document.getElementById("pair-none");
  var foundEl = document.getElementById("pair-found");
  var countdownEl = document.getElementById("pair-countdown");
  var waitingLine = document.getElementById("pair-waiting-line");
  var manualDisclosure = document.getElementById("pair-manual-disclosure");
  var resultLine = document.getElementById("pair-result-line");
  var nothingToCopyLine = document.getElementById("pair-nothing-to-copy-line");
  var expiredNote = document.getElementById("pair-expired-note");
  var copyBtn = document.getElementById("pair-copy-btn");
  var step2 = document.getElementById("step-2");

  if (!code) {
    noneEl.style.display = "block";
    return;
  }
  // The `pair` fragment param is `TICKET@host:port` (see cli.py's `pair` command
  // and pairing_code.mjs's parsePairingCode, which the extension itself parses
  // the same way before redeeming) -- /pair/status expects the bare ticket
  // only, same as /pair/redeem's `ticket` field.
  var ticketOnly = code.split("@")[0];
  foundEl.style.display = "block";
  document.getElementById("pair-code-value").textContent = code;
  if (step2) step2.setAttribute("data-state", "now");
  copyText(code); // best-effort, silent -- the visible Copy button + plain-text code are the real fallback

  // Tracks whether the human clicked Copy themselves -- the one observable signal
  // (from this tab alone) that distinguishes "I copied this to paste it myself"
  // from "the extension found this code on its own", purely for which sentence
  // to show once paired (see docs/designs/onboarding-ux.md section 5.2's
  // "paired <- auto" vs "paired <- manual" rows). Never security-relevant --
  // cosmetic messaging only.
  var userCopied = false;
  if (copyBtn) {
    copyBtn.addEventListener("click", function () {
      userCopied = true;
      copyText(code);
      var original = copyBtn.textContent;
      copyBtn.textContent = "Copied";
      setTimeout(function () { copyBtn.textContent = original; }, 1500);
    });
  }

  var expiresAtMs = exp ? parseInt(exp, 10) * 1000 : null;

  function showExpired() {
    clearInterval(timer);
    countdownEl.textContent = "";
    waitingLine.style.display = "none";
    if (manualDisclosure) manualDisclosure.style.display = "none";
    expiredNote.style.display = "block";
    foundEl.style.opacity = "0.6";
  }

  function showPaired() {
    clearInterval(timer);
    // Collapse the WHOLE page to its final, shortest state -- not just step 2's
    // own marker (docs/designs/onboarding-ux.md: "the page gets shorter as the
    // user progresses"). Real bug this closes: step 1's full body (download
    // button, unzip steps, edge://extensions instructions, the manual `pair`
    // fallback) used to stay on the page forever, because the CSS rule that
    // hides a step's body only fires for data-state="next", never "done". The
    // Android link is the one thing kept -- see _setup_done_html()'s docstring.
    var ladder = document.getElementById("setup-ladder");
    var done = document.getElementById("setup-done");
    if (ladder) ladder.style.display = "none";
    if (done) done.style.display = "flex";
  }

  var everConfirmedPending = false;

  function checkStatus() {
    fetch("/pair/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticket: ticketOnly }),
    })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (!data || !data.ok) return; // transient error -- try again next tick
        if (data.status === "redeemed") {
          showPaired();
        } else if (data.status === "pending") {
          everConfirmedPending = true;
        } else if (data.status === "unknown" && everConfirmedPending) {
          // Was valid, now gone, and never observed as redeemed -- expired.
          showExpired();
        }
      })
      .catch(function () { /* network hiccup -- next tick tries again */ });
  }

  var timer = setInterval(function () {
    if (expiresAtMs) {
      var remainingMs = expiresAtMs - Date.now();
      if (remainingMs <= 0) {
        showExpired();
        return;
      }
      var s = Math.floor(remainingMs / 1000);
      var m = Math.floor(s / 60);
      countdownEl.textContent = "Expires in " + m + ":" + String(s % 60).padStart(2, "0");
    }
    checkStatus();
  }, 1000);
  checkStatus(); // first check immediately -- don't wait a full second to learn we're already paired
})();
</script>
"""


def _setup_done_html() -> str:
    """The final, collapsed state of `/setup` once pairing completes --
    `_PAIR_SCRIPT`'s `showPaired()` swaps `#setup-ladder` out for this.

    Real bug this closes (maintainer finding): before this, `showPaired()`
    only flipped step 2's OWN `data-state` to `done` -- step 1's full body
    (download button, unzip steps, `edge://extensions` instructions, the
    manual `pair` fallback) stayed on the page forever, because the CSS rule
    that hides a step's body only fires for `data-state="next"`, never
    `"done"`. The whole point of this design (docs/designs/onboarding-ux.md:
    "the page gets shorter as the user progresses") applies to `/setup`'s
    OWN final state too, not just to individual steps within it -- this is
    that state, brief by the same rule the rest of the ladder follows.

    The Android link is the one thing kept on purpose: a visitor who just
    paired a desktop browser may still want `/setup/android` for a second
    device, and nothing else on this page is still actionable once paired.
    """
    return """<section class="step" data-state="done" id="setup-done" style="display:none;">
  <span class="step-marker">&#10003;</span>
  <div class="step-main">
    <div class="step-title">You're connected</div>
    <div class="step-context">Finish in the extension's tab &mdash; you can close this one.</div>
    <div class="step-body">
      <p><a class="back-link" href="/setup/android">Add another device (Android) &rarr;</a></p>
    </div>
  </div>
</section>"""


def render_setup_page(*, platform: str, host: str, port: int, android_available: bool) -> str:
    """Render the full `GET /setup` HTML document -- steps 1 and 2 of the
    shared ladder (docs/designs/onboarding-ux.md section 5). Step 3 ("You're
    ready") lives on the options page, which this page hands off to once
    pairing completes inside the extension's own tab.

    Args:
        platform: "desktop" or "android" (from `detect_platform`) -- controls
            a small on-path cue pointing an Android visitor at `/setup/android`
            (see module docstring's "Android page split" section). An
            unrecognized value is treated as "desktop".
        host: The hub's bind host, for display only (e.g. in the manual
            `pair` command shown when no code is present in the URL).
        port: The hub's bind port, for display only.
        android_available: Whether `/setup/android`'s download button will
            currently produce a real artifact (a configured static artifact,
            or a hub that can pack one on demand) -- threaded through only so
            the Android on-path cue can say "(experimental)" either way
            without this page needing to know HOW the download works.

    Returns:
        A complete, self-contained HTML document (inline `<style>`/`<script>`,
        no external stylesheet/framework/CDN dependency -- this page must
        render correctly even with no other network access than the hub
        itself).
    """
    if platform not in _PLATFORMS:
        platform = "desktop"

    step1 = _step(
        "1", "Install the extension", state="now", step_id="step-1", body_html=_step1_body(platform=platform)
    )
    step2 = _step(
        "2", "Connect it", state="next", step_id="step-2", body_html=_step2_body(host=host, port=port)
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Amplifier Browser Bridge &mdash; install</title>
<style>{_TOKENS_CSS}{_COMPONENTS_CSS}</style>
</head>
<body>
<main>
<h1 class="page-title">Amplifier Browser Bridge</h1>
<div id="setup-ladder">
<p class="page-subtitle">Let your agent use this browser.</p>
{_WHY_SAFE_HTML}

{step1}
{step2}
</div>
{_setup_done_html()}
</main>
{_PAIR_SCRIPT}
</body>
</html>
"""


_ANDROID_KNOWN_LIMITS_HTML = """<details class="disclosure">
<summary>Known limits</summary>
<div class="disclosure-body">
<ul>
  <li><b>This will not work on Edge Android stable.</b> Stable supports only a
      small, Microsoft-curated set of extensions &mdash; about two dozen. This
      extension is not on that list, and there is no documented way to get onto
      it.</li>
  <li><b>You need Edge Canary or Beta</b>, and the hidden developer-options flow
      below. Microsoft does not document this flow publicly.</li>
  <li><b>This extension's own code has never been confirmed running on a real
      Android device.</b> Platform behavior was measured with a separate
      throwaway probe extension -- see docs/ANDROID.md, "What remains
      unproven".</li>
  <li><b>Measured battery-optimization impact:</b> 509 seconds dark without the
      exemption below applied, versus 43&ndash;133 seconds (self-recovering in
      under 2 seconds) with it applied.</li>
</ul>
</div>
</details>"""


def _android_download_section(*, download_available: bool, unavailable_reason: str | None) -> str:
    if download_available:
        return (
            '<a class="btn-primary" href="/setup/android-extension.bin" download>'
            "Download extension (.bin)</a>"
            '<div class="step-context">Downloads as <code>.bin</code> on purpose &mdash; '
            "Chromium intercepts <code>.crx</code> downloads and Edge Android "
            "silently discards the file. Rename it to <code>.crx</code> in "
            "<b>My Files</b> before installing.</div>"
        )
    reason = unavailable_reason or "no build available on this hub yet"
    return (
        '<a class="btn-primary" href="#" aria-disabled="true">'
        "No build available on this hub yet</a>"
        f'<div class="step-context">{reason}</div>'
    )


def render_android_setup_page(
    *, host: str, port: int, download_available: bool, unavailable_reason: str | None = None
) -> str:
    """Render the standalone `GET /setup/android` page.

    Deliberately its own page rather than a disclosure on `/setup` -- see
    this module's docstring, "The Android page split" section, for why.

    Args:
        host: The hub's bind host (display only).
        port: The hub's bind port (display only).
        download_available: Whether the download button currently produces a
            real artifact right now -- true for either a configured static
            `--android-artifact` or a hub that can pack one on demand (see
            android_pack.py).
        unavailable_reason: A short, honest, actionable explanation of why
            the download isn't available right now, when `download_available`
            is False (e.g. "no browser binary found to pack a CRX with").
            Never a vague "something went wrong" -- see android_pack.py.
    """
    download_html = _android_download_section(
        download_available=download_available, unavailable_reason=unavailable_reason
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Amplifier Browser Bridge &mdash; Android (experimental)</title>
<style>{_TOKENS_CSS}{_COMPONENTS_CSS}</style>
</head>
<body>
<main>
<p><a class="back-link" href="/setup">&larr; Back to setup</a></p>
<h1 class="page-title">Android (experimental)</h1>
<p class="page-subtitle">Edge on the desktop is the supported platform. This is a sideload with sharp edges.</p>

<div class="note note-caution">
  <b>This file is a live password to this browser, and it never changes.</b>
  Anyone who gets it can connect as this device. Delete it from Downloads once
  connected. Never forward it.
</div>

<div class="step-body">
{download_html}
<ol class="plain">
  <li>Once downloaded, open <b>My Files &rarr; Downloads</b> and rename the file
      so it ends in <code>.crx</code> instead of <code>.bin</code>.</li>
  <li>Edge Canary &rarr; <b>Settings</b> &rarr; <b>About Microsoft Edge</b>.</li>
  <li>Tap the <b>build number 5 times</b> &mdash; this unlocks Developer Options.</li>
  <li>Back &rarr; <b>Developer Options</b> &rarr; <b>Extension install by crx</b>
      &mdash; this requires a local file, not a URL.</li>
  <li>Pick the renamed <code>.crx</code> file.</li>
</ol>
<div class="note note-caution">
  Set Edge Canary's battery to Unrestricted, or it disconnects when the screen is off.
</div>
{_ANDROID_KNOWN_LIMITS_HTML}
</div>
</main>
</body>
</html>
"""
