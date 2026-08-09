# Review round 1 — five original concepts

Reviewers invoked: **design council** (7 lenses: originality-critic,
coherence-guardian, human-advocate, craft-inspector, context-tester,
purpose-keeper, emotion-reader — all 7 responded, 2 internal debate rounds) and
**simulated user research** (3 personas, vision-model inspection of the rendered
contact sheets). Both worked from the actual rendered PNGs via `nano-banana
analyze`, not from the SVG source alone.

Product council was deliberately held for round 2, so that round has an
independent panel rather than re-running the same lenses.

## Verdicts

| Candidate | Design council (final) | User research | Disposition |
|---|---|---|---|
| `01-second-cursor` | 1 FAIL / 6 CONCERN — least-bad, carry forward as base | **Advance** — wins all three personas | **Revise** |
| `02-bridge-span` | 3 FAIL / 4 CONCERN — contested | Kill | Cut |
| `03-handoff` | **7/7 FAIL** — no dissent | Kill | Cut |
| `04-tab-cursor` | 6 FAIL / 1 dissent | Kill | Cut |
| `05-inbound` | 4 FAIL / 3 CONCERN | Kill — most alarming of the set | Cut |

## Findings that drive the revision

**F1 — 01's whole meaning dies at 16px.** The navy "agent" cursor — the element
that carries *second operator, not takeover* — collapses to "a jagged dark
blob" (council) and is "almost invisible" on dark (research). What survives is
one ordinary cursor. Research notes this is at least a *safe* failure (reads as
a generic pointer, not as damage or intrusion), but the concept is gone.
→ **Fix: differentiate the two cursors by more than value. Give the agent
cursor a light keyline so it survives at 16px on both themes.**

**F2 — the shared tile has a contrast defect.** human-advocate computed white
elements landing in the tile's light-gradient corner at ~2.42–2.82:1, below the
WCAG 3:1 non-text floor. This is a defect in the container, affecting 01, 02
and 03 identically.
→ **Fix: darken the gradient so white clears 3:1 everywhere on the tile.**

**F3 — magic numbers.** coherence-guardian: the two cursor instances in 01 are
scaled 2.55 and 2.15, "a magic-number break inside otherwise-precise tile
geometry."
→ **Fix: a stated ratio.**

**F4 — the cross-cutting one, raised independently by originality-critic and
purpose-keeper.** *"None of the five attempted a positive visual device for
consent/invitation itself. All five solve 'not surveillance' negatively (avoid
eyes, avoid apertures) rather than positively inventing 'invited.'"*
purpose-keeper: *"Absence-of-alarm has no settled iconographic vocabulary the
way presence-of-a-threat does, which is exactly why these concepts keep either
collapsing into generic blobs at 16px or inverting into the prohibited
reading."*
→ **Fix: round 2 must include at least one candidate that encodes invitation
positively, not by subtraction.**

## Why each cut candidate was cut

- **03-handoff** — the connecting arc is stroke-width 8 on a 128 viewBox =
  exactly 1.0px at 16px, the thinnest stroke in the set. The one element that
  turns "phone + desktop" into "handoff" is the first thing lost. Also read
  unprompted by the vision model as Apple Continuity iconography.
- **04-tab-cursor** — the cursor knockout is a ~10-unit barb ≈ 1.25px at 16px;
  cannot survive rasterisation. Worse, a hole has no authored colour, so on
  dark theme it "looks like a broken or corrupted icon" — the opposite of the
  reassurance the brief exists to produce. Research adds a category collision:
  blind-read as a tab/bookmark manager. Recorded dissent: originality-critic
  held that the negative-space mechanism is the only genuinely novel idea in
  the set.
- **05-inbound** — chevrons merge into the bracket at 16px, worst silhouette of
  the five; and under the banner-panic test it is the *most alarming* shape:
  "something is getting in through a gap," which is precisely the semantic the
  brief must rule out. Also fails the light-theme condition specifically.
- **02-bridge-span** — the twin terminus circles collapse to identical dots at
  16px, which two independent methods (vision impression and computed contrast)
  both read as a symmetric **face**. Shipping a face next to a debugging banner
  is the risk that decided it. Recorded dissent: emotion-reader rated it the
  strongest genuine warmth in the set and fixable by differentiating the nodes.

## Defect found in the record itself

craft-inspector and purpose-keeper independently caught that `CONCEPTS.md`
described 05's outer chevron as lighter than the inner one, while the shipped
SVG has it reversed. The artifact is the source of truth; `CONCEPTS.md` has
been corrected to match. 05 is cut, so the SVG was left as rendered.

## Carried into round 2

Revisions `06`, `07`, `08` (below), reviewed against `01` as an unmodified
control.
