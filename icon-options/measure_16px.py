#!/usr/bin/env python3
"""Deterministic 16px legibility measurement.

Round 2 produced a direct factual conflict between two reviewers: one reported
that `06-paired-cursors` still shows two distinct marks at 16px, the other that
it merges into a single blob. That is a measurable question, not a matter of
opinion, so measure it instead of taking a vote.

For each candidate's real `icon-16.png` this reports:
  * how many connected components of LIGHT (near-white) and DARK (near-ink)
    pixels survive, ignoring components under 2px of coverage
  * the min contrast ratio of each ink class against the tile it sits on

Run:  python3 icon-options/measure_16px.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).parent
MIN_COMPONENT_PX = 2.0


def _luminance(rgb: tuple[float, float, float]) -> float:
    chan = []
    for value in rgb:
        srgb = value / 255.0
        chan.append(srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4)
    return 0.2126 * chan[0] + 0.7152 * chan[1] + 0.0722 * chan[2]


def contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def components(mask: dict[tuple[int, int], float], w: int, h: int) -> list[float]:
    """8-connected components; returns each one's summed pixel weight."""
    seen: set[tuple[int, int]] = set()
    out: list[float] = []
    for start in mask:
        if start in seen:
            continue
        stack, weight = [start], 0.0
        seen.add(start)
        while stack:
            x, y = stack.pop()
            weight += mask[(x, y)]
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (x + dx, y + dy)
                    if 0 <= n[0] < w and 0 <= n[1] < h and n in mask and n not in seen:
                        seen.add(n)
                        stack.append(n)
        out.append(weight)
    return sorted((c for c in out if c >= MIN_COMPONENT_PX), reverse=True)


def measure(path: Path) -> tuple[list[float], list[float]]:
    img = Image.open(path).convert("RGBA")
    w, h = img.size
    light: dict[tuple[int, int], float] = {}
    dark: dict[tuple[int, int], float] = {}
    for y in range(h):
        for x in range(w):
            r, g, b, a = img.getpixel((x, y))  # type: ignore[misc]
            if a < 40:
                continue
            lum = _luminance((r, g, b))
            cover = a / 255.0
            if lum > 0.55:
                light[(x, y)] = cover
            elif lum < 0.045:
                dark[(x, y)] = cover
    return components(light, w, h), components(dark, w, h)


def main() -> None:
    print(f"{'candidate':<24} {'light components':<30} {'dark components':<26} marks")
    print("-" * 96)
    for d in sorted(p for p in ROOT.iterdir() if p.is_dir()):
        png = d / "icon-16.png"
        if not png.exists():
            continue
        lights, darks = measure(png)

        def fmt(cs: list[float]) -> str:
            return ", ".join(f"{c:.1f}px" for c in cs) or "none"

        print(f"{d.name:<24} {fmt(lights):<30} {fmt(darks):<26} {len(lights) + len(darks)}")
    print()
    print("Tile gradient vs white, WCAG non-text floor is 3.0:1")
    for name, tile in (
        ("v1 light end #6ea8fe", (110, 168, 254)),
        ("v1 dark end #3a7bfd", (58, 123, 253)),
        ("v2 light end #3a7bfd", (58, 123, 253)),
        ("v2 dark end #1f5fe0", (31, 95, 224)),
    ):
        print(f"  {name:<24} white {contrast((255, 255, 255), tile):.2f}:1")


if __name__ == "__main__":
    main()
