"""Resolve the hub URL + token to bake into the Android CRX at pack time.

This is `scripts/package-android.sh`'s answer to a real dead end: on Edge Android,
`chrome.runtime.openOptionsPage()` (wired to both the toolbar click and
`onInstalled` in `background.js`) does nothing usable, and there is no way to type a
32-character extension ID by hand to reach `chrome-extension://<id>/options.html`
directly. Without some other channel, a fresh Android sideload has NO reachable path
to enter a hub URL/token at all. See `extension/bundled_config.mjs`'s own docstring
for the full rationale and the first-run-only adoption logic on the extension side.

This module is deliberately kept as ordinary, importable, unit-testable Python
(rather than embedded inline in the packaging bash script) -- the same judgment call
`extension_integrity.py` and `setup.py` already made for logic complex enough to
deserve a real test suite (IMPLEMENTATION_PHILOSOPHY.md's "necessity"/"directness"
questions). `tests/test_android_bake.py` is the proof.

Read-only with respect to the hub's own configuration
-------------------------------------------------------
This module calls `load_token_store` (`auth.py`) to read whatever token already
exists -- exactly what the hub itself reads at startup -- and NEVER calls
`ensure_token_file` (`setup.py`) or writes anything under
`~/.config/amplifier-browser-bridge/`. Generating/rotating a token is
`amplifier-browser-bridge init`'s job, not this script's; baking a config for Android
only ever reads what's already there, so the artifact and the hub always agree on
what the current token actually is.

Security note
--------------
The artifact this module's output gets packed into now carries a live credential.
See `SECURITY.md`'s "The Android build now embeds a live hub credential in the
artifact itself" section and `docs/ANDROID.md`'s "Zero-configuration builds"
section -- this is a disclosed, accepted trade-off under this project's stated trust
model, not an oversight.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .auth import load_token_store, mask_token, resolve_token_file
from .netinfo import detect_tailscale_ip

DEFAULT_BAKE_PORT = 8900

__all__ = [
    "DEFAULT_BAKE_PORT",
    "BakeConfigError",
    "BakedConfig",
    "build_bundled_config",
    "cli_main",
    "resolve_bake_hub_url",
    "resolve_bake_token",
    "write_bundled_config",
]


class BakeConfigError(RuntimeError):
    """Raised when a hub URL or token cannot be safely resolved for baking.

    Every raise site here is a "BUILD REFUSED" case -- see `cli_main`, which is the
    only caller that turns this into a process exit code, matching the
    `scripts/package.sh` convention of failing the build loudly rather than shipping
    something silently wrong.
    """


@dataclass
class BakedConfig:
    hub_url: str
    hub_token: str
    generated_at: str
    token_masked: str  # for print/log only -- never written to the JSON payload
    auth_disabled: bool


def resolve_bake_hub_url(hub_url: str | None, hub_host: str | None, hub_port: int) -> str:
    """Resolve the full `ws://host:port/device` URL to bake in.

    Resolution order, deliberately mirroring `cli.py`'s `init` command's own host
    resolution (same rationale: an explicit value always wins; auto-detect the
    machine's own Tailscale IP next; see `netinfo.py`):

        1. `hub_url` (an explicit, complete URL) -- used as-is if it looks like a
           real ws(s) URL.
        2. `hub_host` (an explicit host) combined with `hub_port` into
           `ws://<host>:<port>/device`.
        3. Auto-detected Tailscale IP (`netinfo.detect_tailscale_ip`) combined with
           `hub_port`.

    Unlike `init`, there is NO loopback (127.0.0.1) fallback here. `init`'s
    127.0.0.1 fallback is safe there because it's a same-machine dev loop the
    printed instructions clearly label as "NOT reachable from another device." A
    baked Android config that silently resolved to 127.0.0.1 would tell the PHONE
    to connect to itself -- not merely non-cross-device, actively and silently
    wrong, since the whole point of building this artifact is cross-device
    operation. Fail loud instead and name the fix.
    """
    if hub_url:
        if not hub_url.startswith(("ws://", "wss://")):
            raise BakeConfigError(f"--hub-url must start with ws:// or wss://, got: {hub_url!r}")
        return hub_url

    host = hub_host
    if not host:
        host = detect_tailscale_ip()
        if not host:
            raise BakeConfigError(
                "Could not auto-detect this machine's Tailscale IP (`tailscale ip -4` is "
                "unavailable or failed) and no --hub-host (or "
                "AMPLIFIER_BROWSER_BRIDGE_BAKE_HUB_HOST) was given. Unlike `amplifier-browser-bridge "
                "init` (which can safely fall back to 127.0.0.1 for a same-machine dev loop), a "
                "baked Android config resolving to 127.0.0.1 would tell the PHONE to connect to "
                "itself -- silently and permanently wrong, not merely non-cross-device. Pass "
                "--hub-host <this machine's tailnet IP> explicitly."
            )
    return f"ws://{host}:{hub_port}/device"


def resolve_bake_token(*, allow_no_token: bool, token_file: str | Path | None = None) -> tuple[str, bool]:
    """Returns `(token, auth_disabled)`.

    Read-only: uses `load_token_store` (`auth.py`), the exact same function+resolution
    order (`AMPLIFIER_BROWSER_BRIDGE_HUB_TOKEN` env var, then the token file) the hub
    itself uses -- never generates or writes a token (that's `ensure_token_file`/
    `amplifier-browser-bridge init`'s job, and doing so here would mean writing to the
    hub's own config, which this module must never do).
    """
    store = load_token_store(token_file)
    if store.default_token:
        return store.default_token, False
    if allow_no_token:
        return "", True
    resolved_path = resolve_token_file(token_file)
    raise BakeConfigError(
        f"No hub token found (checked AMPLIFIER_BROWSER_BRIDGE_HUB_TOKEN and {resolved_path}). "
        "Run `amplifier-browser-bridge init` first to provision one, set "
        "AMPLIFIER_BROWSER_BRIDGE_HUB_TOKEN, or pass --allow-no-token to build a dev-only "
        "artifact with auth disabled (loud, not a posture to distribute)."
    )


def build_bundled_config(
    *,
    hub_url: str | None = None,
    hub_host: str | None = None,
    hub_port: int = DEFAULT_BAKE_PORT,
    allow_no_token: bool = False,
    token_file: str | Path | None = None,
) -> BakedConfig:
    """Resolve everything `write_bundled_config` needs. Raises `BakeConfigError` (never
    guesses, never silently substitutes a wrong value) if the hub URL or token cannot
    be safely resolved -- see `resolve_bake_hub_url`/`resolve_bake_token`."""
    resolved_url = resolve_bake_hub_url(hub_url, hub_host, hub_port)
    token, auth_disabled = resolve_bake_token(allow_no_token=allow_no_token, token_file=token_file)
    generated_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return BakedConfig(
        hub_url=resolved_url,
        hub_token=token,
        generated_at=generated_at,
        token_masked=mask_token(token) if token else "(none -- auth disabled)",
        auth_disabled=auth_disabled,
    )


def write_bundled_config(stage_dir: str | Path, config: BakedConfig) -> Path:
    """Write `bundled_config.json` into `stage_dir` -- a pack-time STAGE directory
    only, e.g. `scripts/package-android.sh`'s temporary staging directory. NEVER call
    this against the tracked `extension/` source tree; this file must never be
    committed (see that script's own comments and `.gitignore`'s defensive entry).

    Restricts the written file's permissions (`chmod 600`) as defense in depth -- it
    carries a live credential in plaintext until it's packed into the CRX and the
    staging directory is cleaned up. Matches `setup.py`'s `ensure_token_file` posture
    for the same reason.
    """
    out_path = Path(stage_dir) / "bundled_config.json"
    payload = {
        "hubUrl": config.hub_url,
        "hubToken": config.hub_token,
        "generatedAt": config.generated_at,
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        out_path.chmod(0o600)
    except OSError:
        pass  # best-effort; not fatal -- matches ensure_token_file's own posture
    return out_path


def cli_main(argv: list[str] | None = None) -> int:
    """CLI entry point invoked by `scripts/package-android.sh` (via
    `python3 -c "... from amplifier_browser_bridge.android_bake import cli_main ..."`,
    the same `sys.path.insert(0, 'src')` convention `scripts/package.sh` already uses
    for its own inline Python steps -- see that script's own comments).

    Prints a "BUILD REFUSED -- ..." message and returns 1 on any `BakeConfigError`,
    matching `scripts/package.sh`'s existing build-gate convention. On success, prints
    the resolved (masked) token, hub URL, and output path, plus the security
    disclosure this change is required to surface at the build script's own output
    (see SECURITY.md).
    """
    parser = argparse.ArgumentParser(
        description="Bake a hub URL + token into bundled_config.json for the Android CRX build."
    )
    parser.add_argument("--stage-dir", required=True, type=Path)
    parser.add_argument("--hub-url", default=None, help="Full ws://host:port/device override.")
    parser.add_argument("--hub-host", default=None, help="Host only; combined with --hub-port.")
    parser.add_argument("--hub-port", type=int, default=DEFAULT_BAKE_PORT)
    parser.add_argument(
        "--allow-no-token",
        action="store_true",
        help="Build with auth disabled if no token is found, instead of refusing the build.",
    )
    parser.add_argument("--token-file", default=None)
    args = parser.parse_args(argv)

    try:
        config = build_bundled_config(
            hub_url=args.hub_url,
            hub_host=args.hub_host,
            hub_port=args.hub_port,
            allow_no_token=args.allow_no_token,
            token_file=args.token_file,
        )
    except BakeConfigError as e:
        print(f"BUILD REFUSED -- {e}", file=sys.stderr)
        return 1

    out_path = write_bundled_config(args.stage_dir, config)

    print(f"Baked hub URL: {config.hub_url}", file=sys.stderr)
    print(f"Baked token:   {config.token_masked}", file=sys.stderr)
    print(f"Written to:    {out_path}", file=sys.stderr)
    print(file=sys.stderr)
    if config.auth_disabled:
        print(
            "WARNING: --allow-no-token was set and no token was found -- this build has auth "
            "DISABLED baked in. Anyone who can reach the hub's bind address can connect as this "
            "device with no credential at all. Do not distribute this build outside a private dev "
            "loop.",
            file=sys.stderr,
        )
    else:
        print(
            "SECURITY NOTICE: this artifact now contains a live hub credential. Anyone who "
            "obtains this .crx (or the .bin it is temporarily served as during transfer to a "
            "phone) can connect to the hub as this device. This is the accepted trust model for "
            "this project's Android distribution -- see SECURITY.md's 'The Android build now "
            "embeds a live hub credential in the artifact itself' section and docs/ANDROID.md's "
            "'Zero-configuration builds' section. If this file is ever exposed unintentionally, "
            "rotate the token (`amplifier-browser-bridge init --force`) and rebuild.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(cli_main())
