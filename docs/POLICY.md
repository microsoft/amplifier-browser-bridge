# Policy Engine

This is the consent model for Amplifier Browser Bridge: what the agent can see, what it can do
without asking, and what it must ask about first. It implements design doc §6.2
(`docs/designs/browser-bridge.md`). The code lives in `src/amplifier_browser_bridge/policy.py`;
its integration into the hub's dispatch path lives in `src/amplifier_browser_bridge/hub.py`.

**Read this before configuring or extending the denylist or trusting gate detection.** Both
sections below end with an honest accounting of what they do *not* catch.

---

## 1. The model, in one paragraph

The user's stance is explicit and load-bearing: *"I generally want it to be able to access what
I access so that it can leverage/see what I've seen."* This system is **denylist-shaped, not
allowlist-shaped**. By default, every tab a device's browser can see, the agent can see and act
on -- no per-tab grants, no per-session approval, no prompting to read or navigate. Three narrow
mechanisms carve exceptions out of that default: a small denylist of sensitive host categories
(invisible, not just unreadable), a short list of confirmation gates on irreversible/world-visible
actions, and a hub-level kill switch. Everything else runs free, fully audited.

---

## 2. Denylist -- broad by default, narrow by exception

### What's denylisted by default

`policy.DEFAULT_DENYLIST` ships four categories, intentionally short and intentionally
incomplete (design doc §6.2: *"No public maintained list of such domains exists; we maintain ~5
categories."*):

| Category | Default domains | Why the whole host, not just a path |
|---|---|---|
| `financial` | chase.com, bankofamerica.com, wellsfargo.com, citibank.com, capitalone.com, americanexpress.com, paypal.com, venmo.com, fidelity.com, schwab.com, vanguard.com, coinbase.com | Banking/brokerage sites have no legitimate "just browsing for content" use case for an agent |
| `healthcare` | mychart.com, myuhc.com, kaiserpermanente.org, anthem.com, cigna.com, aetna.com | Patient-portal domains carry PHI on essentially every page |
| `auth` | accounts.google.com, login.microsoftonline.com, login.live.com, appleid.apple.com, login.yahoo.com, okta.com | Identity-provider hosts used almost exclusively for entering credentials or completing an IdP-hosted consent screen -- see §4 below for why this is narrower than "anything OAuth" |
| `password_managers` | 1password.com, lastpass.com, bitwarden.com, dashlane.com, keepersecurity.com | Vault UIs; no legitimate reason for an agent to be there at all |

### Matching

Host/domain-based with subdomain support: `sub.chase.com` and `chase.com` both match a
`chase.com` rule; `notchase.com` does **not** (suffix-with-dot-boundary matching, not substring
matching -- see `host_matches_domain` in policy.py for the exact logic and the substring-matching
bug it deliberately avoids). This matching logic itself is correct and covered by
`tests/test_policy.py`'s exact/subdomain/similar-but-different/suffix-of-unrelated-host cases --
if you're debugging an over-broad match, it is very unlikely to be here (see the case study below,
where the real cause turned out to be something else entirely).

### Case study: 49 ordinary tabs hidden as "auth" on a real 531-tab profile

Investigated against a real, heavily-used Edge profile (2026-07-26): `abb tabs` on a device with
531 visible tabs also produced 49 `policy_tab_hidden` audit events, **all** category `auth`, **all**
matching `login.microsoftonline.com`. 49/580 (~8%) hidden under one identity-provider host is not
implausible on its face for a Microsoft-365-heavy user -- but the actual URLs told a different
story once the audit log was extended to include the matched host (see "Audit detail" below): every
one was a path like `/{tenant}/oauth2/(v2.0/)authorize` or `/common/oauth2/v2.0/authorize`, and
their `redirect_uri` query parameters pointed at ordinary first-party content --
`*.sharepoint.com`, `coreidentity.microsoft.com`, `ms.portal.azure.com`, internal
`*.microsoft.com`/`eng.ms` engineering tools, and `localhost` dev servers.

**Root cause:** Microsoft-365-integrated web apps (SharePoint, OneDrive, internal engineering
portals, MSAL-based SPAs using the "redirect" interaction type) routinely perform a **top-level,
full-page** OAuth redirect through `login.microsoftonline.com/.../authorize` to silently refresh a
session -- normally invisible, completing in well under a second, and never rendering any
credential UI (a valid SSO session cookie means the IdP just 302s straight back to
`redirect_uri`). A **backgrounded tab that Edge discards** (unloads its renderer to reclaim memory
-- see the tabs `discarded` field, added for Bug 1 in this same investigation) can freeze mid-round-
trip, with the intermediate `authorize` URL left as its last-known `url` forever -- it never gets a
chance to finish redirecting back to the real app page. The tab is not, and was never, showing the
user a login prompt; it is an ordinary SharePoint/Azure/internal-tool tab that happens to be stuck
displaying IdP plumbing because nothing ever ran again to complete the hop while it sat discarded.

This is **not** the substring/subdomain-matching bug that was the leading hypothesis
going in (`host_matches_domain` is, and was, correct -- see above), and it is **not** an over-broad
denylist entry (`login.microsoftonline.com` is exactly the identity-provider host the design
intends to cover). The bug is a missing distinction: the `auth` category's actual rationale is "the
agent must not read a **live** credential-entry screen" (design doc §6.2: "structurally blind to
live credential/session material") -- but a **discarded** tab has no live renderer at all, so that
rationale does not apply to it. Hiding it anyway was over-hiding an ordinary content tab, directly
contradicting the stated design intent: "hide identity-provider credential-entry hosts, not
ordinary content hosts."

### The fix: a narrow, `auth`-only, discarded-tab exception

`PolicyEngine` now tracks each observed tab's `discarded` state (`_tab_discarded`, fed the same way
as `_tab_hosts` -- from `tabs` results, via `note_tab_discarded`). When a tab matches the `auth`
category **and** is currently known-discarded:

- **Response path** (`filter_tabs_result`): the tab is **shown**, not hidden -- but a
  `policy_tab_hidden`-adjacent audit event (`policy_tab_shown_despite_match`, full detail) is still
  recorded, so this exception itself stays fully auditable.
- **Request path** (`evaluate`): the command is **allowed** to reach the device instead of denied
  (`policy_allowed_despite_match` audited) -- but this does **not** bypass Bug 1's own fail-loud
  discarded-tab check at the extension layer (`background.js`'s `ensureAwake()`): the device still
  refuses to act on a discarded tab unless the caller explicitly passes `wake=true`, and any content
  actually read only happens after that explicit, audited, state-destroying wake. If the session
  genuinely expired and waking lands on a real interactive login form, the *next* observation
  (the same command's own result, or any later `tabs`/`snapshot`) records the fresh, non-discarded
  state and the ordinary `auth` denylist applies again from that point on.

**Scoped to `auth` only** -- `financial`/`healthcare`/`password_managers` are NOT given this
exception. Their rationale is not renderer-liveness ("don't show a live password box"); it's
not-revealing-which-services-the-user-uses-at-all ("don't let the agent learn the user banks with
Chase"), which holds regardless of whether the tab is currently rendered. Only identity-provider
hosts are used as ambient authentication *infrastructure* by countless unrelated first-party apps,
which is what creates this specific stuck-redirect failure mode at scale.

### Invisibility, both directions

A denied tab is **invisible**, not merely unreadable:

- **Response path**: a `tabs` command's result is filtered before it reaches the agent --
  `Hub._ingest_result` (hub.py) intercepts every device `result` envelope, and for `tabs`
  specifically, `PolicyEngine.filter_tabs_result` removes any entry whose host matches the
  denylist before the result is stored or returned. This is the ONE place this happens; it covers
  both an immediately-dispatched `tabs` call and one that was queued and later drained.
- **Request path**: any command explicitly naming a tab_id the hub has already observed to be on
  a denied host is rejected -- `PolicyEngine.evaluate`, called from `Hub.send_command` before a
  command can reach dispatch or a queue (see hub.py's module docstring, "single choke point").
  The rejection reason is deliberately generic (`"target is not accessible under current
  policy"`) and never names the matched category or domain -- naming it would leak exactly what
  the invisibility guarantee is supposed to hide. Full detail (category, matched domain) goes to
  the audit log only (`policy_denied`, `policy_tab_hidden` events).

### Capability binding: why an agent can't talk its way past this

Design doc §6.2: *"The agent names a target; the hub validates that target against the current
grant. A prompt-injected model can be made to want a different tab. It cannot address one it was
not granted."* This system's structural analogue: `PolicyEngine` decides using **its own recorded
observations** (`_tab_hosts`, built exclusively from device `result` envelopes the hub itself
processed), never from anything an agent's request asserts about a target. A prompt-injected
model can claim tab 7 is anything it likes in its own reasoning -- the hub's own memory of tab 7's
last-observed host is what gets checked, and that memory was built entirely from data the device
itself sent, not from agent input.

### Configuring the denylist

User-editable JSON (not YAML -- see "Why JSON, not YAML" below) at:

1. An explicit path passed to `Denylist.load(path)` (library callers only in this phase)
2. `ABB_POLICY_FILE` environment variable
3. `~/.config/amplifier-browser-bridge/policy.json` (conventional default, matching
   `auth.py`'s `tokens.json` precedent)
4. Built-in `DEFAULT_DENYLIST` if none of the above exist

```json
{
  "denylist": {
    "financial": ["chase.com", "mycompany-internal-banking.example.com"],
    "custom_category": ["internal-hr.example.com"]
  }
}
```

**A file's `denylist` section REPLACES the built-in categories entirely -- it does not merge.**
This is a deliberate simplicity choice: merge semantics for a short, human-curated list raise
real questions (does "extend category X" mean union or override? what if you want to *remove* a
default domain?) for marginal benefit, given the default list is short enough to copy-paste. To
extend rather than replace: copy the table above into your file and add to it.

**Why JSON, not YAML:** the design brief allowed either. This repo has no YAML dependency
anywhere (`auth.py`'s token file is JSON too), and adding one for a short key -> list-of-strings
structure would violate the project's "library vs custom code" judgment (IMPLEMENTATION_PHILOSOPHY.md)
-- JSON via the stdlib does the whole job.

### What the denylist does NOT catch (read before relying on it)

- **A tab the hub has never observed a URL for.** The denylist can only judge tabs whose host it
  has actually seen, via a prior `tabs`/`navigate`/`snapshot`/`read` result. A command naming a
  tab_id the hub has zero history for is allowed through (`PolicyEngine.evaluate` documents this
  explicitly) -- there is no a-priori way to know what an unobserved tab_id points at without
  extension-side tagging, which is out of scope for this phase (no extension code changed here).
  In practice this window is narrow: the agent's own normal use of `tabs`/`snapshot` populates the
  cache before it would ever have a reason to target a specific tab_id.
- **Domain-only granularity.** The denylist cannot distinguish paths on a host (e.g. it cannot
  denylist only `/login` on an otherwise-fine site) -- see §4 for why that's a feature, not a
  limitation, for the auth category specifically.
- **Anything not in the list.** This is maintained by hand and is explicitly incomplete. It is a
  starting point, not a security boundary suitable for regulated data without review.

---

## 3. Confirmation gates -- only for irreversible/world-visible actions

The canonical seven categories (confirmed by the user, design doc §6.2):

`purchase` · `send` · `delete` · `oauth_grant` · `file_upload` · `account_creation` ·
`permission_change`

Everything else -- read, navigate, click, type, scroll, open/close tabs -- runs free, fully
audited. A gate that fires returns `needs_confirmation` (see docs/PROTOCOL.md) instead of
dispatching; the command reaches the device only after an explicit `confirm` call redeems the
token.

### How detection works

Two signal channels, defined in `policy.GATE_RULES`:

- **URL patterns**, matched against `args["url"]` (present on every `navigate` command) or
  `args["page_url"]` (an optional hint, or the hub's own last-observed URL for that tab if
  neither is supplied).
- **Label patterns**, matched against `args["label"]` -- the visible text or `aria-label` of the
  clicked/typed element.

Most rules fire on *either* signal (`combine="any"`); `oauth_grant` and `permission_change`
require *both* (`combine="all"`), because their label vocabulary ("Allow", "Grant") is far too
common on its own -- cookie banners and notification-permission prompts say "Allow" too.
`file_upload` also accepts an explicit `args["input_type"] == "file"` hint as an unambiguous
alternative to label matching.

### The label signal -- now wired (Phase 4)

**As of Phase 4, `args["label"]`/`args["input_type"]` are resolved by the hub itself** when a
`click`/`type` command names a `target.ref` and doesn't supply them explicitly. The hub
remembers, per `(device_id, tab_id)`, the `ref -> {label, tag, input_type}` map from the most
recent `snapshot` result (`injected.js`'s `snapshot()` already computes exactly this per
element -- the `name`/`input_type` fields on each node) and from `wait_for` results (which
resolve exactly one ref -- `injected.js`'s `waitFor()` now returns the same fields for that one
element). See `PolicyEngine.note_snapshot`/`note_ref`/`_resolve_ref_hint`, fed from
`Hub._ingest_result`.

This resolution happens **before the command is ever dispatched to the device** -- it runs
inside `PolicyEngine.evaluate`, called from `Hub.send_command` before `_dispatch_live`/enqueue
(the same choke point that has always governed the denylist). A `click` whose ref matches a
gate-worthy label is gated pre-action; the click never reaches the page. This closes the gap
described in earlier phases, where click/type-based gates had a real matching *mechanism*
(`tests/test_policy.py` proved that directly) but zero live signal because nothing populated
the hint args.

- **`navigate`-based gates remain fully live** (`purchase` via checkout URLs, `account_creation`
  via signup URLs) -- `args["url"]` is mandatory for `navigate`, so this signal has always been
  available.
- **`click`/`type`-based gates (`purchase` via button text, `send`, `delete`, `oauth_grant`,
  `file_upload`, `permission_change`) now fire pre-action** whenever the clicked/typed `ref` was
  observed via a prior `snapshot` or `wait_for` on that tab -- which, in this system's intended
  usage, is the only way to obtain a `ref` in the first place. `tests/test_ref_hints.py` proves
  this end-to-end through a real hub-routed `snapshot`/`wait_for` result, not just via a
  synthetic hint supplied directly to `PolicyEngine.evaluate`.
- **What is still NOT gated pre-action:** a `click` targeting a `ref` the hub has never seen in
  any `snapshot`/`wait_for` result for that tab (not possible via the CLI/MCP surfaces, which
  only ever obtain refs that way, but possible if a caller invents one), and a `click` whose ref
  label was captured before the tab navigated to a different URL (see "staleness" below -- the
  hint is discarded rather than trusted, degrading to the same "no signal" case). Both fall back
  to the pre-Phase-4 behavior: allowed through, not gated, because the hub genuinely has no
  reliable signal.
- **Staleness is handled conservatively, not ignored.** `injected.js`'s `window.__abb` (and every
  `ref`) is destroyed on navigation. If the hub's own last-observed URL for a tab differs from
  the URL recorded when a ref's label was captured, the label is discarded -- the hub does not
  claim a click is safe, and does not claim a stale label is real. See policy.py's "Label hints
  are now wired" docstring section for the full reasoning, including the one accepted
  false-negative (a same-page SPA route change can unnecessarily invalidate a still-valid ref).

### Other honest limits

- **False positives are certain, and acceptable.** "Post" is gated as a `send`-category label
  because the user's canonical list explicitly includes "post," but "Post" is also an ordinary
  word on a blog's publish button. A gate firing is a prompt to confirm, not a claim that the
  action is actually dangerous.
- **False negatives are certain too.** We cannot tell a "Delete" button that removes one draft
  from a "Delete" button that deletes an account. Label patterns are narrow and word-boundaried to
  reduce false positives, which necessarily leaves gaps on the false-negative side.
- **`file_upload` has no dedicated wire-protocol verb.** There is no `upload` command in
  `protocol.COMMANDS` in this phase -- detection is entirely dependent on the optional
  `input_type` hint described above.
- **Gate rules are not user-file-configurable in this phase** (unlike the denylist). They are a
  Python-level constant (`policy.GATE_RULES`). This is a scope decision, not an oversight -- the
  brief for this phase required the denylist to be user-editable; it did not require the same for
  gate patterns.

---

## 4. Why "auth & OAuth consent screens" is split across two mechanisms

The design doc's denylist category list and the canonical gate list both mention OAuth. They are
talking about two different surfaces, and conflating them would either make the denylist far too
broad or make the gate unreachable:

- **The `auth` denylist category** covers identity-provider hosts used almost exclusively for
  entering credentials or completing an IdP-hosted consent screen (`accounts.google.com`,
  `login.microsoftonline.com`, ...). Denylisting the *entire* host is safe here specifically
  because nobody has a legitimate "just browsing for content" reason to be there -- the agent
  should be structurally blind to live credential/session material.
- **The `oauth_grant` gate** covers the much more common case of a third-party app's *own* domain
  hosting an authorize/connect flow -- e.g. `github.com/login/oauth/authorize`, one path on a host
  (`github.com`) that legitimately hosts ordinary work content everywhere else and must not be
  wholesale denylisted. The agent legitimately needs to see this page to do the task; only the
  final "Allow"/"Authorize" click needs a human's explicit confirmation.

If a URL matches a denylisted host, the denylist wins outright and the gate check never runs for
that target (`PolicyEngine.evaluate` checks denylist before gates) -- there is no scenario where
"ask for confirmation" is the right answer for a host the agent should never see at all.

---

## 5. Kill switch

`Hub.engage_kill_switch()` immediately:

1. Sets `PolicyEngine.kill_switch_active = True` -- every subsequent `evaluate()` call denies with
   `"kill switch engaged: all dispatch is halted"`, checked first, before the denylist or any
   gate.
2. Walks every device's queue and rejects (not silently drops) each not-yet-dispatched command --
   `poll()` on a rejected `command_id` returns a clear `{"ok": false, "error": "kill switch
   engaged: queued command rejected"}` rather than leaving the caller to wonder why it never
   drained.

`Hub.disengage_kill_switch()` restores normal dispatch.

**What it does not do:** recall a command already sent to a device and awaiting that device's
`result` -- once a frame is on the wire, the hub cannot un-send it. "Immediate" here means no
*new* command can be dispatched and every *queued* one is rejected; it does not mean an in-flight
command is interrupted mid-execution in the browser.

This phase implements the lib-level API only (`Hub.engage_kill_switch` /
`Hub.disengage_kill_switch`). Surfacing it via the CLI or an MCP tool is later work.

---

## 6. Audit

Every policy decision is recorded to the same JSONL audit log as everything else (see
`audit.py`'s module docstring for the full event-name table): `policy_denied`,
`policy_tab_hidden`, `policy_gated`, `policy_confirmed`, `policy_confirmation_expired`,
`kill_switch_engaged`, `kill_switch_rejected`, `kill_switch_disengaged`. This is the compensating
control for broad-by-default access (design doc §6.2) -- since most reads/navigations run
unprompted, the audit log is what lets the human review, after the fact, everything the agent did
and every policy decision the hub made on its behalf.
