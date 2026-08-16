"""Pure-logic pagination, filtering, and summarization for the `tabs` command's
agent-facing response shape.

No I/O, no network, no `HubClient`/websocket dependency of any kind -- this is
the single, fully-unit-testable home for `browser_tabs` response shaping. Both
agent surfaces (`mcp_server.py`'s `browser_tabs` tool and
`modules/tool-browser-bridge`'s `tabs_runner`) import `shape_tabs_response`
from here rather than each reimplementing this logic.

Why this exists: on the maintainer's real device (~728 open tabs), an unpaged
`tabs` result was ~640KB -- large enough to truncate mid-response before it
ever reached an agent's context window, silently destroying whatever the
agent was trying to do with it. The hub->agent wire transfer that produces
that payload is cheap (machine to machine); what's expensive is a payload
that size entering an LLM's context. So this fix lives at the agent-facing
TOOL layer, not the wire protocol: the hub still returns every tab in one
`tabs` command result (nothing about `protocol.py` / `extension/background.js`
/ `hub.py` changes here -- see CONTRIBUTING.md's "keep the two protocol
implementations in sync by hand" convention, which is exactly the surface
this fix deliberately avoids growing), and this module shapes that full
result -- filtering, paginating, or summarizing it -- before either agent
surface hands anything back to the calling agent.
"""

from __future__ import annotations

from typing import Any

# Shared default across both agent surfaces (mcp_server.py's `browser_tabs`
# signature default and the tool module's `browser_tabs` JSON schema default)
# so the default page size cannot drift between the two -- design doc section
# 3.3 requires the two surfaces' tool vocabularies (and behavior) to match.
DEFAULT_LIMIT = 100


class TabsPayloadError(ValueError):
    """Raised when a hub `tabs` response claims `ok: true` but its `result` is
    not the expected list of tab dicts (e.g. a genuine hub/extension protocol
    drift, or a caller feeding this function something that was never a real
    `tabs` response at all).

    This is a protocol-contract violation, not an ordinary command failure --
    CONTRIBUTING.md's "fail loud, never silently" convention forbids absorbing
    it into a quietly-empty tab list or a normal-looking `{"ok": False}"`
    result a caller might reasonably ignore. It is raised, not returned, so it
    cannot be missed.
    """


def shape_tabs_response(
    response: dict[str, Any],
    *,
    window_id: int | None = None,
    url_contains: str | None = None,
    title_contains: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    summary: bool = False,
) -> dict[str, Any]:
    """Filter, paginate, or summarize a raw `tabs` command response.

    `response` is exactly whatever `HubClient.command(target, "tabs", {})`
    returned -- passed through UNMODIFIED (the identical object, not a copy)
    in two cases, both already part of docs/PROTOCOL.md's contract:

    - A non-`live` device's `{"status": "queued", ...}` response: never
      paged, filtered, or reshaped.
    - An `{"ok": False, "error": ...}` response: also returned untouched --
      an error is not something to paginate around.

    Any other shape must be `{"ok": True, "result": [<tab dict>, ...]}`;
    anything else raises `TabsPayloadError` naming exactly what arrived,
    rather than silently returning an empty list.

    Filters (`window_id` exact match, `url_contains` / `title_contains`
    case-insensitive substrings) apply BEFORE pagination -- `matched` in the
    returned shape reflects the post-filter count, while `total` is always
    the unfiltered grand total across every tab on the device. This is what
    lets a caller tell "3 tabs matched my filter" apart from "3 tabs exist".

    `limit=0` means unlimited -- the caller explicitly opting back into the
    old, unpaged full listing.

    `summary=True` short-circuits pagination entirely and returns ONLY
    aggregate orientation (per-window tab counts, totals, discarded/asleep
    counts) with no tab list at all -- the cheap first call an agent should
    make against a profile of unknown size.
    """
    if not isinstance(response, dict):
        raise TabsPayloadError(
            f"expected a dict response from the hub's `tabs` command, got "
            f"{type(response).__name__}: {response!r}"
        )

    # Queued (non-live device) -- identity pass-through, never touched.
    if "status" in response:
        return response

    # Explicit failure -- also untouched. Not something to paginate around.
    if response.get("ok") is False:
        return response

    if response.get("ok") is not True:
        raise TabsPayloadError(
            "expected the hub's `tabs` response to be {'ok': True, 'result': [...]}, "
            "{'ok': False, 'error': ...}, or a queued {'status': ...} shape; got a dict "
            f"with keys {sorted(response.keys())!r} and neither 'ok' nor 'status'"
        )

    payload = response.get("result")
    if not isinstance(payload, list) or not all(isinstance(tab, dict) for tab in payload):
        shape = (
            "a list containing at least one non-dict item"
            if isinstance(payload, list)
            else type(payload).__name__
        )
        raise TabsPayloadError(
            f"expected browser_tabs' `result` to be a list of tab dicts, got {shape}: {payload!r}"
        )

    if offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")
    if limit < 0:
        raise ValueError(f"limit must be >= 0 (0 means unlimited), got {limit}")

    total = len(payload)
    matched_tabs = _filter_tabs(
        payload, window_id=window_id, url_contains=url_contains, title_contains=title_contains
    )
    matched = len(matched_tabs)

    if summary:
        return {"ok": True, "result": _summarize(matched_tabs, total=total, matched=matched)}

    page = matched_tabs[offset:] if limit == 0 else matched_tabs[offset : offset + limit]
    returned = len(page)
    has_more = (offset + returned) < matched

    return {
        "ok": True,
        "result": {
            "tabs": page,
            "total": total,
            "matched": matched,
            "returned": returned,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        },
    }


def _filter_tabs(
    tabs: list[dict[str, Any]],
    *,
    window_id: int | None,
    url_contains: str | None,
    title_contains: str | None,
) -> list[dict[str, Any]]:
    result = tabs
    if window_id is not None:
        result = [t for t in result if t.get("window_id") == window_id]
    if url_contains:
        needle = url_contains.lower()
        result = [t for t in result if needle in str(t.get("url") or "").lower()]
    if title_contains:
        needle = title_contains.lower()
        result = [t for t in result if needle in str(t.get("title") or "").lower()]
    return result


def _summarize(tabs: list[dict[str, Any]], *, total: int, matched: int) -> dict[str, Any]:
    windows: dict[Any, dict[str, int]] = {}
    discarded = 0
    asleep = 0
    for tab in tabs:
        w = windows.setdefault(tab.get("window_id"), {"count": 0, "discarded": 0, "asleep": 0})
        w["count"] += 1
        if tab.get("discarded"):
            w["discarded"] += 1
            discarded += 1
        if tab.get("asleep"):
            w["asleep"] += 1
            asleep += 1
    return {
        "summary": True,
        "total": total,
        "matched": matched,
        "windows": windows,
        "discarded": discarded,
        "asleep": asleep,
    }
