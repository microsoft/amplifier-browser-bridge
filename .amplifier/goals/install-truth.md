# Goal: the documented install path matches reality

## Outcome

Every item below has reached a terminal state (PASS, FAIL, or
BLOCKED-with-named-reason), so a stranger following the README either succeeds
or the exact points where it misleads them are recorded.

## Exit

Complete when **either** every item reaches a terminal state, **or** it is
conclusively demonstrated the remainder cannot, naming the blocker for each.
Items ending FAIL or BLOCKED are residuals, not failures of the goal.

## FILE OWNERSHIP (parallel-safety)

This goal owns, and may only modify:
`README.md`, `INSTALL.md`, `docs/ANDROID.md`, and the source that generates
the served `/setup` page.
Do NOT touch `extension/icons/`, `icon-options/`, either manifest, `popup.*`,
`store-assets/`, or `PUBLISH.md` — sibling goals own those.

## ITEMS

T1. A Digital Twin Universe environment is launched with nothing pre-installed:
no `uv tool install`, no token file, no `~/.config/amplifier-browser-bridge/`,
no repo checkout.

T2. Inside it, the public repo is cloned anonymously
(`https://github.com/microsoft/amplifier-browser-bridge.git`, no credentials)
and the README's install path is executed verbatim — at minimum
`uv tool install .`, then `init`, then starting the hub, then `doctor`. The
real output of each command is recorded inline.

T3. Every discrepancy between what the README says and what the commands
actually do is either fixed in the repo or recorded as a residual naming what
is wrong.

T4. `doctor` run against a hub with zero connected devices produces output
that states that condition plainly rather than appearing broken.

T5. `README.md`, `INSTALL.md`, and `docs/ANDROID.md` each state that Android
support is experimental, requires Edge Canary or Beta with a developer-options
sideload flow, and that Edge Android stable uses a curated allowlist this
extension is not on.

T6. The served `/setup` page carries the same experimental framing as the docs.

## SCOPE-OUTS

- No store submission. No Windows validation. No repo org move.
- No wall-clock soak or monitoring window.
- Uniform completion across items is NOT the goal.
- Do not redesign the hub, policy engine, tier model, or queueing.
- Do not modify files owned by sibling goals.
- Dormant-device eviction is out of scope.

## KNOWN

- The repo is public; anonymous clone verified working at HEAD `8605536`.
- The `uv tool install .` path has never been exercised from a fresh clone. All
  work to date ran from repo source via `uv run`; the installed CLI on the
  development machine is a stale build.
- An earlier defect of exactly this class was found and fixed: `init` printed a
  `doctor` command targeting loopback while instructing the hub to bind a
  tailnet IP, so following the printed steps verbatim always failed.
- Android install currently depends on Edge Canary developer options
  ("Extension install by crx"), and the artifact must be served as `.bin`
  because Chromium intercepts `.crx` downloads.
- `amplifier-digital-twin` is the DTU CLI; `amplifier-gitea` may be needed for
  local-repo mirroring, though this goal clones from the public GitHub URL.

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
