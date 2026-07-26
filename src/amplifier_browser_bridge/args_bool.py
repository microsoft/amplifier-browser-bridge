"""Tolerant boolean coercion for command args that cross process/language boundaries.

A caller-supplied "boolean" arg (`trusted`, `capture_hidden`, `all_frames`, `wake`, ...)
can arrive as three different native types depending on which surface sent it:

  - The CLI's `cmd` escape hatch (`abb cmd <target> screenshot --arg capture_hidden=true`)
    parses EVERY `--arg key=value` as a plain Python STRING -- `args["capture_hidden"]`
    is the string ``"true"``, never the bool ``True``.
  - The MCP server / Amplifier tool module pass a real Python `bool` from their own
    typed parameters (FastMCP signatures / JSON-schema `type: boolean`).
  - A caller scripting the wire protocol directly (or a JSON-only client) could send
    a bare `1`/`0`.

A strict `value is True` identity check silently treats the first and third cases as
`False` -- the caller supplied unambiguous intent, and the check simply doesn't
recognize the shape it arrived in. This is the exact bug behind a real, reported
failure: ``abb cmd <target> screenshot --arg capture_hidden=true`` sent the string
``"true"``; `cdp.py`'s `requires_cdp()` checked ``args.get("capture_hidden") is True``,
which is `False` for a string; the hub never escalated to CDP, and the device's
`screenshot()` failed loud with "requires the target tab to already be active" --
even though the caller passed exactly the flag that should have prevented that.

`truthy()` is the single, shared coercion this codebase uses everywhere a
caller-supplied arg is interpreted as boolean intent -- see docs/PROTOCOL.md's
"Boolean argument coercion" section for the full list of call sites, and
`extension/args_bool.mjs` for the JS-side twin (kept in sync manually, same
discipline as protocol.py/background.js). Absence (`None`/missing) is never treated
as true -- each call site continues to apply its own default via `args.get(key,
default)`; `truthy(None)` is simply `False`.
"""

from __future__ import annotations

from typing import Any

# Recognized true-ish string forms, case-insensitive, surrounding whitespace ignored.
# Deliberately small and explicit rather than "anything non-empty is true" -- a typo'd
# value (e.g. "flase") should not silently do nothing while looking like it worked.
_TRUE_STRINGS = frozenset({"true", "1"})


def truthy(value: Any) -> bool:
    """Coerce a caller-supplied arg value to a bool, tolerant of the shapes it can
    arrive in (real bool, numeric 1, or the strings "true"/"1", case-insensitive).

    Anything else -- including `None`, missing, `"false"`, `0`, or an unrecognized
    string -- is `False`. This mirrors the JS-side `truthy()` in
    `extension/args_bool.mjs` exactly (same recognized values, same default).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):  # bool is checked above, so this is a real int now
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_STRINGS
    return False
