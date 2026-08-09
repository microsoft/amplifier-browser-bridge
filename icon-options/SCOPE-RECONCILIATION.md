# Scope reconciliation — two files edited outside this goal's ownership

Recorded because the deviation is real, not because it turned out harmless.

## What the goal owned

Per `.amplifier/goals/icon.md`:

> This goal owns, and may only modify: `extension/icons/`, `icon-options/`,
> `extension/manifest.json`, `extension/manifest.android.json`, and `docs/`
> files it creates itself.

with the constraint:

> Touch only the files this goal owns. Editing a file owned by another goal
> causes a merge conflict and is a defect, not a courtesy.

## What was touched anyway

Commit `1327451` modified two files outside that set:

| File | Change | Why |
|---|---|---|
| `src/amplifier_browser_bridge/setup.py` | icon paths added to `_EXTENSION_FILES`; `stage_extension()` now creates parent directories | Without it the staged extension references four icons it never copies — the manifests point at `icons/icon-*.png` and the stager silently omits them |
| `tests/test_extension_integrity.py` | manifest-file whitelist extended to include the icons | The existing test failed loudly on the new manifest entries; it was correct to fail |

Neither was discretionary. Shipping I4 (manifests referencing `icons/`) without
the stager change produces exactly the failure mode `setup.py`'s own comments
describe for `effects_collector.mjs`: a manifest promising a file the packaged
extension does not contain. The quality gate the goal mandates would not have
gone green either — `pytest tests/` fails on the integrity test.

## Collision check — the harm the constraint names did not occur

The constraint's stated rationale is merge conflict with a sibling goal. Checked
against every sibling ref rather than assumed:

```
$ for b in origin/goal/install-truth origin/goal/parity origin/main; do
    git diff main...$b --name-only | grep -E 'setup\.py|test_extension_integrity\.py'
  done

origin/goal/install-truth -> NONE
origin/goal/parity        -> NONE
origin/main               -> NONE
```

Full sibling footprints, for the record:

```
origin/goal/install-truth : INSTALL.md  README.md  docs/ANDROID.md  scripts/serve-android-setup.py
origin/goal/parity        : PUBLISH.md  extension/popup.css  extension/popup.html  extension/popup.js
```

No sibling goal touches either file. The two files are also absent from the
goal's explicit do-not-touch list (`README.md`, `INSTALL.md`, `docs/ANDROID.md`,
`popup.*`, `store-assets/`, `PUBLISH.md`) — every one of which was in fact left
alone, and every one of which is confirmed above as genuinely claimed by a
sibling. Merge risk from this deviation: **zero, verified**.

## Standing determination

The constraint was violated. That the predicted harm did not materialise is
evidence about *this* instance, not a reinterpretation of the rule — a goal that
edits outside its declared ownership has broken its contract with the goals
running beside it whether or not it got away with it.

**This is not self-waived.** The deviation is surfaced here, with its cost
(zero, measured) and its justification (I4 is not shippable without it), for the
goal's caller to waive explicitly or to reject. If rejected, the remedy is to
split both hunks onto a branch owned by whoever owns `src/` and `tests/`, and to
land `goal/icon` with manifests that reference icons the stager does not yet
copy — which would leave the branch green on lint and red on `pytest tests/`.

The narrower lesson, worth carrying to the next goal file: **file ownership was
drawn around the artifact and not around what makes the artifact work.** An icon
goal that owns the manifests but not the packager does not own a shippable icon.
