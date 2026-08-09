"""Per-device shared token authentication.

Tailscale ACLs are the documented outer boundary (design doc §4: deny-by-default,
pin which devices may reach the hub port at all). This token exists because tailnet
identity is per-*device*, not per-*application* -- without it, any other extension or
local process on an authorized device could reach the hub with the same identity.

Resolution order (first match wins), and NOTHING here is ever committed to the repo:

    1. `AMPLIFIER_BROWSER_BRIDGE_HUB_TOKEN` environment variable -- used as the default token for all
       devices/agents unless a device has its own entry in the token file.
    2. A JSON token file (`AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE`, default `~/.config/amplifier-browser-bridge/
       tokens.json`) of the shape `{"default": "...", "devices": {"<device_id>": "..."}}`.
    3. No token configured anywhere -> auth is DISABLED. This is a dev-only mode,
       loud about what it is: fine on a private tailnet during development, not a
       posture to ship. `Hub` logs a warning when it starts in this mode.
"""

from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TOKEN_FILE = Path("~/.config/amplifier-browser-bridge/tokens.json")


def _tokens_equal(a: str | None, b: str | None) -> bool:
    """Constant-time token comparison (security review finding: `==` on a
    secret token leaks timing information proportional to the length of the
    matching prefix -- classic timing side-channel on an auth check reachable
    over the network). `hmac.compare_digest` is the stdlib's own answer to
    exactly this problem (it's what it exists for) -- see IMPLEMENTATION_PHILOSOPHY.md's
    library-vs-custom-code judgment: this is a solved, security-sensitive
    primitive, not something to hand-rebuild.

    `compare_digest` requires both arguments be the same type (str or
    bytes); `None` is normalized to `""` first so a missing token never
    raises, it just never matches (a `None`/`""` token was never going to
    validate against a real secret anyway).
    """
    return hmac.compare_digest(a or "", b or "")


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
        and every request is accepted -- see module docstring.

        Comparison is constant-time (`_tokens_equal`, `hmac.compare_digest`)
        -- see that function's docstring for why `==` was a real finding here,
        not a style nit: this check is reachable directly over the network on
        every device `hello` and every agent request.
        """
        if not self.auth_enabled:
            return True
        if device_id and device_id in self.device_tokens:
            return _tokens_equal(token, self.device_tokens[device_id])
        return token is not None and _tokens_equal(token, self.default_token)


def load_token_store(path: str | Path | None = None) -> TokenStore:
    """Load a TokenStore from env + file, per the resolution order in the module
    docstring. Never raises on a missing file -- a missing file just means "no
    per-device overrides configured," which is a normal state."""
    default_token = os.environ.get("AMPLIFIER_BROWSER_BRIDGE_HUB_TOKEN")

    file_path = Path(
        path or os.environ.get("AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE") or DEFAULT_TOKEN_FILE
    ).expanduser()
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


def resolve_token_file(path: str | Path | None = None) -> Path:
    """The exact token-file path `load_token_store` would read from, for callers
    (doctor.py, cli.py) that need to DISPLAY it -- must use the identical resolution
    order (explicit path, then $AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE, then the default) or the path shown
    to a user can silently disagree with the one actually consulted."""
    return Path(
        path or os.environ.get("AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE") or DEFAULT_TOKEN_FILE
    ).expanduser()


def find_sibling_token_files(active_path: str | Path) -> list[Path]:
    """Other files in the same directory as the active token file whose name
    suggests they might ALSO be a token store (contains "token", case-insensitive).

    This is the concrete failure mode `amplifier-browser-bridge doctor`/`amplifier-browser-bridge init` guard against: a stray
    file -- hand-created, left over from before this project settled on
    `tokens.json`, or copied from somewhere else entirely -- sitting unconsulted
    beside the one `amplifier-browser-bridge init`/`amplifier-browser-bridge hub`/`amplifier-browser-bridge doctor` actually read from. There is no
    other supported token-file name in this project; any match here is, by
    definition, not part of the active configuration.
    """
    active = Path(active_path).expanduser().resolve()
    parent = active.parent
    if not parent.is_dir():
        return []
    candidates = []
    for entry in parent.iterdir():
        if not entry.is_file():
            continue
        if entry.resolve() == active:
            continue
        if "token" in entry.name.lower():
            candidates.append(entry)
    return sorted(candidates)


def extract_token_value(path: str | Path) -> str | None:
    """Best-effort extraction of a single token-like value from a candidate sibling
    file, for comparison only -- never raises on unreadable/malformed content.
    Handles both the JSON shape this project's own token files use
    (`{"default": "...", "devices": {...}}`) and a bare single-token text file (the
    shape a hand-created file is likely to have)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        first_line = text.splitlines()[0].strip()
        return first_line or None
    if isinstance(data, dict):
        default = data.get("default")
        return default if isinstance(default, str) else None
    if isinstance(data, str):
        return data
    return None


def mask_token(token: str) -> str:
    """Truncate a token for display -- doctor/init output should never print a full
    secret to a terminal (which may be logged, screen-shared, or scrolled back)."""
    return f"{token[:8]}..." if len(token) > 8 else "***"


def persist_device_token(path: str | Path, device_id: str, token: str) -> None:
    """Add or update one `devices[device_id]` entry in the token file at `path`,
    preserving everything else already in it (the `default` token, every OTHER
    device's token). Used by the pairing flow (`pairing.py`/`hub.py`) to make a
    freshly-minted per-device token durable across a hub restart -- the ticket
    itself is intentionally never persisted (see pairing.py's module docstring),
    but the real token it produces follows the exact same on-disk shape and
    permissions discipline as every other token this project writes.

    Creates the file (with `default: null` -- i.e. no shared token, only this one
    device) if it does not exist yet. Best-effort chmod 0600, matching
    `setup.py`'s `ensure_token_file` (some filesystems/platforms don't support
    it; not fatal either way).
    """
    file_path = Path(path).expanduser()
    file_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, object] = {"default": None, "devices": {}}
    if file_path.is_file():
        try:
            loaded = json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            loaded = None
        if isinstance(loaded, dict):
            data = loaded

    devices = data.get("devices")
    if not isinstance(devices, dict):
        devices = {}
    devices[device_id] = token
    data["devices"] = devices

    file_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        file_path.chmod(0o600)
    except OSError:
        pass  # best-effort; not fatal (e.g. some filesystems/platforms don't support chmod)
