# Lane tooling

- `census.py` — per-tool description bytes and serialized wire bytes for the 31 `browser_*` tools.
  Run at the merge-base (`git stash`) and on the branch to reproduce the before/after table in
  `../DONE-NOTE.md` §1–2.
- `fidelity_check.py` — the mechanical half of the fidelity gate. Reads the two evidence dumps
  (`../evidence-descriptions-stock.txt`, `../evidence-descriptions-lean.txt`) and reports any
  identifier-shaped token present in stock and absent in lean. Expected residue: 2, both in
  `browser_archive`, both non-behavioural — see `../DONE-NOTE.md` §3.

The *enforcing* check is not here: it is
`modules/tool-browser-bridge/tests/test_description_budget.py`, which runs in CI.
