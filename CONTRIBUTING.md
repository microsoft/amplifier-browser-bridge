# Contributing

Thanks for your interest in Amplifier Browser Bridge.

## Contributor License Agreement

This project welcomes contributions and suggestions. Most contributions require you to agree to
a Contributor License Agreement (CLA) declaring that you have the right to, and actually do,
grant us the rights to use your contribution. For details, visit
https://cla.opensource.microsoft.com.

When you submit a pull request, a CLA bot will automatically determine whether you need to
provide a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the
instructions provided by the bot. You will only need to do this once across all repos using our
CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](CODE_OF_CONDUCT.md). For
more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/)
or contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions
or comments.

## What this project is

Read `docs/designs/browser-bridge.md` first -- it is the design of record, including every
measured constraint the architecture is built on. Then `docs/PROTOCOL.md` (wire protocol),
`docs/POLICY.md` (consent model), and `docs/AGENT_SURFACES.md` (MCP server and Amplifier tool
module). The `README.md` is the front door; these docs are where the actual contracts live.

## Repository layout

```
src/amplifier_browser_bridge/   the lib: protocol, addressing, tiers, hub, client, CLI, mcp_server
modules/tool-browser-bridge/    the Amplifier tool module (thin adapter over the lib)
extension/                      the MV3 browser extension (one build, all platforms)
tests/                          unit tests for everything testable without a live browser
docs/                           design doc, protocol, policy, agent surfaces
```

## Dev setup

This project uses [`uv`](https://docs.astral.sh/uv/) for environment and dependency management.

**This is the CONTRIBUTOR install path** -- an *editable* install that resolves imports straight
back to this checkout, so edits to `src/amplifier_browser_bridge/` take effect immediately with
no reinstall. If you just want to USE amplifier-browser-bridge (not develop it), see README.md's
Quickstart instead: `uv tool install .` is the normal, non-editable user install.

```bash
uv pip install -e ".[dev]"   # or: uv pip install -e . pytest ruff pyright
```

Optional extras:

```bash
uv pip install -e ".[mcp]"   # MCP server support (abb-mcp)
```

### Running the hub locally

```bash
abb hub --host 0.0.0.0 --port 8900
```

Auth is disabled by default in dev and this is loudly logged. To enable it, run `abb init`
(generates a token, writes it to the hub's token file) and paste the printed token into the
extension's options page (its toolbar-icon click), and pass `ABB_TOKEN` to the CLI/MCP server.
See `docs/PROTOCOL.md` ("Authentication") for the full resolution order.

### Loading the extension unpacked

Hub URL and token are runtime configuration (`chrome.storage.local`), entered through the
extension's own options page -- never a tracked source file. See README.md's "Setup" section
for the full `abb init` / `abb doctor` flow; the short version for local dev:

1. In Edge, go to `edge://extensions`, enable Developer mode, choose "Load unpacked", and select
   the `extension/` directory (or a directory staged by `abb init` -- see below).
2. Click the extension's toolbar icon (its only UI) to open the options page. Enter the hub's
   Hub URL -- a tailnet IP literal, never a MagicDNS name, see `docs/designs/browser-bridge.md`
   section 4 for why -- and the token from `abb init`/your hub operator, then Save.
3. Confirm the device shows up via `abb devices` or `abb doctor`.

For iterative development, prefer staging via `abb init --dest <dir>` (or reuse an existing
staged directory) over loading directly from `extension/` in this repo checkout -- an unpacked
extension's identity (and therefore its `chrome.storage.local` config) is tied to the exact
directory path it was loaded from, so loading from a stable staged path means future `abb init`
re-runs (which re-copy the JS/HTML/manifest files) never disturb a working configuration.

### Running tests

```bash
pytest tests/                              # root package
pytest modules/tool-browser-bridge/tests/  # Amplifier tool module
```

### Linting and type checking

```bash
ruff format --check .
ruff check .
pyright
```

All three must be clean (formatting, lint, and type checking) before a PR is considered ready.
`pyright` needs to resolve against this repo's own virtualenv (`.venv`) -- run it from a shell
where that venv is active, or pass `--venvpath .` explicitly; running it against an unrelated
environment produces false `reportMissingImports` errors (see `docs/AGENT_SURFACES.md`, "Known
pyright false positive").

### Extension JavaScript

There is no build step for the extension. Files under `extension/` are loaded by Edge as-is.
At minimum, verify syntax before submitting a change:

```bash
node --input-type=module --check < extension/background.js
node --input-type=module --check < extension/injected.js
node --input-type=module --check < extension/options.js
node --input-type=module --check < extension/config_validate.mjs
node --input-type=module --check < extension/frame_refs.mjs
node --input-type=module --check < extension/combine_frames.mjs
node --input-type=module --check < extension/download_claim.mjs
node --input-type=module --check < extension/fetch_utils.mjs
```

(`--input-type=module` is required because these files use ES module `import`/`export` syntax
without a `package.json` declaring `"type": "module"`.)

Pure logic extracted for testability (frame-ref qualification, frame-combine strategy,
download-claiming, byte-cap/base64 helpers) lives in dependency-free `.mjs` modules with
ZERO `chrome.*` usage, each with a companion `*.test.mjs` -- run with Node's built-in test
runner:

```bash
node --test extension/*.test.mjs
```

## Engineering conventions this project holds you to

These are load-bearing, not stylistic preferences -- they show up throughout the design doc and
the code, and PRs that violate them will be asked to change:

- **Fail loud, never silently.** Every command produces exactly one of `{ok: true, result}` or
  `{ok: false, error}`. No synthetic results, no guessed fallbacks, no swallowed exceptions.
- **Behavioral capability probes, never `typeof` checks.** Edge Android ships APIs that are
  present but non-functional (`docs/designs/browser-bridge.md` section 2); a `typeof chrome.x`
  check tells you nothing about whether `x` actually works. Probe with a real, guarded
  invocation instead.
- **Policy is enforced at the hub's single choke point, not scattered through call sites.**
  `PolicyEngine.evaluate`, called from `Hub.send_command`, is the only place a command is
  checked against the denylist and confirmation gates (`docs/POLICY.md`). If you add a new way
  for a command to reach a device, it must go through this path.
- **The queue is a real, inspectable state, never a hidden block.** A command to a non-`live`
  device returns immediately with `{status: "queued", tier, ...}`. Do not add code that blocks
  waiting for a device to reconnect.
- **Poll, don't sleep, for waits.** `wait_for`/`wait_text` poll on an interval; nothing in this
  codebase should block on a fixed `sleep()` hoping a condition becomes true.
- **The extension carries zero site knowledge and zero policy.** Both belong in the hub. If a
  change teaches the extension something about a specific site or service, reconsider where that
  logic belongs.
- **Keep the two protocol implementations in sync by hand.** `protocol.py` is the Python-side
  source of truth for message shapes; `extension/background.js` mirrors it in JS. There is no
  shared codegen in this phase (`docs/PROTOCOL.md`) -- if you change one, check the other.

For the project's broader implementation philosophy (ruthless simplicity, fail-fast error
handling, avoiding speculative abstraction), see the design doc's citations and reasoning
throughout `docs/designs/browser-bridge.md`.

## Pull requests

- Keep PRs scoped to one concern. This repo is under active, concurrent development across
  multiple contributors working in parallel on different areas (extension, hub/protocol, agent
  surfaces, project infrastructure) -- a PR that touches files outside its stated scope is harder
  to review safely alongside others.
- Include what you verified and how (test output, manual reproduction steps, or an explicit
  note if something could not be verified in your environment). See the PR template for the
  specific evidence this project expects.
- If a change touches `docs/PROTOCOL.md` or `docs/POLICY.md`, treat the doc update as part of
  the same PR, not a follow-up -- these documents are the contract, not commentary on it.
