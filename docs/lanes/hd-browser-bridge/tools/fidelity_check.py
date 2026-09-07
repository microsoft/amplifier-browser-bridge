"""Mechanical half of the fidelity gate: stock vs lean tool descriptions.

Every identifier-shaped token in a stock description -- anything containing `_` or
`.`, plus any ALL-CAPS word longer than two characters -- must survive into the lean
description. Those are the parameter names, response fields, capability names and
emphasis markers a caller actually needs; ordinary English is deliberately ignored.

This check is NOT sufficient on its own (a description can lose a whole rule while
keeping every token), which is why DONE-NOTE.md section 3 records a second, by-reading
pass and a third, pinned one in
`modules/tool-browser-bridge/tests/test_description_budget.py`.

Run from the repo root:
    uv run python docs/lanes/hd-browser-bridge/tools/fidelity_check.py

Expected residue: 2 tokens, both in `browser_archive`, both non-behavioural.
"""

from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
STOCK = HERE.parent / "evidence-descriptions-stock.txt"
LEAN = HERE.parent / "evidence-descriptions-lean.txt"

HEADER = re.compile(r"^===== (\S+) \((\d+)\) =====$")
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


def load(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    name: str | None = None
    buf: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        m = HEADER.match(line.rstrip("\n"))
        if m:
            if name:
                out[name] = "".join(buf).strip()
            name, buf = m.group(1), []
        elif name is not None:
            buf.append(line)
    if name:
        out[name] = "".join(buf).strip()
    return out


def interesting(token: str) -> bool:
    return "_" in token or "." in token or (token.isupper() and len(token) > 2)


def main() -> None:
    before, after = load(STOCK), load(LEAN)
    if set(before) != set(after):
        raise SystemExit(f"tool sets differ: {set(before) ^ set(after)}")

    total_missing = 0
    for name in before:
        stock, lean = before[name], after[name]
        stock_tokens = {t for t in TOKEN.findall(stock) if interesting(t)}
        lean_tokens = set(TOKEN.findall(lean))
        lean_tokens |= {part for t in TOKEN.findall(lean) for part in t.split(".")}
        missing = sorted(t for t in stock_tokens if t not in lean_tokens)
        total_missing += len(missing)
        flag = "  MISSING: " + ", ".join(missing) if missing else ""
        print(f"{name:28s} {len(stock):5d} -> {len(lean):5d}  ({len(lean) - len(stock):+6d}){flag}")

    stock_total = sum(len(v) for v in before.values())
    lean_total = sum(len(v) for v in after.values())
    print(f"\nTOTAL {stock_total} -> {lean_total}  ({lean_total - stock_total:+d})")
    print("tokens missing:", total_missing)


if __name__ == "__main__":
    main()
