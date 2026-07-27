---
name: Bug report
about: Report something that doesn't work as documented
title: ""
labels: bug
assignees: ""
---

## What happened

A clear description of the incorrect behavior.

## Expected behavior

What you expected to happen instead, and where that expectation comes from (a section of
`docs/PROTOCOL.md`, `docs/POLICY.md`, `docs/designs/browser-bridge.md`, or the README).

## Environment (this project's behavior varies by device and connectivity tier -- please be specific)

- Edge version and build channel (stable / beta / dev / canary):
- Platform: (Windows / macOS / Linux / Android, and OS version)
- Device connectivity tier at the time, if known (`live` / `intermittent` / `dormant` -- check
  `amplifier-browser-bridge devices` or the `devices` response):
- How you're running the hub (local dev, version/commit):
- Agent surface in use (CLI / MCP server / Amplifier tool module):

## Steps to reproduce

1.
2.
3.

## Evidence

The command(s) run and their actual output (paste the real JSON envelope or CLI output, not a
paraphrase). If the bug is intermittent or tier-dependent, note how many times you reproduced it
and how many attempts.

```
paste here
```

## Anything you ruled out

If you already checked whether this matches a known limit documented in `docs/POLICY.md`
("What the denylist does NOT catch" / "Other honest limits") or the design doc's "Known
unknowns" section, say so -- it helps avoid re-diagnosing a documented gap.
