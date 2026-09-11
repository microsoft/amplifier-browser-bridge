"""The tool-surface budget ratchet.

Every mounted tool's name + description + input_schema is serialised into the
request on EVERY request of EVERY session that mounts this bundle, whether or not
the tool is ever called -- so the tool surface is a permanent, per-request cost,
not a documentation surface. This file is the ratchet that keeps it from growing
back.

Two passes have moved these numbers:

* the lean pass (PR #14) trimmed 31 descriptions, 31,210 -> 25,069 chars;
* the consolidation (this file's current state) replaced those 31 flat tools with
  seven operation-enum tools, 25,069 -> ~9,100 description chars and a full wire
  surface of 47,248 -> ~19,600 chars.

Five things are pinned, and each fails for a different reason:

1. `CEILING` -- a per-tool description ceiling. A description may SHRINK freely;
   growing past its recorded ceiling fails, and the fix is either to trim or to
   consciously raise the number here (a visible, reviewable diff, instead of
   silent drift).

2. `WIRE_CEILING` -- the whole serialised surface, schemas included. Description
   text is not the only per-request cost, and the consolidation moved a lot of
   contract detail INTO parameter descriptions; a ceiling on descriptions alone
   would no longer catch growth.

3. `REQUIRED_TOKENS` -- the behaviour/constraint/parameter/failure-mode tokens a
   future trimming pass must not delete. Checked against the tool's WHOLE
   surface (description + schema), because after the consolidation a parameter's
   contract legitimately lives in the parameter's own description.

4. `MOVED_TO_DOCS` -- the tokens the consolidation deliberately removed from the
   per-request surface, each asserted still present in docs/AGENT_SURFACES.md.
   This turns "we dropped it" into "we relocated it, and here is the check".

5. The shared queued-result contract, present on every tool that can queue.

`<example>` / `<commentary>` blocks are banned outright: they are agent-catalog
formatting, and in a tool description they are pure per-request cost.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from amplifier_module_tool_browser_bridge import _build_tools

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_SURFACES = REPO_ROOT / "docs" / "AGENT_SURFACES.md"

# Per-tool description ceilings, recorded 2026-09-07 after the consolidation.
# Shrinking is always allowed; growing past a ceiling is not.
CEILING: dict[str, int] = {
    "browser_admin": 1530,
    "browser_archive": 1720,
    "browser_capture": 1790,
    "browser_devices": 485,
    "browser_download": 965,
    "browser_page": 1520,
    "browser_tabs": 1100,
}

TOTAL_CEILING = sum(CEILING.values())

# The whole serialised surface -- name + description + input_schema for all seven
# tools, exactly as an provider sees it. 47,248 before the consolidation.
WIRE_CEILING = 20_000

# Behaviour, constraints, parameter semantics and failure modes that must not be
# trimmed away. Checked as substrings of description + serialised schema.
REQUIRED_TOKENS: dict[str, tuple[str, ...]] = {
    "browser_devices": ("tier", "capabilities", "queued", "pending", "queue_position", "command_id"),
    "browser_tabs": (
        "summary",
        "limit",
        "offset",
        "has_more",
        "matched",
        "window_id",
        "url_contains",
        "title_contains",
        "active",
        "discarded",
    ),
    "browser_page": (
        "ref",
        "generation",
        "stale ref",
        "wake",
        "activate",
        "woke",
        "activated",
        "selector",
        "timeout_ms",
        "session_id",
    ),
    "browser_capture": (
        "capture_hidden",
        "debugger",
        "frame_id",
        "multi_page",
        "max_pages",
        "scroll_selector",
        "page_delay_ms",
        "Android",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "AMPLIFIER_BROWSER_BRIDGE_VISION_PROVIDER",
        "vision_provider",
        "stopped_reason",
        "inspect or transcribe",
        "process running this tool",
        "not necessarily the hub host",
        "max_bytes",
        "25MB",
        "byte_length",
    ),
    "browser_download": (
        "max_download_id",
        "since_id",
        "download_id",
        "pattern",
        "timeout_ms",
        "byte_length",
        "chrome.downloads.search",
        "chrome.downloads.download",
    ),
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
        "archive_dir",
        "debugger",
        "ok_with_skips",
        "ok_with_failures",
        "tabs_inventoried",
        "tabs_partial",
        "tabs_not_found",
        "not_found",
        "partial",
        "skipped",
        "catalog",
        "lens",
        "concurrency",
        "top_n",
    ),
    "browser_admin": (
        "install_service",
        "force_token",
        "result.pairing.pair_url",
        "result.setup_url",
        "hub_reachable",
        "manual_hub_command",
        "Tailscale",
        "127.0.0.1",
        "doctor",
        "reconnect_timeout_s",
        "guided",
        "download_url",
        "session_id",
        "on_unknown",
        "redeem",
        "unattended",
        "SEAL",
        "chrome.runtime.reload()",
    ),
}

# Detail the consolidation deliberately took OFF the per-request surface -- all of
# it belonging to the five operations nobody has ever called. Each token must
# still be findable in docs/AGENT_SURFACES.md, so this is a relocation with a
# check on it, not a silent loss.
MOVED_TO_DOCS: tuple[str, ...] = (
    "page.mhtml",
    "page.extracted.md",
    "page.full_page.md",
    "not_captured",
    "tables_with_merged_cells",
    "tabs.json",
    "catalog.json",
    "no_content",
    "why_kept",
    "already_current",
    "/setup/extension.zip",
)

# The shared non-live/queued contract. Four device-addressing tools carry it;
# before the consolidation the same text was repeated on 20.
QUEUE_NOTE_TOOLS = frozenset({"browser_tabs", "browser_page", "browser_capture", "browser_download"})


def _descriptions() -> dict[str, str]:
    return {t.name: t.description for t in _build_tools()}


def _surfaces() -> dict[str, str]:
    """Description + serialised schema -- everything the provider actually sees."""
    return {t.name: t.description + "\n" + json.dumps(t.input_schema) for t in _build_tools()}


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


def test_the_whole_serialised_surface_stays_under_the_wire_ceiling():
    """Descriptions are not the only per-request cost -- schemas ship too."""
    blocks = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in _build_tools()
    ]
    wire = len(json.dumps(blocks))
    assert wire <= WIRE_CEILING, (
        f"serialised tool surface {wire} > {WIRE_CEILING} chars. It was 47,248 before the "
        "consolidation; do not let it grow back through parameter descriptions."
    )


def test_no_example_or_commentary_blocks_anywhere():
    """<example>/<commentary> are agent-catalog formatting; in a tool description
    they are pure per-request cost with no caller value."""
    banned = re.compile(r"</?(example|commentary)\b", re.IGNORECASE)
    offenders = [name for name, desc in _descriptions().items() if banned.search(desc)]
    assert not offenders, f"{offenders} contain <example>/<commentary> blocks"


def test_every_description_leads_with_a_trigger_not_a_preamble():
    """Trigger-first: the header says what the tool is for, in one line -- never
    'This tool ...' / 'You can use this to ...' boilerplate."""
    preamble = re.compile(r"^(this tool|this command|you can|use this tool|the purpose of)", re.IGNORECASE)
    for name, desc in _descriptions().items():
        assert desc == desc.strip(), f"{name} description has leading/trailing whitespace"
        assert not preamble.match(desc), f"{name} opens with preamble, not a trigger: {desc[:60]!r}"
        header = desc.split("\n- ", 1)[0]
        assert len(header) <= 300, (
            f"{name}'s header is {len(header)} chars -- lead with a trigger line, then the ops"
        )


def test_required_semantics_survive_every_future_trim():
    """The fidelity gate, pinned: a leaner surface may not silently drop a
    parameter name, response field or failure-mode token a caller needs."""
    surfaces = _surfaces()
    missing: dict[str, list[str]] = {}
    for name, tokens in REQUIRED_TOKENS.items():
        assert name in surfaces, f"REQUIRED_TOKENS names unknown tool {name}"
        gone = [t for t in tokens if t.lower() not in surfaces[name].lower()]
        if gone:
            missing[name] = gone
    assert not missing, f"tool surface lost required semantics: {missing}"


def test_every_tool_has_a_required_token_list():
    """No tool gets to be exempt from the fidelity gate."""
    assert set(_descriptions()) == set(REQUIRED_TOKENS)


def test_capture_surface_distinguishes_native_pixels_from_external_visual_analysis():
    """Pin the native capture guidance to the built tool surface, not guide prose."""
    capture = next(tool for tool in _build_tools() if tool.name == "browser_capture")
    screenshot_line = next(
        (line for line in capture.description.splitlines() if line.startswith("- screenshot:")), None
    )
    vision_line = next(
        (line for line in capture.description.splitlines() if line.startswith("- vision_read:")), None
    )
    assert screenshot_line is not None, "browser_capture has no '- screenshot:' operation line"
    assert vision_line is not None, "browser_capture has no '- vision_read:' operation line"

    for text in ("pixel data only", "base64 TEXT", "not an image attachment", "vision_read", "visual QA"):
        assert text in screenshot_line
    for text in ("visual QA/interpretation", "OCR", "separate external vision-model call"):
        assert text in vision_line


def test_detail_taken_off_the_wire_is_findable_in_the_docs():
    """The consolidation relocated this detail; it did not delete it."""
    assert AGENT_SURFACES.exists(), f"{AGENT_SURFACES} is missing"
    doc = AGENT_SURFACES.read_text(encoding="utf-8")
    gone = [token for token in MOVED_TO_DOCS if token not in doc]
    assert not gone, (
        f"{gone} were trimmed off the per-request tool surface but are not in "
        f"{AGENT_SURFACES.name} either -- that is a silent loss, not a relocation."
    )


def test_queued_contract_present_on_every_tool_that_can_queue():
    """Every device-addressed tool states the non-live/queued pass-through in its
    OWN description -- a caller typically sees one description in isolation."""
    descs = _descriptions()
    for name in QUEUE_NOTE_TOOLS:
        desc = descs[name]
        assert "queued" in desc, f"{name} lost the queued note"
        assert "browser_devices" in desc, f"{name} lost the poll remedy"
        assert "not an error" in desc, f"{name} lost 'not an error' -- the whole point of the note"
    for name in set(descs) - QUEUE_NOTE_TOOLS:
        if name == "browser_devices":
            continue
        assert 'browser_devices(operation="poll")' not in descs[name], (
            f"{name} carries the queued note but is not listed in QUEUE_NOTE_TOOLS"
        )
