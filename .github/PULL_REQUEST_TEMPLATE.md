## What this changes

## Why

## Evidence

This project runs on measured evidence, not assumed behavior (see
`docs/designs/browser-bridge.md` section 2). Fill in what you actually verified:

- **What was proven:** (e.g. "the hub now rejects a `tab_close` on a denylisted host")
- **On what:** (e.g. "unit test", "manual run against a local hub + real Edge on macOS",
  "real Edge Android device")
- **Output:** paste the real command and its real output -- not a description of what it
  would show.

```
paste real output here
```

- [ ] `ruff format --check .` passes
- [ ] `ruff check .` passes
- [ ] `pyright` passes
- [ ] `pytest tests/` passes
- [ ] `pytest modules/tool-browser-bridge/tests/` passes (if you touched that module)
- [ ] `node --input-type=module --check < extension/<file>.js` passes for any extension file you touched

## Docs

- [ ] `docs/PROTOCOL.md` updated (if the wire protocol changed)
- [ ] `docs/POLICY.md` updated (if the denylist, gates, or audit behavior changed)
- [ ] `docs/designs/browser-bridge.md` updated (if an architectural decision changed)
- [ ] `README.md` updated (if user-facing behavior, setup, or platform support changed)
- [ ] N/A -- no doc-relevant change

## Anything you could not verify

Be explicit if something couldn't be tested in your environment (e.g. no Android device
available, no second machine to test cross-device transport). An honest gap here is better than
a checked box you can't back.
