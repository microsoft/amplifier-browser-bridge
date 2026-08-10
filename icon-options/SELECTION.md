# Selected: `10-handshake-large`

Two pointers meeting tip to tip on a blue tile. The white one is you; the dark
one with the light keyline is the agent. They approach and touch. Nothing
enters through a gap, nothing overlaps or overtakes anything — two operators
meet on the same surface.

At 16px it renders from a reduced source: same tile, same cursor, second cursor
dropped. That is not a second design, it is an optical reduction, and it is
there because the 16px raster was measured rather than assumed. See
**The 16px gate** below.

## What it encodes

The product's actual proposition, not a paraphrase of it: **a second operator
in your browser, by agreement.** The two-cursor pairing states there are two
actors; the tip-to-tip pose states the relation between them is mutual rather
than intrusive.

It is also the answer to the one instruction both round-1 reviewers raised
independently (F4): *invent a positive glyph for "invited," don't just keep
subtracting the surveillance one.* A meeting is a positive device. An arrow
piercing a frame (`05`) is not, and an eye is not.

Shown at 128px with no context and no leading terms, a reader described it as
*"two mouse cursors… their tips are close together in the middle"* and guessed
*"two people using the same screen together, like remote support or
collaborating"* — the product, unprompted.

## Why it beat the others

The deciding evidence is measured, not asserted. `measure_16px.py` counts
connected components of near-white and near-ink pixels in each candidate's real
`icon-16.png` and computes tile contrast against the WCAG 3:1 non-text floor:

| Candidate | Marks at 16px | White-on-tile | Clipped? | 16px verdict on inspection |
|---|---|---|---|---|
| `01-second-cursor` | 1 light + speck | **2.42:1 — fails** | clean | idea lost |
| `06-paired-cursors` | 1 light + speck | 3.87–5.57:1 | clean | "mush… blurry white blob", but **not** broken-looking |
| `07-handshake` | 2 lights + dark | 3.87–5.57:1 | **CLIPPED — 65 mark px outside the tile** | "mush" |
| `08-sanctioned-cursor` | 2 lights + dark | 3.87–5.57:1 | clean | check mark unresolvable |
| `09-lone-cursor` | 1 light | 3.87–5.57:1 | clean | "reads perfectly as an arrow" |
| **`10-handshake-large`** | **1 light (reduced)** | 3.87–5.57:1 | clean | **"perfectly identifiable and relocatable"** |

- **Beat `01`** on a measured accessibility floor: 2.42:1 against white, below
  the 3:1 non-text minimum. Not a taste call.
- **Beat `06`** on carrying the idea. `06` was built to repair F1 and still
  fuses its two cursors into a single 36px light component. It is safe and
  benign, and says nothing.
- **Beat `07`** on a hard defect `07` was never going to survive: a
  deterministic check against the tile silhouette found **65 mark pixels
  hanging off the tile edge** — the large cursor's keyline runs past the bottom
  and right of the rounded square. `10` is the same concept and pose,
  re-proportioned to fit, and measures **0** pixels outside.
- **Beat `08`** on failure mode. The check mark does not resolve at 16px; what
  is left is a dark fleck inside a white circle, read by reviewers as a flag,
  an error, or another extension's notification badge.
- **Beat `09`** on saying anything at all. `09` is the most legible mark in the
  set and the emptiest: one generic pointer, the most common toolbar silhouette
  there is. `10` ships that exact reduction at 16px, and adds the second cursor
  back at every size that can carry it.

## The 16px gate

The I6 inspection was run against the real raster, not the design intent, and
it changed the outcome twice.

**First finding — the two-cursor mark fails at 16px, on its own terms.** Asked
to judge a 16× nearest-neighbour blowup of the true 16px file, an independent
read of the full two-cursor artwork returned:

> *"They have degraded into blobs… the disconnection between the top-left
> cluster and the central cluster looks like a rendering error or corrupted
> data. It looks broken because a standard mouse cursor is a single, contiguous
> object, and this is split into two pieces."*

"Reads as broken" is the exact category the brief exists to avoid, sitting next
to an unsuppressable *"started debugging this browser"* banner. Making the
cursors larger did not fix it; 16 pixels cannot hold two arrow glyphs and a gap.

**Second finding — 32px can.** The same inspection of the 32px raster:

> *"Two distinct marks… a small solid white mouse cursor arrow… a larger black
> mouse cursor arrow with a thick white outline. They clearly read as
> arrow/mouse pointer shapes… perfectly identifiable."*

So the cut is between 16 and 32, and that is where the reduction is applied.

**The shipped 16px file, inspected:**

```
     ++++++++++++
    ++++++++++++++
   +++++++++++++++
   ++++#+++++++++++      # near-white   + tile   (real raster,
   ++++##++++++++++                              extension/icons/icon-16.png)
   ++++###+++++++++
   ++++####++++++++
   ++++######++++++
   ++++####++++++++
   ++++#+##++++++++
   +++++++##+++++++
   ++++++++++++++++
```

> *"It clearly reads as an arrow / mouse pointer shape. The characteristic
> silhouette is perfectly intact… No [it does not read as broken]… perfectly
> identifiable and relocatable. It is not mush."*

## Residual risk, recorded rather than resolved

- **The 16px mark is generic.** It is a plain pointer, and a plain pointer
  collides with every select-tool and remote-desktop glyph already on the
  shelf. This is a deliberate trade: legible-and-benign beat distinctive-and-
  broken at the one size where the icon is mostly seen. The distinctiveness
  lives at 32px and above.
- **16px and 128px are not the same picture.** Standard optical reduction, but
  it is a real inconsistency and a coherence reviewer would flag it. Both share
  tile, palette, corner radius and cursor glyph; the reduction only drops the
  second cursor.
- **Nothing here was validated on a human.** Every persona in both rounds was
  simulated, and every "read" above came from a vision model. The product
  council asked for a moderated study, n≥5, run with the debugging banner
  actually visible, tripwire at ≥20% of participants using alarm-family
  language attributed to the icon. That study has not been run. Swapping the
  icon is one command (`icon-options/pick.sh`) and fully reversible, so this is
  logged as a follow-up, not a ship blocker.
- **F4 is contested, not settled.** The product council's own dissent notes the
  "encode invitation positively" mandate came from design-critique lenses and
  was never validated the way the contrast and legibility findings were. `10`
  satisfies it at 32px and above and abandons it at 16px.
- **The product council would still ship `09`.** Its case is failure mode: `09`
  has no idea to lose. At 16px the two are now identical artwork, so the
  disagreement only concerns 32px and above — where the measured evidence shows
  the two-cursor mark is legible and correctly read.

## The moderated study: owner, gate, re-inspection, tripwire

Round 2 left one requirement unsatisfied rather than unmet-and-forgotten: no
human has seen this icon. This section is the commitment that closes it. It
exists because "logged as a follow-up" is not a plan until someone is
accountable for it, it is due at a moment, and it fails loudly.

**Owner: `bkrabach`** — at the time this section was written, the GitHub owner
of `origin`, which was then a personal repository under the `bkrabach` account
(since deleted; the project now lives at `github.com/microsoft/amplifier-browser-bridge`,
and that original URL is intentionally not linked here because it no longer
resolves). Named from evidence, not assigned: the repo carried no `CODEOWNERS`,
and all 71 commits were authored by the `Amplifier` bot identity, so the remote
owner was the only accountable human this repository actually attested to.
**Acknowledgement is pending** — this is a derived assignment, not an accepted
handoff, and the owner may reassign it.

**Due: the store-submission gate, not a calendar date.** The study blocks
marking the submission checklist in `PUBLISH.md` (owned by `goal/parity`)
complete. It does **not** block merging `goal/icon` to `main`. The anchor is
deliberate: the icon has no external audience until listing, so first listing
is simultaneously the earliest moment the study can produce real data and the
last moment the answer can still be changed for free.

**The specific 16px re-inspection.** Run against the raster in the branch under
test — never the SVG source, never a blowup, never a mockup:

```
python3 icon-options/measure_16px.py
#  expect: 10-handshake-large   21.0px   none   1
#  expect: tile contrast >= 3.87:1 (WCAG non-text floor is 3.0:1)

md5sum extension/icons/icon-16.png icon-options/10-handshake-large/icon-16.png
#  expect: identical -- currently 140f7ab03026ad0e8e44c9b532a77939
```

The md5 check is the load-bearing one. The whole reason this icon has a reduced
16px source is that the two-cursor artwork reads as *broken* at 16px. If a build
change ever lets the full artwork reach the 16px slot, these two hashes diverge
and that defect is back. Participants must see the real 16px file in a live
toolbar with the *"started debugging this browser"* banner actually visible —
the banner is the context that makes an alarm read possible at all.

**Tripwire.**

| | |
|---|---|
| Sample | Moderated, n ≥ 5 |
| Measured | Share of participants using alarm-family language — *error, broken, warning, stop, cancel, blocked, virus, hacked* — **attributed to the icon**, asked as a separate question from the banner |
| Threshold | **≥ 20%** (≥1 of 5, ≥2 of 10) |
| If tripped | `cd icon-options && ./pick.sh 09-lone-cursor` |
| Decided by | The owner above — not a panel |

The revert is one command and costs nothing at the size in dispute: `09` and
`10` ship **byte-identical 16px files** (md5 `140f7ab0…`), so the tripwire can
only ever change 32/48/128. That is exactly the range where the two panels
disagreed and where dissent #1 remains on the record, unresolved. The tripwire
is the instrument that settles it with data instead of another argument.

**If the study does not run by the gate**, it resolves BLOCKED on the `PUBLISH.md`
checklist, named as such. The icon may still ship. What may not happen is any
claim that it was validated on humans — every read in this document came from a
vision model, and that stays true until this study says otherwise.
