"""Per-tool description bytes + serialized wire bytes for this repo's 31 tools.

Run from the repo root:  uv run python docs/lanes/hd-browser-bridge/tools/census.py
Compare against the merge-base with:  git stash && <rerun> && git stash pop
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "modules" / "tool-browser-bridge"))
sys.path.insert(0, str(ROOT / "src"))

from amplifier_module_tool_browser_bridge import _build_tools

tools = _build_tools()
for t in tools:
    print(f"{len(t.description):6d}  {t.name}")

blocks = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]
print()
print(f"tools                : {len(tools)}")
print(f"description chars    : {sum(len(t.description) for t in tools)}")
print(f"input_schema chars   : {sum(len(json.dumps(t.input_schema)) for t in tools)}")
print(f"name chars           : {sum(len(t.name) for t in tools)}")
print(f"wire, per-tool sum   : {sum(len(json.dumps(b)) for b in blocks)}")
print(f"wire, single array   : {len(json.dumps(blocks))}")
