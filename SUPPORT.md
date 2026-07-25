# Support

## How to file issues and get help

This project uses GitHub Issues to track bugs and feature requests. Please search
[existing issues](../../issues) before filing a new one to avoid duplicates.

For new issues, use one of the issue templates (bug report or feature request) and fill in the
requested detail -- for bug reports in particular, this project depends on real environment
detail (Edge version, platform, device connectivity tier) because behavior differs measurably
between desktop and Android and even between desktop platforms (see
`docs/designs/browser-bridge.md` section 2 for why this project treats "which device" as a
first-class variable rather than an afterthought).

## What's in scope here

- Bugs in the hub, extension, protocol, policy engine, or agent surfaces (Python lib, CLI, MCP
  server, Amplifier tool module).
- Documentation gaps or inaccuracies against `docs/designs/browser-bridge.md`,
  `docs/PROTOCOL.md`, `docs/POLICY.md`, or `docs/AGENT_SURFACES.md`.
- Questions about extending the policy engine's denylist or gate rules for your own deployment.

## What's out of scope here

- **Security vulnerabilities.** Do not file these as public issues -- see [SECURITY.md](SECURITY.md)
  and report through MSRC instead.
- Support for browsers other than Microsoft Edge, or platforms other than Windows, macOS, Linux,
  and Edge Android -- these are explicit non-goals of the project (see the design doc's
  "Non-goals" section).
- General Tailscale configuration and troubleshooting -- refer to
  [Tailscale's own documentation](https://tailscale.com/kb/) for anything not specific to how
  this project uses it (see `docs/designs/browser-bridge.md` section 4 for what this project
  assumes about your tailnet).
- Requests to weaken the consent model's structural guarantees (capability binding enforced at
  the hub, denylist invisibility, confirmation gates on irreversible actions) -- these are
  deliberate design decisions documented in `docs/POLICY.md`, not defaults to be argued around
  case by case.

## Microsoft Support Policy

Support for this project is limited to the resources listed above.
