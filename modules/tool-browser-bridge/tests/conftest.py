"""Shared test helper: reach a consolidated tool by its pre-consolidation name.

These tests were written against the 31 flat tools this module used to mount.
Rather than rewrite every one of them for the seven operation-enum tools, they
go through `LEGACY_TOOLS` -- which means each of them now doubles as a live
exercise of the compatibility table, on top of what it already asserted.

`legacy_tool("browser_click").execute({...})` resolves `browser_click` to
`("browser_page", "click")`, injects `operation="click"`, and calls the real
tool. Nothing is stubbed: the assertions downstream still see the real runner,
the real `Target`, and the real `ToolResult`.
"""

from __future__ import annotations

from typing import Any

from amplifier_core import ToolResult


class LegacyTool:
    """A pre-consolidation tool name, bound to its (tool, operation) pair."""

    def __init__(self, former_name: str, module: Any = None) -> None:
        if module is None:
            import amplifier_module_tool_browser_bridge as module

        self.former_name = former_name
        mapping = module.LEGACY_TOOLS.get(former_name)
        assert mapping is not None, f"{former_name!r} has no entry in LEGACY_TOOLS"
        tool_name, self.operation = mapping
        matches = [t for t in module._build_tools() if t.name == tool_name]
        assert len(matches) == 1, f"expected exactly one tool named {tool_name!r}, found {len(matches)}"
        self._tool = matches[0]

    @property
    def name(self) -> str:
        return self._tool.name

    @property
    def description(self) -> str:
        return self._tool.description

    @property
    def input_schema(self) -> dict[str, Any]:
        return self._tool.input_schema

    async def execute(self, input_data: dict[str, Any]) -> ToolResult:
        return await self._tool.execute({**input_data, "operation": self.operation})


def legacy_tool(former_name: str, module: Any = None) -> LegacyTool:
    return LegacyTool(former_name, module)
