#!/usr/bin/env python3
"""Render every icon concept in this directory to PNGs at 16/32/48/128 and
build light/dark side-by-side preview composites.

Each concept is authored as an SVG at viewBox 0 0 128 128 so cairosvg can
render any size crisply. Re-run after editing SOURCES:

    python3 icon-options/build_icons.py
"""

from __future__ import annotations

from pathlib import Path

import cairosvg
from PIL import Image, ImageColor, ImageDraw, ImageFont

ROOT = Path(__file__).parent
SIZES = [16, 32, 48, 128]

# ---------------------------------------------------------------------------
# Palette -- taken from the project's own style.css :root tokens so the icon
# is the same blue as the product surface it belongs to.
#   --accent: #6ea8fe   --accent-strong: #3a7bfd   --bg: #0b0d12
# ---------------------------------------------------------------------------
BLUE_LT = "#6ea8fe"
BLUE = "#3a7bfd"
BLUE_DK = "#1f5fe0"
NAVY = "#0b2545"
INK = "#0b0d12"
PAPER = "#FFFFFF"

# A classic pointer arrow drawn in a 0..24 box, tip at the origin.
CURSOR = "M0 0 L0 17.6 L4.6 13.4 L7.4 20.4 L11.0 18.9 L8.2 12.1 L14.2 11.7 Z"

SOURCES: dict[str, str] = {}


# 01 -- TWO OPERATORS ---------------------------------------------------------
# Idea: two cursors in one browser. You are white; the agent is navy. Neither
# one owns the surface -- the agent is a second operator, not a takeover.
SOURCES["01-second-cursor"] = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="g1" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{BLUE_LT}"/>
      <stop offset="1" stop-color="{BLUE}"/>
    </linearGradient>
  </defs>
  <rect x="4" y="4" width="120" height="120" rx="26" fill="url(#g1)"/>
  <g transform="translate(24,20) scale(2.55)">
    <path d="{CURSOR}" fill="{PAPER}"/>
  </g>
  <g transform="translate(66,56) scale(2.15)">
    <path d="{CURSOR}" fill="{NAVY}"/>
  </g>
</svg>"""


# 02 -- THE SPAN --------------------------------------------------------------
# Idea: a bridge, read literally. Two endpoints -- the agent's machine and your
# browser -- and one deliberate span joining them.
SOURCES["02-bridge-span"] = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="g2" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{BLUE_LT}"/>
      <stop offset="1" stop-color="{BLUE}"/>
    </linearGradient>
  </defs>
  <rect x="4" y="4" width="120" height="120" rx="26" fill="url(#g2)"/>
  <path d="M 30 86 Q 64 26 98 86" fill="none" stroke="{PAPER}"
        stroke-width="12" stroke-linecap="round"/>
  <rect x="24" y="86" width="80" height="12" rx="6" fill="{NAVY}"/>
  <circle cx="30" cy="86" r="15" fill="{PAPER}"/>
  <circle cx="98" cy="86" r="15" fill="{PAPER}"/>
  <circle cx="98" cy="86" r="6.5" fill="{BLUE}"/>
</svg>"""


# 03 -- CROSS-DEVICE ----------------------------------------------------------
# Idea: the machine you are on reaching the browser you are not on. A phone
# hands control to a desktop window across the gap between devices.
SOURCES["03-handoff"] = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="g3" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{BLUE_LT}"/>
      <stop offset="1" stop-color="{BLUE}"/>
    </linearGradient>
  </defs>
  <rect x="4" y="4" width="120" height="120" rx="26" fill="url(#g3)"/>
  <!-- desktop window, right -->
  <rect x="54" y="40" width="58" height="48" rx="8" fill="{PAPER}"/>
  <rect x="54" y="40" width="58" height="13" rx="8" fill="{NAVY}"/>
  <rect x="54" y="47" width="58" height="6" fill="{NAVY}"/>
  <!-- phone, left -->
  <rect x="18" y="52" width="26" height="44" rx="7" fill="{PAPER}"/>
  <rect x="18" y="52" width="26" height="9" rx="7" fill="{NAVY}"/>
  <rect x="18" y="56" width="26" height="5" fill="{NAVY}"/>
  <!-- handoff arc, phone -> window -->
  <path d="M 26 44 Q 64 12 100 30" fill="none" stroke="{PAPER}"
        stroke-width="8" stroke-linecap="round"/>
  <path d="M 100 30 L 86 26 L 94 40 Z" fill="{PAPER}"/>
</svg>"""


# 04 -- YOUR TAB, DRIVEN ------------------------------------------------------
# Idea: a browser tab with a cursor cut clean out of it. The pointer is not on
# top of your browser -- it is part of it. Transparent glyph, no tile.
SOURCES["04-tab-cursor"] = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <path fill="{BLUE}" fill-rule="evenodd" d="
    M 20 30 Q 20 22 28 22 L 60 22 Q 68 22 68 30 L 68 34 L 100 34
    Q 112 34 112 46 L 112 96 Q 112 108 100 108 L 28 108
    Q 16 108 16 96 L 16 34 Z
    M 46 46 L 46 92 L 58 81 L 66 99 L 76 94.6 L 68 77 L 84 75 Z
  "/>
</svg>"""


# 05 -- COMMANDS ARRIVING -----------------------------------------------------
# Idea: your viewport, with instructions entering it from outside through a
# deliberate opening in the frame. Transparent glyph, no tile.
SOURCES["05-inbound"] = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <path d="M 44 26 L 96 26 Q 110 26 110 40 L 110 88 Q 110 102 96 102 L 44 102"
        fill="none" stroke="{BLUE}" stroke-width="13"
        stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M 18 44 L 40 64 L 18 84" fill="none" stroke="{BLUE}"
        stroke-width="13" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M 52 44 L 74 64 L 52 84" fill="none" stroke="{BLUE_LT}"
        stroke-width="13" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""


# ---------------------------------------------------------------------------
# ROUND 2 REVISIONS. Driven by review round 1 (see REVIEW-ROUND-1.md):
#   F1 the agent cursor must survive 16px -> it gets a light keyline
#   F2 white on the tile must clear WCAG 3:1 -> gradient darkened to
#      #3a7bfd (3.90:1 vs white) -> #1f5fe0 (5.62:1 vs white)
#   F3 no magic numbers -> the two cursors are a stated 1.3 : 1
#   F4 encode invitation POSITIVELY, not by avoiding surveillance -> 07, 08
# ---------------------------------------------------------------------------

TILE2 = f"""<defs>
    <linearGradient id="gt" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{BLUE}"/>
      <stop offset="1" stop-color="{BLUE_DK}"/>
    </linearGradient>
  </defs>
  <rect x="4" y="4" width="120" height="120" rx="26" fill="url(#gt)"/>"""


# 06 -- 01 REVISED ------------------------------------------------------------
# Same idea, executed so it survives. The agent cursor now carries a white
# keyline, so it stays a distinct second actor at 16px on either theme instead
# of collapsing into a dark blob.
SOURCES["06-paired-cursors"] = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  {TILE2}
  <g transform="translate(22,20) scale(2.6)">
    <path d="{CURSOR}" fill="{PAPER}"/>
  </g>
  <g transform="translate(64,54) scale(2.0)">
    <path d="{CURSOR}" fill="{PAPER}" stroke="{PAPER}" stroke-width="12"
          stroke-linejoin="round"/>
    <path d="{CURSOR}" fill="{INK}"/>
  </g>
</svg>"""


# 07 -- MET, NOT BREACHED -----------------------------------------------------
# Positive device for invitation: two pointers meeting tip to tip. Mutual and
# symmetric -- nothing enters through a gap, two parties agree. The opposite
# grammar to an arrow piercing a frame.
SOURCES["07-handshake"] = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  {TILE2}
  <g transform="translate(52,54) rotate(180) scale(2.0)">
    <path d="{CURSOR}" fill="{PAPER}"/>
  </g>
  <g transform="translate(74,72) scale(2.0)">
    <path d="{CURSOR}" fill="{PAPER}" stroke="{PAPER}" stroke-width="12"
          stroke-linejoin="round"/>
    <path d="{CURSOR}" fill="{INK}"/>
  </g>
</svg>"""


# 08 -- THE SANCTIONED POINTER ------------------------------------------------
# Positive device for invitation: a pointer wearing a granted mark. Not "we
# avoided drawing an eye" -- an affirmative statement that this control was
# authorised. Both glyphs (arrow, check) are among the most legible in UI.
SOURCES["08-sanctioned-cursor"] = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  {TILE2}
  <g transform="translate(22,18) scale(2.8)">
    <path d="{CURSOR}" fill="{PAPER}"/>
  </g>
  <circle cx="88" cy="88" r="29" fill="{PAPER}"/>
  <path d="M 75 88 L 85 98 L 102 76" fill="none" stroke="{INK}"
        stroke-width="13" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""


# 09 -- THE LONE POINTER ------------------------------------------------------
# The product council's round-2 recommendation: 08 with the badge deleted. One
# white cursor on the corrected tile. Says nothing beyond "pointer", but has
# the most benign failure mode of anything in the set.
SOURCES["09-lone-cursor"] = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  {TILE2}
  <g transform="translate(34,26) scale(3.2)">
    <path d="{CURSOR}" fill="{PAPER}"/>
  </g>
</svg>"""


# 10 -- 07 AT MAXIMUM SIZE ----------------------------------------------------
# Same pose as 07, both cursors scaled to the largest that still fits two
# glyphs plus a visible gap inside the tile. Built because the I6 inspection of
# 07's real 16px raster found the marks separable but the arrow shapes lost --
# this tests whether that is fixable by size or is a hard limit of 16 pixels.
SOURCES["10-handshake-large"] = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  {TILE2}
  <g transform="translate(56,55) rotate(180) scale(2.15)">
    <path d="{CURSOR}" fill="{PAPER}"/>
  </g>
  <g transform="translate(68,64) scale(2.15)">
    <path d="{CURSOR}" fill="{PAPER}" stroke="{PAPER}" stroke-width="9"
          stroke-linejoin="round"/>
    <path d="{CURSOR}" fill="{INK}"/>
  </g>
</svg>"""


# Optical reduction, used ONLY at 16px. Measured: two arrow glyphs do not
# survive a 16px raster -- they read as one broken object (REVIEW-ROUND-2.md).
# 32px carries them perfectly. So the shipped option drops the second cursor at
# 16px rather than rendering it as unreadable debris. Same tile, same cursor.
REDUCTIONS: dict[str, str] = {
    "10-handshake-large": f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  {TILE2}
  <g transform="translate(34,26) scale(3.2)">
    <path d="{CURSOR}" fill="{PAPER}"/>
  </g>
</svg>""",
}


# ---------------------------------------------------------------------------
IDEAS = {
    "01-second-cursor": "Two cursors in one browser -- the agent is a second operator, not a takeover.",
    "02-bridge-span": "A bridge read literally: two endpoints, one deliberate span joining them.",
    "03-handoff": "Cross-device control -- the machine you are on reaching the browser you are not on.",
    "04-tab-cursor": "Your tab with a cursor cut out of it -- the pointer is part of your browser, not on top of it.",
    "05-inbound": "Your viewport, with commands entering through a deliberate opening in the frame.",
    "06-paired-cursors": "01 revised: two cursors in one browser, executed so the agent cursor still reads at 16px.",
    "07-handshake": "Two pointers meeting tip to tip -- invitation as mutual agreement, not as something entering a gap.",
    "08-sanctioned-cursor": "A pointer wearing a granted mark -- control that was affirmatively authorised.",
    "09-lone-cursor": "08 with the badge deleted: one pointer, nothing else. Safest failure mode, least meaning.",
    "10-handshake-large": "07 with both cursors at the largest size two glyphs plus a gap allow -- tests whether 16px arrow legibility is buyable with size.",
}


def render(name: str, svg: str) -> None:
    out = ROOT / name
    out.mkdir(parents=True, exist_ok=True)
    (out / "source.svg").write_text(svg + "\n")
    (out / "IDEA.md").write_text(f"# {name}\n\n{IDEAS[name]}\n")
    reduction = REDUCTIONS.get(name)
    if reduction is not None:
        (out / "source-16px.svg").write_text(reduction + "\n")
    for size in SIZES:
        source = reduction if (reduction is not None and size <= 16) else svg
        png = cairosvg.svg2png(bytestring=source.encode(), output_width=size, output_height=size)
        assert png is not None
        (out / f"icon-{size}.png").write_bytes(png)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def preview(bg: str, fg: str, out_name: str) -> None:
    """Grid: one row per concept, columns 128/48/32/16 plus a 16px 6x blowup."""
    names = sorted(SOURCES)
    pad, label_w, row_h = 24, 210, 152
    cols = [128, 48, 32, 16]
    blow = 16 * 6
    width = label_w + sum(cols) + blow + pad * (len(cols) + 2)
    height = pad * 2 + 34 + row_h * len(names)
    canvas = Image.new("RGB", (width, height), ImageColor.getrgb(bg))
    draw = ImageDraw.Draw(canvas)
    f_lbl, f_hdr = _font(19), _font(15)

    x = label_w + pad
    for size in cols:
        draw.text((x, pad), f"{size}px", font=f_hdr, fill=fg)
        x += size + pad
    draw.text((x, pad), "16px @6x", font=f_hdr, fill=fg)

    for row, name in enumerate(names):
        y0 = pad + 34 + row * row_h
        draw.text((pad, y0 + 50), name, font=f_lbl, fill=fg)
        x = label_w + pad
        for size in cols:
            img = Image.open(ROOT / name / f"icon-{size}.png").convert("RGBA")
            canvas.paste(img, (x, y0 + (128 - size) // 2), img)
            x += size + pad
        img16 = Image.open(ROOT / name / "icon-16.png").convert("RGBA")
        big = img16.resize((blow, blow), Image.Resampling.NEAREST)
        canvas.paste(big, (x, y0), big)

    canvas.save(ROOT / out_name)
    print(f"wrote {ROOT / out_name}  ({width}x{height})")


def main() -> None:
    for name, svg in sorted(SOURCES.items()):
        render(name, svg)
        print(f"rendered {name}: {', '.join(str(s) for s in SIZES)}")
    preview("#FFFFFF", "#0b0d12", "preview-light.png")
    preview("#0b0d12", "#e7e9ee", "preview-dark.png")


if __name__ == "__main__":
    main()
