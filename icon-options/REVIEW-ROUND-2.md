# Review round 2 — revisions vs. control

Candidates in play: `01-second-cursor` (unmodified control), `06-paired-cursors`
(01 revised for F1/F2/F3), `07-handshake` and `08-sanctioned-cursor` (both new,
attempting F4). `09-lone-cursor` was added mid-round — see below.

Reviewers invoked: **product council** (6 lenses: outcomist, intent-keeper,
user-advocate, outcome-cartographer, positioning-critic, bet-sizer — all 6
responded, 2 internal debate rounds) and **simulated user research** (same 3
personas as round 1). A deliberately different panel from round 1's design
council, so this round is an independent check rather than a re-run.

## The two panels disagreed

| Reviewer | Winner | Core argument |
|---|---|---|
| Product council | **`08` with its badge deleted** | Safest failure mode; the badge is pure liability at 16px, so remove it rather than fix it |
| User research | **`07-handshake`** | The only candidate where two elements are still countable at 16px; a bare cursor is generic and unfindable in a crowded toolbar |

Both agreed on the eliminations: `01` out on the measured contrast failure,
`06` out because its fix does not survive to 16px.

The product council's recommendation did not exist as a candidate, so it was
built and rendered as **`09-lone-cursor`** in order to be measured against the
others on equal terms rather than argued about in the abstract.

## The disagreement was factual, so it was measured

The two panels made directly contradictory claims about whether `06` still
shows two marks at 16px. That is measurable. `measure_16px.py` counts connected
components of near-white and near-ink pixels in each candidate's real
`icon-16.png` and computes tile contrast against white:

```
candidate                light components               dark components            marks
------------------------------------------------------------------------------------------
01-second-cursor         14.0px                         5.0px                      2
02-bridge-span           32.0px                         4.0px                      2
03-handoff               28.0px, 15.0px, 14.0px         6.0px                      4
04-tab-cursor            none                           none                       0
05-inbound               none                           none                       0
06-paired-cursors        36.0px                         5.0px                      2
07-handshake             23.8px, 6.0px                  3.0px                      3
08-sanctioned-cursor     21.0px, 13.0px                 7.0px                      3
09-lone-cursor           21.0px                         none                       1

Tile gradient vs white, WCAG non-text floor is 3.0:1
  v1 light end #6ea8fe     white 2.42:1
  v1 dark end #3a7bfd      white 3.87:1
  v2 light end #3a7bfd     white 3.87:1
  v2 dark end #1f5fe0      white 5.57:1
```

Findings:

- **`06`'s two cursors do fuse.** One light component, 36.0px — the big white
  cursor and the small cursor's white keyline merge into a single mass. The
  user research was right and the earlier vision read was wrong. F1's fix did
  not achieve its purpose at the size that mattered.
- **`07` is the only candidate with two *separate* light components** (23.8px
  and 6.0px) plus a distinct ink core. Two marks genuinely survive.
- **F2 is confirmed numerically.** The original tile's light end computes
  2.42:1 against white, under the 3:1 non-text floor; the corrected tile runs
  3.87–5.57:1. `01` is disqualified on measurement, not taste.
- **Metric limitation, stated honestly:** `04` and `05` report zero components
  because they are mid-luminance blue glyphs on transparency and fall into
  neither the near-white nor the near-ink class. The measurement says nothing
  about them. Both were already cut in round 1 on other grounds.

## The deciding risk was then tested directly

The product council's sole disqualification of `07` was that two cursors
meeting might read as an **X / cancellation / collision** — an alarm-adjacent
semantic the brief forbids. The council itself flagged that read as probably
vision-model variance (its dissent #3).

So it was put to a blind test: `07` at 128px beside an 8× blowup of its true
16px render, no context, no leading terms, and the alarm question asked
directly. Result, verbatim, in `SELECTION.md`. The reader described two cursors
pointing toward each other, guessed *"an app that lets two people connect their
computers or work together on the same screen"*, answered **no** to the X /
cancellation / collision question with a reason (*"the arrows aren't crossing
over each other"*), and counted three separate things in the 16px blowup.

The named risk did not reproduce under blind conditions. `07` selected.

## Recorded dissent carried forward

1. **Product council still prefers the bare cursor** (`09`). Its case is failure
   mode: when `07`'s idea is lost, the leftover is ambiguous; `09` has no idea
   to lose. Countered by the toolbar-relocatability finding — a bare pointer is
   the most collision-prone silhouette on the shelf — and by the blind test
   showing `07`'s read is not in fact alarming. Recorded, not resolved.
2. **No human has seen any of these.** Every persona in both rounds was
   simulated. The product council asked for a moderated study, n≥5, with the
   debugging banner visible, tripwire at ≥20% alarm-family language attributed
   to the icon. Not run. Logged in `SELECTION.md` as a follow-up; the swap is
   one command and fully reversible.
3. **F4 is contested.** The "encode invitation positively" mandate came from
   design-critique lenses and was never validated the way the contrast and
   legibility findings were. `07` satisfies it; a future round should not treat
   it as settled.

---

## Post-review verification gate (I6), and what it changed

Selection was **not** final at the end of round 2. The 16px inspection was run
against the real raster afterwards, and it moved the answer twice.

**1. `07-handshake` is clipped.** A deterministic check — render each candidate
at 512px, render the tile silhouette alone, and count mark pixels (near-white
or near-ink) that fall outside the silhouette — found:

```
06-paired-cursors      mark px outside tile:    0  -> clean
07-handshake           mark px outside tile:   65  -> CLIPPED
09-lone-cursor         mark px outside tile:    0  -> clean
10-handshake-large     mark px outside tile:    0  -> clean
```

The large cursor's white keyline runs off the bottom and right of the rounded
tile. Neither review round caught it; a vision read of the 128px render did
(*"the white outline of the large black cursor is clipped by the bottom edge
and the right edge"*), and the pixel check confirmed it. `07` was replaced by
`10-handshake-large` — same concept, same pose, re-proportioned to fit, 0px
outside.

**2. 16px cannot hold two arrow glyphs.** Inspecting the true 16px raster of the
two-cursor artwork, magnified 16× with no smoothing:

> *"They have degraded into blobs… the disconnection between the top-left
> cluster and the central cluster looks like a rendering error or corrupted
> data. It looks broken because a standard mouse cursor is a single, contiguous
> object, and this is split into two pieces."*

This is the one semantic category the brief forbids by name. Enlarging the
cursors did not fix it — it is a hard limit of the raster, not a craft error.
The same inspection of the **32px** raster returned *"they clearly read as
arrow/mouse pointer shapes… perfectly identifiable."*

So the shipped option renders 16px from a reduced source (same tile, same
cursor, second cursor dropped) and 32/48/128 from the full artwork. The
reduction is version-controlled as `10-handshake-large/source-16px.svg`.

Re-inspected after the change, the shipped 16px file returns: *"It clearly
reads as an arrow / mouse pointer shape… No [not broken]… perfectly
identifiable and relocatable. It is not mush."*

**What this vindicates.** The product council's winner was the bare cursor, on
the argument that failure mode beats meaning at toolbar size. The measurements
agree with it exactly at 16px — and disagree above it, where the two-cursor
mark measures legible and reads correctly. The shipped set takes the council's
answer at the size its argument holds and the research panel's answer at every
size where it does not.
