"""Browser-observed effects: what the browser actually did after a state-changing
command, reported by the extension and parsed here.

This is the D3 fix from `docs/designs/confirmation-gate.md` ("attribution first,
gating second"): a `click`/`type`/`key`/`navigate` result now carries an `effects`
block naming every non-GET request, navigation, download, and tab opened in a
bounded window after dispatch. These are **browser-asserted** (design doc section
2 there): the page cannot suppress a request it actually made, so this is the one
page-immune signal available before any classifier or scope check runs.

`EffectsReport.from_wire` never returns `None` -- an absent payload (an older
extension build, or a device whose effects tier is `"none"`) parses to
`EffectsReport(tier="none", window_ms=0, attribution="none")`, so a caller can
always distinguish "the browser observed nothing happened" from "nothing could be
observed" by checking `tier`, never by treating an absent block as silence.

See `extension/effects_collector.mjs` for the hand-synced JS-side twin that
accumulates the raw `chrome.webRequest`/`webNavigation`/`downloads`/`tabs` events
and builds the wire payload this module parses -- kept in sync by hand, the same
discipline `CONTRIBUTING.md` documents for `protocol.py`/`background.js`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# "cdp"        -- CDP Network.requestWillBeSent (desktop only, requires debugger
#                 already attached; blind to nothing but tabId:-1 service-worker
#                 requests).
# "webrequest" -- chrome.webRequest (observe-only). Sees XHR/fetch mutations,
#                 which "navigation" cannot. Costs a manifest permission.
# "navigation" -- chrome.webNavigation.onCommitted + downloads.onCreated +
#                 tabs.onCreated + a post-action tabs.get. Free (already-granted
#                 permissions on desktop), but blind to XHR/fetch mutations --
#                 exactly the SPA case that matters most on modern enterprise
#                 apps (design doc section 3, Candidate D's tier table).
# "none"       -- no effects collection at all (extension too old, or the
#                 behavioral probe failed every tier).
EffectsTier = Literal["cdp", "webrequest", "navigation", "none"]


@dataclass(frozen=True)
class ObservedRequest:
    """One non-GET/HEAD network request observed during the collection window."""

    method: str
    url: str
    type: str | None = None  # webRequest resourceType, e.g. "xmlhttprequest", "sub_frame"
    cross_origin: bool = False


@dataclass(frozen=True)
class ObservedNavigation:
    """One navigation commit observed during the collection window."""

    url: str
    transition_type: str | None = None  # "form_submit" | "link" | "reload" | ...
    origin_changed: bool = False


@dataclass(frozen=True)
class EffectsReport:
    """Everything the browser was observed to do after a state-changing command.

    Always present on a state-changing command's result (see hub.py's
    `_ingest_result`) -- never omitted, never `None`. `tier="none"` is how a
    caller distinguishes "nothing happened" from "we could not observe."
    """

    tier: EffectsTier
    window_ms: int = 0
    attribution: Literal["time_window", "none"] = "none"
    requests: tuple[ObservedRequest, ...] = ()
    navigations: tuple[ObservedNavigation, ...] = ()
    downloads: tuple[str, ...] = ()
    tabs_opened: tuple[int, ...] = ()

    @property
    def state_changing(self) -> bool:
        """True if ANY of: a non-GET/HEAD request; a navigation whose
        transition_type is 'form_submit'; a download started; a tab opened.

        Browser-asserted throughout (design doc section 2) -- a page can add
        decoy effects to trigger a false positive here, but cannot suppress a
        real one, which is the correct failure direction for a safety signal.
        """
        if any(r.method.upper() not in ("GET", "HEAD") for r in self.requests):
            return True
        if any(n.transition_type == "form_submit" for n in self.navigations):
            return True
        if self.downloads:
            return True
        return bool(self.tabs_opened)

    @staticmethod
    def from_wire(payload: dict[str, Any] | None) -> EffectsReport:
        """Parse the extension's `effects` wire block. Never raises on a
        malformed/partial payload -- degrades field-by-field to safe defaults,
        consistent with this module's "always present, honest degradation"
        contract. An absent payload (`None`) parses to the empty/`"none"` tier
        report, never to an exception or a silently-omitted result."""
        if not isinstance(payload, dict):
            return EffectsReport(tier="none", window_ms=0, attribution="none")

        tier = payload.get("tier")
        if tier not in ("cdp", "webrequest", "navigation", "none"):
            tier = "none"

        window_ms = payload.get("window_ms")
        window_ms = int(window_ms) if isinstance(window_ms, (int, float)) else 0

        attribution = payload.get("attribution")
        if attribution not in ("time_window", "none"):
            attribution = "none"

        requests = tuple(
            ObservedRequest(
                method=str(r.get("method", "")).upper(),
                url=str(r.get("url", "")),
                type=r.get("type") if isinstance(r.get("type"), str) else None,
                cross_origin=bool(r.get("cross_origin", False)),
            )
            for r in payload.get("requests", [])
            if isinstance(r, dict) and r.get("method") and r.get("url")
        )

        navigations = tuple(
            ObservedNavigation(
                url=str(n.get("url", "")),
                transition_type=n.get("transition_type")
                if isinstance(n.get("transition_type"), str)
                else None,
                origin_changed=bool(n.get("origin_changed", False)),
            )
            for n in payload.get("navigations", [])
            if isinstance(n, dict) and n.get("url")
        )

        downloads = tuple(str(d) for d in payload.get("downloads", []) if isinstance(d, str))
        tabs_opened = tuple(int(t) for t in payload.get("tabs_opened", []) if isinstance(t, (int, float)))

        return EffectsReport(
            tier=tier,
            window_ms=window_ms,
            attribution=attribution,
            requests=requests,
            navigations=navigations,
            downloads=downloads,
            tabs_opened=tabs_opened,
        )

    def to_wire(self) -> dict[str, Any]:
        """Wire shape attached to a result envelope's `effects` field (and to
        the `action_effects` audit event) -- see docs/PROTOCOL.md."""
        return {
            "tier": self.tier,
            "window_ms": self.window_ms,
            "attribution": self.attribution,
            "state_changing": self.state_changing,
            "requests": [
                {"method": r.method, "url": r.url, "type": r.type, "cross_origin": r.cross_origin}
                for r in self.requests
            ],
            "navigations": [
                {"url": n.url, "transition_type": n.transition_type, "origin_changed": n.origin_changed}
                for n in self.navigations
            ],
            "downloads": list(self.downloads),
            "tabs_opened": list(self.tabs_opened),
        }


# The collection window held open on the acting tab after a STATE_CHANGING_COMMANDS
# dispatch's own result, before the effects report is finalized (design doc section
# 11.5). Lives here (not just in background.js) so hub-side tests and documentation
# reference one canonical number.
EFFECTS_WINDOW_MS: int = 1500

# The commands effects collection and flow-elevation/scope enforcement apply to
# (design doc section 11.4). A pure data constant -- no behavior -- so policy.py
# and hub.py both import the same set rather than each hardcoding it.
STATE_CHANGING_COMMANDS: frozenset[str] = frozenset({"click", "type", "key", "navigate"})

__all__ = [
    "EFFECTS_WINDOW_MS",
    "STATE_CHANGING_COMMANDS",
    "EffectsReport",
    "EffectsTier",
    "ObservedNavigation",
    "ObservedRequest",
]

# Unused import guard: `field` is imported for parity with the design doc's own
# dataclass declarations even though no field currently needs a default_factory;
# keep the import so future additive fields (e.g. a mutable default) don't need
# a new import line. (Documented rather than removed to match the module's
# additive-evolution stance -- see KERNEL_PHILOSOPHY.md.)
_ = field
