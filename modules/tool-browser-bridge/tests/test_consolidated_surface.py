"""The consolidation gate: 7 operation-enum tools, and nothing lost on the way.

Before this, the module mounted 31 flat tools -- one per browser-bridge command.
Every one of those 31 name+description+schema blocks is serialised into the
request on EVERY request of EVERY session that mounts this bundle, called or
not. `android_inspector`/`ios_inspector`/`terminal_inspector` already established
the cheaper shape: one tool per subject area, an `operation` enum inside it,
shared parameters declared once.

Three things are pinned here, and each fails for a different reason:

1. `test_exactly_seven_tools_are_mounted` -- the count itself. This is the test
   that failed before the consolidation (it saw 31) and passes after.

2. `LEGACY_TOOLS` -- the compatibility table. Every one of the 31 former tool
   names maps to exactly one (new_tool, operation) pair, and every pair it names
   really exists in the built surface. A former name that maps nowhere is a
   silently dropped capability; this is the test that catches it.

3. Operation visibility -- every operation is NAMED IN THE TOOL DESCRIPTION, not
   hidden in code only. An operation an agent cannot see is an operation that
   does not exist.
"""

from __future__ import annotations

import json

import pytest

from amplifier_module_tool_browser_bridge import LEGACY_TOOLS, _build_tools

EXPECTED_TOOLS = {
    "browser_devices",
    "browser_tabs",
    "browser_page",
    "browser_capture",
    "browser_download",
    "browser_archive",
    "browser_admin",
}

# The 31 flat tools this module mounted before the consolidation. Recorded
# literally so the compatibility table is checked against a fixed historical
# list, not against itself.
FORMER_TOOL_NAMES = (
    "browser_devices",
    "browser_tabs",
    "browser_snapshot",
    "browser_read",
    "browser_click",
    "browser_type",
    "browser_key",
    "browser_scroll",
    "browser_navigate",
    "browser_tab_open",
    "browser_reload",
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
    "browser_poll",
    "browser_establish_session",
    "browser_narrow_scope",
    "browser_setup",
    "browser_setup_status",
    "browser_archive",
    "browser_archive_convert",
    "browser_archive_catalog",
    "browser_update_extension",
)


def test_exactly_seven_tools_are_mounted():
    """The consolidation itself: 7 operation-enum tools, not 31 flat ones."""
    names = [t.name for t in _build_tools()]
    assert len(names) == len(set(names)), f"duplicate tool names: {names}"
    assert set(names) == EXPECTED_TOOLS, (
        f"expected exactly the 7 consolidated tools, got {len(names)}: {sorted(names)}"
    )


def test_former_tool_names_are_exactly_the_thirty_one():
    """The historical list is 31 distinct names -- the figure this item quotes."""
    assert len(FORMER_TOOL_NAMES) == 31
    assert len(set(FORMER_TOOL_NAMES)) == 31


def test_every_former_tool_name_resolves_through_the_compatibility_table():
    """No capability is silently dropped: each of the 31 maps to one (tool, op)."""
    unmapped = [name for name in FORMER_TOOL_NAMES if name not in LEGACY_TOOLS]
    assert not unmapped, f"former tool names with no compatibility mapping: {unmapped}"

    stale = set(LEGACY_TOOLS) - set(FORMER_TOOL_NAMES)
    assert not stale, f"compatibility table names tools that never existed: {sorted(stale)}"

    tools = {t.name: t for t in _build_tools()}
    for former, mapping in LEGACY_TOOLS.items():
        assert isinstance(mapping, tuple) and len(mapping) == 2, (
            f"{former} must map to exactly one (tool, operation) pair, got {mapping!r}"
        )
        tool_name, op = mapping
        assert tool_name in tools, f"{former} -> unknown tool {tool_name!r}"
        ops = tools[tool_name].input_schema["properties"]["operation"]["enum"]
        assert op in ops, f"{former} -> {tool_name}(operation={op!r}) but that op does not exist: {ops}"


def test_the_mapping_is_a_function_not_a_guess():
    """Exactly one pair per former name, and every pair reachable."""
    pairs = list(LEGACY_TOOLS.values())
    assert len(pairs) == len(set(pairs)), (
        f"two former tools map to the same (tool, op) pair: {sorted(p for p in pairs if pairs.count(p) > 1)}"
    )
    reachable = {
        (t.name, op) for t in _build_tools() for op in t.input_schema["properties"]["operation"]["enum"]
    }
    assert set(pairs) == reachable, f"operations with no former name, or vice versa: {reachable ^ set(pairs)}"


def test_every_operation_is_named_in_its_tool_description():
    """An operation an agent cannot see in the description does not exist."""
    missing: dict[str, list[str]] = {}
    for tool in _build_tools():
        ops = tool.input_schema["properties"]["operation"]["enum"]
        gone = [op for op in ops if op not in tool.description]
        if gone:
            missing[tool.name] = gone
    assert not missing, f"operations hidden from the description: {missing}"


def test_every_tool_declares_operation_and_requires_it():
    for tool in _build_tools():
        props = tool.input_schema["properties"]
        assert "operation" in props, f"{tool.name} has no operation parameter"
        assert props["operation"]["enum"], f"{tool.name}'s operation enum is empty"
        assert "operation" in tool.input_schema.get("required", []), f"{tool.name} does not require operation"


def test_shared_target_parameters_are_declared_once_per_tool():
    """device_id/tab_id/window_id/timeout_s are declared once, not per operation."""
    for tool in _build_tools():
        blob = json.dumps(tool.input_schema)
        for shared in ("device_id", "tab_id", "window_id", "timeout_s"):
            assert blob.count(f'"{shared}":') <= 1, (
                f"{tool.name} declares {shared} more than once -- shared params go in one place"
            )


def test_the_five_never_called_operations_are_marked_rarely_used():
    """Kept, not deleted -- and honestly labelled so nobody reaches for them first."""
    tools = {t.name: t for t in _build_tools()}
    for former in (
        "browser_download",
        "browser_narrow_scope",
        "browser_archive_convert",
        "browser_archive_catalog",
        "browser_update_extension",
    ):
        tool_name, op = LEGACY_TOOLS[former]
        desc = tools[tool_name].description
        line = next((ln for ln in desc.splitlines() if ln.strip().startswith(f"- {op}:")), None)
        assert line is not None, f"{tool_name} has no '- {op}:' line for former {former}"
        assert "RARELY USED" in line.upper(), f"{tool_name}'s {op} line is not marked rarely used: {line}"


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOLS))
def test_each_operation_line_stays_within_the_per_line_budget(tool_name: str):
    """Per-op description lines <= ~160 chars. Contract detail goes below the list."""
    tool = next(t for t in _build_tools() if t.name == tool_name)
    over = [
        (ln[:40], len(ln)) for ln in tool.description.splitlines() if ln.startswith("- ") and len(ln) > 165
    ]
    assert not over, f"{tool_name} operation lines over budget: {over}"
