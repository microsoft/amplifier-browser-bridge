"""Addressing is the load-bearing contract of this whole system (design doc §6.1) --
these tests are deliberately thorough."""

from __future__ import annotations

import pytest

from amplifier_browser_bridge.addressing import Target, TargetError, parse_target


def test_device_only() -> None:
    t = parse_target("edge-macos-1")
    assert t == Target(device_id="edge-macos-1")


def test_device_and_tab() -> None:
    t = parse_target("edge-macos-1/42")
    assert t.device_id == "edge-macos-1"
    assert t.tab_id == 42
    assert t.window_id is None


def test_device_window_and_tab() -> None:
    t = parse_target("edge-macos-1/7/42")
    assert t.device_id == "edge-macos-1"
    assert t.window_id == 7
    assert t.tab_id == 42


def test_trailing_ref() -> None:
    t = parse_target("edge-macos-1/42#e3")
    assert t.tab_id == 42
    assert t.ref == "e3"


def test_device_only_with_ref() -> None:
    t = parse_target("edge-macos-1#e9")
    assert t.device_id == "edge-macos-1"
    assert t.tab_id is None
    assert t.ref == "e9"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "/42",  # missing device_id
        "device/notanint",
        "device/1/notanint",
        "device/1/2/3",  # too many parts
        "device/42#",  # empty ref
    ],
)
def test_invalid_targets_raise(bad: str) -> None:
    with pytest.raises(TargetError):
        parse_target(bad)


def test_two_tabs_produce_distinct_targets() -> None:
    """The exact scenario the reference implementation could not express: two tabs on
    the same device must produce genuinely distinct, non-colliding addresses."""
    tab_a = parse_target("edge-macos-1/10")
    tab_b = parse_target("edge-macos-1/11")
    assert tab_a != tab_b
    assert tab_a.tab_id != tab_b.tab_id
    assert tab_a.device_id == tab_b.device_id


def test_to_dict_omits_unset_fields() -> None:
    t = Target(device_id="d1")
    assert t.to_dict() == {"device_id": "d1"}


def test_to_dict_from_dict_round_trip() -> None:
    t = Target(device_id="d1", window_id=2, tab_id=3, ref="e1")
    assert Target.from_dict(t.to_dict()) == t


def test_from_dict_rejects_missing_device_id() -> None:
    with pytest.raises(TargetError):
        Target.from_dict({"tab_id": 1})


def test_from_dict_rejects_non_integer_tab_id() -> None:
    with pytest.raises(TargetError):
        Target.from_dict({"device_id": "d1", "tab_id": "not-an-int"})


def test_with_ref_is_immutable() -> None:
    original = Target(device_id="d1", tab_id=5)
    updated = original.with_ref("e1")
    assert original.ref is None  # unchanged
    assert updated.ref == "e1"
    assert updated.device_id == original.device_id
