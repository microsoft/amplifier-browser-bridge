# Agent decision guide: which mechanism, when

This system exposes roughly a dozen distinct mechanisms for reading and acting on a
page, plus modifiers that change how each one behaves. Per design doc section 13
("Mechanism, not policy"), **the bridge never picks one for you** -- it exposes every
option with honest tradeoffs and lets the calling agent choose. This guide is the map
that makes those tradeoffs legible in one place, grounded in what was actually
measured against real pages, not guessed.

Read this alongside `docs/PROTOCOL.md`'s "Command vocabulary", "Frames", and
"Content-extraction mechanisms" sections, which define each mechanism precisely --
this guide is the decision layer on top, not a replacement.

---

## "I want the text of this page" -- start here

Work through these in order; each one names the failure mode that sends you to the
next.

1. **`read` (top frame only, the default).** Fast, cheap, no model call. Fails you
   when the content you need lives in an embedded frame (a SharePoint/M365 document
   viewer is the canonical case -- the top frame carries only nav chrome).
2. **`read` with `args.all_frames=true`.** Gathers every frame's text uniformly (see
   PROTOCOL.md's "Frames"). Slower -- every frame must be instrumented before any
   result returns -- so raise `timeout_s` alongside it. Fails you when the frame that
   matters renders its content to `<canvas>` instead of DOM text (see #4 below) --
   you'll get a real result back, just an unhelpfully short one (measured: a Word
   Online viewer frame's entire DOM text was `"PAGE 1 OF 5 | CONFIDENTIAL..."`, 108
   characters, no document body at all).
3. **`snapshot`** if you need element `ref`s to click/type next, not just text --
   otherwise `read` is simpler for text-only extraction. Same frame semantics as
   above (`all_frames`/`frame_id`). **Refs are only valid from the most recent
   snapshot of a given frame** -- a ref from a superseded snapshot fails loud with a
   specific "stale ref" error rather than silently resolving against the wrong
   element. Take a fresh snapshot after any navigation or after taking a second
   snapshot of the same tab.
4. **Content is canvas-rendered (no DOM text at all).** Word/PowerPoint/Excel Online
   are the measured case: the entire document body is painted to `<canvas>`, so no
   `read`/`snapshot` combination -- however clever -- can ever surface it. This is a
   structural limit, not a tuning problem. Two real options, each a distinct
   mechanism, not an automatic fallback:
   - **`fetch_bytes`** (device-only target) -- fetches the URL from the extension's
     own context, credentials included (rides the user's real session). Gets you the
     underlying file's raw bytes. Fails when the file is IRM/RMS-protected (see #5).
   - **`vision_read`** -- captures a screenshot and extracts text via a vision-model
     call. Costs a real model call, produces no element refs, but works on anything
     you can see, including a rendered document viewer.
5. **The fetched bytes are a real file, but not a document you can parse.**
   IRM/RMS-protected Office files are the measured case: `fetch_bytes`/`grab_image`
   return real bytes with the correct content-type, but the payload is an
   **encrypted OLE2 container** -- there is no document inside to parse with any
   library, because the protection is applied at the file-format layer, not just
   access control. **`vision_read` is the only path** that works here: it captures
   what the user's *browser* renders (already decrypted for the authenticated user
   viewing it on-screen), not the file's raw bytes.
6. **`fetch_bytes` fails with an HTTP/hotlink error** (some CDNs check the request's
   Referer/Origin). Try **`grab_image`** instead -- it fetches from the *page's own*
   main-world script context, so the request carries the page's real Referer/cookie
   context. Requires a `tab_id` (the page doing the fetching); `fetch_bytes` does
   not. Each command's error text names the other as the alternative -- neither
   auto-retries as the other.

---

## Modifiers that change behavior on ANY of the above

### `wake` -- discarded/sleeping tabs

At real-world scale (hundreds of open tabs), Edge unloads most background tabs to
reclaim memory. **Measured naming detail: Edge reports this as `status: "unloaded"`
on the tab, not a `discarded: true` boolean** -- check `tabs`'s `status`/`discarded`
fields before assuming a tab is live. A command against a discarded tab fails loud
with a specific, actionable error rather than Edge's own misleading "extension
manifest must request permission" message.

**Waking a tab means reloading it, which destroys in-page state** -- unsaved form
data, scroll position, ephemeral JS state, all gone. This is why `wake` is opt-in
(`args.wake=true`), never automatic -- co-working etiquette means the agent doesn't
silently blow away state the human might come back to. Only use it when you've
concluded the tab's *current* state doesn't matter, or when you're about to
`navigate` there anyway.

### `activate` vs. `vision_read` -- heavy SPAs backgrounded

**Measured finding**: a heavy enterprise SPA timed out at 170s while backgrounded,
and completed in ~2s against the same tab immediately after activating it. DOM
injection/traversal on a large, fully-hydrated page is viable when the tab is
actually compositing, and can hang indefinitely when it isn't.

Two real options when a `read`/`snapshot`/`click`/`type`/`key` times out on a
backgrounded tab, named in the timeout error itself -- pick based on whether stealing
focus is acceptable:

- **`args.activate=true`** -- foregrounds the tab first. Fast, exact DOM, real element
  refs for follow-up clicks. **Steals the human's focus** -- the one thing co-working
  etiquette otherwise forbids doing silently. Result reports `"activated": true` only
  when it actually changed anything.
- **`vision_read`** -- screenshot + vision-model text extraction. No focus steal. No
  element refs (you get text, not something to click). Costs a real model call. This
  is the mechanism to reach for when the human is actively working in a tab you don't
  want to yank out from under them.

Raising `timeout_s` alone is a third option if the page is just slow, not
structurally broken while backgrounded -- try it first if you don't know which
you're dealing with.

### `trusted` -- CDP-backed input

`click`/`type`/`key` dispatch untrusted synthetic events by default
(`isTrusted: false`) -- most pages accept these fine. If a page specifically checks
`event.isTrusted` (some anti-automation / payment-form scripts do), pass
`args.trusted=true` to escalate to CDP-backed `Input.dispatch*` calls, which produce
real `isTrusted: true` events. Requires the `debugger` capability on the device
(desktop only -- **genuinely absent on Edge Android**, fails loud naming the
limitation rather than silently falling back to untrusted input).

**Measured constraint: CDP is unavailable on M365-origin tabs inside an enterprise
tenant** -- attaching `chrome.debugger` to a `*.officeapps.live.com`/similar tab was
observed to fail with `"The extensions gallery cannot be scripted"` (a policy-level
block, not a capability gap this project can route around). If you need trusted
input or hidden-tab capture on an M365-hosted page and hit this, there is no
alternative CDP path -- fall back to untrusted injected input, or `vision_read` for
extraction.

### `capture_hidden` -- screenshotting a non-active tab

`screenshot` without this flag only works if the target tab is already the active
tab of a focused window (`chrome.tabs.captureVisibleTab`'s own restriction).
`args.capture_hidden=true` escalates to CDP (`Page.captureScreenshot`), which can
capture any tab regardless of focus/foreground state -- same `debugger` capability
requirement and M365-tenant caveat as `trusted` above. `vision_read` defaults
`capture_hidden` to `true` specifically because it exists for the "don't want to
activate this tab" case.

### `timeout_s` -- raise before you assume something is broken

Every command accepts this. The hub's own default (120s) is already generous for
real-world heavy SPAs; a timeout error names the specific alternative mechanisms
above rather than just saying "timeout" -- read the error text, it's written to be
actionable, not generic.

---

## "The action I'm about to take might be consequential"

Every `click`/`type`/`key`/`navigate` result carries a `classification` block (deterministic
scoring, `docs/designs/confirmation-gate.md`) and an `effects` block (what the browser actually
observed happen) -- read them before assuming a plain `{ok: true}` means "nothing worth noting
happened":

- **`classification.status == "elevated"`** means the command was gated -- you already know this
  from `status: "needs_confirmation"` on the response itself.
- **`classification.status == "unknown"`** means the bridge could not classify the action at all
  (an un-snapshotted ref, a stale hint, a canvas-rendered page) -- the command still ran (under
  the default `on_unknown: "allow"`), but you got no signal either way. If you're driving toward
  something you suspect might be consequential and get `unknown`, take a fresh `snapshot` (or call
  `describe` on the specific ref) before proceeding, rather than treating silence as "safe."
- **`effects.state_changing == true`** on an action whose `classification.status` was `"clear"`
  is the single most actionable combination in this system: the classifier saw nothing
  concerning in the label/URL/page context, but the browser observed a real non-GET request,
  form submission, download, or new tab. This is exactly the failure mode `docs/designs/
  confirmation-gate.md` was written to close (a bland "Next" button that turns out to submit a
  privilege-elevation step) -- read the `effects.requests`/`.navigations` list before assuming
  a "clear" classification means the action was inert.
- **`classification.advisory` is always `true`.** Every page-asserted signal (label, form,
  heading text) is forgeable by the page itself -- do not build automation that trusts a
  `"clear"` classification as a security guarantee. `effects` (browser-asserted) is the one
  page-immune signal; it is still post-hoc, not preventive.

## Quick reference table

| You want... | Try first | Falls back to | Because |
|---|---|---|---|
| Text of the top frame | `read` | `read(all_frames=true)` | Content may live in an embedded frame |
| Text across a whole page incl. iframes | `read(all_frames=true)` | `fetch_bytes`/`vision_read` | Content may be canvas-rendered, not DOM text |
| Refs to click/type | `snapshot` | -- | Refs expire on next snapshot/navigation |
| A linked file's bytes | `fetch_bytes` | `grab_image` | Hotlink/Referer protection on the source |
| Text from a canvas-rendered doc | `fetch_bytes` (raw file) | `vision_read` | Encrypted/IRM-protected files have no parseable text |
| A tab that's discarded/unloaded | (fails loud) | `wake=true` | Reload destroys in-page state -- opt-in only |
| A slow/backgrounded heavy SPA | raise `timeout_s` | `activate=true` or `vision_read` | Foreground is fast; backgrounded can hang |
| A page that rejects synthetic input | plain `click`/`type`/`key` | `trusted=true` (desktop only) | CDP unavailable on Android and M365-tenant tabs |
| A non-active tab's pixels | (fails loud) | `capture_hidden=true` (desktop only) | Same CDP/M365 caveat as `trusted` |

This guide describes tradeoffs; it does not prescribe. See design doc section 13 for
why: two reasonable agents in two different situations can legitimately want
different mechanisms for the same page, and this layer's job is to make the real
costs visible, not to guess on your behalf.
