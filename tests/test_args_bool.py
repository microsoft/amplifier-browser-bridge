"""Tests for the shared boolean-arg coercion helper (args_bool.py) -- the fix for a
real reported bug: `amplifier-browser-bridge cmd <target> screenshot --arg capture_hidden=true` sent the
STRING "true" (the CLI's `cmd` escape hatch always sends string args), but
`cdp.py`'s `requires_cdp()` checked `args.get("capture_hidden") is True`, which is
`False` for a string. The hub never escalated to CDP.

This enumerates every boolean arg in the Python side of the codebase and asserts
each accepts real bool True, the string "true", and the int 1 -- the three shapes a
caller can legitimately send (see args_bool.py's module docstring).
"""

from __future__ import annotations

import pytest

from amplifier_browser_bridge.args_bool import truthy
from amplifier_browser_bridge.cdp import requires_cdp

# ---------------------------------------------------------------------------
# truthy() itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, "true", "TRUE", " True ", 1, "1"])
def test_truthy_recognizes_every_true_ish_shape(value: object) -> None:
    assert truthy(value) is True


@pytest.mark.parametrize("value", [False, "false", "FALSE", 0, "0", None, "", "yes-ish-typo", 2, 2.0, "treu"])
def test_truthy_defaults_to_false_for_everything_else(value: object) -> None:
    assert truthy(value) is False


def test_truthy_does_not_raise_on_missing_arg() -> None:
    args: dict[str, object] = {}
    assert truthy(args.get("capture_hidden")) is False


# ---------------------------------------------------------------------------
# Every boolean arg in the Python side of the codebase, enumerated: each must
# accept True, "true", and 1 (the exact regression this bug class produces).
# ---------------------------------------------------------------------------

_BOOLEAN_ARG_CASES = [
    ("trusted", "click"),
    ("trusted", "type"),
    ("trusted", "key"),
    ("capture_hidden", "screenshot"),
]


@pytest.mark.parametrize("arg_name,command", _BOOLEAN_ARG_CASES)
@pytest.mark.parametrize("value", [True, "true", 1])
def test_requires_cdp_accepts_every_true_ish_shape(arg_name: str, command: str, value: object) -> None:
    assert requires_cdp(command, {arg_name: value}) is True


@pytest.mark.parametrize("arg_name,command", _BOOLEAN_ARG_CASES)
@pytest.mark.parametrize("value", [False, "false", 0, None])
def test_requires_cdp_rejects_every_false_ish_shape(arg_name: str, command: str, value: object) -> None:
    args = {} if value is None else {arg_name: value}
    assert requires_cdp(command, args) is False


def test_requires_cdp_string_true_regression() -> None:
    """The exact reported bug: CLI `--arg capture_hidden=true` sends a STRING.
    Before the fix, this returned False (a strict `is True` identity check) and the
    hub never escalated to CDP."""
    assert requires_cdp("screenshot", {"capture_hidden": "true"}) is True
    assert requires_cdp("click", {"trusted": "true"}) is True
