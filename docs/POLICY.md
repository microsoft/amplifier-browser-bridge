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
bug it deliberately avoids).

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

### The signal that isn't wired up yet -- read this

**`args["label"]` is populated by nothing in this codebase today.** `injected.js`'s `snapshot()`
already computes exactly this string per element (the `name` field on each snapshot node) -- a
future caller (a CLI flag, an MCP tool wrapper, or the extension itself) can pass it straight
through as `args["label"]` on a `click`/`type` command. Until something does:

- **`navigate`-based gates are fully live today** (`purchase` via checkout URLs, `account_creation`
  via signup URLs) -- `args["url"]` is mandatory for `navigate` in the existing wire protocol, so
  this signal has always been available.
- **`click`/`type`-based gates (`purchase` via button text, `send`, `delete`, `oauth_grant`,
  `file_upload`, `permission_change`) have zero real signal in the currently wired system and will
  not fire**, because nothing populates `args["label"]`/`args["page_url"]`/`args["input_type"]`
  yet. This is not a bug in this phase -- it is the honest boundary of what a hub with no DOM
  access can know about a `click`, documented rather than silently assumed to work.
  `tests/test_policy.py` proves the matching *mechanism* is correct by supplying these hints
  directly; wiring a real caller to populate them is future work (outside this phase's scope,
  which does not touch the extension, CLI, or MCP server).

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
