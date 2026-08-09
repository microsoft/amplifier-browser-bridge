# Goal: exemplar parity items, built or reasoned away

## Outcome

Every item below has reached a terminal state (PASS, FAIL, or
BLOCKED-with-named-reason). A written decision not to build something is a
terminal state, not a gap.

## Exit

Complete when **either** every item reaches a terminal state, **or** it is
conclusively demonstrated the remainder cannot, naming the blocker for each.
Items ending FAIL or BLOCKED are residuals, not failures of the goal.

## FILE OWNERSHIP (parallel-safety)

This goal owns, and may only modify:
`extension/popup.html`, `extension/popup.css`, `extension/popup.js`,
`store-assets/`, `PUBLISH.md`.
Do NOT modify `extension/manifest.json` or `extension/manifest.android.json` —
a sibling goal owns both. If a popup requires a manifest change
(`action.default_popup`), record the exact required edit as a residual for a
later reconciliation step instead of making it.
Do NOT touch README.md, INSTALL.md, docs/ANDROID.md, `extension/icons/`, or
`icon-options/`.

## ITEMS

P1. A popup UI is either built (`popup.html`/`popup.css`/`popup.js`) or
resolved FAIL/BLOCKED with a written reason it does not earn its place here.
If built, it must show real state — connection status, device identity — and
must not fabricate a value it cannot obtain. State the exact manifest edit it
would need, without making that edit.

P2. A `store-assets/` directory is either created with real assets or resolved
with a written reason. Note that store submission is explicitly out of scope
for this project, which bears on whether this earns its place.

P3. A `PUBLISH.md` is either written or resolved with a written reason.

## SCOPE-OUTS

- No store submission. No Windows validation. No repo org move.
- No wall-clock soak or monitoring window.
- Uniform completion across items is NOT the goal — a reasoned "no" is success.
- Do not redesign the hub, policy engine, tier model, or queueing.
- Do not modify files owned by sibling goals, including either manifest.
- Do not copy the exemplars' files wholesale; each item must earn its place in
  this project or be declined with a reason.

## KNOWN

- Exemplars at `../teams-transcript-md` and `../loop-page-md` (read-only) each
  ship `popup.html/css/js`, `store-assets/`, `PUBLISH.md`, `INSTALL.md`,
  `privacy/`, `index.html`, `package.sh`.
- This repo already has `index.html`, `privacy/`, `INSTALL.md`, and a
  `scripts/package.sh` with a build-gating integrity check.
- Those exemplars are single-purpose page-to-markdown extractors driven by a
  human clicking a button. This project is a persistent background bridge an
  agent drives remotely, with no per-action human click — so the exemplars'
  UI surface is not automatically the right surface here.
- The extension's only current UI is its options page, which on Edge Android
  proved unreachable through the toolbar affordance.

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
