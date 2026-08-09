# Icon concepts — amplifier-browser-bridge

Five candidate toolbar icons. Each is authored as SVG at viewBox `0 0 128 128`
and rendered to 16/32/48/128 px PNG by `build_icons.py`.

## What the icon has to carry

The product is **cross-device browser control**: an agent running on one
machine acts as a *second operator* inside the user's own logged-in browser on
another device.

Three constraints shape the mark:

1. **The banner problem.** The extension requests `<all_urls>` and
   `chrome.debugger`. Edge shows an unsuppressable *"started debugging this
   browser"* banner the entire time it runs. This icon is the thing a user
   associates that banner with. It must read as *sanctioned, invited, mine* —
   not as *something is watching me*. This rules out eye/aperture/surveillance
   imagery on semantics alone.
2. **16px is the real size.** The toolbar render is 16px. A concept that only
   works at 128px has failed.
3. **Both toolbar themes.** Light and dark. A filled tile guarantees contrast
   on both; a transparent glyph is lighter-weight but must survive on white
   and on near-black.

Palette is taken from the project's own `style.css` `:root` tokens, so the icon
is the same blue as the product surface: `--accent #6ea8fe`,
`--accent-strong #3a7bfd`, `--bg #0b0d12`.

## Candidates

| # | Name | The idea it encodes | Treatment |
|---|------|--------------------|-----------|
| 01 | `01-second-cursor` | Two cursors in one browser — the agent is a second operator, not a takeover. | Filled blue tile |
| 02 | `02-bridge-span` | A bridge read literally: two endpoints, one deliberate span joining them. | Filled blue tile |
| 03 | `03-handoff` | Cross-device control — the machine you are on reaching the browser you are not on. | Filled blue tile |
| 04 | `04-tab-cursor` | Your tab with a cursor cut out of it — the pointer is part of your browser, not on top of it. | Transparent glyph |
| 05 | `05-inbound` | Your viewport, with commands entering through a deliberate opening in the frame. | Transparent glyph |

### 01-second-cursor

A rounded blue gradient tile. Two pointer arrows: a large white one (you) and a
smaller navy one (the agent), offset diagonally so they overlap. Neither owns
the surface. The navy cursor is deliberately *not* white — the two operators
are visibly different actors.

### 02-bridge-span

A rounded blue gradient tile. A white arch springs between two white node
circles; a navy deck runs horizontally beneath. The right node has a blue
centre, marking it as the browser end. Literal bridge.

### 03-handoff

A rounded blue gradient tile. A white phone (left) and a white desktop window
(right), each with a navy title bar, joined by a white arrow arcing over the
gap between them. The gap is the point: two devices, not one.

### 04-tab-cursor

No tile. A single solid blue silhouette of a browser window with a tab on its
top-left, with a pointer arrow knocked *out* of the body as negative space.
One shape, two reads. The cursor is a hole in the browser rather than an
object placed on it.

### 05-inbound

No tile. A thick blue bracket open to the left — a viewport with a deliberate
gap in its frame — with two chevrons entering through the opening, the inner
one lighter than the outer. Commands arriving from elsewhere.

## Files

```
icon-options/
  01-second-cursor/{source.svg,IDEA.md,icon-{16,32,48,128}.png}
  02-bridge-span/  { …same… }
  03-handoff/      { …same… }
  04-tab-cursor/   { …same… }
  05-inbound/      { …same… }
  preview-light.png   contact sheet on white, incl. 6x blowup of the 16px render
  preview-dark.png    contact sheet on #0b0d12
  build_icons.py      regenerates everything from the SVG sources
```

## Round-2 candidates

Added after review round 1. See `REVIEW-ROUND-1.md` for the findings (F1–F4)
that produced them, and `SELECTION.md` for the final choice.

| # | Name | The idea it encodes | Origin |
|---|------|--------------------|--------|
| 06 | `06-paired-cursors` | 01 revised: two cursors, executed so the agent cursor still reads at 16px. | fixes F1/F2/F3 |
| 07 | `07-handshake` | Two pointers meeting tip to tip — invitation as mutual agreement. | attempts F4 |
| 08 | `08-sanctioned-cursor` | A pointer wearing a granted mark — control affirmatively authorised. | attempts F4 |
| 09 | `09-lone-cursor` | One pointer, nothing else. Safest failure mode, least meaning. | the product council's round-2 recommendation, built so it could be measured rather than argued about |
| 10 | `10-handshake-large` | 07's concept and pose, re-proportioned to fit the tile and to survive the raster. | **selected** |

`10-handshake-large` additionally ships `source-16px.svg`, an optical reduction
used only at 16px where two arrow glyphs measurably do not survive.
