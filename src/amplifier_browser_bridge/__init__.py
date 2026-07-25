"""Amplifier Browser Bridge -- cross-device agent <-> real-browser bridge (Edge, over Tailscale).

See docs/designs/browser-bridge.md for the architecture and its evidence base, and
docs/PROTOCOL.md for the wire protocol this package implements.

Public API (the "single home" for all logic -- CLI, and later MCP server and Amplifier
tool module, are thin adapters over this):

    Target, parse_target   -- addressing (device/window/tab -> element_ref)
    Tier                    -- the three-tier connectivity model
    Hub                     -- the hub server (device registry, queue, routing, audit)
    HubClient, HubError     -- the agent-side client used to talk to a running hub
    TokenStore, load_token_store, AuditLog -- supporting hub infrastructure
"""

from .addressing import Target, TargetError, parse_target
from .audit import AuditLog
from .auth import TokenStore, load_token_store
from .client import HubClient, HubError
from .hub import DEFAULT_PORT, Hub
from .protocol import COMMANDS, PROTOCOL_VERSION
from .tiers import Tier

__all__ = [
    "COMMANDS",
    "DEFAULT_PORT",
    "PROTOCOL_VERSION",
    "AuditLog",
    "Hub",
    "HubClient",
    "HubError",
    "Target",
    "TargetError",
    "Tier",
    "TokenStore",
    "load_token_store",
    "parse_target",
]
