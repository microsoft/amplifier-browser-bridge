# icons/

These are the PNGs both manifests point at, via `icons` and
`action.default_icon`. They currently hold **`10-handshake-large`** from
`../../icon-options/` — two pointers meeting tip to tip: you and the agent,
two operators on one surface, by agreement. See
`../../icon-options/SELECTION.md` for why it was chosen over the other nine
candidates.

**`icon-16.png` is deliberately not the same picture as the other three.** The
16px raster was measured, and 16 pixels cannot hold two arrow glyphs and a gap
— they degrade into two disconnected specks that read as a broken icon. 32px
holds them fine. So 16px renders from `source-16px.svg`: same tile, same
cursor, second cursor dropped. This is an optical reduction, not a second
design.

| File           | Size    | Used at                                     |
| -------------- | ------- | ------------------------------------------- |
| `icon-16.png`  | 16×16   | Toolbar action — **the size that matters**, optically reduced |
| `icon-32.png`  | 32×32   | Toolbar action (high-DPI / Windows)         |
| `icon-48.png`  | 48×48   | `edge://extensions/` management page        |
| `icon-128.png` | 128×128 | Store listing / install dialog              |

## Swap to a different option

Eight other candidates are kept in `../../icon-options/`, each with its
`source.svg`, its rendered PNGs, and a one-line `IDEA.md` stating what it
encodes. To change the active set:

```bash
# interactive list, with each option's idea printed
../../icon-options/pick.sh

# or pick by name (short or full)
../../icon-options/pick.sh handshake
../../icon-options/pick.sh 09-lone-cursor
```

Then reload the extension at `edge://extensions/` to pick up the new files.

## Regenerate from source

Every option is authored as SVG at viewBox `0 0 128 128` inside
`../../icon-options/build_icons.py`. Edit it and rerun:

```bash
python3 ../../icon-options/build_icons.py     # re-render all options + previews
python3 ../../icon-options/measure_16px.py    # 16px legibility + contrast check
../../icon-options/pick.sh <option>           # install one here
```

`measure_16px.py` is the honest check: it counts how many distinct marks
actually survive in the real 16px raster, and computes tile contrast against
the WCAG 3:1 non-text floor. Two of the original candidates were eliminated by
that script rather than by opinion.
