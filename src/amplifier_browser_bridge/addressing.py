"""Addressing: the single biggest structural fix over the reference implementation.

The reference implementation this project supersedes had one global "work tab" and no
`tabId` parameter on any command -- it was structurally incapable of multi-device,
multi-window, multi-tab, or selective-grant operation. Every command in this system
carries an explicit target:

    device_id / profile_id / window_id / tab_id  ->  element_ref

`profile_id` is best-effort: there is no clean Edge API for profile identity, and
`chrome.storage.local` is already scoped per-profile, so today `profile_id` and
`device_id` are equivalent in practice. It exists as a distinct field for
forward-compatibility, not because we can currently distinguish more than the
extension install identity. Documented honestly rather than implying fidelity we
don't have (the same discipline the design doc applies throughout).
"""

from __future__ import annotations

from dataclasses import dataclass, replace


class TargetError(ValueError):
    """Raised when a target string or dict cannot be parsed into a valid Target."""


@dataclass(frozen=True, slots=True)
class Target:
    """An explicit address for a command.

    `device_id` is always required. `window_id` and `tab_id` are the browser's own
    integer ids, passed through unmodified. `ref` is an element reference, stable
    only within the snapshot that produced it (resets on navigation).
    """

    device_id: str
    window_id: int | None = None
    tab_id: int | None = None
    ref: str | None = None

    def with_ref(self, ref: str) -> Target:
        return replace(self, ref=ref)

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {"device_id": self.device_id}
        if self.window_id is not None:
            d["window_id"] = self.window_id
        if self.tab_id is not None:
            d["tab_id"] = self.tab_id
        if self.ref is not None:
            d["ref"] = self.ref
        return d

    @staticmethod
    def from_dict(d: dict[str, object]) -> Target:
        device_id = d.get("device_id")
        if not device_id or not isinstance(device_id, str):
            raise TargetError("target dict missing required 'device_id' (string)")
        window_id = d.get("window_id")
        tab_id = d.get("tab_id")
        ref = d.get("ref")
        if window_id is not None and not isinstance(window_id, int):
            raise TargetError(f"target.window_id must be an integer, got: {window_id!r}")
        if tab_id is not None and not isinstance(tab_id, int):
            raise TargetError(f"target.tab_id must be an integer, got: {tab_id!r}")
        if ref is not None and not isinstance(ref, str):
            raise TargetError(f"target.ref must be a string, got: {ref!r}")
        return Target(device_id=device_id, window_id=window_id, tab_id=tab_id, ref=ref)


def parse_target(s: str) -> Target:
    """Parse a CLI-friendly target string.

    Forms (an optional trailing ``#ref`` is accepted on any of them):

        device_id                    -- device-level only (e.g. `tabs`, `devices`)
        device_id/tab_id             -- the common case: one browser tab
        device_id/window_id/tab_id   -- fully qualified (disambiguates same tab_id
                                         reused across windows, which some browsers do)
        device_id/tab_id#ref         -- also carries a resolved element ref

    `tab_id` and `window_id` must parse as integers (the browser's own ids). Raises
    TargetError with a specific, actionable message on anything else -- addressing
    is the load-bearing contract here, so we fail loud rather than guess.
    """
    if not s or not s.strip():
        raise TargetError("empty target string")

    ref: str | None = None
    if "#" in s:
        s, ref = s.split("#", 1)
        if not ref:
            raise TargetError(f"empty ref after '#' in target: {s!r}")

    parts = s.split("/")
    if len(parts) > 3:
        raise TargetError(
            f"target has too many '/'-separated parts (max 3: device_id/window_id/tab_id): {s!r}"
        )

    device_id = parts[0]
    if not device_id:
        raise TargetError(f"target is missing device_id: {s!r}")

    window_id: int | None = None
    tab_id: int | None = None

    if len(parts) == 2:
        tab_id = _parse_int(parts[1], "tab_id", s)
    elif len(parts) == 3:
        window_id = _parse_int(parts[1], "window_id", s)
        tab_id = _parse_int(parts[2], "tab_id", s)

    return Target(device_id=device_id, window_id=window_id, tab_id=tab_id, ref=ref)


def _parse_int(raw: str, field: str, original: str) -> int:
    try:
        return int(raw)
    except ValueError as e:
        raise TargetError(f"target.{field} must be an integer, got {raw!r} in: {original!r}") from e
