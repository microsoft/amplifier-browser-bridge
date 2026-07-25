"""Per-device shared token authentication.

Tailscale ACLs are the documented outer boundary (design doc §4: deny-by-default,
pin which devices may reach the hub port at all). This token exists because tailnet
identity is per-*device*, not per-*application* -- without it, any other extension or
local process on an authorized device could reach the hub with the same identity.

Resolution order (first match wins), and NOTHING here is ever committed to the repo:

    1. `ABB_HUB_TOKEN` environment variable -- used as the default token for all
       devices/agents unless a device has its own entry in the token file.
    2. A JSON token file (`ABB_TOKEN_FILE`, default `~/.config/amplifier-browser-bridge/
       tokens.json`) of the shape `{"default": "...", "devices": {"<device_id>": "..."}}`.
    3. No token configured anywhere -> auth is DISABLED. This is a dev-only mode,
       loud about what it is: fine on a private tailnet during development, not a
       posture to ship. `Hub` logs a warning when it starts in this mode.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TOKEN_FILE = Path("~/.config/amplifier-browser-bridge/tokens.json")


@dataclass
class TokenStore:
    default_token: str | None = None
    device_tokens: dict[str, str] = field(default_factory=dict)

    @property
    def auth_enabled(self) -> bool:
        return self.default_token is not None or bool(self.device_tokens)

    def validate(self, token: str | None, device_id: str | None = None) -> bool:
        """True if `token` is acceptable for `device_id` (or as a general agent token
        when device_id is None). If no token is configured anywhere, auth is disabled
        and every request is accepted -- see module docstring."""
        if not self.auth_enabled:
            return True
        if device_id and device_id in self.device_tokens:
            return token == self.device_tokens[device_id]
        return token is not None and token == self.default_token


def load_token_store(path: str | Path | None = None) -> TokenStore:
    """Load a TokenStore from env + file, per the resolution order in the module
    docstring. Never raises on a missing file -- a missing file just means "no
    per-device overrides configured," which is a normal state."""
    default_token = os.environ.get("ABB_HUB_TOKEN")

    file_path = Path(path or os.environ.get("ABB_TOKEN_FILE") or DEFAULT_TOKEN_FILE).expanduser()
    device_tokens: dict[str, str] = {}
    if file_path.is_file():
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        if isinstance(data, dict):
            default_token = data.get("default", default_token)
            devices = data.get("devices")
            if isinstance(devices, dict):
                device_tokens = {str(k): str(v) for k, v in devices.items()}

    return TokenStore(default_token=default_token, device_tokens=device_tokens)
