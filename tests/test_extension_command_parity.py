"""Guards against exactly the drift class that shipped `describe` as dead code.

`protocol.py`'s `PAGE_WORLD_COMMANDS`/`COMMANDS` are the Python-side source of truth
for which commands get dispatched into `injected.js`'s page-world path, and for the
full command vocabulary this hub knows about; `extension/background.js` mirrors both
sets by hand in JS (CONTRIBUTING.md: "keep the two protocol implementations in sync by
hand ... there is no shared codegen in this phase").

`describe` was added to `protocol.py`'s `PAGE_WORLD_COMMANDS` and fully implemented in
`injected.js` (its `describe()` function and `dispatch()`'s `case "describe"`), but was
never added to `background.js`'s own copy of the set -- so `executeCommand()` fell
through every branch and returned `{ok: false, error: "unsupported command: describe"}`
for every real call, silently, in shipped code.

This test is the drift detector: it parses `background.js`'s own JS `Set` literal and
asserts it names exactly the same commands as `protocol.py`'s `PAGE_WORLD_COMMANDS`, so
a future edit to one without the other fails loud here instead of shipping silently
broken. Extension JS has no build step and no import from Python (full codegen is out
of scope, per CONTRIBUTING.md), but this cheap, text-based parity check is well worth
its complexity next to the alternative -- a dead command nobody notices until a real
device hits it.

The second parity test below guards `SUPPORTED_COMMANDS` -- the Tier 0 handshake set
`sendHello()` reports in `hello.commands` (docs/PROTOCOL.md's "hello" section,
skew.py). If this ever drifted from `protocol.py`'s `COMMANDS`, the hub's own skew
detection would report FALSE staleness (a real, implemented command missing from
`SUPPORTED_COMMANDS`) or FALSE currency (a command in `SUPPORTED_COMMANDS` that
`executeCommand()` doesn't actually implement) -- exactly the kind of silent drift
this whole feature exists to make impossible to miss.
"""

from __future__ import annotations

import re
from pathlib import Path

from amplifier_browser_bridge.protocol import COMMANDS, PAGE_WORLD_COMMANDS

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKGROUND_JS = _REPO_ROOT / "extension" / "background.js"

_SET_RE = re.compile(r"const PAGE_WORLD_COMMANDS = new Set\(\[(.*?)\]\);", re.DOTALL)
_SUPPORTED_SET_RE = re.compile(r"const SUPPORTED_COMMANDS = new Set\(\[(.*?)\]\);", re.DOTALL)
_STRING_RE = re.compile(r'"([^"]+)"')


def _extract_background_js_page_world_commands() -> set[str]:
    text = _BACKGROUND_JS.read_text(encoding="utf-8")
    match = _SET_RE.search(text)
    assert match is not None, "could not find `const PAGE_WORLD_COMMANDS = new Set([...])` in background.js"
    return set(_STRING_RE.findall(match.group(1)))


def _extract_background_js_supported_commands() -> set[str]:
    text = _BACKGROUND_JS.read_text(encoding="utf-8")
    match = _SUPPORTED_SET_RE.search(text)
    assert match is not None, "could not find `const SUPPORTED_COMMANDS = new Set([...])` in background.js"
    return set(_STRING_RE.findall(match.group(1)))


def test_background_js_declares_a_nonempty_page_world_commands_set() -> None:
    """Sanity-checks the extraction itself isn't silently matching nothing (which
    would make the parity test below vacuously -- and wrongly -- pass)."""
    assert len(_extract_background_js_page_world_commands()) > 0


def test_background_js_page_world_commands_matches_protocol_py() -> None:
    js_commands = _extract_background_js_page_world_commands()
    py_commands = set(PAGE_WORLD_COMMANDS)
    assert js_commands == py_commands, (
        "extension/background.js's PAGE_WORLD_COMMANDS Set has drifted from "
        "protocol.py's PAGE_WORLD_COMMANDS -- a command present in one but not the "
        "other is either dead in shipped code (present in protocol.py, missing from "
        "background.js -- exactly the `describe` bug this test guards against) or "
        "will hit background.js's `unsupported command` fallback despite protocol.py "
        "claiming to support it.\n"
        f"protocol.py only: {sorted(py_commands - js_commands)}\n"
        f"background.js only: {sorted(js_commands - py_commands)}"
    )


def test_background_js_declares_a_nonempty_supported_commands_set() -> None:
    """Same extraction-sanity guard as above, for the Tier 0 handshake set."""
    assert len(_extract_background_js_supported_commands()) > 0


def test_background_js_supported_commands_matches_protocol_py() -> None:
    js_commands = _extract_background_js_supported_commands()
    py_commands = set(COMMANDS)
    assert js_commands == py_commands, (
        "extension/background.js's SUPPORTED_COMMANDS Set (reported in hello.commands, "
        "the Tier 0 version-skew handshake) has drifted from protocol.py's COMMANDS. A "
        "command in protocol.py but missing here makes the hub wrongly believe every "
        "connected device is stale for it (skew.py reports device_behind for a command "
        "background.js may well implement); a command here but missing from protocol.py "
        "makes the hub wrongly believe IT is the one behind (skew.py reports "
        "hub_behind) for a command that was never real.\n"
        f"protocol.py only: {sorted(py_commands - js_commands)}\n"
        f"background.js only: {sorted(js_commands - py_commands)}"
    )
