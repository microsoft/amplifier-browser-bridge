"""Where the hub lives -- decided once, persisted, read by every consumer.

## The bug this closes

`init` resolves the hub's host (auto-detected Tailscale IP, or an explicit
`--hub-host`) and prints commands built from it -- `service install --host X`,
the manual `hub --host X` fallback, the `pair` invocation that carries
`AMPLIFIER_BROWSER_BRIDGE_HUB_URL=ws://X:PORT/agent`. `service install` bakes
that same host into the systemd unit / launchd plist as an explicit
`ExecStart`/`ProgramArguments` argument.

But nothing durable records the DECISION itself. `~/.config/amplifier-browser-bridge/
tokens.json` has no host/port field (see auth.py) -- it was never meant to.
So every OTHER client of this project -- a bare `amplifier-browser-bridge devices`,
the MCP server, the Amplifier tool module -- has no way to read back where
`init` (or `service install`) decided the hub would be. Each one falls back,
independently, to a hardcoded `ws://127.0.0.1:8900/agent` -- which is wrong on
exactly the cross-device setups this project exists for, and wrong in exactly
the same way every time, because it is the same hardcoded literal repeated in
four places.

This module is the fix: ONE persisted fact (`{"host": ..., "port": ...}`),
written at the moment `init`/`service install` DECIDES where the hub lives,
read by every consumer's own `DEFAULT_HUB_URL` computation (cli.py,
mcp_server.py, the tool module) instead of each hardcoding the loopback
fallback independently. Fix the mechanism once; every printed command and
every client's default location can never drift out of agreement with each
other again -- by construction, not by someone remembering to update a
fourth call site next time.

## Resolution order (first match wins) -- `resolve_hub_url`

    1. `AMPLIFIER_BROWSER_BRIDGE_HUB_URL` (or an explicit CLI flag, which is
       checked by the caller BEFORE this module is ever consulted -- see
       cli.py's `doctor --hub-url`) -- always wins, exactly as before.
    2. The persisted hub location file written here.
    3. `ws://127.0.0.1:{DEFAULT_PORT}/agent` -- the last-resort, same-machine-
       only default this project has always had.

A stale persisted value is corrected the same way the decision was made in
the first place: re-run `amplifier-browser-bridge init --hub-host <ip>` or
`amplifier-browser-bridge service install --host <ip>` (both already
overwrite this file as part of resolving where the hub lives) -- never by
hand-editing JSON.

## `DEFAULT_PORT` lives here now, not in hub.py

Moved from hub.py so this module has no dependency on it (hub.py pulls in
aiohttp and the whole server-side stack -- mcp_server.py and the Amplifier
tool module both deliberately avoid that import to stay thin adapters).
hub.py re-exports it (`from .hub_location import DEFAULT_PORT`) so every
existing `from .hub import DEFAULT_PORT` import keeps working unchanged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 8900

DEFAULT_HUB_LOCATION_FILE = Path("~/.config/amplifier-browser-bridge/hub_location.json")


@dataclass(frozen=True, slots=True)
class HubLocation:
    host: str
    port: int

    def to_agent_url(self) -> str:
        return f"ws://{self.host}:{self.port}/agent"


def resolve_hub_location_file(path: str | Path | None = None) -> Path:
    """The exact path `read_hub_location`/`write_hub_location` consult, for
    callers (doctor.py) that need to DISPLAY it. Same resolution order as
    `auth.resolve_token_file`: explicit path, then
    `$AMPLIFIER_BROWSER_BRIDGE_HUB_LOCATION_FILE`, then the default -- so the
    path shown to a user can never silently disagree with the one actually
    read/written.
    """
    return Path(
        path or os.environ.get("AMPLIFIER_BROWSER_BRIDGE_HUB_LOCATION_FILE") or DEFAULT_HUB_LOCATION_FILE
    ).expanduser()


def read_hub_location(path: str | Path | None = None) -> HubLocation | None:
    """The persisted hub location, or `None` if nothing has ever been
    persisted (no file yet) or the file is unreadable/malformed -- never
    raises. A missing or corrupt file just means "nothing decided yet,"
    which is a normal state (falls through to the next item in
    `resolve_hub_url`'s resolution order), not an error.
    """
    file_path = resolve_hub_location_file(path)
    if not file_path.is_file():
        return None
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    host = data.get("host")
    port = data.get("port")
    if not isinstance(host, str) or not host:
        return None
    if not isinstance(port, int) or isinstance(port, bool):
        return None
    return HubLocation(host=host, port=port)


def write_hub_location(host: str, port: int, *, path: str | Path | None = None) -> Path:
    """Persist where the hub lives, at the moment that's decided (`init`,
    `service install`) -- see module docstring. Best-effort: a write failure
    (unwritable config dir, read-only filesystem) is not fatal to the
    command that's persisting it, since the resolved host/port it just
    computed is still printed and used exactly as before; only the
    convenience default for OTHER, later commands is lost. Callers that want
    to know whether the write actually happened can inspect the return
    value's existence themselves; this function raises nothing.

    Returns the path written (or that would have been written), same as
    `resolve_hub_location_file`, so a caller can log/report it either way.
    """
    file_path = resolve_hub_location_file(path)
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(json.dumps({"host": host, "port": port}, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass  # best-effort -- see docstring; the caller's own resolved host/port is unaffected
    return file_path


def resolve_hub_url(*, path: str | Path | None = None) -> str:
    """The default hub agent-route URL every consumer starts from, per the
    resolution order in the module docstring. This is exactly what each of
    cli.py, mcp_server.py, and the tool module used to compute independently
    as `os.environ.get("AMPLIFIER_BROWSER_BRIDGE_HUB_URL", "ws://127.0.0.1:8900/agent")`
    -- now a single function they all call instead, so they can never
    disagree with each other about what the loopback-fallback literal even
    is.

    An explicit CLI flag (e.g. `doctor --hub-url`) is resolved by the
    caller BEFORE reaching this function -- it is checked first in every
    caller (`hub_url or resolve_hub_url()`), so it always wins regardless of
    what's below.
    """
    env = os.environ.get("AMPLIFIER_BROWSER_BRIDGE_HUB_URL")
    if env:
        return env
    location = read_hub_location(path)
    if location is not None:
        return location.to_agent_url()
    return f"ws://127.0.0.1:{DEFAULT_PORT}/agent"


__all__ = [
    "DEFAULT_HUB_LOCATION_FILE",
    "DEFAULT_PORT",
    "HubLocation",
    "read_hub_location",
    "resolve_hub_location_file",
    "resolve_hub_url",
    "write_hub_location",
]
