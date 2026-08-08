# Migration: the `abb`/`ABB_` acronym is gone

This project never used acronyms as a matter of style; an early build slipped one in
anyway (`abb`, short for Amplifier Browser Bridge) and it spread into the console
scripts, environment variables, and the extension's internal storage keys before
anyone caught it. This change removes it everywhere. **Nothing here is a silent
fallback** -- the old names are simply no longer read, per this project's fail-loud
discipline (see `docs/POLICY.md`). If you were running a previous version, you must
take the manual steps below.

## What changed

| Old | New |
|---|---|
| `abb` (console script) | `amplifier-browser-bridge` |
| `abb-mcp` (console script) | `amplifier-browser-bridge-mcp` |
| `ABB_HUB_URL` | `AMPLIFIER_BROWSER_BRIDGE_HUB_URL` |
| `ABB_TOKEN` | `AMPLIFIER_BROWSER_BRIDGE_TOKEN` |
| `ABB_TOKEN_FILE` | `AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE` |
| `ABB_HUB_TOKEN` | `AMPLIFIER_BROWSER_BRIDGE_HUB_TOKEN` |
| `ABB_AUDIT_LOG` | `AMPLIFIER_BROWSER_BRIDGE_AUDIT_LOG` |
| `ABB_EXTENSION_SRC` | `AMPLIFIER_BROWSER_BRIDGE_EXTENSION_SRC` |
| `ABB_VISION_PROVIDER` | `AMPLIFIER_BROWSER_BRIDGE_VISION_PROVIDER` |
| `ABB_VISION_MODEL` | `AMPLIFIER_BROWSER_BRIDGE_VISION_MODEL` |
| `ABB_POLICY_FILE` | `AMPLIFIER_BROWSER_BRIDGE_POLICY_FILE` |
| `ABB_MCP_TRANSPORT` | `AMPLIFIER_BROWSER_BRIDGE_MCP_TRANSPORT` |
| `ABB_ANDROID_SIGNING_KEY` | `AMPLIFIER_BROWSER_BRIDGE_ANDROID_SIGNING_KEY` |
| `./abb-audit.jsonl` (default audit log filename) | `./amplifier-browser-bridge-audit.jsonl` |
| extension `chrome.storage.local` keys `abb_hub_url`, `abb_hub_token`, `abb_device_id`, `abb_profile_id` | `amplifier_browser_bridge_hub_url`, `amplifier_browser_bridge_hub_token`, `amplifier_browser_bridge_device_id`, `amplifier_browser_bridge_profile_id` |

Unaffected (these already used the full spelled-out name): the Python package/import
name `amplifier_browser_bridge`, the PyPI project name `amplifier-browser-bridge`,
and the on-disk config directory `~/.config/amplifier-browser-bridge/`.

## What breaks, concretely

- **The CLI binary is renamed.** `abb` no longer exists after you reinstall; the
  command is now `amplifier-browser-bridge`. Any shell alias, script, systemd unit,
  or `PATH` shim that invokes `abb` will fail with "command not found" until updated.
- **The MCP server binary is renamed.** `abb-mcp` -> `amplifier-browser-bridge-mcp`.
  Update any MCP client config (Claude Desktop's `claude_desktop_config.json`, an
  Amplifier bundle, etc.) that references the old binary name.
- **Every `ABB_*` environment variable stops being read.** If you have any of them
  set (in your shell profile, a `systemd` unit's `Environment=`, a `.env` file, a
  process manager config, ...), the CLI/MCP server/Amplifier tool module will now
  print a warning naming exactly which old variable is set and what to rename it to
  (see "How this fails loud" below) -- but it will NOT use the old value. You must
  edit those configs yourself.
- **The running hub's audit log path does not change on its own.** If you relied on
  the default `./abb-audit.jsonl` filename (rather than an explicit `--audit-log`/
  `$AMPLIFIER_BROWSER_BRIDGE_AUDIT_LOG`), a freshly started hub now writes to
  `./amplifier-browser-bridge-audit.jsonl` instead -- a NEW, empty file. Your old
  audit history is still sitting in `./abb-audit.jsonl`; nothing deletes it, but
  nothing appends to it either. If you need continuity, point `--audit-log`
  explicitly at the old path, or archive it and let the new file start fresh.
  `amplifier-browser-bridge gate-summary` reads whatever path you tell it to.
- **The extension's saved Hub URL/token become invisible, not wrong.** The extension
  reads `amplifier_browser_bridge_hub_url`/`amplifier_browser_bridge_hub_token` from
  `chrome.storage.local` now. Your old `abb_hub_url`/`abb_hub_token` values are still
  sitting in storage (nothing deletes them), but the extension no longer looks at
  them. On next load it will report itself as unconfigured -- see "How this fails
  loud" for the exact message.

## How this fails loud (so you know what happened)

- **CLI / MCP server / Amplifier tool module**: **no automatic detection.**
  An earlier version of this project shipped `src/amplifier_browser_bridge/legacy_env.py`,
  which printed a warning naming any stale `ABB_*` variable still set in the
  environment. That module was deleted (E1, honest-disclosure pass) once it
  was recognized for what it was: fail-loud machinery built to protect an
  installed base of users who had run a published version under the old
  acronym -- and at the time this rename shipped, and at every point since,
  that installed base was zero (this project has never been published to
  PyPI, the Chrome Web Store, or Edge Add-ons). Three call sites plus a
  55-line module existed to guard against a scenario with no one in it. If
  you have an old `ABB_*` variable set today, the new
  `AMPLIFIER_BROWSER_BRIDGE_*` variable it should have been renamed to will
  simply not be set -- you'll see whatever this project's ordinary "missing
  configuration" failure looks like at that call site (e.g. `doctor`'s
  `hub_reachable`/`token_match` checks), not a message naming the specific
  old variable. The manual rename table above is still the correct fix; only
  the automatic reminder is gone. If this project ever *does* build an
  installed base under the old names (unlikely, given it never shipped under
  them), reintroducing a warning like this for the real users affected would
  be the correct move, not a mistake to avoid repeating.

- **The extension**: if the old `abb_hub_url`/`abb_hub_token` keys hold a value but
  the new keys don't (i.e. this is an existing install, not a fresh one), the
  toolbar badge shows a red `!`, its tooltip reads *"Amplifier Browser Bridge:
  configuration key names changed -- click the toolbar icon to re-enter your hub
  URL and token (see MIGRATION.md)"*, the browser console logs the same explanation,
  and the options page's status line reads *"Configuration key names changed in
  this version -- your previous Hub URL/token are no longer read. Re-enter them
  below and click Save."* This is deliberately distinct from the plain "Not
  configured" message a first-ever install sees -- see `extension/background.js`'s
  `legacyConfigDetected`.

## How to get running again

1. **Reinstall** so the new console scripts exist:
   ```bash
   uv tool install --reinstall .   # or: uv tool install --reinstall amplifier-browser-bridge
   ```
   Confirm: `amplifier-browser-bridge --help` and `amplifier-browser-bridge-mcp --help` both work; `abb` no longer does.

2. **Update any `ABB_*` environment variables** you had set, to their
   `AMPLIFIER_BROWSER_BRIDGE_*` equivalents (see the table above). Check shell
   profiles, `systemd` units, `.env` files, and MCP client configs.

3. **Restart the hub** with the new command and (if you use auth) the new token
   env var:
   ```bash
   AMPLIFIER_BROWSER_BRIDGE_TOKEN_FILE=~/.config/amplifier-browser-bridge/tokens.json \
     amplifier-browser-bridge hub --host 0.0.0.0 --port 8900
   ```

4. **Re-enter the extension's configuration.** Click the toolbar icon to open the
   options page, re-enter the Hub URL and token (same values as before -- they
   still work at the hub; only the storage key names changed), and click Save.
   The badge clears and the status line reports "Connected" once the extension
   reconnects.

5. **Update any MCP client config** that pointed at `abb-mcp` to
   `amplifier-browser-bridge-mcp`.

If you're unsure whether you're affected, run `amplifier-browser-bridge doctor` --
it will surface any of the fail-loud messages above as part of its normal
diagnostic output.
