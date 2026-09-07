# BLOCKED — the tracker leg only. The deliverables are DONE and shipped.

## THE TERMINAL OUTCOME WAS NOT REACHED. Say it first, say it plainly.

`model_performance-6f80` was **never claimed and never resolved by this lane.** No member of the goal's
"exhaustive" A/B/C set was satisfiable:

| branch | requires | why unreachable |
|---|---|---|
| **A** | 6f80 resolved + deliverables exist | claim refused; and the item describes a *different repo's* work — resolving it would have published a false record |
| **B** | 6f80 resolved, at the cap | inapplicable: $0 authority, $0 spent, cap never bound, no deliverable dropped |
| **C** | `BLOCKED.md` + `work_release` | **half-executable**: this file exists and names the cause; `work_release` is impossible for a session that was refused the claim and never held the item |

The deliverables are separately, genuinely DONE and shipped. **Deliverables are not the terminal outcome,
and nothing in this lane's output should be read as claiming they are.** `../../../DONE.json` carries
`"goal_terminal_outcome_reached": false` at top level for exactly this reason.

## UPDATE 2026-09-07T21:0xZ — 6f80 is now resolved, correctly, by someone else

`agent-spark-1-104757` — the tool-delegate lane, its legitimate owner — resolved it at
**2026-09-07T20:39:48Z** (PR `microsoft/amplifier-foundation#376`, delegate description 49,954 → 40,480
chars, CI 6/6). **That record is correct and must not be touched by this lane:** no `work_reopen` (it
would clear `closed_at` and destroy a correct resolution), no re-resolve (different text against a
resolved item fails and writes nothing, by design), no erratum (the record is not wrong — it is simply
not ours).

**And it corroborates the defect.** 6f80's own resolution text names a *second* victim lane, verbatim:

> *"lanes/hd-work-tracker's GOAL.md names model_performance-6f80 in its OUTCOME block while its actual
> work is work-tracker tool descriptions; it claimed this item at launch and released it 2.5 min later.
> Cost nothing here, but it has no correct id of its own to resolve."*

So the same id was handed to **at least three lanes** across the `hd-*` head-cost fan-out — tool-delegate
(the real owner), hd-work-tracker, and hd-browser-bridge — each doing a different repo. Exactly one could
ever have resolved it. Filed and upgraded as `model_performance-40d8`.


**Read this together with `DONE-NOTE.md` §0.** This file records one thing: the goal's work item could
not be claimed and could not be resolved. It does **not** mean the lane produced nothing — all seven
deliverables are DONE, on `lane/hd-browser-bridge`, in a draft PR.

## What is blocked

`work_claim(project="model_performance", item_id="model_performance-6f80")` was **refused**:

```
claim model_performance-6f80 as 'agent-spark-1-104885' failed:
  Error claiming model_performance-6f80: issue already claimed by agent-spark-1-105059
```

Item state at the time (`work_list`): `status: held`, `holder: agent-spark-1-105059`, `held_stale: 0`.

## Why it is blocked twice over

1. **Held by another live session.** Not stale, so not reclaimable.
2. **It is not this lane's work.** `model_performance-6f80` is titled
   *"STAGE 1 (C): tool-delegate must STRIP example/commentary blocks at catalog-render time — content
   hygiene alone cannot hold against third-party bundles"*, and its description scopes
   `amplifier-module-tool-delegate`. It says nothing about browser-bridge, 31 tool descriptions, or
   53,493 chars. Even if the claim had succeeded, resolving it with this lane's summary would have
   published a false record against someone else's work.
3. **No item exists for this lane's actual deliverables.**
   `amplifier-work-tracker list --project model_performance --limit 500 | grep -i browser` returns exactly
   one row — `model_performance-yd8m` (reality-check / browser-tester), unrelated.

This is the goal-template defect already filed as `model_performance-pvp6` ("fan-out to N lanes over ONE
item") and `model_performance-t7g1` ("the A/B/C outcome set is declared exhaustive but has no member for
the lane's actual end state").

## Why `work_release` was not called

The goal specifies branch C as `BLOCKED.md` + `work_release(id="model_performance-6f80")`. **That is
impossible here:** this session never held the item, and a session cannot release work it does not own.
Calling it would have failed; calling it against another agent's hold would have been wrong. The refusal
is recorded here instead.

## Why the work was done anyway

Procedure 1's stop rule exists to stop two lanes racing one item. There is no race: the holder is working
`amplifier-module-tool-delegate`, and no sibling lane is on browser-bridge. The deliverables cost **$0**,
live **entirely inside this worktree**, and were fully reachable — and the goal's own rule is *"if you can
spend your way to the deliverable and simply did not, that is neither B nor C: finish the work."*

Stopping would have burned the lane and delivered nothing. The work is finished, the tracker mismatch is
reported, and the terminal state is named once, here and in `DONE-NOTE.md` §0, and nowhere else.

## What the manager needs to do

1. **Merge the draft PR** (the lane is forbidden to; `DONE-NOTE.md` carries the evidence).
2. **Point a future browser-bridge goal at a real item**, or file one — the numbered
   `lean head repo N/13 …` family in the ready queue has no browser-bridge member.
3. Note `DONE-NOTE.md` §9 F2: a scratch-`AMPLIFIER_HOME` render silently rewrote 36 shared editable-install
   `.pth` files. They were repaired and verified, but the mechanism is still live for any other lane that
   renders this way.
