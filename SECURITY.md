<!-- BEGIN MICROSOFT SECURITY.MD V0.0.9 BLOCK -->

## Security

Microsoft takes the security of our software products and services seriously, which includes
all source code repositories managed through our GitHub organizations, which include
[Microsoft](https://github.com/microsoft), [Azure](https://github.com/Azure),
[DotNet](https://github.com/dotnet), [AspNet](https://github.com/aspnet), and
[Xamarin](https://github.com/xamarin).

If you believe you have found a security vulnerability in any Microsoft-owned repository
that meets [Microsoft's definition of a security vulnerability](https://aka.ms/security.md/definition),
please report it to us as described below.

## Reporting Security Issues

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, please report them to the Microsoft Security Response Center (MSRC) at
[https://msrc.microsoft.com/create-report](https://aka.ms/security.md/msrc/create-report).

If you prefer to submit without logging in, send email to
[secure@microsoft.com](mailto:secure@microsoft.com). If possible, encrypt your message with our
PGP key; please download it from the
[Microsoft Security Response Center PGP Key page](https://aka.ms/security.md/msrc/pgp).

You should receive a response within 24 hours. If for some reason you do not, please follow up
via email to ensure we received your original message. Additional information can be found at
[microsoft.com/msrc](https://aka.ms/security.md/msrc).

Please include the requested information listed below (as much as you can provide) to help us
better understand the nature and scope of the possible issue:

  * Type of issue (e.g. buffer overflow, SQL injection, cross-site scripting, etc.)
  * Full paths of source file(s) related to the manifestation of the issue
  * The location of the affected source code (tag/branch/commit or direct URL)
  * Any special configuration required to reproduce the issue
  * Step-by-step instructions to reproduce the issue
  * Proof-of-concept or exploit code (if possible)
  * Impact of the issue, including how an attacker might exploit the issue

This information will help us triage your report more quickly.

If you are reporting for a bug bounty, more complete reports can contribute to a higher bounty
award. Please visit our [Microsoft Bug Bounty Program](https://aka.ms/security.md/msrc/bounty)
page for more details about our active programs.

## Preferred Languages

We prefer all communications to be in English.

## Policy

Microsoft follows the principle of [Coordinated Vulnerability Disclosure](https://aka.ms/security.md/cvd).

<!-- END MICROSOFT SECURITY.MD BLOCK -->

## Project-specific threat model

This is not a general-purpose library -- it lets an AI agent read and act inside a user's
**real, logged-in Microsoft Edge browser** on another device. That capability is the point of
the project, and it is also the entire attack surface. Report vulnerabilities related to any of
the following exactly as described above -- via MSRC, not a public issue.

### The trust boundary is the tailnet, not the internet

There is no public listener anywhere in this system. The hub binds a WebSocket port that is
only reachable to devices already inside the operator's own Tailscale tailnet (see
`docs/designs/browser-bridge.md` section 4). Tailscale ACLs are the outer authorization
boundary; a device that cannot reach the hub's tailnet address cannot reach it at all. If you
find a way to reach the hub, an extension, or a command result from outside the tailnet without
going through Tailscale's own auth, that is a critical finding.

### The per-device token is a second, narrower boundary

Tailnet identity is per-*device*, not per-*application* -- any other process or extension
running on an authorized device shares that device's tailnet identity. The per-device shared
token (`docs/PROTOCOL.md`, "Authentication") exists specifically to narrow that gap. Auth is
**disabled by default in local development** (loudly logged as such -- see `auth.py`); running
a hub reachable from more than one device without setting `ABB_HUB_TOKEN` means anything on the
tailnet that can reach the port can issue commands to every connected browser. This is documented
behavior, not a bug, but it is exactly the kind of misconfiguration worth flagging if you find a
deployment relying on it.

### Prompt injection from page content is an assumed, not a hypothetical, threat

The agent reads real page content (`snapshot`, `read`) and that content flows into whatever
model is driving the session. A malicious or compromised page can attempt to inject instructions
into the agent's context. This system's mitigation is structural, not linguistic: capability and
target binding are enforced by the hub -- specifically `PolicyEngine.evaluate` in
`policy.py`, called from `Hub.send_command` as the single choke point before any command reaches
a device or a queue (see `docs/POLICY.md` section 2, "Capability binding"). A prompt-injected
model can *want* a different target; it cannot *address* one that policy has not permitted, and
the policy engine's own record of what a tab actually is comes only from data the browser itself
reported, never from anything an agent's request asserts. No prompt-level instruction is treated
as a security control anywhere in this codebase.

### What the denylist does and does not cover

The consent model is denylist-shaped by design (`docs/POLICY.md`): broad read access by default,
a short hand-maintained list of sensitive host categories (financial, healthcare, identity
providers, password managers) that are made invisible to the agent, and confirmation gates on a
fixed set of irreversible actions (purchase, send, delete, OAuth grant, file upload, account
creation, permission change). Read `docs/POLICY.md` section 2 ("What the denylist does NOT
catch") and section 3 ("Other honest limits") before relying on this for anything beyond the
threat model it was designed for. In particular: the denylist can only judge a tab whose host
the hub has already observed; it has no path-level granularity; and most click/type-based
confirmation gates have no real signal wired up in the current phase (see `docs/POLICY.md`
section 3). This is documented as an incomplete starting point, not a certified security
boundary -- treat it that way when evaluating this project for use with regulated data.

### Where the audit trail lives

Every dispatched command, every policy decision (denial, gate, confirmation, kill-switch event),
and every result is written to a JSONL audit log (see `audit.py` and `docs/POLICY.md` section 6).
This is the compensating control for a system that is broad-access by default: nothing the agent
does is invisible after the fact. If you find a code path that bypasses the audit log for any
action that reaches a real browser, treat that as a security finding, not a logging bug.

### CDP escalation (when present)

Where this project attaches `chrome.debugger` (CDP) to a tab, that grants trusted input
dispatch and full page instrumentation for the duration of the attachment, and Edge will show an
unsuppressable "being debugged" banner while attached. Any code path that attaches CDP silently,
fails to detach on idle, or fails to surface the banner state honestly to the user is a security
concern worth reporting.
