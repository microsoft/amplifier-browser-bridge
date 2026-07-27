"""Detects environment variables still set under their pre-rename `ABB_*` names.

This project dropped the `abb`/`ABB_` acronym in favor of the spelled-out
`amplifier-browser-bridge`/`AMPLIFIER_BROWSER_BRIDGE_*` convention (see
MIGRATION.md). The old names are NOT read as a fallback -- per this project's
fail-loud discipline, a silent fallback here would mean someone runs for weeks
on a config they think they migrated. Instead, every entry point (the CLI, the
MCP server, the Amplifier tool module) calls `warn_legacy_env_vars()` once at
startup so a leftover `ABB_*` variable produces a legible, actionable message
instead of a confusing "empty value" error several layers downstream.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

# Every renamed variable, old name -> new name. Kept in one place so the CLI,
# MCP server, and Amplifier tool module entry points all check the identical
# set -- see this module's docstring for why duplication here would be a bug
# waiting to happen (one entry point's list silently drifting from another's).
RENAMED_ENV_VARS: dict[str, str] = {
    "ABB_HUB_URL": "AMPLIFIER_BROWSER_BRIDGE_HUB_URL",
    "ABB_TOKEN": "AMPLIFIER_BROWSER_BRIDGE_TOKEN",
    "ABB_TOKEN_FILE": "AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE",
    "ABB_HUB_TOKEN": "AMPLIFIER_BROWSER_BRIDGE_HUB_TOKEN",
    "ABB_AUDIT_LOG": "AMPLIFIER_BROWSER_BRIDGE_AUDIT_LOG",
    "ABB_EXTENSION_SRC": "AMPLIFIER_BROWSER_BRIDGE_EXTENSION_SRC",
    "ABB_VISION_PROVIDER": "AMPLIFIER_BROWSER_BRIDGE_VISION_PROVIDER",
    "ABB_VISION_MODEL": "AMPLIFIER_BROWSER_BRIDGE_VISION_MODEL",
    "ABB_POLICY_FILE": "AMPLIFIER_BROWSER_BRIDGE_POLICY_FILE",
    "ABB_MCP_TRANSPORT": "AMPLIFIER_BROWSER_BRIDGE_MCP_TRANSPORT",
    "ABB_ANDROID_SIGNING_KEY": "AMPLIFIER_BROWSER_BRIDGE_ANDROID_SIGNING_KEY",
}


def legacy_env_vars_present() -> dict[str, str]:
    """Old-name -> new-name pairs for every renamed variable that is set under
    its old name AND not already set under its new name (if both are set, the
    new one is honored and there is nothing to warn about)."""
    return {old: new for old, new in RENAMED_ENV_VARS.items() if old in os.environ and new not in os.environ}


def warn_legacy_env_vars(*, stream: TextIO = sys.stderr) -> None:
    """Print one line per legacy `ABB_*` variable still set in the environment,
    naming its replacement. The old value is never read as a fallback -- this
    is the fail-loud message doing its job, not a silent compatibility shim."""
    for old, new in legacy_env_vars_present().items():
        print(
            f"amplifier-browser-bridge: ${old} is set, but this project renamed it to "
            f"${new} (the old 'abb'/'ABB_' acronym was dropped -- see MIGRATION.md). "
            f"${old} is no longer read; set ${new} instead.",
            file=stream,
        )
