# Specification: a reduced-permission build variant

**This document is a specification only.** It does not build the variant,
does not recommend building it, and does not decide the question for the
maintainer. It exists to answer, concretely and against the real code, a
claim made by a reviewer during this project's security review:

> "You do not fix that with a smarter approval flow. You fix it by removing
> the permission that broke the approval flow."

The claim was made in the context of `chrome.debugger`: a live CDP
experiment (`docs/designs/approval-channel-options.md`) showed that a
proposed in-extension human-approval UI could itself be driven by the very
agent it needed to exclude, because that agent already held CDP access. The
reviewer's broader claim, tested below, was that shrinking the permission
grant would make **roughly half** of this project's confirmation/mediation
machinery unnecessary. **That broader claim does not hold up against the
actual code** -- see "Testing the 'half the codebase' claim" below.

---

## 1. What the variant would look like

Three changes, matching the goal's own framing (DOM-only, narrow host
permissions, no `chrome.debugger`):

1. **Drop the `debugger` permission from `manifest.json`.** This project
   already has a working precedent for exactly this: `manifest.android.json`
   is byte-for-byte identical to `manifest.json` except for omitting
   `debugger` (Android genuinely lacks the capability). A desktop
   reduced-permission variant would be structurally the same kind of
   manifest fork, for a chosen-not-forced reason instead of a
   platform-forced one.
2. **DOM-only command execution.** Every command already has an
   injection-only (`chrome.scripting` + `injected.js`) implementation as
   the *default* path -- CDP is an escalation on top of it, never a
   replacement for it (design doc §7: "CDP: opt-in, not default"). A
   DOM-only variant does not need new code to execute commands; it needs
   the CDP escalation *removed*, not an injection-only path *added*, because
   that path already exists and is already what every command uses unless a
   caller explicitly asks for CDP.
3. **Narrower host permissions.** This is the one change with no existing
   precedent anywhere in this codebase, and the one with the largest
   product-shape consequences -- see §2.

## 2. What capability is lost

### Losing `chrome.debugger`

- **Trusted (`isTrusted: true`) input dispatch is gone.** `cdp.requires_cdp`
  documents exactly what this loses: pages that specifically check
  `event.isTrusted` to reject synthetic automation (some anti-automation and
  payment-form scripts) would no longer be operable via `args.trusted=true`
  -- every `click`/`type`/`key` would be limited to `injected.js`'s ordinary
  (untrusted) synthetic dispatch, which already works for most pages today
  and is already what Edge Android is limited to (`docs/DECISION_GUIDE.md`:
  "genuinely absent on Edge Android... fails loud naming the limitation").
  The failure mode is not new; it already exists for one platform.
- **Hidden/background-tab screenshot capture is gone.** `args.capture_hidden`
  requires CDP because `chrome.tabs.captureVisibleTab` only works on the
  active tab of a focused window. Without CDP, `screenshot` degrades to
  active-tab-only everywhere, the same limitation Android already has.
  `vision_read` (screenshot + vision-model text extraction) would still work
  for the *active* tab, but its most compelling case today --
  reading a tab the human isn't looking at without stealing focus -- would
  no longer be answerable at all on this variant.
- **The debugging banner disappears entirely**, along with the soft-detach
  machinery that exists purely to manage its visibility (`cdp.py`'s
  `DEFAULT_SOFT_DETACH_IDLE_SECONDS`, `Hub.soft_detach_idle_tabs`). This is a
  genuine user-experience improvement for anyone concerned about the
  persistent "being debugged" bar.
- **The approval-channel design space reopens.** `docs/designs/
  approval-channel-options.md`'s cancellation note names two conditions that
  would reopen the human-approval-channel decision: a channel whose security
  property is measured against every capability the agent holds, or **"a
  per-session way to deny `chrome.debugger` entirely."** A build variant
  that drops the permission *at the manifest level* satisfies that condition
  more strongly than a per-session toggle would -- worth flagging for the
  maintainer as a second-order consequence, not just a permission diet.

### Losing broad host permissions (`<all_urls>` -> something narrower)

This is where the variant's cost is largest, and it is worth separating
clearly from the `chrome.debugger` question above, because the two
permissions serve entirely different parts of the product:

- **`activeTab` alone (the sibling projects' pattern) is structurally
  incompatible with this project's stated design goals.** `activeTab` grants
  temporary access to a tab only in direct response to a user gesture
  (clicking the extension's own toolbar icon or a context-menu item) on
  *that* tab. This project's explicit goals (design doc §1): "acts on tabs
  that are not focused, without stealing focus" and "broad access by
  default, not an approval nightmare." `activeTab` satisfies neither --  it
  requires the tab to be the one currently in front of a human, and it
  requires a fresh user gesture per tab, which is exactly the per-tab
  approval flow the maintainer explicitly said they did not want (`docs/
  POLICY.md` §1 quotes the maintainer directly on this point).
- **`optional_host_permissions` with a runtime picker** (Chrome's
  documented middle ground) would let a user grant specific sites
  incrementally, but every additional site requires either a fresh
  `chrome.permissions.request()` prompt or a pre-authorized list -- which
  reduces to the same "approval nightmare" the maintainer rejected, just
  spread out over time instead of concentrated at install.
- **A curated allowlist of domains** could serve a narrower deployment (e.g.
  "only ever used against `github.com` and `contoso.sharepoint.com`"), but
  this is a per-deployment configuration choice, not something a single
  reduced-permission *build variant* can encode once -- the whole premise of
  this project is that the set of sites an agent might need to act on is
  the same unpredictable set the human already browses to, which cannot be
  known in advance.

**Bottom line: there is no host-permission narrowing that preserves this
project's stated purpose.** Every alternative to `<all_urls>` either breaks
the "broad access by default" goal outright (`activeTab`) or reintroduces
per-site approval friction the maintainer explicitly rejected (optional
permissions, curated allowlists). A reduced-permission variant that is
honest about this would keep `<all_urls>` and narrow only `chrome.debugger`
-- narrowing host permissions is not a smaller version of this product; it
is a different product.

## 3. Testing the "half the codebase" claim against the real code

The reviewer's claim was that shrinking the permission grant -- specifically
dropping `chrome.debugger` -- would make roughly half of the confirmation-
gate/mediation machinery unnecessary. To test this, every module that
references `cdp`/`debugger`/`trusted`/`capture_hidden` was inventoried
directly (line counts as of this writing, `wc -l` + `grep`):

| Module | Total lines | CDP-related | Removable if `chrome.debugger` is dropped? |
|---|---|---|---|
| `cdp.py` | 197 | 197 (100%) | **Yes, entirely** -- this module exists solely for CDP attach/detach bookkeeping |
| `tests/test_cdp.py` | 475 | 475 (100%) | **Yes, entirely** |
| `hub.py` | 1,346 | ~47 lines reference cdp/debugger/trusted/capture_hidden directly (`_ensure_cdp_attached`, the `CdpRegistry` wiring, the `_cdp` flag strip, CDP-specific audit events) | Partial -- roughly 150-250 lines of a 1,346-line file, by inspection of the surrounding blocks, not a line-by-line extraction |
| `effects.py` | 199 | 1 of 4 effects tiers (`"cdp"`, alongside `"webrequest"`, `"navigation"`, `"none"`) | Partial, small -- the `webrequest`/`navigation` tiers are independent and remain fully functional without CDP |
| `extension/background.js` | 1,946 | 32 references to `debugger`/CDP handling | Partial -- a meaningful but minority slice of a file that also handles the entire non-CDP command vocabulary, tab/window management, and the capability probe |
| `manifest.json` | 30 | 1 line (`"debugger"` in `permissions`) | Yes -- already proven as a safe removal by `manifest.android.json`'s precedent |

**Compare that to the modules that make up the actual confirmation-gate and
mediation machinery -- which do NOT reference CDP at all:**

| Module | Total lines | Touches CDP? |
|---|---|---|
| `policy.py` (denylist, confirmation gates, escalation categories, capability binding) | 1,441 | No |
| `classify.py` (action classification/scoring) | 594 | No |
| `scope.py` (session write-scope) | 339 | No |
| `audit.py` (audit log) | 70 | No |

**The confirmation-gate machinery is orthogonal to CDP, not built on top of
it.** The denylist, the confirmation-gate classifier, the escalation-category
forcing (`ESCALATION_CATEGORIES`/`permission_change`, `docs/POLICY.md` §3.1),
session write-scope, and the audit log all exist to mediate *ordinary*
`click`/`type`/`navigate` actions -- purchase buttons, delete buttons, OAuth
grants -- regardless of whether the input dispatching them was CDP-trusted
or plain injected. Dropping `chrome.debugger` does not touch any of this: a
`click` on a "Delete" button is scored, gated, and audited identically
whether or not the click event that eventually fires is `isTrusted`. The
mediation machinery's entire job is deciding *whether to allow an action*,
not *how the input event was produced* -- those are different layers, and
CDP only ever affected the latter.

**Rough total: even counting every partial estimate above at its high end
(cdp.py 197 + test_cdp.py 475 + hub.py 250 + effects.py 30 + background.js's
CDP share estimated generously at 300 of 1,946), the removable total is on
the order of 1,250 lines** -- against roughly 2,850 lines of policy/
classify/scope/audit machinery (1,441 + 594 + 339 + 70 + 197 + 199, counting
`cdp.py`/`effects.py` in the mediation-machinery baseline since the
reviewer's claim was about "mediation machinery" broadly) that would remain
completely untouched. **That is a real, non-trivial reduction -- likely in
the range of a third of the CDP-and-mediation-adjacent code, not
"half."** The reviewer's underlying instinct (permission scope drives
mediation complexity) is directionally right for the two CDP-specific use
cases; it does not generalize to "half the codebase," because the bulk of
what this project calls its mediation machinery was never built to gate CDP
usage in the first place -- it was built to gate what an agent does to a
page, which happens regardless of which input-dispatch mechanism produced
the click.

**Honest caveat on these numbers:** every "partial" figure above (`hub.py`,
`background.js`, `effects.py`) is an estimate from inspecting the matched
lines and their surrounding blocks, not a line-by-line extraction performed
by actually building the variant. Building it would produce exact numbers;
this specification's job was to test the claim's *order of magnitude*, and
at that level of precision the claim does not hold.

## 4. What this changes about store eligibility and admin approval

- **Dropping `chrome.debugger` alone, while keeping `<all_urls>`,
  meaningfully improves both.** `chrome.debugger` is the single most
  scrutinized permission on both the Chrome Web Store and Edge Add-ons
  (it is the permission most associated with malware in review guidance from
  both stores), and it is the one this project's own
  `docs/permission-justifications.md` could not fully defend (see that
  document's verdict on `chrome.debugger`). Removing it removes the
  permission most likely to trigger manual review escalation or an outright
  rejection, and removes the mandatory "being debugged" banner that concerns
  IT admins evaluating a force-install candidate.
- **`<all_urls>` alone, without `chrome.debugger`, is a far more common and
  better-understood grant.** Ad blockers, password managers, and many
  legitimate productivity extensions request `<all_urls>` with a
  correspondingly ordinary review outcome. An IT admin reviewing this
  variant for `ExtensionInstallForcelist` approval would very likely find it
  a materially easier "yes" than the full-permission build, even though the
  core "broad access to every page" risk this project's threat model is
  built around is completely unchanged.
- **This does not achieve store *eligibility* in the sense of "this would
  pass store review as a narrow, single-purpose extension."** `<all_urls>`
  plus a general-purpose remote-control command vocabulary is still an
  unusual, broad-capability extension by any store's review standards --
  the variant improves the odds of approval; it does not make this project
  look like `teams-transcript-md` or `loop-page-md`, and no host-permission
  change available to it (§2) can, without abandoning the product's purpose.

## 5. What this document does not do

This document specifies the variant and tests one specific claim against
the real code. It does not:

- Recommend building the variant.
- Decide whether the lost capabilities (trusted input, hidden-tab capture,
  the reopened approval-channel design space) are worth the reduction.
- Estimate the engineering effort of actually forking the manifest, wiring a
  build flag, and maintaining two variants going forward.

Those are the maintainer's calls to make, informed by the numbers above.
