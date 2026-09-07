"""The tool-description budget ratchet.

Every one of these 31 tool descriptions is serialised into the request on EVERY
request of EVERY session that mounts this bundle, whether or not the tool is
ever called -- so the description surface is a permanent, per-request cost, not
a documentation surface. This file is the ratchet that keeps it from growing
back.

Three things are pinned, and each fails for a different reason:

1. `CEILING` -- a per-tool character ceiling, recorded at the values the lean
   pass landed on. A description may SHRINK freely; growing past its recorded
   ceiling fails, and the fix is either to trim or to consciously raise the
   number here (a visible, reviewable diff, instead of silent drift).

2. `OVER_BUDGET` -- the standard is trigger-first and <= ~600 chars. A tool
   description is NOT an agent description, so a genuine PARAMETER CONTRACT is
   allowed to exceed it -- but only if it is named here with the parameters
   that forced it. A new over-budget description fails until someone writes
   down why. This is the enforcement point for "name every description left
   over ~600 chars".

3. `REQUIRED_TOKENS` -- the behaviour/constraint/parameter/failure-mode tokens
   a future trimming pass must not delete. A shorter description that silently
   loses `capture_hidden`, `wake`, `include_cookies` or `since_id` costs the
   caller far more than the bytes it saved: an argument nobody can interpret
   gets passed wrong.

`<example>` / `<commentary>` blocks are banned outright: they are agent-catalog
formatting, and in a tool description they are pure per-request cost.
"""

from __future__ import annotations

import re

from amplifier_module_tool_browser_bridge import _build_tools

# Trigger-first, <= ~600 chars. Anything above this must appear in OVER_BUDGET.
SOFT_BUDGET = 600

# Per-tool ceilings, recorded 2026-09-07 after the lean pass (31,210 -> 25,071
# chars total). Shrinking is always allowed; growing past a ceiling is not.
CEILING: dict[str, int] = {
    "browser_archive": 2692,
    "browser_archive_catalog": 2079,
    "browser_archive_convert": 1821,
    "browser_click": 297,
    "browser_devices": 311,
    "browser_download": 461,
    "browser_downloads_list": 618,
    "browser_establish_session": 518,
    "browser_fetch_bytes": 940,
    "browser_grab_image": 797,
    "browser_key": 337,
    "browser_narrow_scope": 550,
    "browser_navigate": 260,
    "browser_poll": 322,
    "browser_read": 765,
    "browser_reload": 436,
    "browser_screenshot": 1414,
    "browser_scroll": 280,
    "browser_setup": 1386,
    "browser_setup_status": 420,
    "browser_snapshot": 1032,
    "browser_tab_activate": 462,
    "browser_tab_close": 248,
    "browser_tab_open": 463,
    "browser_tabs": 911,
    "browser_type": 306,
    "browser_update_extension": 2123,
    "browser_vision_read": 1318,
    "browser_wait_download": 826,
    "browser_wait_for": 333,
    "browser_wait_text": 343,
}

TOTAL_CEILING = sum(CEILING.values())

# Every description above SOFT_BUDGET, with the parameter contract that forced
# it. "The queue note alone" means the tool's own lead is inside budget and the
# shared non-live/queued contract (_QUEUE_NOTE, 235 chars, on 20 tools) is what
# pushes it over.
OVER_BUDGET: dict[str, str] = {
    "browser_tabs": "summary / limit / offset / window_id / url_contains / title_contains, "
    "plus the six-field result contract (total vs matched vs returned)",
    "browser_snapshot": "ref generation + stale-ref failure mode; wake (destroys in-page state); activate (steals focus)",
    "browser_read": "wake (destroys in-page state) and activate (steals focus) -- the queue note pushes it over",
    "browser_screenshot": "capture_hidden (+ debugger capability), frame_id, multi_page, max_pages, "
    "scroll_selector, page_delay_ms, and the Android no-CDP limit",
    "browser_vision_read": "the vision-provider env-var contract, capture_hidden's inverted default, "
    "and the seven-field return shape",
    "browser_fetch_bytes": "max_bytes / byte cap, the return shape, and the "
    "browser_grab_image fallback for Referer-checking targets",
    "browser_grab_image": "max_bytes / byte cap and the return shape -- the queue note pushes it over",
    "browser_downloads_list": "the max_download_id -> since_id baseline protocol -- the queue note pushes it over",
    "browser_wait_download": "the exactly-one-of download_id / since_id rule, pattern, and the return shape",
    "browser_setup": "install_service, force_token, and the result.* shape "
    "(hub_reachable / pairing / warnings / service / manual_hub_command / setup_url)",
    "browser_archive": "the six-level depth ladder, the no-wake guarantee, wake / tab_ids / all_frames / "
    "captures / injection_timeout_s / include_cookies, and the five-valued per-tab status",
    "browser_archive_convert": "archive_dir, tab_ids, the two output files, and the "
    "not_captured / merged-cell / multi-body failure modes",
    "browser_archive_catalog": "the two-layer model, catalog, lens, concurrency, top_n, and the "
    "not_found / no_content per-tab states",
    "browser_update_extension": "reconnect_timeout_s and the five distinct outcomes "
    "(already_current / updated / guided-unchanged / guided-no-ack / fail-loud)",
}

# Behaviour, constraints, parameter semantics and failure modes that must not be
# trimmed away. Checked as substrings of the description.
REQUIRED_TOKENS: dict[str, tuple[str, ...]] = {
    "browser_devices": ("tier", "capabilities"),
    "browser_tabs": ("summary", "limit", "offset", "has_more", "matched", "window_id", "url_contains"),
    "browser_snapshot": ("ref", "generation", "stale ref", "wake", "activate", "woke", "activated"),
    "browser_read": ("wake", "activate", "woke", "activated"),
    "browser_screenshot": (
        "capture_hidden",
        "debugger",
        "frame_id",
        "multi_page",
        "max_pages",
        "scroll_selector",
        "page_delay_ms",
        "Android",
    ),
    "browser_vision_read": (
        "capture_hidden",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "AMPLIFIER_BROWSER_BRIDGE_VISION_PROVIDER",
        "vision_provider",
        "stopped_reason",
    ),
    "browser_fetch_bytes": ("max_bytes", "25MB", "byte_length", "browser_grab_image"),
    "browser_grab_image": ("max_bytes", "25MB", "byte_length", "tab_id"),
    "browser_downloads_list": ("max_download_id", "since_id", "chrome.downloads.search"),
    "browser_download": ("download_id", "chrome.downloads.download"),
    "browser_wait_download": ("download_id", "since_id", "pattern", "timeout_ms", "byte_length"),
    "browser_poll": ("queued", "pending", "queue_position"),
    "browser_establish_session": ("session_id", "browser_narrow_scope"),
    "browser_narrow_scope": ("on_unknown", "redeem", "unattended", "SEAL"),
    "browser_setup": (
        "install_service",
        "force_token",
        "result.pairing.pair_url",
        "result.setup_url",
        "hub_reachable",
        "manual_hub_command",
        "Tailscale",
        "127.0.0.1",
    ),
    "browser_setup_status": ("doctor",),
    "browser_archive": (
        "L0",
        "L1",
        "L2",
        "L3",
        "L4",
        "L5",
        "wake",
        "tab_ids",
        "all_frames",
        "captures",
        "injection_timeout_s",
        "include_cookies",
        "dest_dir",
        "debugger",
        "ok_with_skips",
        "ok_with_failures",
        "tabs_inventoried",
        "tabs_partial",
        "tabs_not_found",
        "not_found",
        "partial",
        "skipped",
    ),
    "browser_archive_convert": (
        "archive_dir",
        "tab_ids",
        "page.mhtml",
        "page.extracted.md",
        "page.full_page.md",
        "assets/",
        "not_captured",
        "tables_with_merged_cells",
    ),
    "browser_archive_catalog": (
        "archive_dir",
        "catalog",
        "lens",
        "concurrency",
        "top_n",
        "tab_ids",
        "tabs.json",
        "catalog.json",
        "not_found",
        "no_content",
        "why_kept",
    ),
    "browser_update_extension": (
        "reconnect_timeout_s",
        "already_current",
        "updated",
        "guided",
        "download_url",
        "/setup/extension.zip",
    ),
    "browser_wait_for": ("selector", "timeout_ms"),
    "browser_wait_text": ("timeout_ms",),
    "browser_tab_open": ("active",),
    "browser_reload": ("chrome.runtime.reload()",),
}

# The shared non-live/queued contract, which 20 tools append verbatim.
QUEUE_NOTE_TOOLS = frozenset(
    {
        "browser_tabs",
        "browser_snapshot",
        "browser_read",
        "browser_click",
        "browser_type",
        "browser_key",
        "browser_scroll",
        "browser_navigate",
        "browser_tab_open",
        "browser_tab_close",
        "browser_tab_activate",
        "browser_screenshot",
        "browser_vision_read",
        "browser_wait_for",
        "browser_wait_text",
        "browser_fetch_bytes",
        "browser_grab_image",
        "browser_downloads_list",
        "browser_download",
        "browser_wait_download",
    }
)


def _descriptions() -> dict[str, str]:
    return {t.name: t.description for t in _build_tools()}


def test_ceiling_covers_exactly_the_shipped_tools():
    """A new tool must arrive with a recorded ceiling, not slip in unmeasured."""
    assert set(_descriptions()) == set(CEILING)


def test_no_description_exceeds_its_recorded_ceiling():
    over = {
        name: (len(desc), CEILING[name])
        for name, desc in _descriptions().items()
        if len(desc) > CEILING[name]
    }
    assert not over, (
        "description(s) grew past the recorded ceiling (name: actual -> allowed): "
        f"{over}. Trim, or raise the number in CEILING deliberately."
    )


def test_total_description_surface_does_not_grow():
    total = sum(len(d) for d in _descriptions().values())
    assert total <= TOTAL_CEILING, f"total description surface {total} > {TOTAL_CEILING}"


def test_every_over_budget_description_is_named_with_the_contract_that_forced_it():
    actual_over = {name for name, desc in _descriptions().items() if len(desc) > SOFT_BUDGET}
    unnamed = actual_over - set(OVER_BUDGET)
    assert not unnamed, (
        f"{sorted(unnamed)} exceed the ~{SOFT_BUDGET}-char standard without being named in "
        "OVER_BUDGET. Either trim to budget, or record the parameter contract that forces it."
    )
    stale = set(OVER_BUDGET) - actual_over
    assert not stale, (
        f"{sorted(stale)} are listed in OVER_BUDGET but are now within budget -- delete the entry."
    )
    for name, reason in OVER_BUDGET.items():
        assert reason.strip(), f"{name}'s OVER_BUDGET entry must say which parameters forced it"


def test_no_example_or_commentary_blocks_anywhere():
    """<example>/<commentary> are agent-catalog formatting; in a tool description
    they are pure per-request cost with no caller value."""
    banned = re.compile(r"</?(example|commentary)\b", re.IGNORECASE)
    offenders = [name for name, desc in _descriptions().items() if banned.search(desc)]
    assert not offenders, f"{offenders} contain <example>/<commentary> blocks"


def test_every_description_leads_with_a_trigger_not_a_preamble():
    """Trigger-first: the first sentence says what the tool does, in one line --
    never 'This tool ...' / 'You can use this to ...' boilerplate."""
    preamble = re.compile(r"^(this tool|this command|you can|use this tool|the purpose of)", re.IGNORECASE)
    for name, desc in _descriptions().items():
        assert desc == desc.strip(), f"{name} description has leading/trailing whitespace"
        assert not preamble.match(desc), f"{name} opens with preamble, not a trigger: {desc[:60]!r}"
        # 260 admits a trigger line that enumerates ("Diagnose which link is broken:
        # token store, hub location, ...") while still catching a paragraph-length opener.
        lead = re.split(r"(?<=[.!?]) ", desc, maxsplit=1)[0]
        assert len(lead) <= 260, f"{name}'s opening sentence is {len(lead)} chars -- lead with a trigger line"


def test_required_semantics_survive_every_future_trim():
    """The fidelity gate, pinned: a leaner description may not silently drop a
    parameter name, response field or failure-mode token a caller needs."""
    descs = _descriptions()
    missing: dict[str, list[str]] = {}
    for name, tokens in REQUIRED_TOKENS.items():
        assert name in descs, f"REQUIRED_TOKENS names unknown tool {name}"
        gone = [t for t in tokens if t.lower() not in descs[name].lower()]
        if gone:
            missing[name] = gone
    assert not missing, f"descriptions lost required semantics: {missing}"


def test_queued_contract_present_on_every_tool_that_can_queue():
    """Every device-addressed tool states the non-live/queued pass-through in its
    OWN description -- a caller (or an MCP client) typically sees one description
    in isolation."""
    descs = _descriptions()
    for name in QUEUE_NOTE_TOOLS:
        desc = descs[name]
        assert "queued" in desc, f"{name} lost the queued note"
        assert "browser_poll" in desc, f"{name} lost the browser_poll remedy"
        assert "not an error" in desc, f"{name} lost 'not an error' -- the whole point of the note"
    for name in set(descs) - QUEUE_NOTE_TOOLS:
        if name == "browser_poll":
            continue
        assert "call browser_poll(device_id, command_id)" not in descs[name], (
            f"{name} carries the queued note but is not listed in QUEUE_NOTE_TOOLS"
        )
