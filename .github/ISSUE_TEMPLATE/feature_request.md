---
name: Feature request
about: Suggest a new command, capability, or agent surface
title: ""
labels: enhancement
assignees: ""
---

## What do you want to do that isn't possible today

Describe the concrete task, not just the mechanism -- e.g. "drag-and-drop reorder a list" rather
than "add a drag command."

## Where does it fit in the existing model

This project has a fairly opinionated shape (see `docs/designs/browser-bridge.md`). Help place
your request:

- New command in the existing vocabulary (`docs/PROTOCOL.md`), or something the vocabulary can't
  express today?
- Does it need a new agent-surface tool (`browser_*` in `docs/AGENT_SURFACES.md`), or is it
  reachable through the existing `abb cmd` escape hatch?
- Does it interact with the policy engine (`docs/POLICY.md`) -- e.g. should it be gated as an
  irreversible action, or excluded from the denylist for some reason?
- Does it depend on `chrome.debugger`/CDP (Phase 6, not yet built) -- e.g. trusted input events
  or background-tab screenshots?

## Platform scope

Does this apply to Edge desktop, Edge Android, or both? If you know of a platform-specific
limitation (see the platform support table in the README and `docs/designs/browser-bridge.md`
section 7), mention it.

## Alternatives considered

Is there a way to accomplish this with today's command vocabulary (possibly less conveniently)?
