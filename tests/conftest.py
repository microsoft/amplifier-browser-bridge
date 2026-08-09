"""Shared, autouse test isolation.

Real bug caught while implementing the hub-location fix: `init`/`service
install` now persist a resolved host/port to `~/.config/amplifier-browser-bridge/
hub_location.json` (hub_location.py) at the moment they decide it -- but
several existing tests invoke those commands via `CliRunner` WITHOUT
patching that path (unlike the token file, which every such test already
redirects via an explicit `--token-file tmp_path/...`). Without this
fixture, running the test suite silently overwrites the REAL developer's
`~/.config/amplifier-browser-bridge/hub_location.json` -- confirmed by
running `pytest tests/` on a machine with a real hub configured and finding
its hub_location.json clobbered with a test's mock IP afterward.

This autouse, session-wide fixture points `AMPLIFIER_BROWSER_BRIDGE_HUB_LOCATION_FILE`
at a per-test tmp path for every single test, so no test -- present or
future -- can touch a real machine's persisted hub location by omission.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_hub_location_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMPLIFIER_BROWSER_BRIDGE_HUB_LOCATION_FILE", str(tmp_path / "hub_location.json"))
