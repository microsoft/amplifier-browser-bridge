# Browser Bridge bundle

Adds the `tool-browser-bridge` Amplifier tool module to a session, giving an
Amplifier agent the `browser_*` tools described in `docs/AGENT_SURFACES.md`: drive
the user's real, logged-in Microsoft Edge browser on other devices, over the
user's own Tailscale network (see `docs/designs/browser-bridge.md`).

This bundle only adds the tool module -- it assumes a hub (`amplifier-browser-bridge hub`, see the
repo README) is already running somewhere reachable, and that at least one Edge
extension has connected to it. The tools work with zero connected devices too
(`browser_devices` just returns an empty list), so adding this bundle is safe even
before any device is set up.

## Composing this bundle

Reference the tool module's `source:` the same way any other Amplifier tool
module is referenced -- from a published location once this repo has one:

```yaml
tools:
  - module: tool-browser-bridge
    source: git+https://github.com/microsoft/amplifier-browser-bridge@main#subdirectory=modules/tool-browser-bridge
```

For local development against a checkout of this repo:

```yaml
tools:
  - module: tool-browser-bridge
    source: ./modules/tool-browser-bridge
```

## Configuration

The tool module reads the same two environment variables the CLI and MCP server
use (set them in the environment the Amplifier session runs in, not in this
file):

| Variable | Default | Purpose |
|---|---|---|
| `AMPLIFIER_BROWSER_BRIDGE_HUB_URL` | `ws://127.0.0.1:8900/agent` | The hub's agent-route WebSocket URL. Point this at the hub's tailnet IP (never a MagicDNS name -- see design doc section 4). |
| `AMPLIFIER_BROWSER_BRIDGE_TOKEN` | unset | Per-device/agent shared token, if the hub has auth enabled (see `auth.py`). |

## What this bundle provides

Sixteen tools: `browser_devices`, `browser_tabs`, `browser_snapshot`,
`browser_read`, `browser_click`, `browser_type`, `browser_key`, `browser_scroll`,
`browser_navigate`, `browser_tab_open`, `browser_tab_close`,
`browser_tab_activate`, `browser_screenshot`, `browser_wait_for`,
`browser_wait_text`, `browser_poll`. See `docs/AGENT_SURFACES.md` for the full
vocabulary, the addressing model (`device_id` -> `tab_id` -> `ref`), and the
three-tier connectivity model every tool honors (`live` executes immediately;
`intermittent`/`dormant` return a `{"status": "queued", ...}` result instead of
blocking).

## What this bundle deliberately does NOT add

Policy/consent gating (denylist categories, irreversible-action confirmation) is
a separate later phase (design doc section 10, step 5) and lives in the hub, not
in this tool module -- see `policy.py`/`hub.py` once that phase lands. This
bundle exposes exactly the capabilities the hub currently permits.
