# Goal: a designed icon for amplifier-browser-bridge

## Outcome

Every item below has reached a terminal state (PASS, FAIL, or
BLOCKED-with-named-reason), so the extension ships an icon chosen through
review rather than picked, or the reason it could not be is recorded.

## Exit

Complete when **either** every item reaches a terminal state, **or** it is
conclusively demonstrated the remainder cannot, naming the blocker for each.
Items ending FAIL or BLOCKED are residuals, not failures of the goal.

## FILE OWNERSHIP (parallel-safety)

This goal owns, and may only modify:
`extension/icons/`, `icon-options/`, `extension/manifest.json`,
`extension/manifest.android.json`, and `docs/` files it creates itself.
Do NOT touch README.md, INSTALL.md, docs/ANDROID.md, popup.*, store-assets/,
or PUBLISH.md — sibling goals own those.

## ITEMS

I1. At least three visually distinct icon concepts exist as rendered PNG files
at 128px under `icon-options/`, each with a one-line statement of the idea it
encodes. If no image-generation capability is reachable, this resolves BLOCKED
naming that, and concepts are instead recorded as written specifications
detailed enough for someone else to render.

I2. The concepts have been through at least two review rounds. Each round draws
on whichever of the design council, product council, and simulated user
research can be invoked; each round's feedback is recorded under
`icon-options/`. A round whose reviewers cannot be invoked resolves that round
BLOCKED-with-named-reason; the item proceeds on the rounds that did run.

I3. One concept is selected, with written rationale naming what it encodes and
why it beat the others.

I4. The selected concept is rendered at 16, 32, 48 and 128 px under
`extension/icons/` and referenced from both manifests via `icons` and
`action.default_icon`.

I5. Non-selected concepts are retained under `icon-options/`.

I6. Legibility at 16px is assessed by inspecting the rendered 16px file itself.
If it cannot be inspected, resolve BLOCKED naming that rather than asserting.

## SCOPE-OUTS

- No store submission. No Windows validation. No repo org move.
- No wall-clock soak or monitoring window.
- Uniform completion across items is NOT the goal.
- Do not redesign the hub, policy engine, tier model, or queueing.
- Do not modify files owned by sibling goals.

## KNOWN

- The extension today has no icons at all: no `icons/` directory, no `icons`
  manifest block, no `action.default_icon`. It shows the generic puzzle piece.
- Exemplars at `../teams-transcript-md` and `../loop-page-md` (read-only) each
  ship `icons/{16,32,48,128}` plus an `icon-options/` directory of candidates.
- This extension requests `<all_urls>` and `chrome.debugger`, and Edge shows an
  unsuppressable "started debugging this browser" banner while it runs. The
  icon is what a user identifies that banner with.
- The product is cross-device browser control: an agent on one machine acting
  as a second operator in the user's own logged-in browser on another device.

## Constraints

- Quality gate green before any commit: `ruff format --check .`, `ruff check .`,
  `pyright --venvpath . src tests`, `pytest tests/`,
  `pytest modules/tool-browser-bridge/tests/`, `node --test extension/*.test.mjs`,
  and `node --input-type=module --check` on each JS/MJS file.
- Commit to THIS worktree's branch only. Push that branch to `origin`. Do NOT
  push to `main` and do NOT merge — a separate step reconciles the branches.
- Other goals are running in parallel on sibling worktrees. Touch only the files
  this goal owns (see FILE OWNERSHIP). Editing a file owned by another goal
  causes a merge conflict and is a defect, not a courtesy.
- Do not stop, restart, or signal PID 4066708 (hub, port 8900) or the static
  server on port 8686. Do not write to `~/.config/amplifier-browser-bridge/`.
  Do not drive the attached browsers.
- Never use `pkill -f`. Kill by explicit PID read from `ss -ltnp`.
- Background processes: `setsid CMD </dev/null >log 2>&1 &`.
- Real device hardware (phone, Mac browser) is NOT available. Anything needing
  it resolves BLOCKED with that reason — never simulated, never asserted.

## Reporting

Show evidence inline as produced — real command output, real file paths, real
rendered artifacts. Do not assert a result without the output demonstrating it.
Final report: a table of every item with its terminal state and one line of
evidence, then residuals with named reasons.
