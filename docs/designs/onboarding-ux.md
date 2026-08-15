# Onboarding UX — one language for `/setup` and the options page

Status: design spec, ready to implement. No code in this repo has been changed.

---

## 1. The idea

The two screens are **one three-step ladder**, not two pages.

```
Step 1  Install the extension     -> /setup
Step 2  Connect it                -> /setup shows the code, options page resolves it
Step 3  You're ready              -> options page (where the user lands and stays)
```

Both pages render the same ladder with the same step component. The options page opens
with steps 1 and 2 already checked. That single fact — arriving to see your own history
carried over — is what makes the two screens feel like one journey. Everything else in
this spec serves it.

**The pages cannot share a stylesheet by URL, so the shared language is a
copy-pasted token block.** §2 is that block, verbatim. It goes in both pages, byte
for byte. Nothing outside the block hardcodes a color, size, or space.

---

## 2. Tokens — paste this identically into both pages

```css
:root {
  /* type — 1.25 ratio, 16px base. Only 4 of the 5 appear on a given page. */
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

  /* space — 4px base, only these seven values */
  --s-1: 4px;  --s-2: 8px;  --s-3: 12px; --s-4: 16px;
  --s-6: 24px; --s-8: 32px; --s-12: 48px;

  --radius: 8px;
  --radius-sm: 4px;
  --measure: 34rem;   /* max line length, ~66ch — both pages */

  /* color — light */
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
```

**Why `prefers-color-scheme` and not "pick one".** `/setup` is dark today, the options
page is light. Neither is wrong; both are *unconditional*, which is what makes them clash
with each other and with the browser chrome they sit in. Following the OS makes the two
pages agree with each other and with `edge://extensions` at the same time, for free.

### Color roles — the distinction the maintainer asked for

| Role | Means | Never means |
|---|---|---|
| `pending` (slate) | Expected, not done yet. Fresh install, waiting for the extension, request in flight. | A problem |
| `ok` (green) | Confirmed working right now | |
| `alert` (red) | **Confirmed broken and actionable**: token rejected, hub unreachable, code expired | "not yet" |
| `caution` (amber) | Real risk the user must read: the Android credential warning | An error |

A user who has done nothing yet must never see red. `pending` is deliberately
**desaturated slate, not accent blue** — accent means "act here", pending means "wait".

---

## 3. The step component

One component, both pages, three states.

```
┌─ marker ─┐
│  (1)     │  Step title                      <-- --t-title, --w-semi, --lh-tight
└──────────┘  One line of context, if needed. <-- --t-sm, --ink-dim
              [ body: button / list / code ]  <-- indented to align with title
```

- **Marker**: 24px circle, `--s-3` gap to title, top-aligned with the title's cap height.
- **Body indent**: aligns with the title (marker width + gap = 36px). On viewports
  under 480px, indent collapses to 0 and the marker sits inline before the title.
- **Vertical rhythm**: `--s-8` between steps, `--s-4` between title and body,
  `--s-3` between body elements.

| State | Marker | Title | Body |
|---|---|---|---|
| `now` | number, `--accent` fill, `--accent-ink` text | `--ink` | visible |
| `done` | check glyph, `--ok-bg` fill, `--ok-ink` glyph | `--ink-dim` | **removed** — replaced by one `--t-sm` result line |
| `next` | number, `--surface-raised` fill, `--ink-faint` text | `--ink-faint` | hidden |

A `done` step is a **single line**. That is the whole trick: as the user progresses, the
page gets shorter, not longer.

### Other shared primitives

- **Button, primary**: `--accent` bg, `--accent-ink` text, `--w-semi`, `--radius`,
  padding `--s-3 --s-6`, **min-height 44px**. One per screen, maximum.
- **Button, quiet**: transparent bg, `--border` 1px, `--ink` text, same metrics.
- **Note block**: `--radius`, 3px left border, `--s-3 --s-4` padding, tinted with a
  state's `-bg`/`-ink`/`-line` trio. Used for `alert` and `caution` only.
- **Disclosure**: `<details>` with a `--t-sm`, `--ink-dim` summary phrased as the
  user's own question ("Why not one click?"). **All explanation lives here.**
- **Code**: `--font-mono`, `--surface-raised` bg, `--radius-sm`, `user-select: all`.

---

## 4. Copy rules

1. An instruction is **one line, ≤ 10 words**. If it needs a second sentence, the
   second sentence goes behind a disclosure.
2. **No paragraph is ever on the path.** Zero exceptions.
3. Never explain a mechanism the user did not experience.
4. Banned on-path: *tailnet, provenance, sideload, artifact, UUID, service worker,
   secure context*. They may appear inside disclosures.
5. "hub" appears on-path only when immediately followed by its address, or replaced
   by **"your agent"** — which is what it actually is, to this user.

---

## 5. Screen 1 — `/setup`

```
Amplifier Browser Bridge                              --t-page
Let your agent use this browser.                      --t-body, --ink-dim
                                        [ Why is this safe? ▸ ]   <-- disclosure

(1)  Install the extension                            step: now
     [ Download extension (.zip) ]                    primary button
     1. Unzip it.
     2. Open edge://extensions, turn on Developer mode.
     3. Load unpacked -> pick the unzipped folder.
     Settings opens by itself.                        --t-sm --ink-dim
                                        [ Why isn't this one click? ▸ ]
                                        [ Android (experimental) ▸ ]

(2)  Connect it                                       step: next -> now
     [ code area — see §5.2 ]
```

### 5.1 Disclosure contents (unchanged prose, relocated)

- **"Why is this safe?"** — the existing token/pairing-code explanation, verbatim.
  Replaces the always-visible "This is a sideload…" block.
- **"Why isn't this one click?"** — the existing Chromium-can't-install-a-zip
  explanation, verbatim.
- **"Android (experimental)"** — see §5.3.

### 5.2 The code area — four states

Step 2 renders exactly one of these. **Nothing about pasting appears until pasting
is actually needed.**

| State | Marker | Content |
|---|---|---|
| **waiting** (default) | `2`, accent | Code at `--t-title` mono, `user-select:all`. `[Copy]` quiet button. Below, `--t-meta --ink-faint`: `Expires in 6:02`. Below that, one `--t-sm --pending-ink` line: **"Waiting for the extension…"** Then a disclosure: **"It didn't connect on its own ▸"** → *"Open the extension's Settings, then paste the code under 'Enter a code by hand'."* |
| **paired** ← auto | check, ok | Code, countdown, copy button, and all instructions **removed**. One line: **"Connected. Finish in the extension's tab."** Plus `--t-sm --ink-dim`: **"Nothing to copy — it found the code itself."** |
| **paired** ← manual | check, ok | Same, without the second line. |
| **expired** | `!`, alert | Code struck through and dimmed. Alert note: **"This code expired."** Body: `Run amplifier-browser-bridge pair for a new one.` |
| **no code in link** | `2`, pending | `Run amplifier-browser-bridge pair on your computer, then paste the code into the extension's Settings.` (Pre-existing behavior; unchanged.) |

**One behavioral requirement:** the page must poll the hub for redemption of *this*
code and flip `waiting → paired` within ~2s. Without it the setup tab keeps showing a
live countdown for a code that has already been used — which is the current bug in a
new costume. Poll on the same interval as the existing countdown tick.

Copy that is **deleted**: *"Copied to your clipboard. Open the extension's Settings — it
should pair itself. If not, paste under 'Enter a code by hand'."* It narrates a
clipboard write the user did not ask for, then pre-explains a failure that usually
doesn't happen.

### 5.3 Android

Stays a disclosure under step 1, in this order:

1. **Caution note, always visible when open** — the credential warning, condensed:
   > **This file is a live password to this browser, and it never changes.**
   > Anyone who gets it can connect as this device. Delete it from Downloads once
   > connected. Never forward it.
2. Download button (or the disabled "no build on this hub yet" state).
3. The 5 install steps as a bare `<ol>`, one line each. Unchanged wording.
4. **Battery caution note** — condensed to: *"Set Edge Canary's battery to
   Unrestricted, or it disconnects when the screen is off."* Measurements move
   into the nested disclosure.
5. Nested disclosure **"Known limits ▸"** — everything else currently there:
   stable-Edge unsupported, undocumented flow, never-confirmed-on-real-hardware,
   the 509s/85s figures.

The credential warning cannot be nested. Everything else can.

---

## 6. Screen 2 — the options page

### 6.1 Structure (same ladder, arriving mid-journey)

```
Amplifier Browser Bridge                              --t-page

(v)  Extension installed                              step: done
(v)  Paired with 100.124.126.19                        step: done
     Paired automatically — nothing to copy.           --t-sm --ink-dim [conditional]

(3)  You're ready                                     step: now
     [ payload — see §6.2 ]
```

The two `done` rows are one line each. They exist solely so the user recognizes the
page they just left. That is worth two lines.

The `Paired automatically — nothing to copy.` line renders **only** when auto-discovery
won. If the user pasted the code, the line is omitted entirely.

**Delete** the current provenance paragraph (*"These values were obtained by pairing
with a hub — the hub URL and token were fetched automatically…"*). It explains a
mechanism to a user who watched it happen, in a state where it may not even be true.

### 6.2 Step 3 — the post-pairing payload

This is the moment the product says what it is for.

```
(3)  You're ready

     Your agent can use this browser now.             --t-body

     - Sees pages you're signed in to
     - Clicks, types, opens and closes tabs
     - Only while this browser is running

     You can close this tab.                          --w-semi

     [ Disconnect ]                                   quiet button
     Pin the toolbar icon to see when it's active.    --t-sm --ink-dim

     [ Connection details ▸ ]
```

Three bullets, ≤ 7 words each. They are the honest scope of what was just granted —
the user agreed to broad access, so they get to read what that means in three lines,
not in a policy document.

**"You can close this tab."** is the single most important sentence on this screen. It
is the answer to the question the current page leaves hanging.

**`Disconnect`** is the counterweight to broad access and must be one click from the
top level, never behind a disclosure. On click: revert step 2 and 3 to `pending`,
show *"Disconnected. Pair again to reconnect."*, and reveal the pairing controls.

**"Connection details ▸"** absorbs everything technical that currently sits on the
path: the `ws://` URL, the device id, the audit-log pointer.

### 6.3 The status line — states

The current status line becomes step 3's marker + first line. Same four-class
vocabulary as before, now expressed through the step component.

| Condition | Marker / class | Title | First line |
|---|---|---|---|
| Connected | check, `ok` | You're ready | Your agent can use this browser now. |
| Never paired | `3`, `pending` | Not connected yet | Open your hub's setup link, or enter a code below. |
| Connecting / in flight | `3`, `pending` | Connecting… | Give it a moment. |
| Still connecting past the watch ceiling (bug report, 2026-08 — see options.js's `renderWatchTimedOut`) | `!`, `alert` | Still not connected | Names the elapsed wait, the hub host, and what to check (address, hub running, reachability) — never repeats "Give it a moment." |
| Token rejected | `!`, `alert` | Hub refused this browser | `lastError.message`, then: *Pair again to get a fresh code.* |
| Hub unreachable | `!`, `alert` | Can't reach 100.124.126.19 | `lastError.message`, then: *Check the hub is running.* |
| Config keys changed | `!`, `alert` | Settings need re-pairing | Existing message, unchanged. |

The page keeps polling in the background while "Connecting / in flight" is showing —
a real reconnect after a hub restart can legitimately take minutes (background.js's own
backoff), so the page must not treat the first "still connecting" answer as settled. It
only gives up (rendering the "Still connecting past the watch ceiling" row above) once a
bounded ceiling elapses with no change, and stops polling entirely if the tab is hidden.

`pending` for both "never paired" and "in flight" — preserving the existing, correct
rule that a user who has done nothing wrong never sees red.

### 6.4 Pairing controls

While unpaired, step 3's body is the pairing UI:

- `Looking for a pairing code…` (`--pending-ink`), `[Check again]` quiet button.
- **"Enter a code by hand ▸"** — disclosure, unchanged fields.
- **"Manual setup ▸"** — disclosure, unchanged fields. Rename from "Manual
  configuration (advanced)"; drop the MagicDNS paragraph down one level into a nested
  **"Which address? ▸"**.

Once paired, all three collapse into the step-2 `done` row.

---

## 7. Done when

- [ ] Both pages carry the §2 block byte-identically; no color, size, or space
      literal exists outside it.
- [ ] Both pages render the §3 step component; a `done` step is one line.
- [ ] Screenshot both at 390px and 1280px, light and dark. They look like one product.
- [ ] Auto-pair success: the setup tab shows a check, not a countdown; the options
      page never mentions pasting.
- [ ] No paragraph is reachable without opening a disclosure.
- [ ] A non-technical reader can say what the product does after reading step 3 only.
- [ ] The Android credential warning is visible the moment that section opens.
- [ ] `Disconnect` is one click from the top of the options page.
- [ ] Contrast: all text ≥ 4.5:1, all borders/markers ≥ 3:1, both schemes.
- [ ] Every button ≥ 44px tall.
