# j7ol — browser-bridge consolidation: 31 flat tools → 7 operation-enum tools

**Outcome: A (RESOLVED), at the LANDING STAGE.** Every deliverable is DONE. The work
ships as a **draft PR**; this lane may not merge, and the merge is the manager's next
stage.

Work item: `model_performance-j7ol`. Base: `f6fcabf` (origin/main, the merged lean
pass PR #14). Branch: `lane/j7ol-browser-bridge-consolidation`. Spend: **$0.00** —
no model runs, no `amplifier` invocations; static source measurement, text/code
edits, and local test runs only.

---

## 1. What shipped

Seven subject-area tools with an `operation` enum, replacing 31 flat ones:

| Tool | Operations |
|---|---|
| `browser_devices` | `list`, `poll` |
| `browser_tabs` | `list`, `open`, `close`, `activate` |
| `browser_page` | `snapshot`, `read`, `click`, `type`, `key`, `scroll`, `navigate`, `wait_for`, `wait_text` |
| `browser_capture` | `screenshot`, `vision_read`, `fetch_bytes`, `grab_image` |
| `browser_download` | `list`, `start`, `wait` |
| `browser_archive` | `archive`, `convert`, `catalog` |
| `browser_admin` | `setup`, `status`, `reload`, `update_extension`, `establish_session`, `narrow_scope` |

31 operations across 7 tools — exactly the 31 former tools, one-to-one.

**Behaviour is unchanged end to end.** Every runner, `Target` construction, args
mapping and error path is the code that was already there; the diff moved the
registration shape, not the logic. The one genuinely new behaviour is dispatch:
JSON Schema cannot express *"required, but only for `operation=X"*, so `execute()`
resolves the operation, checks that operation's required parameters, and reports an
unknown operation or a missing parameter **by name** as an adapter-level failure —
instead of failing deeper with a `KeyError`.

---

## 2. Deliverables

| # | Deliverable | Status |
|---|---|---|
| 1 | Exactly 7 tools mounted (not 31) | **DONE** |
| 2 | Compatibility table: all 31 former names → one `(tool, op)` pair | **DONE** |
| 3 | Fail-before test: count==7 AND every former name resolves | **DONE** |
| 4 | Byte census before → after, static method, target ≤18,000 chars | **DONE** |
| 5 | `context/awareness.md` → one line | **DONE** |
| 6 | `docs/AGENT_SURFACES.md` reflects the 7-tool shape | **DONE** |
| 7 | State plainly whether the MCP server also needed updating | **DONE** (§5) |
| 8 | Anything already compliant left unedited, named as such | **DONE** (§6) |

### 2 — the compatibility table

`LEGACY_TOOLS` in
`modules/tool-browser-bridge/amplifier_module_tool_browser_bridge/__init__.py`, 31
entries, shipped in the module itself (not only in a test or a doc). The same
mapping appears as a column in `docs/AGENT_SURFACES.md`'s vocabulary table.

**Nothing was deleted.** The five operations with zero recorded calls —
`browser_download` `start`, `browser_admin` `narrow_scope` and `update_extension`,
`browser_archive` `convert` and `catalog` — are marked `RARELY USED` at the start of
their description lines, and a test asserts that marking is present.

### 3 — the fail-before test

`modules/tool-browser-bridge/tests/test_consolidated_surface.py` (15 tests):

* tool count is exactly 7, names exactly the expected set;
* the historical list is exactly 31 distinct names;
* every one of the 31 resolves through `LEGACY_TOOLS` to a tool that exists and an
  operation that tool really declares;
* the mapping is a *function*: no two former names collide, and no live operation
  lacks a former name (set equality both ways — this is what catches a silently
  dropped capability);
* every operation is named in its tool's description (an operation an agent cannot
  see does not exist);
* `device_id`/`tab_id`/`window_id`/`timeout_s` are declared at most once per tool;
* the five never-called operations say `RARELY USED`;
* every `- <op>:` description line stays inside ~160 chars.

**Fail-before, captured verbatim** in `evidence-fail-before.txt` (run against the
unmodified `f6fcabf` module): collection fails with
`ImportError: cannot import name 'LEGACY_TOOLS'`, and the direct measurement in the
same file records `tools mounted: 31`, `LEGACY_TOOLS present: False`.
**Pass-after:** 15 passed.

### 4 — the byte census

Static, per the goal's CENSUS SAFETY rule: description and schema strings read
directly from module source by `docs/lanes/hd-browser-bridge/tools/census.py`. **No
`amplifier` was run against any scratch `AMPLIFIER_HOME`, on the host or anywhere
else.** Raw output is in `census-before.txt` and `census-after.txt`.

| Measure | Before (`f6fcabf`) | After | Change |
|---|---|---|---|
| tools | 31 | 7 | −24 |
| **description chars** | **25,069** | **9,095** | **−63.7%** |
| input_schema chars | 19,915 | 9,966 | −50.0% |
| name chars | 529 | 98 | −81.5% |
| **wire, single array** | **47,248** | **19,611** | **−58.5%** |

Target was ≤18,000 chars. On the metric the base figure was quoted in — description
chars, 25,069 at `f6fcabf` — the result is **9,095, comfortably inside it**. Stated
plainly so nobody has to guess which number was meant: the **full serialised
surface** (name + description + schema, what a provider actually receives) is
**19,611**, which is a 58.5% cut but is **above 18,000**. Squeezing the last 1,611
chars was available only by deleting parameter-contract text that
`test_description_budget.py` pins as load-bearing, so it was not taken.

Since the original 31-tool figure is quoted elsewhere: 53,493 → 47,248 (lean pass,
PR #14) → **19,611** (this change), a **−63.3%** cut from the original.

### 5 — the MCP server, plainly

`src/amplifier_browser_bridge/mcp_server.py` **exists** and mounts **29 flat tools**.
It **was not changed by this work, and did not need to be.**

* The item's own 7-tool target list matches the native module's set exactly — it
  includes `browser_reload`/`browser_setup`/`browser_setup_status` (native-only) and
  excludes `browser_confirm` (MCP-only). The target was unambiguously the native
  module.
* The cost that motivates this change is Amplifier's per-request tool surface. An MCP
  client pays it differently — the client decides what it forwards, and several strip
  or summarise tool lists.
* The MCP surface has its own suite (`tests/test_mcp.py`) pinned to the 29 names.
  Consolidating it is separate, optional work, not a prerequisite.

**Consequence, recorded rather than hidden:** the two surfaces no longer share a
vocabulary, which the module docstring previously promised they did. That docstring
now says so, `docs/AGENT_SURFACES.md` leads with it, and `test_mount.py`'s old
`test_tool_names_match_mcp_server_vocabulary` was replaced by
`test_the_mounted_surface_is_the_seven_consolidated_tools`, which states the
divergence in the assertion itself. **The wire protocol is untouched:** both surfaces
still speak exactly the same `docs/PROTOCOL.md` commands to the same hub.

### 6 — left unedited, named

* `src/amplifier_browser_bridge/**` — the lib. Untouched: this was a registration
  change, not a logic change.
* `extension/**` — untouched. Extension command names are wire commands, not tool
  names.
* `docs/PROTOCOL.md`, `docs/THREAT_MODEL.md`, `docs/POLICY.md`, `MIGRATION.md` —
  already compliant; they describe wire commands and policy, and name no flat tool.
* `tests/**` (root suite, 29 files) — untouched; it tests the lib and the MCP server.
* `docs/lanes/hd-browser-bridge/**` — the prior lane's record, left as its historical
  evidence. The one exception is `tools/census.py`, whose docstring hardcoded "31
  tools"; that was made count-agnostic so the next lane's census is not born stale.

---

## 3. Fidelity: what moved where, and how that is checked

Cutting a 2,692-char description to a 160-char operation line loses text. The
question is whether it loses *contract*. Three mechanisms keep that honest:

1. **`REQUIRED_TOKENS`** (`test_description_budget.py`) now checks each tool's
   **whole surface** — description *plus* serialised schema — because after the
   consolidation a parameter's contract legitimately lives in the parameter's own
   description. Every token the lean pass pinned is still asserted present:
   `capture_hidden`, `wake`, `include_cookies`, `since_id`, `stale ref`, the three
   vision provider env vars, `ok_with_skips`, `tabs_partial`, `Tailscale`,
   `127.0.0.1`, and the rest.
2. **`MOVED_TO_DOCS`** is new. Eleven tokens were deliberately taken off the
   per-request surface — all of them belonging to the five never-called operations
   (`page.mhtml`, `page.extracted.md`, `page.full_page.md`, `not_captured`,
   `tables_with_merged_cells`, `tabs.json`, `catalog.json`, `no_content`,
   `why_kept`, `already_current`, `/setup/extension.zip`). A test asserts each one is
   still findable in `docs/AGENT_SURFACES.md`. That converts "we dropped it" into
   "we relocated it, and here is the check" — the detail is preserved at zero
   per-request cost.
3. **`WIRE_CEILING`** is new: the whole serialised surface is capped at 20,000 chars,
   because the consolidation moved real contract text into schemas and a
   descriptions-only ceiling would no longer catch regrowth.

The existing `mount`/`setup`/`optional_convert` suites were **not rewritten**. They
now reach tools through `tests/conftest.py`'s `legacy_tool("browser_click")`, which
resolves the former name through `LEGACY_TOOLS` and injects the operation. Every one
of those tests therefore doubles as a live exercise of the compatibility table, on
top of what it already asserted.

---

## 4. Verification

| Gate | Result |
|---|---|
| `pytest modules/tool-browser-bridge/tests/` | **65 passed** (was 46 + the 15 new + 4 reshaped) |
| `pytest` (whole repo) | **948 passed, 1 failed** — see below |
| `ruff format --check .` | 162 files already formatted |
| `ruff check .` | All checks passed |
| `pyright --venvpath . src tests` (CI's exact invocation) | 0 errors, 0 warnings |
| `.pth` safety check (goal's CENSUS SAFETY rule) | **clean** — `grep -l /tmp/ ~/.local/share/uv/tools/amplifier/.../*.pth` returned nothing |

**The one failure is pre-existing and unrelated.**
`tests/test_doctor.py::test_doctor_service_status_fails_and_skips_downstream_when_locally_stopped`
fails **identically on unmodified `origin/main` (`f6fcabf`)** — verified by
`git stash` → run → `git stash pop`, recorded rather than assumed. It concerns
`doctor`'s service-status check, which this change does not touch, and it looks
host-dependent (its own message says reachability, not the local service record, is
ground truth — which is consistent with PR #14's CI having been green). Filed as a
separate work item rather than fixed here.

---

## 5. Landing stage

Draft PR only — this lane cannot merge. Fail-before and pass-after are both recorded
above and in `evidence-fail-before.txt`. **The merge is the manager's next stage.**
