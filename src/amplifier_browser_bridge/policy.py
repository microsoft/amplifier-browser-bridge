"""The policy engine: denylist, confirmation gates, capability binding, kill switch.

This is the structural anti-injection measure described in design doc §6.2:

    "The agent names a target; the hub validates that target against the current
    grant. A prompt-injected model can be made to *want* a different tab. It
    cannot *address* one it was not granted."

This system is denylist-shaped, not allowlist-shaped (design doc §6.2, user
requirement: *"I generally want it to be able to access what I access."*), so the
structural guarantee is phrased in denylist terms: the hub decides whether a target
is permitted using state IT observed itself (`_tab_hosts`, built from device
`result` envelopes), never from anything the agent asserts about the target. An
agent (or a prompt-injected model riding inside one) can claim tab 7 is anything it
likes -- the hub's own observation of tab 7's last-known host is what gets checked.

Three independent mechanisms live here, all reached through exactly one entry
point (`PolicyEngine.evaluate`), which `Hub.send_command` (hub.py) calls before a
command is ever allowed to reach `_dispatch_live` or a device queue -- see hub.py's
module docstring on "single choke point" for the dispatch-side half of this
guarantee:

1. **Denylist** (broad-read-by-default, narrow-by-exception): a small,
   hand-maintained, user-editable set of sensitive host categories. Matching is
   host/domain-based with subdomain support -- see `host_matches_domain`.
2. **Confirmation gates**: a short, fixed list of irreversible/world-visible
   action categories (purchase, send, delete, oauth_grant, file_upload,
   account_creation, permission_change) that must be explicitly confirmed via a
   single-use, expiring token before they execute. See `GATE_RULES` and the
   "what this cannot catch" section below -- read it before trusting this gate.
3. **Kill switch**: a hub-level stop-all. See `Hub.engage_kill_switch` in hub.py
   (the queue-draining side lives there, since it needs `DeviceRegistry`; the
   flag this checks lives here).

---

## Honest limits of gate detection (read this before relying on it)

Gate detection is **best-effort pattern matching**, not semantic understanding of
what a page does. Two signal channels feed it, and both have real gaps:

- **URL patterns** (`args["url"]` for `navigate`, or a cached/last-observed tab URL
  for other commands): reliable *when a URL is available*. A `navigate` command
  always carries one. A `click`/`type` command does not carry the current page URL
  in this phase's wire protocol -- the policy engine falls back to whatever URL it
  last observed for that tab (via a prior `tabs`/`navigate`/`snapshot`/`read`
  result). If nothing has been observed yet, there is no URL signal at all.
- **Label patterns** (`args["label"]`, matched against a button/link's visible
  text or `aria-label`): reliable *when a label is available*. As of Phase 4,
  the hub resolves this itself when the caller doesn't supply it explicitly --
  see "Label hints are now wired" below -- but the resolution has its own
  honest gap: a `ref` the hub has never seen in a `snapshot`/`wait_for` result
  carries no label, and a `ref` whose tab has since navigated to a different
  URL is treated as **stale** and discarded rather than trusted. Both cases
  fall back to "no label signal" -- the same pre-Phase-4 behavior -- rather
  than guessing.

### Label hints are now wired (Phase 4)

The hub remembers, per `(device_id, tab_id)`, the `ref -> {label, tag,
input_type}` map from the most recent `snapshot` result (and incrementally
from `wait_for` results, which resolve exactly one ref). See
`PolicyEngine.note_snapshot` / `note_ref` / `_resolve_ref_hint`, fed from
`Hub._ingest_result`. When a `click`/`type` command names a `target.ref` and
doesn't supply `args["label"]`/`args["input_type"]` itself, `evaluate()` looks
them up from this cache before running gate rules -- **before the command is
ever dispatched to the device** (it runs inside `PolicyEngine.evaluate`, which
`Hub.send_command` calls before `_dispatch_live`/enqueue; see hub.py's module
docstring on the choke point). This is what makes click/type-based gates fire
pre-action rather than after the click has already landed on the page.

**Why this approach, not extension-side resolve-then-report:** the
alternative design (the extension resolves a click's label and reports it to
the hub in a first round trip, then the hub decides whether to actually
dispatch in a second) also gates pre-action, but costs an extra WebSocket
round trip per click and requires the extension to understand two-phase
command execution. Remembering `snapshot`/`wait_for` results the hub already
receives is free -- no protocol round trip, no extension complexity -- at the
cost of the staleness handling described below. Given every click is, in
practice, preceded by a `snapshot` or `wait_for` in this system's intended
usage (there is no other way to obtain a `ref`), the hub already has the data
it needs by the time a `click` arrives.

**Staleness, handled conservatively:** `injected.js`'s `window.__abb` (and
therefore every `ref`) is destroyed on navigation (see injected.js's module
docstring). If the hub's own last-observed URL for a tab (`_tab_hosts`, fed by
`navigate`/`snapshot`/`read`/`tabs` results -- never by anything a caller
asserts) differs from the URL recorded when a ref's label was captured, the
hub treats the label as unknown rather than risk gating (or failing to gate)
based on a DOM that may no longer exist. This is deliberately conservative in
both directions -- it does not claim a click is safe, and it does not claim a
sanitized label is real; it degrades to the pre-Phase-4 "no signal" case. One
known false-negative this produces: a same-page client-side (SPA) route change
that updates the tab's URL without a full navigation may invalidate a still-
valid ref's label unnecessarily. We accept this rather than risk the reverse
(trusting a label for a DOM that changed underneath it).

Tests in `tests/test_ref_hints.py` exercise both the wiring (a `click`
targeting a ref observed via a real hub-routed `snapshot` result fires a gate
with no explicit label in the `click`'s own args) and the staleness guard.
- We cannot reliably tell a "Post" button that publishes a public tweet from a
  "Post" button that saves a private draft. Label patterns are deliberately
  narrow (word-boundaried, category-specific phrases) to reduce false positives,
  but false positives and false negatives both remain possible. A gate that fires
  is a *prompt to confirm*, not a guarantee that the action is actually dangerous
  -- and a gate that does *not* fire is not a guarantee that the action is safe.
- `file_upload` has no dedicated wire-protocol command in this phase (see
  `protocol.COMMANDS` -- there is no `upload` verb). Detection is entirely
  dependent on an optional `args["input_type"] == "file"` hint from the caller.
  Nothing populates it yet, for the same reason as `label` above.
- `oauth_grant` intentionally does *not* rely on denylisting whole identity
  provider domains (see `DEFAULT_DENYLIST["auth"]` for what *is* denylisted, and
  why the split matters, below). It relies on a URL-pattern match against known
  OAuth authorize-endpoint path shapes combined with a label match -- both
  channels have the gaps described above.

## Why "auth & OAuth consent screens" splits across two different mechanisms

The design doc's denylist category list and the canonical gate list both mention
OAuth, and they are talking about two different surfaces:

- `DEFAULT_DENYLIST["auth"]` denylists **identity-provider login/credential-entry
  hosts** (`accounts.google.com`, `login.microsoftonline.com`, ...) -- domains
  that exist almost exclusively for entering credentials or completing an
  identity-provider-hosted consent screen. These hosts are picked because
  denylisting the *entire* host is safe: nobody has a legitimate "just browsing"
  reason to be on `accounts.google.com` for content, only to authenticate. Making
  the agent structurally blind to these hosts protects live credential/session
  material from ever entering an agent's context.
- The `oauth_grant` **gate** (not denylist) exists for the much more common case
  of a third-party app's *own* domain hosting an authorize/connect flow (e.g. a
  SaaS app's "Connect your Google Drive" button, or `github.com/login/oauth/
  authorize` -- a single path on a host, `github.com`, that is not and should not
  be wholesale denylisted, since it hosts ordinary work content everywhere else).
  These need a *gate*, not a denylist entry, because the agent legitimately needs
  to see the page to do the task; only the final "Allow"/"Authorize" click needs
  a human confirmation.

If a URL matches a denylisted host, the denylist wins outright (the command is
denied, full stop) -- the gate check never even runs for that target. This is
correct: there is no scenario where "gate this" is the right answer for a host we
have already decided the agent should never see.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from .addressing import Target
from .audit import AuditLog
from .classify import (
    ActionDescriptor,
    Classification,
    ClassifierProfile,
    _count_family_terms,
)
from .classify import classify as _classify

# The commands effects collection, flow-elevation, and (in a later phase)
# scope enforcement apply to (design doc section 11.4). Re-exported (bare
# import, not aliased -- ruff PLC0414) from effects.py so policy.py and
# hub.py share one canonical set; `hub.py` imports `STATE_CHANGING_COMMANDS`
# from `policy`, not `effects`, for that reason.
from .effects import STATE_CHANGING_COMMANDS, EffectsReport

# How long a tab's flow-elevated status survives with no new triggering
# observation, before it lapses on its own (design doc section 11.4:
# "FLOW_TTL_SECONDS = 900 (15 min). Flow elevation is per (device_id, tab_id).").
FLOW_TTL_SECONDS: float = 900.0

# ---------------------------------------------------------------------------
# Denylist
# ---------------------------------------------------------------------------

DEFAULT_POLICY_FILE = Path("~/.config/amplifier-browser-bridge/policy.json")

# Intentionally short and intentionally incomplete -- design doc §6.2: "No public
# maintained list of such domains exists; we maintain ~5 categories." This is a
# starting point, not a claim of completeness. Extend it (see docs/POLICY.md).
DEFAULT_DENYLIST: dict[str, list[str]] = {
    "financial": [
        "chase.com",
        "bankofamerica.com",
        "wellsfargo.com",
        "citibank.com",
        "capitalone.com",
        "americanexpress.com",
        "paypal.com",
        "venmo.com",
        "fidelity.com",
        "schwab.com",
        "vanguard.com",
        "coinbase.com",
    ],
    "healthcare": [
        "mychart.com",
        "myuhc.com",
        "kaiserpermanente.org",
        "anthem.com",
        "cigna.com",
        "aetna.com",
    ],
    # Identity-provider login/credential-entry hosts -- see the module docstring
    # section "Why 'auth & OAuth consent screens' splits across two different
    # mechanisms" for why this is narrower than "anything OAuth-related."
    "auth": [
        "accounts.google.com",
        "login.microsoftonline.com",
        "login.live.com",
        "appleid.apple.com",
        "login.yahoo.com",
        "okta.com",
    ],
    "password_managers": [
        "1password.com",
        "lastpass.com",
        "bitwarden.com",
        "dashlane.com",
        "keepersecurity.com",
    ],
}


def host_of(url: str | None) -> str | None:
    """Extract a lowercased hostname (no port) from a URL, or None if unparsable.

    Uses `urlsplit` (stdlib) rather than hand-rolled parsing -- URLs are exactly
    the kind of thing worth trusting a mature parser with (design doc IMPLEMENTATION_PHILOSOPHY:
    library over custom code when the problem is well-solved and not domain-specific).
    """
    if not url:
        return None
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return None
    return hostname.lower() if hostname else None


def host_matches_domain(host: str, domain: str) -> bool:
    """True if `host` is `domain` or a subdomain of it.

    Suffix-with-dot-boundary matching, not substring matching -- this is the
    detail that makes subdomain handling "sane" rather than accidentally unsafe:

        host_matches_domain("sub.chase.com", "chase.com")   -> True  (subdomain)
        host_matches_domain("chase.com", "chase.com")       -> True  (exact)
        host_matches_domain("notchase.com", "chase.com")    -> False (NOT a substring match)
        host_matches_domain("chase.com.evil.com", "chase.com") -> False (suffix, not prefix)
    """
    host = host.lower()
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)


@dataclass
class Denylist:
    """A loaded set of denylist categories -> domain lists, with a match() that
    reports which category/domain matched (for the audit log -- never for the
    agent-facing error, which must stay generic; see PolicyEngine.evaluate)."""

    categories: dict[str, list[str]] = field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_DENYLIST.items()}
    )

    def match(self, host: str | None) -> tuple[str, str] | None:
        """Returns (category, matched_domain) for the first hit, or None."""
        if not host:
            return None
        for category, domains in self.categories.items():
            for domain in domains:
                if host_matches_domain(host, domain):
                    return category, domain
        return None

    @staticmethod
    def load(path: str | Path | None = None) -> Denylist:
        """Resolution order (design doc + auth.py's TokenStore precedent):

            1. explicit `path` argument
            2. `ABB_POLICY_FILE` environment variable
            3. conventional path (~/.config/amplifier-browser-bridge/policy.json)
            4. built-in DEFAULT_DENYLIST if none of the above exist

        A user file's `"denylist"` key **replaces** the built-in categories
        entirely -- it does not merge. This is a deliberate simplicity choice:
        merge semantics for a short, human-curated list add real complexity
        (what does "extend category X" vs "add category Y" mean? what if a user
        wants to *remove* a default domain?) for marginal benefit, when the
        default list is short enough to copy-paste and edit directly. To extend
        rather than replace: copy the defaults (shown in docs/POLICY.md) into
        your file and add to them.
        """
        import json
        import os

        file_path = Path(path or os.environ.get("ABB_POLICY_FILE") or DEFAULT_POLICY_FILE).expanduser()
        if not file_path.is_file():
            return Denylist()
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return Denylist()
        denylist_section = data.get("denylist") if isinstance(data, dict) else None
        if not isinstance(denylist_section, dict):
            return Denylist()
        categories: dict[str, list[str]] = {}
        for category, domains in denylist_section.items():
            if isinstance(domains, list):
                categories[str(category)] = [str(d) for d in domains]
        return Denylist(categories=categories or {k: list(v) for k, v in DEFAULT_DENYLIST.items()})


# ---------------------------------------------------------------------------
# Confirmation gates
# ---------------------------------------------------------------------------

GateCombine = Literal["any", "all"]


@dataclass(frozen=True)
class GateRule:
    """One irreversible/world-visible action pattern. See the module docstring's
    "Honest limits of gate detection" section before trusting this to be complete
    or precise -- it is deliberately conservative pattern matching, not semantic
    understanding of page behavior."""

    category: str
    commands: frozenset[str]
    label_patterns: tuple[re.Pattern[str], ...] = ()
    url_patterns: tuple[re.Pattern[str], ...] = ()
    # "any": a match in either channel (that has data available) fires the rule.
    # "all": every channel that HAS a pattern list must also have a match --
    # used for oauth_grant/permission_change, where a label alone ("Allow") is
    # far too common a word to gate on by itself.
    combine: GateCombine = "any"

    def matches(self, label: str | None, url: str | None) -> dict[str, Any] | None:
        label_hit = _first_match(self.label_patterns, label)
        url_hit = _first_match(self.url_patterns, url)
        has_label_channel = bool(self.label_patterns)
        has_url_channel = bool(self.url_patterns)

        if self.combine == "all":
            if has_label_channel and not label_hit:
                return None
            if has_url_channel and not url_hit:
                return None
            if not label_hit and not url_hit:
                return None
        else:  # "any"
            if not label_hit and not url_hit:
                return None

        return {"category": self.category, "label_match": label_hit, "url_match": url_hit}


def _first_match(patterns: tuple[re.Pattern[str], ...], value: str | None) -> str | None:
    if not value:
        return None
    for pattern in patterns:
        if pattern.search(value):
            return pattern.pattern
    return None


def _re(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


# The canonical seven categories, confirmed by the user (design doc §6.2). Not
# user-file-configurable in this phase (unlike the denylist) -- see the module
# docstring; this is future work, not an oversight.
GATE_RULES: tuple[GateRule, ...] = (
    GateRule(
        category="purchase",
        commands=frozenset({"click"}),
        label_patterns=_re(
            r"\bplace order\b",
            r"\bbuy now\b",
            r"\bconfirm purchase\b",
            r"\bcomplete (?:your )?purchase\b",
            r"\bpay now\b",
            r"\bconfirm (?:order|payment)\b",
            r"\bsubmit payment\b",
            r"\bcheckout\b",
        ),
    ),
    GateRule(
        category="purchase",
        commands=frozenset({"navigate"}),
        url_patterns=_re(r"/checkout(?:/|$)", r"checkout\.stripe\.com", r"paypal\.com/checkoutnow"),
    ),
    GateRule(
        category="send",
        commands=frozenset({"click"}),
        label_patterns=_re(
            r"^send$",
            r"\bsend message\b",
            r"\bsend email\b",
            r"\bpublish\b",
            r"\btweet\b",
            r"\breply\b",
            r"\bshare\b",
            r"\bpost\b",
        ),
    ),
    GateRule(
        category="delete",
        commands=frozenset({"click"}),
        label_patterns=_re(
            r"\bdelete\b",
            r"\bremove\b",
            r"\btrash\b",
            r"\bpermanently delete\b",
            r"\bdeactivate\b",
        ),
    ),
    GateRule(
        category="oauth_grant",
        commands=frozenset({"click"}),
        label_patterns=_re(r"\ballow\b", r"\bauthorize\b", r"\bgrant access\b", r"\baccept\b"),
        url_patterns=_re(r"oauth2?/authorize", r"/o/oauth2", r"/login/oauth/authorize"),
        combine="all",
    ),
    GateRule(
        category="file_upload",
        commands=frozenset({"click", "type"}),
        label_patterns=_re(r"\bupload\b", r"\bchoose file\b", r"\battach\b"),
    ),
    GateRule(
        category="account_creation",
        commands=frozenset({"click"}),
        label_patterns=_re(
            r"\bsign up\b", r"\bcreate account\b", r"\bregister\b", r"\bcreate (?:my|your|a) account\b"
        ),
    ),
    GateRule(
        category="account_creation",
        commands=frozenset({"navigate"}),
        url_patterns=_re(r"/sign[-_]?up(?:/|$)", r"/register(?:/|$)", r"/create-account"),
    ),
    GateRule(
        category="permission_change",
        commands=frozenset({"click"}),
        label_patterns=_re(
            r"\bgrant\b",
            r"\bchange permission\b",
            r"\bupdate permission\b",
            r"\bmake public\b",
            r"\bmake private\b",
        ),
        url_patterns=_re(r"/settings/permissions", r"/security/permissions"),
        combine="all",
    ),
)

# input_type-based file-upload detection: a separate, simpler channel from the
# label-based GateRule above -- an explicit, unambiguous hint rather than a
# fuzzy text pattern. See the "Honest limits" docstring section: nothing
# populates this yet, but the mechanism is ready the moment a caller does.
FILE_UPLOAD_INPUT_TYPES: frozenset[str] = frozenset({"file"})


# ---------------------------------------------------------------------------
# Confirmation tokens
# ---------------------------------------------------------------------------

DEFAULT_CONFIRMATION_TTL_SECONDS = 300.0  # 5 minutes -- long enough for a human
# to notice and respond to a gated action in the same turn, short enough that a
# stale token from an abandoned turn can't be replayed much later.


class PolicyError(RuntimeError):
    """Raised by PolicyEngine.consume_confirmation on an invalid/expired/used token."""


@dataclass
class PendingConfirmation:
    token: str
    target: Target
    command: str
    args: dict[str, Any]
    category: str | None
    detected: dict[str, Any]
    created_at: float
    expires_at: float
    used: bool = False


@dataclass
class PolicyDecision:
    """The single return type of PolicyEngine.evaluate. `status` drives Hub's
    branch; `reason`/`category`/`token`/`detected` carry the detail.

    The remaining fields are additive (docs/designs/confirmation-gate.md
    section 8, "Migration" -- all optional, all defaulted, existing
    construction sites unaffected):

    - `classification`: the full scored `Classification` (classify.py),
      attached on every state-changing decision regardless of outcome --
      `advisory=True` always; never a security boundary on its own.
    - `redeem`: how a `needs_confirmation` token may be redeemed. `"agent"`
      (default) is self-attestation, honestly labeled as such -- see the
      design doc section 3, Candidate E.
    - `confirm_scope`: `"action"` (this one command) or `"flow"` (redeeming
      clears flow elevation for this tab until its origin changes or
      FLOW_TTL_SECONDS elapses).
    - `reason_code`: set on `deny`/`unknown` outcomes needing a specific,
      machine-readable cause (`"out_of_scope"`, `"unclassifiable"`, or one of
      classify.py's `ReasonCode` values).
    - `expires_at`: the confirmation token's expiry (epoch seconds), when
      `status == "gate"`.
    """

    status: Literal["allow", "deny", "gate"]
    reason: str | None = None
    category: str | None = None
    token: str | None = None
    detected: dict[str, Any] | None = None
    classification: Classification | None = None
    redeem: Literal["agent", "out_of_band"] = "agent"
    confirm_scope: Literal["action", "flow"] = "action"
    reason_code: str | None = None
    expires_at: float | None = None


# Generic, agent-facing denial text. Deliberately does NOT name the matched
# category or domain -- that detail goes to the audit log only. A denied tab
# must stay invisible; an agent that gets a *specific* reason ("this is a
# banking site") has just been told a denied tab exists and roughly what it is,
# which defeats the invisibility guarantee just as surely as showing it in a
# `tabs` listing would. See docs/POLICY.md.
DENY_REASON = "target is not accessible under current policy"
KILL_SWITCH_REASON = "kill switch engaged: all dispatch is halted"


class PolicyEngine:
    """The single evaluation surface `Hub.send_command` calls before a command
    may reach dispatch or a queue. See the module docstring for the full
    structural argument; in short: this class makes decisions from state it
    observed itself (`_tab_hosts`), never from anything an agent asserts about
    a target in a request."""

    def __init__(
        self,
        audit: AuditLog,
        denylist: Denylist | None = None,
        gate_rules: tuple[GateRule, ...] = GATE_RULES,
        confirmation_ttl: float = DEFAULT_CONFIRMATION_TTL_SECONDS,
        profile: ClassifierProfile | None = None,
        on_unknown: Literal["allow", "gate", "deny"] = "allow",
    ) -> None:
        self._audit = audit
        self.denylist = denylist if denylist is not None else Denylist.load()
        # Retained for one release (docs/designs/confirmation-gate.md section
        # 8, "Migration") -- GATE_RULES has no real importers outside this
        # module, and `evaluate()` no longer consults it: gate decisions are
        # now made by `classify.classify()` against `self.profile`. Kept as a
        # constructor param purely so existing call sites (tests included)
        # that pass `gate_rules=...` don't break.
        self.gate_rules = gate_rules
        self.confirmation_ttl = confirmation_ttl
        self.profile = profile if profile is not None else ClassifierProfile()
        # Stands in for a per-session SessionScope's `on_unknown` (scope.py is
        # not implemented in this phase -- see this module's docstring
        # section below on what's deliberately deferred). Applies hub-wide
        # rather than per-session until scope.py exists.
        self.on_unknown: Literal["allow", "gate", "deny"] = on_unknown
        self.kill_switch_active = False

        # (device_id, tab_id) -> last-observed full URL. Built exclusively from
        # the hub's OWN observations of device results (tabs/navigate/snapshot/
        # read) -- never from agent-supplied target/args. This is what makes the
        # capability-binding guarantee structural rather than advisory: an agent
        # cannot assert its way past a host the hub itself has already recorded.
        self._tab_hosts: dict[tuple[str, int], str] = {}

        # (device_id, tab_id) -> last-observed `discarded` state, from `tabs`
        # results (see `filter_tabs_result`). Bug 2 case study: at real-profile
        # scale (500+ tabs), Microsoft/Azure-integrated first-party apps
        # (SharePoint, OneDrive, internal engineering portals, ...) routinely
        # perform a top-level, full-page OAuth redirect through
        # `login.microsoftonline.com/.../oauth2/(v2.0/)authorize` to silently
        # refresh a session -- normally invisible, completing in milliseconds.
        # A BACKGROUNDED tab that Edge discards (unloads its renderer to
        # reclaim memory) mid-round-trip freezes with that intermediate
        # authorize URL as its last-known `url` forever -- it never gets a
        # chance to finish redirecting back to the real app page. Verified
        # live against a real 531-tab profile: 49 tabs, all "auth"-category,
        # ALL on `login.microsoftonline.com` with `redirect_uri` hosts like
        # `*.sharepoint.com`, `coreidentity.microsoft.com`,
        # `ms.portal.azure.com`, internal `*.microsoft.com`/`eng.ms` tools,
        # and `localhost` dev servers -- ordinary content tabs, not live
        # credential-entry screens (see docs/POLICY.md and
        # docs/ISSUE_CASE_STUDIES-equivalent narrative in this repo's PR
        # history for the full investigation).
        #
        # A DISCARDED tab has no live renderer -- nothing is being displayed
        # to the user right now, so the specific risk the `auth` category
        # exists to prevent ("the agent reads a live credential-entry
        # screen") does not apply. `financial`/`healthcare`/
        # `password_managers` are NOT given this exception: their rationale
        # is not renderer-liveness, it's not-revealing-which-services-the-
        # user-uses-at-all, which holds regardless of discard state.
        self._tab_discarded: dict[tuple[str, int], bool] = {}

        # (device_id, tab_id) -> {"url": <url at capture time>, "nodes": {ref:
        # {"label", "tag", "input_type"}}}. Fed by `note_snapshot` (a full
        # `snapshot` result) and `note_ref` (a single resolved `wait_for`
        # ref) -- see `_resolve_ref_hint` and the module docstring's "Label
        # hints are now wired" section (Phase 4). Never trusted across a
        # URL change -- see `_resolve_ref_hint`'s staleness check.
        self._tab_refs: dict[tuple[str, int], dict[str, Any]] = {}

        self._confirmations: dict[str, PendingConfirmation] = {}

        # (device_id, tab_id) -> {"by": "observed_effect"|"page_context", "at":
        # epoch_seconds, "origin": the origin recorded at trigger time}. D3 +
        # flow elevation (design doc section 11.4): a tab that was observed to
        # do something state-changing (or whose page context matched a
        # consequence family) enters an elevated-consequence context -- every
        # subsequent state-changing command in it gates until the tab's
        # committed ORIGIN changes (see note_tab_url below), a flow-scoped
        # confirmation is redeemed (see hub.py's _handle_agent_confirm), or
        # FLOW_TTL_SECONDS elapses. This is how a bland "Next" becomes
        # catchable -- not by its label, but by the observed character of the
        # flow it sits in.
        self._flow_elevated: dict[tuple[str, int], dict[str, Any]] = {}

    def _clear_flow(self, device_id: str, tab_id: int, *, reason: str) -> None:
        key = (device_id, tab_id)
        if key in self._flow_elevated:
            del self._flow_elevated[key]
            self._audit.record("flow_cleared", device_id=device_id, tab_id=tab_id, reason=reason)

    def _flow_state(self, device_id: str, tab_id: int | None) -> dict[str, Any] | None:
        """Live (non-expired) flow-elevation state for (device_id, tab_id), or
        None. Lazily expires on read rather than running a background sweep --
        same ruthless-simplicity reasoning as `_expire_stale` for confirmation
        tokens."""
        if tab_id is None:
            return None
        key = (device_id, tab_id)
        state = self._flow_elevated.get(key)
        if state is None:
            return None
        if time.time() - state["at"] > FLOW_TTL_SECONDS:
            del self._flow_elevated[key]
            self._audit.record("flow_cleared", device_id=device_id, tab_id=tab_id, reason="ttl")
            return None
        return state

    # ------------------------------------------------------------------
    # Observation intake -- called by Hub as device results arrive
    # ------------------------------------------------------------------

    def note_tab_url(self, device_id: str, tab_id: int | None, url: str | None) -> None:
        """Record the hub's own observation of what URL a tab is on. Called from
        `navigate`/`snapshot`/`read` results and from each entry of a `tabs`
        result (see `filter_tabs_result`).

        Also clears flow elevation (see `_flow_elevated`) the moment this
        tab's committed ORIGIN changes -- design doc section 11.4: flow
        elevation survives same-origin navigation (an SPA route change, a
        page reload) but not a genuine origin change, which is treated as
        "the agent has left the elevated-consequence context.\""""
        if tab_id is None or not url:
            return
        previous = self._tab_hosts.get((device_id, tab_id))
        self._tab_hosts[(device_id, tab_id)] = url
        if previous is not None and host_of(previous) != host_of(url):
            self._clear_flow(device_id, tab_id, reason="origin_change")

    def note_tab_discarded(self, device_id: str, tab_id: int | None, discarded: bool | None) -> None:
        """Record the hub's own observation of a tab's `discarded` state, from
        a `tabs` result (see `filter_tabs_result`, and background.js's
        `listTabs()`). Used only to narrow the `auth` denylist category's
        invisibility guarantee -- see this class's `__init__` docstring for
        `_tab_discarded` for the full case-study reasoning."""
        if tab_id is None or discarded is None:
            return
        self._tab_discarded[(device_id, tab_id)] = bool(discarded)

    def note_snapshot(
        self, device_id: str, tab_id: int | None, url: str | None, nodes: list[dict[str, Any]]
    ) -> None:
        """Record a full `ref -> {label, tag, input_type}` map from a
        `snapshot` result -- replaces any prior map for this tab outright
        (a fresh snapshot is authoritative for the page it was taken on;
        stale entries from a since-navigated-away page must not linger).
        Called from `Hub._ingest_result`."""
        if tab_id is None or not url:
            return
        node_map: dict[str, dict[str, Any]] = {}
        for n in nodes:
            ref = n.get("ref") if isinstance(n, dict) else None
            if not isinstance(ref, str):
                continue
            node_map[ref] = {
                "label": n.get("name"),
                "tag": n.get("tag"),
                "input_type": n.get("input_type"),
            }
        self._tab_refs[(device_id, tab_id)] = {"url": url, "nodes": node_map}

    def note_ref(
        self,
        device_id: str,
        tab_id: int | None,
        url: str | None,
        ref: str | None,
        *,
        label: str | None = None,
        tag: str | None = None,
        input_type: str | None = None,
    ) -> None:
        """Incremental update for a single resolved ref -- e.g. from a
        `wait_for` result, which resolves exactly one element without a full
        page snapshot. If the tab has moved to a different URL since the
        cached map was built, the old map is discarded first (same
        authority-of-the-latest-observation reasoning as `note_snapshot`)."""
        if tab_id is None or not url or not ref:
            return
        key = (device_id, tab_id)
        cache = self._tab_refs.get(key)
        if cache is None or cache.get("url") != url:
            cache = {"url": url, "nodes": {}}
            self._tab_refs[key] = cache
        cache["nodes"][ref] = {"label": label, "tag": tag, "input_type": input_type}

    def note_effects(
        self, device_id: str, tab_id: int | None, effects: EffectsReport, url: str | None
    ) -> None:
        """Browser-asserted (design doc section 2). If `effects.state_changing`,
        marks (device_id, tab_id) flow-elevated with reason `"observed_effect"`.
        This is the D3-to-flow-elevation wiring: the same observation that
        fixes attribution (the result/audit now names the actual request) also
        feeds the ONE page-immune pre-hoc signal this design has -- a tab that
        was just observed to do something consequential stays elevated until
        its origin changes, a flow confirmation is redeemed, or the TTL
        lapses. Called from `Hub._ingest_result`, symmetric with
        `note_snapshot`/`note_ref`."""
        if tab_id is None or not effects.state_changing:
            return
        self._flow_elevated[(device_id, tab_id)] = {
            "by": "observed_effect",
            "at": time.time(),
            "origin": host_of(url) if url else None,
        }
        self._audit.record(
            "flow_elevated", device_id=device_id, tab_id=tab_id, trigger="observed_effect", url=url
        )

    def note_page_context(
        self, device_id: str, tab_id: int | None, url: str | None, title: str | None, headings: list[str]
    ) -> None:
        """Page-asserted, weak (design doc section 2(a): advisory, not a
        boundary -- every input here is forgeable). Marks (device_id, tab_id)
        flow-elevated with reason `"page_context"` when >=2 distinct terms
        from any classifier family appear across `title` + `headings`. Fed
        from `snapshot`/`read` results (`Hub._ingest_result`)."""
        if tab_id is None:
            return
        context_text = " ".join(t for t in ([title] + list(headings)) if t)
        if not context_text:
            return
        for terms in self.profile.families.values():
            matched = _count_family_terms(terms, context_text)
            if len(matched) >= 2:
                self._flow_elevated[(device_id, tab_id)] = {
                    "by": "page_context",
                    "at": time.time(),
                    "origin": host_of(url) if url else None,
                }
                self._audit.record(
                    "flow_elevated", device_id=device_id, tab_id=tab_id, trigger="page_context", url=url
                )
                return

    def _resolve_ref_hint(self, target: Target) -> dict[str, Any] | None:
        """Best-effort `{label, tag, input_type}` for `target.ref`, or `None`
        if unknown or stale. See the module docstring's "Label hints are now
        wired" section for the full reasoning; in short: this never invents
        a label, and discards one the moment the hub's own observations
        suggest the page may have changed since it was captured."""
        if target.tab_id is None or not target.ref:
            return None
        key = (target.device_id, target.tab_id)
        cache = self._tab_refs.get(key)
        if cache is None:
            return None
        current_url = self._tab_hosts.get(key)
        if current_url is not None and current_url != cache.get("url"):
            return None  # stale -- see "Staleness, handled conservatively"
        return cache["nodes"].get(target.ref)

    def filter_tabs_result(self, device_id: str, tabs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Response-path filtering for a `tabs` command result (design doc §6.2:
        denied tabs must be invisible, not merely unreadable).

        Updates the host cache for EVERY tab observed (denied ones included --
        we must still remember a tab is denied so future request-path targeting
        of it is rejected), but returns a list with denied entries removed.
        """
        visible: list[dict[str, Any]] = []
        for tab in tabs:
            tab_id = tab.get("tab_id")
            url = tab.get("url")
            discarded = tab.get("discarded") if isinstance(tab.get("discarded"), bool) else None
            if isinstance(tab_id, int):
                self.note_tab_url(device_id, tab_id, url)
                self.note_tab_discarded(device_id, tab_id, discarded)
            host = host_of(url) if isinstance(url, str) else None
            deny_hit = self.denylist.match(host)
            if deny_hit is not None:
                category, matched_domain = deny_hit
                if category == "auth" and discarded:
                    # Bug 2 case study (see `_tab_discarded`'s docstring in
                    # __init__): a discarded tab has no live renderer, so the
                    # `auth` category's actual rationale ("don't let the
                    # agent read a LIVE credential-entry screen") does not
                    # apply -- most commonly this is a background tab frozen
                    # mid-way through a first-party app's silent OAuth
                    # session-refresh redirect through this host, not a real
                    # login prompt. Shown, not hidden -- but still fully
                    # audited, and still subject to `evaluate()`'s own
                    # discarded-aware exception (which additionally requires
                    # an explicit `wake=true` at the extension layer -- see
                    # background.js's `ensureAwake()` -- before any content
                    # is actually read).
                    self._audit.record(
                        "policy_tab_shown_despite_match",
                        device_id=device_id,
                        tab_id=tab_id,
                        category=category,
                        matched_domain=matched_domain,
                        host=host,
                        reason="discarded_auth_tab",
                    )
                    visible.append(tab)
                    continue
                # Full detail here -- category, matched rule, and the actual
                # host -- is the compensating control this project relies on
                # for broad-by-default access (design doc §6.2, docs/POLICY.md
                # §6). Previously this recorded `category` only, which is why
                # the audit log could not explain what was hidden or why (Bug
                # 2 requirement 3). Safe to log in full: the audit log is for
                # the human who owns the browser, not the agent -- the
                # agent-facing DENY_REASON stays generic (see `evaluate()`).
                self._audit.record(
                    "policy_tab_hidden",
                    device_id=device_id,
                    tab_id=tab_id,
                    category=category,
                    matched_domain=matched_domain,
                    host=host,
                )
                continue
            visible.append(tab)
        return visible

    # ------------------------------------------------------------------
    # Core decision
    # ------------------------------------------------------------------

    def evaluate(
        self,
        target: Target,
        command: str,
        args: dict[str, Any],
        *,
        skip_gate: bool = False,
    ) -> PolicyDecision:
        """The single entry point. See Hub.send_command (hub.py) for the choke
        point that guarantees every command passes through here exactly once,
        before it can reach `_dispatch_live` or a device queue.

        Decision flow (docs/designs/confirmation-gate.md section 12):

            1. kill switch active?                     -> deny
            2. resolve url_context / host
            3. denylist match (discarded-auth exception)? -> deny
            4. command not state-changing?              -> allow, classification=None
            5. skip_gate (post-confirmation re-dispatch)? -> allow (denylist already ran)
            6. build ActionDescriptor (caller args win; else hub's own
               observations: ref hint cache, tab-host cache, flow state)
            7. classify(descriptor, profile)
            8. status == "unknown" -> on_unknown handling
            9. status == "elevated" -> gate
            10. otherwise -> allow, classification attached

        `scope.py` (caller-declared write scope, Candidate C) is not
        implemented in this phase -- see this module's docstring and
        docs/designs/confirmation-gate.md's build-order section 15. Step 5 of
        the design doc's flow (session write-scope enforcement) is therefore
        absent; `on_unknown` is a hub-wide constructor setting standing in for
        a per-session one until scope.py exists.
        """
        if self.kill_switch_active:
            return PolicyDecision(status="deny", reason=KILL_SWITCH_REASON)

        url_context = self._resolve_url_context(target, command, args)
        host = host_of(url_context)

        deny_hit = self.denylist.match(host)
        if deny_hit is not None:
            category, domain = deny_hit
            is_discarded = (
                target.tab_id is not None
                and self._tab_discarded.get((target.device_id, target.tab_id)) is True
            )
            if category == "auth" and is_discarded:
                # Symmetric with `filter_tabs_result`'s exception -- see
                # `_tab_discarded`'s docstring in __init__ for the full case
                # study. This does NOT bypass Bug 1's own fail-loud discarded-
                # tab check (background.js's `ensureAwake()`): the command
                # still reaches the device, which still refuses to act on a
                # discarded tab unless the caller explicitly passed
                # `wake=true`. This only stops the HUB from blocking the
                # attempt before it gets that far.
                self._audit.record(
                    "policy_allowed_despite_match",
                    device_id=target.device_id,
                    tab_id=target.tab_id,
                    command=command,
                    category=category,
                    matched_domain=domain,
                    reason="discarded_auth_tab",
                )
            else:
                self._audit.record(
                    "policy_denied",
                    device_id=target.device_id,
                    tab_id=target.tab_id,
                    command=command,
                    category=category,
                    matched_domain=domain,
                )
                return PolicyDecision(status="deny", reason=DENY_REASON, category=category)

        if command not in STATE_CHANGING_COMMANDS:
            # read/tabs/screenshot/etc. are never classified -- classify.py's
            # scoring only applies to the commands that can actually change
            # state (design doc section 11.4's STATE_CHANGING_COMMANDS).
            return PolicyDecision(status="allow", classification=None)

        if skip_gate:
            # Post-confirmation re-dispatch: a human already explicitly
            # approved this exact (target, command, args) via a single-use
            # token (Hub._handle_agent_confirm). Skip classification/gating
            # entirely -- the denylist check above still ran unconditionally.
            return PolicyDecision(status="allow")

        descriptor = self._build_descriptor(target, command, args, url_context, host)
        classification = _classify(descriptor, self.profile)

        if classification.status == "unknown":
            self._audit.record(
                "policy_unclassified",
                device_id=target.device_id,
                tab_id=target.tab_id,
                command=command,
                reason_code=classification.reason_code,
                on_unknown=self.on_unknown,
            )
            if self.on_unknown == "deny":
                return PolicyDecision(
                    status="deny",
                    reason="action could not be classified (no page semantics observed)",
                    reason_code="unclassifiable",
                    classification=classification,
                )
            if self.on_unknown == "gate":
                pending = self._create_confirmation(target, command, args, None, {})
                return PolicyDecision(
                    status="gate",
                    category=None,
                    token=pending.token,
                    detected={},
                    classification=classification,
                    reason_code=classification.reason_code,
                    expires_at=pending.expires_at,
                )
            # "allow" (default): proceed, but the caller always sees the
            # classification and its reason_code -- fail-loud means visible,
            # not necessarily blocked (design doc section 6).
            return PolicyDecision(status="allow", classification=classification)

        if classification.status == "elevated":
            # confirm_scope is "flow" only when the flow channel was the SOLE
            # contributing signal (design doc section 12, step 10) -- if any
            # other channel also scored, the elevate-worthy evidence is about
            # THIS action specifically, not just the ambient flow context.
            contributing = [s for s in classification.signals if s.weight > 0]
            confirm_scope: Literal["action", "flow"] = (
                "flow" if contributing and all(s.channel == "flow" for s in contributing) else "action"
            )
            category = classification.categories[0] if classification.categories else None
            detected = {
                "category": category,
                "score": classification.score,
                "matched": [m for s in classification.signals for m in s.matched],
            }
            pending = self._create_confirmation(target, command, args, category, detected)
            self._audit.record(
                "policy_gated",
                device_id=target.device_id,
                tab_id=target.tab_id,
                command=command,
                category=category,
                detected=detected,
                token=pending.token,
                classification=classification.to_wire(),
            )
            return PolicyDecision(
                status="gate",
                category=category,
                token=pending.token,
                detected=detected,
                classification=classification,
                confirm_scope=confirm_scope,
                expires_at=pending.expires_at,
            )

        return PolicyDecision(status="allow", classification=classification)

    def _build_descriptor(
        self,
        target: Target,
        command: str,
        args: dict[str, Any],
        url_context: str | None,
        host: str | None,
    ) -> ActionDescriptor:
        """Assembles an `ActionDescriptor` for `classify()`. Caller-supplied
        `args` values always win over the hub's own cached hints (Phase 4
        precedent, `_resolve_ref_hint`'s docstring) -- this is a fallback,
        never an override."""
        label = args.get("label") if isinstance(args.get("label"), str) else None
        input_type = args.get("input_type") if isinstance(args.get("input_type"), str) else None
        href = args.get("href") if isinstance(args.get("href"), str) else None
        href_cross_origin = (
            args.get("href_cross_origin") if isinstance(args.get("href_cross_origin"), bool) else None
        )
        form_method = args.get("form_method") if isinstance(args.get("form_method"), str) else None
        form_action = args.get("form_action") if isinstance(args.get("form_action"), str) else None
        form_cross_origin = (
            args.get("form_cross_origin") if isinstance(args.get("form_cross_origin"), bool) else None
        )
        is_submit = args.get("is_submit") if isinstance(args.get("is_submit"), bool) else None
        page_title = args.get("page_title") if isinstance(args.get("page_title"), str) else None
        nearest_heading = (
            args.get("nearest_heading") if isinstance(args.get("nearest_heading"), str) else None
        )
        dialog_title = args.get("dialog_title") if isinstance(args.get("dialog_title"), str) else None

        hint = self._resolve_ref_hint(target)
        if hint is not None:
            if label is None and isinstance(hint.get("label"), str):
                label = hint["label"]
            if input_type is None and isinstance(hint.get("input_type"), str):
                input_type = hint["input_type"]
            if href is None and isinstance(hint.get("href"), str):
                href = hint["href"]
            if href_cross_origin is None and isinstance(hint.get("href_cross_origin"), bool):
                href_cross_origin = hint["href_cross_origin"]
            if form_method is None and isinstance(hint.get("form_method"), str):
                form_method = hint["form_method"]
            if form_action is None and isinstance(hint.get("form_action"), str):
                form_action = hint["form_action"]
            if form_cross_origin is None and isinstance(hint.get("form_cross_origin"), bool):
                form_cross_origin = hint["form_cross_origin"]
            if is_submit is None and isinstance(hint.get("is_submit"), bool):
                is_submit = hint["is_submit"]
            if nearest_heading is None and isinstance(hint.get("nearest_heading"), str):
                nearest_heading = hint["nearest_heading"]
            if dialog_title is None and isinstance(hint.get("dialog_title"), str):
                dialog_title = hint["dialog_title"]
            if page_title is None and isinstance(hint.get("page_title"), str):
                page_title = hint["page_title"]

        flow_state = self._flow_state(target.device_id, target.tab_id)

        return ActionDescriptor(
            command=command,
            label=label,
            role=None,
            tag=None,
            input_type=input_type,
            href=href,
            href_cross_origin=href_cross_origin,
            form_method=form_method,
            form_action=form_action,
            form_cross_origin=form_cross_origin,
            is_submit=is_submit,
            page_title=page_title,
            nearest_heading=nearest_heading,
            dialog_title=dialog_title,
            url=url_context,
            origin=host,
            flow_elevated=flow_state is not None,
            flow_elevated_by=flow_state.get("by") if flow_state else None,
        )

    def _resolve_url_context(self, target: Target, command: str, args: dict[str, Any]) -> str | None:
        """Best-available URL signal for this command, in priority order. See
        the module docstring's "Honest limits" section for what each of these
        actually guarantees."""
        url = args.get("url")
        if isinstance(url, str) and url:
            return url
        page_url = args.get("page_url")
        if isinstance(page_url, str) and page_url:
            return page_url
        if target.tab_id is not None:
            return self._tab_hosts.get((target.device_id, target.tab_id))
        return None

    # ------------------------------------------------------------------
    # Confirmation lifecycle
    # ------------------------------------------------------------------

    def _create_confirmation(
        self,
        target: Target,
        command: str,
        args: dict[str, Any],
        category: str | None,
        detected: dict[str, Any],
    ) -> PendingConfirmation:
        now = time.time()
        token = uuid.uuid4().hex
        pending = PendingConfirmation(
            token=token,
            target=target,
            command=command,
            args=dict(args),
            category=category,
            detected=detected,
            created_at=now,
            expires_at=now + self.confirmation_ttl,
        )
        self._confirmations[token] = pending
        return pending

    def consume_confirmation(self, token: str) -> PendingConfirmation:
        """Single-use: raises PolicyError on unknown, already-used, or expired
        tokens; otherwise marks the token used and returns the original
        (target, command, args) so the caller can re-dispatch with skip_gate=True.

        Checks THIS token's own expiry directly, rather than running the lazy
        `_expire_stale` sweep first -- sweeping first would delete the very
        token being asked about before we can distinguish "expired" from
        "never existed," collapsing two different, useful error messages into
        one. `_expire_stale` still runs afterward, to reclaim other abandoned
        tokens as a side effect of any confirm attempt.
        """
        pending = self._confirmations.get(token)
        if pending is None:
            raise PolicyError("unknown confirmation token")
        if pending.used:
            raise PolicyError("confirmation token already used")
        if time.time() > pending.expires_at:
            del self._confirmations[token]
            self._audit.record("policy_confirmation_expired", token=token, category=pending.category)
            raise PolicyError("confirmation token expired")
        pending.used = True
        del self._confirmations[token]
        self._expire_stale()
        return pending

    def _expire_stale(self) -> None:
        """Lazy cleanup of OTHER abandoned tokens -- no background task needed
        (ruthless simplicity: token volume is bounded by human/agent interaction
        speed, same reasoning as AuditLog's synchronous writes)."""
        now = time.time()
        expired = [t for t, p in self._confirmations.items() if now > p.expires_at and not p.used]
        for t in expired:
            pending = self._confirmations.pop(t)
            self._audit.record("policy_confirmation_expired", token=t, category=pending.category)

    # ------------------------------------------------------------------
    # Kill switch (flag only -- queue draining lives in Hub, which owns the
    # DeviceRegistry; see Hub.engage_kill_switch)
    # ------------------------------------------------------------------

    def engage_kill_switch(self) -> None:
        self.kill_switch_active = True

    def disengage_kill_switch(self) -> None:
        self.kill_switch_active = False
