---
bundle:
  name: browser-bridge
  version: 0.1.0
  description: >-
    Cross-device control of the user's real, logged-in Microsoft Edge browser
    (desktop and Android) over the user's own Tailscale network. Adds 25
    browser_* Amplifier tools wrapping amplifier-browser-bridge's HubClient --
    the agent becomes a second operator sharing a live browsing session, not
    a robot driving a disposable one.

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main
  - bundle: browser-bridge:behaviors/browser-bridge
---

# Browser Bridge bundle

This is the real, loadable Amplifier bundle for this repo. It composes
foundation (base tools, session config, agents) plus this repo's own
[`behaviors/browser-bridge.yaml`](behaviors/browser-bridge.yaml), which adds:

- the `tool-browser-bridge` Amplifier tool module (`modules/tool-browser-bridge/`) --
  **25** `browser_*` tools, one native Amplifier tool per browser-bridge command
- [`context/awareness.md`](context/awareness.md), a thin always-on context file
  covering the addressing model, the three-tier connectivity model, and the
  queued-result hazard every `browser_*` caller must know about (see
  "What this bundle provides" below)

This bundle assumes a hub (`amplifier-browser-bridge hub`, see this repo's
[README](README.md)) is already running somewhere reachable, and that at
least one Edge extension has connected to it. The tools work with zero
connected devices too (`browser_devices` just returns an empty list), so
composing this bundle is safe even before any device is set up.

## Composing this bundle

**Reference the whole published bundle** (recommended -- gives you foundation
plus the tools plus the awareness context in one step):

```yaml
includes:
  - bundle: git+https://github.com/microsoft/amplifier-browser-bridge@main
```

...or set it as your default session bundle in `~/.amplifier/settings.yaml`.
Two narrower composition patterns (behavior-only; tool-module-only, e.g. for
local dev via `./modules/tool-browser-bridge`) plus their exact YAML are
documented in `docs/AGENT_SURFACES.md` and this file's own git history -- not
repeated here to keep this system prompt lean (see S1/S2 in the bundle repo
validation report).

## Configuration

Reads `AMPLIFIER_BROWSER_BRIDGE_HUB_URL` (default `ws://127.0.0.1:8900/agent`)
and `AMPLIFIER_BROWSER_BRIDGE_TOKEN` (default unset) from the session's
environment -- not from this bundle. **Left unset, every `browser_*` call
silently targets an unauthenticated localhost hub** and does nothing useful
unless one happens to be running there; there is no error at bundle-load
time, only when a tool is actually called (`hub error: ...`). Point
`HUB_URL` at the hub's Tailscale IP, never a MagicDNS name. Full detail:
`docs/designs/browser-bridge.md` section 4, `auth.py`.

## What this bundle provides

25 `browser_*` tools -- see `docs/AGENT_SURFACES.md` for the full vocabulary.
[`context/awareness.md`](context/awareness.md) (loaded into every session
that composes this bundle) covers the addressing model (`device_id` ->
`tab_id` -> `ref`) and the three-tier connectivity model every tool honors:
`live` executes; `intermittent`/`dormant` return `{"status": "queued", ...}`
instead of blocking -- **never misread this as an empty success.**

## What this bundle deliberately does NOT add

**The MCP server is a separate, alternative agent surface, not composed
here.** `src/amplifier_browser_bridge/mcp_server.py` exposes the same lib to
any MCP-speaking client (Claude Desktop, a bare `mcp` SDK session, etc.) with
zero Amplifier dependency. An Amplifier session gets the native tool module
instead (lower latency -- no stdio subprocess -- and consistent tool-call
semantics with the rest of the session); the MCP server remains documented
and runnable separately per `docs/AGENT_SURFACES.md`. There is no need to run
both against the same Amplifier session.

Policy/consent gating (denylist categories, irreversible-action confirmation)
is enforced by the hub (`policy.py`/`hub.py`), not by this tool module or
bundle -- this bundle exposes exactly the capabilities the hub currently
permits, and forwards the hub's `{"status": "needs_confirmation", ...}` or
queued/error shapes back to the calling agent unmodified.

---

@foundation:context/shared/common-system-base.md
