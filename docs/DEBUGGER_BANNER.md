# The "started debugging this browser" banner

At some point after you install this extension, Edge will put a bar across the top of your
browser reading something like:

> **Amplifier Browser Bridge started debugging this browser.**   [ Cancel ]

**That is the system working, not breaking.** It is the browser telling you, unprompted and in
a place an extension cannot fake or hide, that the agent just escalated to a level of control
that deserves to be announced. This project deliberately does not try to suppress it -- a
mechanism exists to (see "What suppresses it" below) and using it would remove the one visible
signal you get for free.

This document exists because "an alarming bar appeared in my browser" is a bad first experience
when nobody told you it was coming, and because the banner's exact scope and lifetime are
**not documented by Google or Microsoft anywhere** -- so every claim below names its source.

---

## When it appears

Only when a command genuinely needs the Chrome DevTools Protocol (CDP), which in this project
means exactly two things:

| What you (or the agent) asked for | Why CDP is required |
|---|---|
| `trusted` input -- a click/type/key event with `isTrusted: true` | Injected synthetic events are `isTrusted: false`; some sites check |
| `capture_hidden` -- screenshot a tab that is not the foreground tab | `chrome.tabs.captureVisibleTab` can only ever capture the active tab |

Everything else -- `snapshot`, `read`, ordinary `click`/`type`, `navigate`, `tabs`, waiting,
downloads -- runs through injection and raises no banner at all. CDP is escalated per-tab, on
demand, never speculatively (`src/amplifier_browser_bridge/cdp.py`,
`hub.py`'s `_ensure_cdp_attached`).

## When it goes away

This project attaches CDP for as short a time as it can. The hub soft-detaches a CDP session
after **20 seconds** of not needing it (`cdp.py`'s `DEFAULT_SOFT_DETACH_IDLE_SECONDS`,
configurable per-hub via `Hub(cdp_idle_seconds=...)`), and Chromium removes the banner **5
seconds after the last detach**. So in practice the banner clears roughly **25 seconds after
the agent stops doing CDP work** -- it is not a session-long fixture.

It is also *not* transient in the other direction: it does not fade on its own while a session
is attached, and navigating does not dismiss it.

## What it covers -- browser-wide, not just the tab being driven

**This is the part people are most often surprised by.** The banner is not attached to the tab
the agent is working in. It appears on **every tab in every window** of that browser profile,
and there is exactly **one banner per extension**, no matter how many tabs that extension has
CDP sessions on.

So: an agent doing a `trusted` click on one background tab puts a bar on the tab you are
personally reading, in a different window. That is intended browser behavior, not a bug in this
project.

## What the Cancel button does

The banner's only button is labelled **Cancel**, and pressing it **detaches every CDP session
that extension holds**, across all tabs -- not just the one you happened to be looking at. It
is a working kill switch for CDP specifically, always available to you, that no code in this
project can intercept or override.

This project treats an unsolicited detach as a real event rather than an error to paper over:
the extension reports it to the hub (`cdp_detached`, `background.js`), the hub's `CdpRegistry`
updates, and any subsequent CDP-requiring command re-attaches deliberately (raising the banner
again) rather than silently degrading to an untrusted injected event the caller did not ask
for.

Opening DevTools on an attached tab, or the tab crashing/closing, produces the same detach.

## What suppresses it -- and why we do not

Two mechanisms exist, both in Chromium's own source:

1. **The `--silent-debugger-extension-api` command-line switch.** Launching Edge with it
   suppresses the banner for every extension, for that whole browser session. **Do not do this
   to run this project.** It removes the signal for every extension you have installed, not
   just this one, permanently for that launch.
2. **Enterprise policy installation.** An extension installed via `ExtensionInstallForcelist`
   (see [DESKTOP_DISTRIBUTION.md](DESKTOP_DISTRIBUTION.md)) is exempted from the banner
   entirely -- Chromium checks `Manifest::IsPolicyLocation(extension_->location())` and skips
   the warning. **This is a real, and rarely-stated, consequence of that distribution channel:
   a force-installed copy of this extension can use CDP on a user's browser with no visible
   indication at all.** If you deploy this by policy, the banner is not part of your users'
   experience and you should not count on it as a disclosure mechanism.

## Sources -- what is verified, and what is not

**Verified, from current Chromium source (`chromium/src`, `main`).** Every claim above about
scope, lifetime, and the Cancel button is read directly out of the browser's implementation:

| Claim | File | Evidence |
|---|---|---|
| Shown on every tab in every window until dismissed or closed | `chrome/browser/devtools/global_confirm_info_bar.h` | Class comment: *"GlobalConfirmInfoBar is shown for every tab in every browser until it is dismissed or the close method is called. It listens to all tabs in all browsers and adds/removes confirm infobar to each of them."* |
| One banner per extension, not per attached tab | `chrome/browser/extensions/api/debugger/extension_dev_tools_infobar_delegate.cc` | `Delegates` is a `std::map<ExtensionId, ...>`; a second attach by the same extension reuses the existing delegate and stops its pending close timer |
| Raised on attach; removed 5s after the last detach | `.../extension_dev_tools_infobar_delegate.h` | `kAutoCloseDelay = base::Seconds(5)`; *"`infobar_` is set after attaching an extension and is deleted 5 seconds after detaching the extension"* |
| Navigation does not dismiss it | `.../extension_dev_tools_infobar_delegate.cc` | `ShouldExpire()` returns `false` |
| Its only button is Cancel, and it detaches the extension's sessions | `.../extension_dev_tools_infobar_delegate.cc` | `GetButtons()` returns `BUTTON_OK` labelled `IDS_APP_CANCEL`; `Accept()` is mapped to `Cancel()` |
| Created by `attach()`, and suppressed for policy installs | `chrome/browser/extensions/api/debugger/debugger_api.cc` | `ExtensionDevToolsClientHost::Attach()` is the only creation site; `suppress_warning` is set by `--silent-debugger-extension-api` **or** `Manifest::IsPolicyLocation(extension_->location())` |

**Explicitly NOT verified.** Read these before treating the table above as a guarantee:

- **Google does not document this banner at all.** The public `chrome.debugger` API reference
  (developer.chrome.com, checked 2026-08-08) does not mention an infobar, a banner, or a
  warning anywhere on the page. Everything above comes from source, not from documentation.
- **Microsoft documents no Edge-specific behavior for it.** Edge is Chromium-derived, and this
  project has no reason to believe it diverges here, but "Chromium `main` does X" is not the
  same statement as "Edge 150 does X". Nothing above was measured against a real Edge install.
- **This project has never observed its own banner.** This extension's CDP path has not been
  exercised against a real Edge browser end-to-end; the CDP measurements in
  `designs/browser-bridge.md` section 2 were taken over `--remote-debugging-port`, which is a
  different mechanism that raises no extension infobar. The 20s-soft-detach-plus-5s arithmetic
  above is derived, not timed.
- **One in-repo observation contradicts the source reading.** A comment in
  `extension/background.js` (the capability-probe section) states that calling *any*
  `chrome.debugger` method, "including the nominally read-only `getTargets()`", raises the
  banner, and describes that as a real field observation. Chromium's
  `DebuggerGetTargetsFunction::Run()` does not go through `Attach()` and does not create an
  infobar, so the two disagree. **This is unresolved.** The extension's conservative behavior
  (presence-check `chrome.debugger` rather than probing it) is correct either way, so nothing
  depends on resolving it -- but do not cite either side as settled until someone reproduces it
  on a real Edge install.
- **Android is a different mechanism entirely** (Chromium uses a message, not an infobar, and
  `static_assert(!BUILDFLAG(IS_ANDROID))` guards the infobar path). Moot here: `chrome.debugger`
  is genuinely absent on Edge Android, so no CDP command reaches an Android device at all --
  they fail loud instead (`hub.py`'s `_ensure_cdp_attached`).
