"""Browser-state archive orchestrator: capture the state of a browser at a chosen
depth, from "just the URLs" to "everything we can physically get" -- and return a
**manifest**, never the payload.

## Why this exists (the load-bearing design constraint)

The bug this project just fixed (`paging.py`, ed4a42d) was `browser_tabs` dumping
~640KB into an LLM context and truncating mid-response. A raw MHTML document, a full
`outerHTML` dump, or a browser's entire history can each individually be many times
that size. Adding a raw agent-facing tool that returns any one of those payloads
directly would recreate the exact same failure in a new shape.

So: every deep-capture payload (DOM, MHTML, screenshots) is written HUB-SIDE, straight
to disk, by `run_archive` below. The agent-facing tool (`browser_archive`, see
`mcp_server.py` / `modules/tool-browser-bridge`) returns only the **manifest** this
module builds -- paths, counts, byte sizes, per-tab status, failures. The bytes
themselves never become a tool's return value. Correspondingly, none of the ten wire
commands this module composes (`windows`, `page_state`, `mhtml`, `nav_history`,
`history_list`, `bookmarks_list`, `sessions_list`, `top_sites`, `reading_list`,
`cookies_list` -- see `protocol.py`'s `COMMANDS`) is registered as its own agent-facing
tool in this phase; `browser_archive` is the only new tool.

## The depth ladder

Cheapest to deepest, each level a strict superset of the level below:

    L0 -- windows/groups/tabs inventory. NO tab wake, NO page contact at all.
    L1 -- L0 + visible text per tab (`read`).
    L2 -- L1 + DOM/forms/localStorage/sessionStorage/scroll per tab (`page_state`).
    L3 -- L2 + screenshots per tab.
    L4 -- L3 + MHTML per tab.
    L5 -- L4 + navigation history per tab, AND browser-wide profile data
          (history/bookmarks/sessions/top_sites/reading_list). Cookies are NEVER
          included at L5 (or any level) unless `include_cookies=True` is passed
          explicitly -- see "Cookies are opt-in" below.

`mhtml`/`screenshot`/`nav_history` (CDP-based) and `text`/`dom` (JS injection) are
independent capture ROUTES, not a fallback ladder of one over the other -- a page can
be fully archivable via one route while being completely dead to the other. Observed
live: a tab showing a browser error page failed `text`/`dom` outright ("Frame with ID
0 is showing error page" -- JS injection cannot run on an error page at all), while
`mhtml`/`screenshot`/`nav_history` all succeeded on the same tab, landing ~147KB of
real artifacts on disk. A page that refuses all injected script is not necessarily
unarchivable: the CDP path reaches it regardless. This is why per-tab status has a
`"partial"` state (see "No silent partial success" below) rather than a binary one.

## The no-wake guarantee

At real-world scale (700+ tabs), most tabs are discarded (Edge unloaded their
renderer to reclaim memory) or asleep (Edge's "sleeping tabs" feature). Waking one --
whether by reloading it (`args.wake=true` on `read`/`page_state`) or by attaching CDP
to it (an unavoidable side effect of `screenshot`'s `capture_hidden`, `mhtml`, and
`nav_history` -- see `docs/PROTOCOL.md`'s "Discarded tabs" section) -- destroys real,
unsaved in-page state.

This module enforces the guarantee **before issuing any per-tab command**, not by
relying on each wire command's own (weaker, differently-shaped) protection: a tab
flagged `discarded`/`asleep` in the L0 inventory is SKIPPED for every L1+ capture
(recorded as `status: "skipped"` with an explanation) unless the caller passes
`wake=True`. L0 itself never contacts a tab at all -- it needs nothing from this
guarantee to be safe.

## No silent partial success

A per-tab (or profile-data) capture failure is recorded in the manifest and the run
CONTINUES -- one dead tab or one denied permission must never abort an otherwise-good
archive. But the manifest is built so a failure is impossible to miss:

- Every failure (and every intentional skip) is collected into a top-level
  `manifest["failures"]` list, never buried three levels deep.
- `manifest["status"]` is `"ok"` ONLY when there were zero failures and zero skips --
  `"ok_with_failures"` or `"ok_with_skips"` otherwise. A caller scanning just this one
  key can never mistake a degraded run for a clean one.
- `manifest["summary"]` reports every axis this module inventories or captures, and
  never collapses the INVENTORY axis (what actually exists in the browser) into the
  CAPTURE axis (what had its page content pulled down) -- the two are legitimately
  different numbers, at every depth. `windows_inventoried`/`tab_groups_inventoried`/
  `tabs_inventoried` are populated at EVERY depth including L0 (`None`, never a
  misleading bare `0`, if that axis's own capture failed -- see `_inventoried_count`).
  `tabs_capture_attempted`/`tabs_captured`/`tabs_skipped`/`tabs_failed` describe
  per-tab CONTENT capture and are honestly all `0` at L0 -- L0 does no page contact
  by design (see the depth ladder above), and that is success, not failure. `profile`
  is `None` below L5, otherwise item-level capture counts. An L0 archive of 735 tabs
  reports `tabs_inventoried: 735` even though `tabs_captured` is `0`: the summary
  must never read as "nothing was archived" when the inventory says otherwise.
- Per-tab status is not binary either. A tab's `captures` dict holds one entry per
  attempted capture (`text`/`dom`/`screenshot`/`mhtml`/`nav_history`, whichever ran
  at this depth); the tab's own `status` rolls those up into exactly one of three
  outcomes (see `_tab_status`): `"ok"` ONLY when every attempted capture succeeded,
  `"failed"` ONLY when every attempted capture failed, and `"partial"` -- the middle
  state -- when some succeeded and some failed. A tab that is intentionally
  `"skipped"` (no-wake guarantee, above) never contacts the browser at all and is a
  distinct fourth state, never confused with any of the three capture outcomes.
  `summary["tabs_partial"]` counts these explicitly alongside `tabs_captured`/
  `tabs_skipped`/`tabs_failed`. This is the direct fix for the bug where a tab with
  3 of 5 captures succeeding (mhtml/nav_history/screenshot ok, text/dom failed on a
  browser error page -- see the depth-ladder note on CDP vs. JS-injection routes,
  above) was reported `"failed"` and counted as zero captures in `summary`, discarding
  the ~147KB of real artifacts already written to disk for that tab. A `"partial"` (or
  `"failed"`) tab always contributes at least one entry to `manifest["failures"]` (see
  `_capture_tab`'s `record`), so a run containing any partial tab is never reported as
  plain `"ok"`.

- A `tab_id` explicitly named in `tab_ids` can vanish between when the caller read the
  tab inventory and when this archive ran (the tab was closed) -- or simply never
  existed at all -- and is then absent from the live `tabs` list entirely, with
  nothing to capture. This is a FIFTH per-tab state, `"not_found"`, distinct from
  `"ok"`/`"partial"`/`"failed"`/`"skipped"`: those four all describe a tab that
  existed at capture time; `"not_found"` means it didn't. Observed live: an archive
  requesting 4 tab_ids got capture entries for only 3 -- the 4th (already closed)
  appeared in no `tabs` entry, no `failures` entry, and no `skipped` record, so
  nothing in the manifest told the caller their fourth tab was ever requested.
  `_capture_tab` never runs for a not-found id (there is no tab to target); instead
  `manifest["tabs"][tab_id]` gets a synthetic `{"status": "not_found", "reason":
  ...}` entry so every id in `tab_ids` is accounted for one way or another. This
  accounting is computed once (from `tab_ids` against the live inventory) and applied
  **at every depth, including L0** -- L0 does no page contact at all (see the depth
  ladder above), but "we did no page contact" and "we ignored what you asked for" are
  different things, and L0 already reads the full tab inventory so it has everything
  it needs to know which requested ids are absent. (A prior version of this fix
  computed the per-tab `"not_found"` entries only inside the L1+ per-tab capture
  loop, so an L0 request for a nonexistent `tab_id` still silently reported plain
  `"ok"` -- the exact bug this accounting exists to prevent, one depth over.) This is
  a BENIGN outcome (closed-before-we-got-to-it, or never existed, is not a capture
  failure) so it does not add an entry to `manifest["failures"]`, mirroring how a
  `"skipped"` tab (also benign, also not a failure) is handled -- but, also like
  `"skipped"`, it is never silently folded into a plain `"ok"` run:
  `summary["tabs_not_found"]` counts it explicitly, and the run-level `status` becomes
  `"ok_with_skips"` (the existing "something benign didn't get captured" bucket)
  rather than plain `"ok"`. The top-level `manifest["requested_tab_ids_not_found"]`
  list (present whenever `tab_ids` names at least one id absent from the live
  inventory, at any depth including L0) remains a convenience summary of the same
  ids; the per-tab `"not_found"` entries are the load-bearing fix, since they live in
  the same `manifest["tabs"]` dict a caller already scans for every other tab's
  outcome.

## Impossible depth: fail loud, never silently degrade

`mhtml` (L4) and `nav_history` (L5) are unconditionally CDP-requiring -- see
`cdp.requires_cdp`'s `_ALWAYS_CDP_COMMANDS`. There is no lower-fidelity fallback for
either on a device without the `debugger` capability (e.g. Edge Android -- genuinely
absent there, not merely unprobed). Requesting L4 or L5 on such a device raises
`ArchiveError` BEFORE anything is captured or written to disk, naming exactly why and
which depths remain available. This module never silently returns an "L4" archive
that quietly contains no MHTML because CDP wasn't available -- that would be worse
than refusing the request outright.

## Cookies are opt-in, at the orchestrator level

`cookies_list` is an ordinary, ungated wire command (a direct caller using the CLI's
`cmd` escape hatch gets cookies like any other command -- see `docs/PROTOCOL.md`). The
opt-in gate lives HERE: `include_cookies` defaults to `False` and is never implied by
requesting a deeper archive level, including L5. A default that silently exfiltrates
session tokens into an archive directory on disk is a bad default regardless of what
the manifest permits (see `docs/permission-justifications.md` section 6).

## Queued means wait, not fail

Real-world finding (a live 126-tab archive, measured): a `command()` call to a device
that is not `live` (docs/PROTOCOL.md's three-tier model) does not fail -- it returns
`{"status": "queued", "command_id": ..., "tier": ...}` *immediately*, and the hub goes
on to actually execute it once the device reconnects, with the real result retrievable
via `poll` (docs/PROTOCOL.md's "This is the load-bearing non-blocking guarantee"). A
prior version of this module treated a queued response as an immediate per-capture
failure and never called `poll` -- so when a large archive's own command volume pushed
the device from `live` to `intermittent` partway through (see "Pacing" below), every
remaining capture came back `queued`, was recorded as `"failed"`, and the run reported
`tabs_failed: 101` out of 111 attempted -- while the device went on to actually execute
most of those 200+ "failed" commands, whose real results were retrieved by nothing and
discarded. The work was done; the answers were thrown away.

The fix: `_safe_command` (the single choke point every wire call in this module already
goes through) now follows a queued response with `client.poll(device_id, command_id)`,
on a fixed interval (`poll_interval_s`), until it resolves to a real `{"ok": ...}`
result -- or until `poll_max_wait_s` elapses, at which point it gives up and returns an
honest, clearly-worded failure (never a silent hang -- see `_resolve_queued`). Every
downstream capture recorder (`_record_read_capture`, `_record_mhtml_capture`, etc.) and
`_command_outcome` itself are UNCHANGED by this fix: they already only ever see a
resolved `{"ok": true/false, ...}` shape, so the entire correctness fix lives in one
function. `_command_outcome`'s own `status == "queued"` branch becomes a defensive
fallback for a queued response that is missing `command_id` (a protocol violation) --
see its docstring.

This is a correctness fix independent of *why* a device is non-live: it holds whether
the orchestrator itself caused the degradation (see "Pacing" below) or the device went
non-live for any other reason (network blip, browser backgrounded, laptop lid closed).

## Pacing: giving the device room to breathe

The same live 126-tab archive: nothing in this module limited how fast per-tab
commands were dispatched, and CDP-based captures (`mhtml` especially -- routinely
multi-megabyte, `Page.captureSnapshot` -- and `nav_history`) are the ones observed to
be heavy enough, fired back-to-back with zero gap, to overwhelm the browser
extension's single-threaded service worker and knock the device off `live` mid-run --
the archive caused the very condition the previous section's bug then mishandled.

`_CdpPacer` addresses this with two independent, narrowly-scoped guards, applied only
before the three CDP-requiring per-tab captures (`mhtml`, `nav_history`, and
`screenshot` when it is using `capture_hidden` -- see `cdp.py`'s `requires_cdp`); the
lightweight JS-injection captures (`text`/`dom`) and the browser-wide profile-data
commands are never paced, since they are not what the real-world incident implicates:

1. **A floor on the interval since the previous CDP dispatch** (`cdp_pace_s`) --
   enforced unconditionally, so a large archive can never fire CDP-heavy commands
   back-to-back with zero gap, regardless of what the device's last-reported tier
   says (tier only updates on the device's own heartbeat/hello, so it can lag real-time
   degradation by seconds).
2. **A live tier check immediately before dispatch** (`client.list_devices()` -- a
   cheap, hub-local call; no device round trip). If the device has already fallen off
   `live`, this waits, with exponential backoff capped at `cdp_backpressure_max_wait_s`,
   for it to recover before allowing the next CDP dispatch -- rather than adding more
   commands to a device that has already shown it cannot keep up. This is advisory,
   never a correctness gate: if the wait budget is exhausted, or the `list_devices()`
   check itself fails, dispatch proceeds anyway -- the previous section's poll-until-
   resolved handling is what guarantees a correct eventual outcome regardless.

Both are diagnostics-friendly, not just silent: `run_archive`'s optional `on_progress`
callback (if given) is invoked with structured events as pacing/backpressure/queued-
wait activity happens -- live, as the run progresses, not only in the final manifest
(a small sample never saturates anything, so a live signal is worth more than a faster
happy path measured on five tabs). `manifest["pacing"]` also summarizes the whole run's
pacing activity (tier checks, backpressure pauses, queued-command waits) for a caller
that only reads the manifest after the fact.

## Device health: recovering from a real disconnect, not just a slow one

Real-world finding, a live 62-tab L4 archive (`captures=["mhtml", "screenshot",
"nav_history"]`, `wake=true`, macOS Edge over Tailscale): `tabs_captured: 36`,
`tabs_failed: 24`, `tabs_partial: 2`, `duration_s: 742.9`, `pacing: {cdp_tier_checks:
126, cdp_backpressure_events: 1, cdp_backpressure_paused_s: 1.5, queued_waits: 0}`. The
failures clustered into one clear signature: `device ... disconnected mid-command`
(registry.py's `DeviceRecord.unbind` failing every in-flight future the moment the
websocket drops), then ~20 tabs in a row of `Detached while handling command` and CDP
`{"code":-32603,"message":"Internal error"}` (chrome.debugger's own protocol-level
errors once its session has come loose), then one `could not reach hub ... timed out
waiting for a response` (client.py's own connection-level failure). The device's CDP
layer fell over mid-run and never came fully back within the pacing this module already
had -- `_CdpPacer` did exactly what it was designed to do (126 tier checks, one
backpressure pause) but that design ASSUMES the device is merely slow: `_wait_for_recovery`
waits up to `cdp_backpressure_max_wait_s` (default 20s) for tier to read `live` again,
then dispatches anyway if it doesn't. For a device that is genuinely gone -- not slow,
gone -- 20s is nowhere near enough for a real websocket + `hello` reconnect, so every one
of the ~24 remaining dispatches paid that same 20s wait, gave up, and fired straight into
the same dead connection again: one disconnect cascaded into a batch-wide failure instead
of a single, contained one.

The fix has three parts, all in `_CdpPacer`, none touching how a genuine per-tab capture
failure is recorded (module docstring's "No silent partial success" section is
unaffected -- this is a PACING fix, not a correctness one):

1. **Classify device-health failures, distinct from page-content failures**
   (originally `_is_device_health_error`, matched against `_DEVICE_HEALTH_SIGNATURES`).
   `Detached while handling command`, CDP `-32603` (`Internal error`), `disconnected
   mid-command`, and `could not reach hub` are properties of the CONNECTION/DEBUGGER
   SESSION, never of the page being captured -- an ordinary "this page failed to
   render" error never matches any of these.

   **Correction (see "Per-tab CDP isolation" below):** this four-signature bucket was
   itself over-broad. `Detached while handling command` and CDP `-32603` turned out to
   be PER-TAB symptoms (a single tab's `chrome.debugger` session came loose -- e.g. a
   tab woken from discard whose renderer never produced a paintable surface for
   `Page.captureScreenshot`), not device-wide ones; only `disconnected mid-command`
   (a websocket teardown, `registry.py`'s `DeviceRecord.unbind`) and `could not reach
   hub` (a transport-level failure, `client.py`) are genuine DEVICE-level events. The
   classifier is now split in two: `_is_device_disconnect_error` (the narrower,
   genuinely device-level pair -- this is what feeds `record_result`'s trip counting
   below) and `_is_per_tab_cdp_session_error` (the per-tab pair -- used only for the
   isolation/reattach logic in `_capture_tab`, never for trip counting). The 62-tab
   incident narrated here was a genuine full disconnect (confirmed by the
   `disconnected mid-command` root signal), so it is unaffected by this correction --
   see "Per-tab CDP isolation" for the DIFFERENT incident that motivated it.
2. **Recover from a genuine disconnect instead of cascading through the rest of the
   batch** (`_CdpPacer.record_result` + `_wait_for_device_recovery`). Once
   `device_health_trip_threshold` (default 3) CONSECUTIVE CDP-paced captures fail with a
   device-health signature, the pacer treats the next CDP dispatch specially: instead of
   the ordinary `cdp_backpressure_max_wait_s`-bounded wait, it waits up to
   `device_recovery_max_wait_s` (default 120s -- sized for a real reconnect, not a stale
   tier reading) for tier to confirm `live` again before letting dispatch proceed. Tabs
   captured during the trip window keep their honest failure (never retried after the
   fact -- "a tab that genuinely can't be captured after recovery is still an honest
   per-tab failure"), but the REMAINING tabs, once the device is actually back, resume
   capturing normally instead of failing one by one. If the recovery budget is exhausted
   without confirmation, the pacer does not resume at the cadence that just overwhelmed
   the device: it doubles its own effective `cdp_pace_s` (capped at 8x,
   `_slowdown_multiplier`) for the rest of the run -- an adaptive slowdown, applied once
   real evidence of trouble exists, rather than a blind global change.
3. **Bound the burst load proactively, before any failure occurs at all**
   (`cdp_burst_size`/`cdp_burst_cooldown_s`). Independent of tier or any health signal, a
   mandatory `cdp_burst_cooldown_s` (default 5s) pause is inserted every `cdp_burst_size`
   (default 25) consecutive CDP dispatches -- giving the device's single-threaded
   extension service worker recurring breathing room instead of the archive firing every
   CDP-heavy command it has as fast as the pace floor allows, which is what let the batch
   tip the device over in the first place.

All three default such that a small archive's behavior and timing are unaffected:
`device_health_trip_threshold`/`cdp_burst_size` only ever engage past their respective
count (never for a run smaller than that), and both can be set to `0` to disable
entirely -- the same "0 disables" convention `cdp_backpressure_max_wait_s`/`cdp_pace_s`
already established. `manifest["pacing"]` reports every new knob's configured value
alongside cumulative counts (`device_health_signals`, `device_health_trips`,
`device_health_recoveries`, `cdp_slowdown_multiplier`, `cdp_burst_cooldowns`) for a
caller that only reads the manifest after the fact; `on_progress` also receives live
`device_health_signal`/`device_health_trip`/`device_recovered`/`device_recovery_gave_up`/
`burst_cooldown` events as they happen.

## Per-tab CDP isolation: a screenshot detach must not doom mhtml on the same tab

Real-world finding, TWO clean 28-tab eval runs (same physical device, same tabs, CDP-only
L4 -- `mhtml`+`screenshot`+`nav_history`, current `cdp_burst_size`/`cdp_chunk_size`
defaults, one run WITH chunking engaged and one WITHOUT): both produced the IDENTICAL
result -- rate 0.571, 9 failed, 5 health_trips. Proactive chunking, added on the theory
that burst load was overwhelming the device's CDP layer, changed NOTHING. That theory is
wrong: a device genuinely being overwhelmed by burst volume would behave differently once
the burst is bounded into smaller chunks with real settles between them. It didn't --
which means the failure is not load-dependent at all. It is PER-TAB.

Per-tab manifest analysis (identical across both runs) named the real mechanism:

    9 tabs: [screenshot] {"code":-32603,"message":"Internal error"}   <- fails FIRST
    9 tabs: [mhtml]      Detached while handling command.             <- SAME 9 tabs, after
    3 tabs: [mhtml]      device ... disconnected mid-command          <- genuine disconnects

The 9 screenshot-failures and 9 mhtml-failures are THE SAME 9 TABS (9/9 overlap), and the
ordering is always screenshot-then-mhtml, never the reverse: zero tabs failed screenshot
while mhtml still succeeded. `Page.captureScreenshot` throws `-32603` on a tab woken from
discard/asleep whose renderer never produced a paintable surface (nothing to screenshot).
That failure detaches the tab's OWN `chrome.debugger` session; the SUBSEQUENT `mhtml`
dispatch for the SAME tab (module docstring order, before this fix: `screenshot` at L3
runs before `mhtml` at L4) inherits that broken session and fails with `Detached while
handling command` -- a per-page-content-independent, per-TAB cascade, not a device-wide
one. One un-screenshottable tab was losing BOTH its screenshot AND its mhtml.

Compounding this: the (pre-correction) device-health classifier treated `-32603` and
`Detached while handling command` as DEVICE-level signals (see "Device health" above's
correction note), so a PER-TAB screenshot fault was tripping the 120s
`device_recovery_max_wait_s` escalation meant for a genuinely disconnected device -- the
5 "health trips" per run were this misclassification, not real device failures, and were
responsible for most of the run's 295s duration.

**The fix has two independent parts, both in this module, neither touching hub.py/cdp.py
(the extension-side attach/detach machinery itself is unmodified and untested here --
this fix is entirely at the ORCHESTRATION layer, choosing what order to dispatch in and
what to do with a result already received):**

1. **Reorder per-tab CDP dispatch so the higher-value, structurally-safer captures run
   BEFORE the one identified as the trigger** (`_capture_tab`). `mhtml` (L4) and
   `nav_history` (L5) -- both structural document/session snapshots via
   `Page.captureSnapshot`/`Page.getNavigationHistory`, neither requiring a painted frame
   -- now dispatch BEFORE `screenshot` (L3), which is the one observed to throw `-32603`
   on a tab with no paintable surface. `text`/`dom` (JS-injection, never CDP-paced) are
   unaffected and still run first. Depth-ladder gating (`depth_idx >= _DEPTH_INDEX[...]`)
   is unchanged for every capture -- only the DISPATCH ORDER within one `_capture_tab`
   call moved; `CAPTURE_NAMES`'s tuple order (used for depth-reachability bookkeeping,
   never for dispatch sequencing) is unaffected.
2. **Isolate remaining per-tab CDP captures from an observed per-tab detach**
   (`_is_per_tab_cdp_session_error`, matched against `_PER_TAB_CDP_SIGNATURES` --
   `Detached while handling command` and CDP `-32603` ONLY, deliberately excluding the
   genuine-disconnect pair). If a CDP-based capture's own result matches this per-tab
   signature, `_capture_tab` issues one best-effort, explicit `attach` command (the same
   wire command `cdp.py`'s docstring already describes an agent's own explicit `attach`
   producing) for THIS tab before the NEXT CDP-based capture on it -- rather than
   assuming a fresh attach will happen on its own. This is orchestrator-level recovery
   bookkeeping, not itself a paced/recorded capture: it is dispatched with `is_cdp=False`
   (never counted in `support.pacer`'s device-health accounting) and never appears in the
   tab's own `captures` dict; a failed re-attach simply means the next capture proceeds
   and, if the tab truly cannot be reached, fails and is recorded as an honest per-tab
   failure like any other (module docstring's "No silent partial success" section is
   unaffected). Combined with (1), this protects EVERY per-tab CDP capture from EVERY
   other one on the same tab, not just the one specific screenshot-then-mhtml ordering
   the eval evidence named.

**Re-scoping the device-health classifier** (the other half of this fix, described in
"Device health" above's correction note): `_CdpPacer.record_result` now calls
`_is_device_disconnect_error` (matching only `disconnected mid-command`/`could not reach
hub`) instead of the old four-signature classifier, so an isolated per-tab `-32603`/
`Detached while handling command` can NEVER consume `device_health_trip_threshold`'s
budget or trigger `device_recovery_max_wait_s`'s 120s wait -- only a genuine run of
device-level disconnects does. `device_health_trip_threshold`/`device_recovery_max_wait_s`
themselves, and the trip/recovery MACHINERY in `_CdpPacer`, are unchanged: this is a
re-scoping of WHAT counts as a trip signal, never a removal of the recovery path itself,
which remains exactly what handles a genuine full-device disconnect (like the 62-tab
incident above).

**What this does NOT prove**: the tests this fix ships with (fake-client, no live
browser -- see "Impossible depth"'s sibling constraint on how this module is tested)
prove the MECHANISM is correct: a tab whose screenshot throws `-32603` still gets its
mhtml captured, and an isolated per-tab signal no longer trips the recovery wait. They do
not, by themselves, prove a specific improved capture rate, health-trip count, or
duration on the real 28-tab device this finding was measured against -- that requires a
live eval run of the same scenario with this fix engaged, measured the same way the two
identical baseline numbers above were.

## Chunking: keeping the device under its drop threshold in the first place

Two clean eval data points (same physical device, same tabs, CDP-only L4 --
`mhtml`+`screenshot`+`nav_history` -- current `cdp_burst_size`/`cdp_burst_cooldown_s`
defaults) made the LOAD-DEPENDENT shape of the failure explicit, rather than merely
time-dependent or random:

    12 tabs:  rate 0.75,  0 health_trips,  48s   -- device not stressed at small N
    28 tabs:  rate 0.571, 5 health_trips, 295s   -- device overwhelmed: 6x duration,
                                                    rate collapsed recovering drops
                                                    that should never have happened

`cdp_burst_size`/`cdp_burst_cooldown_s` (above) is real but WEAKER than this finding
calls for: it pauses every `cdp_burst_size` (default 25) dispatches WITHIN one
continuous loop, and never confirms the device is actually healthy before resuming --
a breather, not a checkpoint. 25 is also far above the drop threshold these two data
points bracket (fine at 12, overwhelmed at 28): a burst cooldown at the default size
never engages at all before a run of this size is already in the failure zone.

`cdp_chunk_size` is the PROACTIVE fix this finding calls for: `run_archive`'s per-tab
capture loop (L1+) splits `selected_tabs` into sequential sub-batches of at most
`cdp_chunk_size` tabs (`_chunk_tabs`) instead of one continuous loop over the whole
list. Each chunk is processed to completion; then, BEFORE the next chunk starts (never
WITHIN one), `_settle_between_chunks` inserts:

1. An unconditional `cdp_chunk_cooldown_s` (default 5s) pause -- the same "give the
   device a fixed breather" rationale as `cdp_burst_cooldown_s`, but a coarser,
   between-WHOLE-CHUNKS unit rather than a within-loop one.
2. A bounded tier-confirmation wait (`_CdpPacer.wait_until_live`) that the device
   reads `live` again -- REUSING the exact same `client.list_devices()` tier-check
   path `_wait_for_recovery`/`_wait_for_device_recovery` above already use, no new
   device-health mechanism invented, just applied at a new checkpoint. The wait
   budget is `device_recovery_max_wait_s` ITSELF (the same "sized for a real
   reconnect, not a stale tier reading" budget the reactive mid-chunk recovery
   already uses above) -- no new knob for this either.

Unlike every other wait in this module, this ONE is not merely advisory: if the device
never confirms `live` again within that budget, `run_archive` does not dispatch the
remaining chunks at all. Every tab in every not-yet-started chunk gets an honest
`"failed"` entry (`_chunk_aborted_tab_entry`) with a reason naming exactly what
happened, and is added to `manifest["failures"]` like any other real failure -- never a
silent drop, never a hang, and never a one-by-one cascade through tabs that were never
going to succeed anyway. A tab that WAS already attempted keeps whatever honest outcome
it already had (module docstring's "No silent partial success" section is otherwise
unaffected by this feature).

Chunking COMPOSES with, rather than replaces, the reactive recovery machinery above: a
device-health trip and recovery INSIDE a chunk is handled exactly as before -- chunking
never touches `_CdpPacer`'s per-dispatch pacing or its `before_dispatch`/`record_result`
logic at all. Chunking's whole purpose is to make that mid-chunk case rare, by never
letting one continuous dispatch run climb into the overwhelmed zone the 28-tab data
point measured, in the first place.

`cdp_chunk_size<=0` (or `None`) disables chunking entirely, mirroring this module's
established "0 disables" convention -- `_chunk_tabs` then returns the WHOLE tab list as
a single chunk, so the capture loop is byte-for-byte the pre-chunking single continuous
loop (no settle ever runs, since there is only ever one chunk to finish). The same is
true, with zero code path difference, whenever `len(selected_tabs) <= cdp_chunk_size`:
a small run is exactly one chunk and behaves exactly as it did before this feature
existed.

**Chosen defaults** (`DEFAULT_CDP_CHUNK_SIZE = 8`, `DEFAULT_CDP_CHUNK_COOLDOWN_S = 5.0`):
the two eval data points bracket the drop threshold between 12 (safe) and 28
(overwhelmed) tabs of continuous CDP dispatch; 8 keeps every chunk comfortably below
even the SAFE end of that bracket -- a real margin, not just barely under the line --
while still splitting a 28-tab run into a modest ~4 chunks, so the proactive fix does
not itself dominate wall-clock time on a large run. `cdp_chunk_cooldown_s` reuses
`cdp_burst_cooldown_s`'s own default (5s) for the same "give the device a breather"
rationale, as an independently tunable knob (burst cooldown paces WITHIN one
continuous dispatch loop; this paces BETWEEN whole chunks, a coarser and more
conservative unit). Both new knobs are surfaced in `manifest["pacing"]`
(`cdp_chunk_size`, `cdp_chunk_cooldown_s`, `chunks_total`, `chunks_completed`,
`chunk_settle_waits`, `chunk_settle_wait_total_s`, `chunk_settle_gave_up`) for a caller
that only reads the manifest after the fact.

**What this does NOT prove**: these two data points make the failure LOAD-dependent
and this fix mechanically sound -- proactively bounding burst size below the measured
safe/overwhelmed boundary, using the device's own already-existing health signal to
confirm it before continuing -- but they do not, by themselves, prove a specific
improved capture rate on a real device. That requires a live eval run of the same
28-tab scenario with chunking engaged, measured the same way the two baseline numbers
above were.

**Correction, from a THIRD eval run (see "Per-tab CDP isolation" above): the
load-dependent theory this section is built on is DISPROVEN.** Running the identical
28-tab scenario WITH chunking engaged produced the IDENTICAL result to running it
WITHOUT chunking (rate 0.571, 9 failed, 5 health_trips, both runs) -- if the device were
genuinely being overwhelmed by burst volume, bounding that volume into smaller,
settled-between chunks should have changed the outcome, and it changed nothing. The real
failure is per-tab (a screenshot detach cascading into mhtml on the SAME tab), not
load-dependent, and chunking was never addressing it. `cdp_chunk_size`/
`cdp_chunk_cooldown_s` are left in place as an OPT-IN knob (default unchanged, `<= 0`
still disables it) rather than removed outright -- a large archive genuinely can still
generate more total dispatches than a small one, and a proactive settle-between-batches
mechanism is not inherently wrong, even though it does not address the specific failure
mode this module's evidence points to. Do not reach for it as the fix for a low capture
rate; reach for the per-tab isolation and re-scoped health classification above first.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .addressing import Target
from .client import HubError

# Depth ladder, cheapest to deepest -- see module docstring. Each level's index is
# used purely for ">=" comparisons ("does this run need to do at least as much as
# L2's work"), never for anything positional/ordinal beyond that.
DEPTHS: tuple[str, ...] = ("L0", "L1", "L2", "L3", "L4", "L5")
_DEPTH_INDEX: dict[str, int] = {d: i for i, d in enumerate(DEPTHS)}
DEFAULT_DEPTH = "L0"

# The capability that gates L4/L5 -- see module docstring's "Impossible depth"
# section and cdp.py's `_ALWAYS_CDP_COMMANDS`.
_CDP_CAPABILITY = "debugger"
_CDP_REQUIRED_FROM_DEPTH = "L4"

# Per-tab capture names, in the SAME order the depth ladder introduces them --
# see module docstring's "Injection budget and explicit capture selection"
# section. These are the keys that show up in `manifest["tabs"][tab_id]
# ["captures"]`, not the wire command names (`read`/`page_state` map to
# `text`/`dom` respectively; `screenshot`/`mhtml`/`nav_history` are unchanged).
CAPTURE_NAMES: tuple[str, ...] = ("text", "dom", "screenshot", "mhtml", "nav_history")
# Which depth level FIRST includes each capture -- used by `_validate_captures`
# to reject (pre-flight) a `captures` argument that names nothing reachable at
# the requested `depth`, which would otherwise silently capture nothing for
# every tab in the run.
_CAPTURE_OWNING_DEPTH: dict[str, str] = {
    "text": "L1",
    "dom": "L2",
    "screenshot": "L3",
    "mhtml": "L4",
    "nav_history": "L5",
}
# JS-injection-based capture routes (chrome.scripting + injected.js) -- as
# opposed to the CDP-based routes (screenshot/mhtml/nav_history). See module
# docstring's "Injection budget" section: these two are the ones observed to
# time out on heavy hydrated SPAs at both 90s and 120s budgets, while the
# CDP-based routes succeeded on the same tabs.
_INJECTION_CAPTURE_NAMES: frozenset[str] = frozenset({"text", "dom"})

# ---------------------------------------------------------------------------
# Pacing / queued-resolution defaults -- see module docstring's "Queued means
# wait, not fail" and "Pacing" sections for the real-world incident these
# close and the reasoning behind each number.
# ---------------------------------------------------------------------------

# Wire commands this module treats as CDP-requiring for PACING purposes --
# `mhtml`/`nav_history` are unconditionally so (cdp.py's `_ALWAYS_CDP_COMMANDS`);
# `screenshot` is CDP-based only when dispatched with `capture_hidden` (see
# `_capture_tab`'s `use_capture_hidden`), so it is paced via an explicit
# `is_cdp` argument at its own call site rather than membership here.
_CDP_PACED_COMMANDS: frozenset[str] = frozenset({"mhtml", "nav_history"})

# Floor on the interval between successive CDP-requiring dispatches. Fired
# unconditionally (not just reactively) because the real-world incident this
# closes was CAUSED by zero-gap dispatch of heavy CDP captures, not merely
# revealed after the fact -- see `_CdpPacer`.
DEFAULT_CDP_PACE_S: float = 0.2

# Cap on how long `_CdpPacer` will wait (exponential backoff) for a degraded
# device to return to `live` before giving up and allowing dispatch to proceed
# anyway. Advisory only: `_resolve_queued` (below) is what guarantees a correct
# eventual outcome regardless of whether this budget was enough.
DEFAULT_CDP_BACKPRESSURE_MAX_WAIT_S: float = 20.0

# ---------------------------------------------------------------------------
# Device-health escalation -- see module docstring's "Device health: recovering
# from a real disconnect, not just a slow one" section for the real-world
# incident (a live 62-tab L4 archive, measured: 36 captured, 24 failed, 2
# partial, 742.9s) this closes. `DEFAULT_CDP_BACKPRESSURE_MAX_WAIT_S` above
# assumes the device is merely SLOW to answer (a stale tier reading catching
# up); it has no answer for a device that has genuinely fallen off CDP mid-run
# and needs real time to reconnect -- that case instead produced a cascade of
# ~20 back-to-back `Detached while handling command`/CDP -32603 failures, each
# one paying the ordinary 20s backpressure wait and then dispatching into the
# same dead connection again.
# ---------------------------------------------------------------------------

# Consecutive CDP-paced capture failures matching a GENUINE device-level
# disconnect signature (see `_is_device_disconnect_error` -- deliberately NOT the
# narrower per-tab `-32603`/`Detached while handling command` faults, which
# `_is_per_tab_cdp_session_error` handles separately and never counts here; see
# module docstring's "Per-tab CDP isolation" section) before `_CdpPacer` stops
# treating the device as merely slow and starts treating it as genuinely IN
# TROUBLE -- escalating
# to `_wait_for_device_recovery`'s much larger budget instead of the ordinary
# tier-backpressure wait. `<= 0` disables device-health escalation entirely
# (mirroring the existing "0 disables" convention `cdp_backpressure_max_wait_s`
# already established) -- every CDP-paced capture is still attempted and
# recorded normally, just without this extra layer of protection.
DEFAULT_DEVICE_HEALTH_TRIP_THRESHOLD: int = 3

# Cap on how long `_CdpPacer` will wait (bounded exponential backoff) for the
# device to report `live` again after tripping the threshold above --
# deliberately much larger than `cdp_backpressure_max_wait_s`: a genuine
# extension reconnect (fresh websocket + `hello` handshake) can take real
# wall-clock time that an ordinary "the tier reading is just stale" backoff
# was never sized for. If this budget is exhausted without confirmation, the
# pacer does not resume at the same cadence that just overwhelmed the device --
# see `_CdpPacer._slowdown_multiplier`.
DEFAULT_DEVICE_RECOVERY_MAX_WAIT_S: float = 120.0

# Proactive bound on burst load, independent of any failure signal: after this
# many consecutive CDP-requiring dispatches, `_CdpPacer` forces a
# `cdp_burst_cooldown_s` pause regardless of tier or device-health state --
# giving the device's single-threaded extension service worker breathing room
# before a large batch has any chance to tip it over in the first place,
# rather than only reacting once it already has. `<= 0` disables burst
# cooldowns entirely. Never triggers on a small archive (the common case,
# unaffected by this fix): only a run whose CDP-heavy tab count actually
# exceeds this needs it.
DEFAULT_CDP_BURST_SIZE: int = 25
DEFAULT_CDP_BURST_COOLDOWN_S: float = 5.0

# ---------------------------------------------------------------------------
# Chunking -- see module docstring's "Chunking: keeping the device under its
# drop threshold in the first place" section for the two measured eval data
# points (12 tabs: 0 trips, 48s; 28 tabs: 5 trips, 295s) this responds to.
# Unlike `cdp_burst_size` above (a breather WITHIN one continuous dispatch
# loop, never confirming health before resuming), chunking is a PROACTIVE
# checkpoint BETWEEN whole sub-batches of the tab list -- it never fires a
# burst large enough to reach the observed drop threshold in the first place.
# ---------------------------------------------------------------------------

# Maximum number of tabs processed per chunk before `run_archive` pauses for
# an inter-chunk settle (`_settle_between_chunks`) -- see module docstring.
# The two eval data points bracket the drop threshold between 12 (measured
# safe) and 28 (measured overwhelmed) tabs of continuous CDP dispatch; 8
# keeps every chunk comfortably below even the safe end of that bracket (a
# real margin, not just barely under the line) while still splitting a
# 28-tab run into a modest ~4 chunks, so the fix does not itself dominate
# wall-clock time on a large run. `<=0` (or `None`) disables chunking
# entirely -- `_chunk_tabs` returns the whole tab list as a single chunk, so
# the capture loop is byte-for-byte the pre-chunking single continuous loop.
# Never engages at all for a run whose tab count is already `<= cdp_chunk_size`.
DEFAULT_CDP_CHUNK_SIZE: int = 8

# Unconditional pause inserted BETWEEN chunks (never within one), before the
# tier-confirmation wait -- same "give the device a fixed breather" rationale
# as `cdp_burst_cooldown_s` (whose default this mirrors), but an
# independently tunable knob operating at a coarser, whole-chunk unit.
DEFAULT_CDP_CHUNK_COOLDOWN_S: float = 5.0

# How often `_resolve_queued` polls a still-queued/pending command for its
# real result.
DEFAULT_POLL_INTERVAL_S: float = 2.0

# Total time `_resolve_queued` will spend polling ONE queued command before
# giving up and recording it as a failure -- never an unbounded/silent hang
# (module docstring's "Queued means wait, not fail" section). A caller
# archiving a device known to be dormant for a long stretch can raise this;
# the default is generous relative to the measured intermittent dark-window
# ceiling (tiers.py's `INTERMITTENT_MAX_SECONDS`, 150s) without matching it
# exactly -- a single stuck capture should not be allowed to dominate an
# entire large archive's wall-clock time.
DEFAULT_POLL_MAX_WAIT_S: float = 90.0


class ArchiveError(ValueError):
    """Raised for a PRE-FLIGHT failure that stops the whole run before anything is
    captured or written to disk: an unknown depth string, an unknown device, or a
    depth that is structurally impossible on this device (see module docstring's
    "Impossible depth" section). Never raised for an ordinary per-tab or
    profile-data capture failure -- those are recorded in the returned manifest and
    the run continues; see the module docstring's "No silent partial success"
    section. Callers (mcp_server.py's `browser_archive`, the Amplifier tool
    module's runner) catch this the same way they catch `HubError` and convert it
    to `{"ok": False, "error": str(e)}`.
    """


class _ArchiveClient(Protocol):
    """Structural type for the three `HubClient` methods this module actually needs --
    `HubClient` itself satisfies this, and so does a duck-typed test double (see
    tests/test_archive.py), the same pattern `vision_read.py`'s `_CommandClient`
    already establishes. `poll` was added alongside "Queued means wait, not fail"
    (module docstring) -- it is what lets `_resolve_queued` retrieve a queued
    command's eventual real result instead of treating the queued response itself
    as the final outcome."""

    async def command(self, target: Target, command: str, args: dict[str, Any]) -> dict[str, Any]: ...

    async def list_devices(self) -> list[dict[str, Any]]: ...

    async def poll(self, device_id: str, command_id: str) -> dict[str, Any]: ...


def _depth_index(depth: str) -> int:
    try:
        return _DEPTH_INDEX[depth]
    except KeyError:
        raise ArchiveError(f"unknown archive depth {depth!r} -- valid depths: {', '.join(DEPTHS)}") from None


def _validate_captures(captures: list[str] | None, *, depth: str, depth_idx: int) -> frozenset[str] | None:
    """Validates and normalizes the caller's explicit `captures` argument (see
    module docstring's "Injection budget and explicit capture selection"
    section and `run_archive`'s docstring). Returns `None` -- meaning "no
    narrowing at all: every capture the depth ladder would attempt runs",
    the pre-existing strict-superset default -- when the caller omitted
    `captures` entirely.

    Raises `ArchiveError`, a PRE-FLIGHT failure before anything is captured
    (same posture as `_depth_index`/the "Impossible depth" check below), for:

        - an empty list (ambiguous -- omit the argument for the real default)
        - an unrecognized name (a typo silently capturing nothing for that
          name is worse than refusing up front)
        - a `captures` set with NO name reachable at the requested `depth` --
          e.g. `captures=["mhtml"]` with `depth="L1"` -- which would silently
          capture NOTHING for every tab in the run. Because the reachability
          check depends only on `depth` (never on any tab's own data), this
          one pre-flight check is sufficient: if it passes, every tab that
          actually reaches per-tab capture is guaranteed a non-empty
          attempted set (see `_tab_status`'s defensive fallback for the
          belt-and-suspenders case this is meant to make unreachable).
    """
    if captures is None:
        return None
    if not captures:
        raise ArchiveError(
            "captures, if given, must name at least one capture -- omit the argument entirely for "
            "the default (every capture the depth ladder attempts, the pre-existing behavior)."
        )
    unknown = sorted(set(captures) - set(CAPTURE_NAMES))
    if unknown:
        raise ArchiveError(
            f"captures names unrecognized capture(s) {unknown} -- valid names: {', '.join(CAPTURE_NAMES)}."
        )
    reachable = {name for name in captures if _DEPTH_INDEX[_CAPTURE_OWNING_DEPTH[name]] <= depth_idx}
    if not reachable:
        needed = ", ".join(f"{n} (needs depth {_CAPTURE_OWNING_DEPTH[n]}+)" for n in sorted(captures))
        raise ArchiveError(
            f"captures={sorted(captures)!r} names no capture reachable at depth {depth!r} -- the "
            f"depth ladder would never attempt any of them at this depth ({needed}). Use a deeper "
            "depth, or a different captures set."
        )
    return frozenset(captures)


def _sanitize_component(value: str) -> str:
    """Filesystem-safe directory-name component (IMPLEMENTATION_PHILOSOPHY.md's
    Windows-compatibility checklist: sanitize any external value used in a
    filename/path). `device_id` is normally a plain uuid4, but this is defensive
    regardless of what it happens to contain."""
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value)
    return sanitized.strip("-") or "device"


def _write_json(path: Path, data: Any) -> int:
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def _command_outcome(result: Any) -> tuple[bool, Any, str | None]:
    """Classifies a raw hub command result into `(ok, data, error)`.

    By the time this is called, `result` has ALREADY been through
    `_safe_command`'s queued-resolution (module docstring's "Queued means wait,
    not fail" section) -- so in normal operation it only ever sees a terminal
    shape: an explicit `{"ok": true, ...}` success, an explicit
    `{"ok": false, ...}` failure, or a synthetic `{"ok": false, ...}` produced
    by `_resolve_queued` giving up after `poll_max_wait_s`. The `status ==
    "queued"` branch below is therefore a DEFENSIVE fallback, not the primary
    path: it only triggers for a queued response missing `command_id` (a
    protocol violation -- docs/PROTOCOL.md's queued response always includes
    one), which `_resolve_queued` cannot poll and returns unresolved. Treated
    as an ordinary capture failure, same as any other -- never a silent hang.
    """
    if not isinstance(result, dict):
        return False, None, f"unexpected non-dict response: {result!r}"
    if result.get("status") == "queued":
        return (
            False,
            None,
            (
                f"device is not live -- this command was queued (tier={result.get('tier')!r}, "
                f"command_id={result.get('command_id')!r}) and could not be resolved (missing a "
                "pollable command_id, which is a protocol violation -- docs/PROTOCOL.md's queued "
                "response always includes one)."
            ),
        )
    if result.get("ok") is True:
        return True, result.get("result"), None
    return False, None, str(result.get("error") or f"unrecognized response shape: {result!r}")


def _capture_failed(error: str) -> dict[str, Any]:
    return {"status": "failed", "error": error}


# Device-health failure signatures -- see module docstring's "Device health:
# recovering from a real disconnect, not just a slow one" section. Matched as
# plain case-insensitive substrings against a CDP-paced capture's own error
# text (never against `text`/`dom`/profile-data errors -- those are not paced
# and are not implicated by the incident this responds to).
#
# Deliberately narrow and literal, not a broad heuristic: each one was observed
# VERBATIM in a real incident this module closes. Originally bucketed together as
# a single `_DEVICE_HEALTH_SIGNATURES` tuple (a 62-tab L4 archive: 36 captured, 24
# failed, 2 partial, 742.9s) -- see module docstring's "Device health" section --
# but a LATER incident (two 28-tab eval runs, "Per-tab CDP isolation" section)
# proved that bucket was itself over-broad: `-32603`/`Detached while handling
# command` are properties of a single TAB's `chrome.debugger` session, not the
# device/connection as a whole, and must never consume the device-recovery budget
# a genuine full disconnect needs. The signatures are now split into two disjoint
# sets by what they actually indicate, not merged into one:
#
#   - `_DEVICE_DISCONNECT_SIGNATURES`: the CONNECTION itself is in trouble
#     (websocket teardown / hub unreachable) -- these, and ONLY these, count
#     toward `_CdpPacer`'s device-health trip threshold (`_is_device_disconnect_error`).
#   - `_PER_TAB_CDP_SIGNATURES`: ONE tab's CDP session came loose -- used only to
#     decide whether `_capture_tab` should force a fresh `attach` before this
#     tab's NEXT CDP capture (`_is_per_tab_cdp_session_error`), never fed into the
#     device-health trip counter.
#
# `"-32603"` matches CDP's own JSON-RPC "Internal error" code
# (`{"code":-32603,"message":"Internal error"}`) rather than the bare phrase
# "internal error", which is common enough in unrelated page/server error text to
# risk false-positively treating an ordinary page failure as a CDP-session signal.
_DEVICE_DISCONNECT_SIGNATURES: tuple[str, ...] = (
    "disconnected mid-command",  # registry.py's `DeviceRecord.unbind` -- websocket gone
    "could not reach hub",  # client.py -- transport-level failure to reach the hub at all
)

_PER_TAB_CDP_SIGNATURES: tuple[str, ...] = (
    "detached while handling command",  # this ONE tab's chrome.debugger session came loose
    "-32603",  # CDP JSON-RPC "Internal error" -- e.g. no paintable surface to screenshot
)


def _is_device_disconnect_error(error: Any) -> bool:
    """True if `error` names a genuine DEVICE-LEVEL disconnect (the websocket/hub
    connection itself is in trouble) -- see `_DEVICE_DISCONNECT_SIGNATURES` above.
    Used by `_CdpPacer.record_result` to decide whether a CDP-paced capture's
    failure should count toward the device-health trip threshold; never changes
    how the failure itself is recorded in the per-tab manifest (module docstring's
    "No silent partial success" section still applies unchanged -- this is purely
    a PACING signal, not a correctness one).

    Deliberately narrower than the tab-level `_is_per_tab_cdp_session_error`
    below -- a single tab's `chrome.debugger` session coming loose (`-32603`,
    `Detached while handling command`) is NOT evidence the device itself is in
    trouble (module docstring's "Per-tab CDP isolation" section), and must never
    consume this budget."""
    if not isinstance(error, str) or not error:
        return False
    lowered = error.lower()
    return any(signature in lowered for signature in _DEVICE_DISCONNECT_SIGNATURES)


def _is_per_tab_cdp_session_error(error: Any) -> bool:
    """True if `error` names a PER-TAB CDP session fault (this one tab's
    `chrome.debugger` session came loose) -- see `_PER_TAB_CDP_SIGNATURES` above.
    Used only by `_capture_tab` to decide whether to force a fresh `attach` for
    THIS tab before its next CDP-based capture (module docstring's "Per-tab CDP
    isolation" section) -- deliberately never fed into `_CdpPacer.record_result`
    or the device-health trip counter; a per-tab fault must never be mistaken for
    device-wide trouble, which is exactly the misclassification this split
    corrects."""
    if not isinstance(error, str) or not error:
        return False
    lowered = error.lower()
    return any(signature in lowered for signature in _PER_TAB_CDP_SIGNATURES)


@dataclass
class _RunSupport:
    """Bundles the run-level pacing/polling knobs `_safe_command` needs beyond
    the ordinary per-call `(client, target, command, args)` -- so `run_archive`'s
    many per-tab/per-profile helper functions thread ONE extra parameter instead
    of four or five. One instance is created per `run_archive` call and shared
    across every capture in that run (see module docstring's "Queued means wait,
    not fail" and "Pacing" sections).

    `on_progress`, if given, is called synchronously with a structured event
    dict as pacing/backpressure/queued-wait activity happens DURING the run --
    not only reflected in the final manifest. A caller's callback raising is NOT
    caught here: a broken callback is the caller's own bug and must fail loud,
    same as this project's convention for every other genuine-bug case
    (CONTRIBUTING.md) -- it is never silently swallowed just because it happens
    to be attached to an optional diagnostics hook.

    The `queued_*` counters are cumulative diagnostics `_resolve_queued` writes
    into, surfaced verbatim in `manifest["pacing"]` at the end of the run.

    The `chunk_*` counters are the RUN-level chunking accounting (module
    docstring's "Chunking" section) -- deliberately kept here rather than on
    `_CdpPacer`, which owns only per-DISPATCH pacing; chunking operates one
    level up, on the whole per-tab capture loop, and applies regardless of
    whether any individual tab in a chunk happens to need a CDP-paced capture
    at all. `chunks_total`/`chunks_completed` are populated by `run_archive`
    itself; `chunk_settles`/`chunk_settle_wait_total_s`/`chunk_gave_up` are
    written by `_settle_between_chunks`.
    """

    pacer: _CdpPacer
    poll_interval_s: float
    poll_max_wait_s: float
    on_progress: Callable[[dict[str, Any]], None] | None = None
    queued_waits: int = 0
    queued_wait_total_s: float = 0.0
    queued_timeouts: int = 0
    chunks_total: int = 0
    chunks_completed: int = 0
    chunk_settles: int = 0
    chunk_settle_wait_total_s: float = 0.0
    chunk_gave_up: bool = False

    def emit(self, event: dict[str, Any]) -> None:
        if self.on_progress is not None:
            self.on_progress(event)


class _CdpPacer:
    """Backpressure for CDP-requiring per-tab captures only (`mhtml`,
    `nav_history`, and `screenshot` when using `capture_hidden`) -- see module
    docstring's "Pacing" section for why these three, and only these three, are
    implicated by the real-world incident this closes.

    Three independent guards, applied before every CDP dispatch:

    1. A floor on the interval since the previous CDP dispatch
       (`min_interval_s`, scaled by `_slowdown_multiplier` -- see below) --
       enforced unconditionally, so a large archive can never fire CDP-heavy
       commands back-to-back with zero gap, regardless of what the device's
       last-reported tier says (tier only updates on the device's own
       heartbeat/hello, so it can lag real-time degradation by seconds).
    2. A proactive burst cooldown (`burst_size`/`burst_cooldown_s`):
       independent of tier or any failure signal, a mandatory pause every
       `burst_size` consecutive CDP dispatches -- bounding how much burst load
       a single archive can throw at the device before it has any chance to
       tip CDP over, rather than only reacting once it already has (module
       docstring's "Device health" section, priority 2).
    3. A live tier check (`client.list_devices()` -- a cheap, hub-local call;
       no device round trip) immediately before dispatch. If the device has
       already fallen off `live`, this waits (bounded exponential backoff, cap
       `max_wait_s`) for it to recover before allowing the dispatch to proceed,
       instead of adding yet another command to a device that has already
       shown it cannot keep up. If `record_result` (below) has seen enough
       consecutive device-health failures to trip, this ordinary wait is
       preceded by the much larger `_wait_for_device_recovery` escalation.

    Advisory, never a correctness gate: if every wait budget is exhausted, or
    the `list_devices()` check itself fails (`HubError`), dispatch proceeds
    anyway -- `_resolve_queued` is what guarantees a correct eventual outcome
    regardless of whether pacing was enough. `record_result` never touches how
    a capture's own failure is recorded in the manifest -- it only feeds this
    pacer's own dispatch-rate decisions for SUBSEQUENT captures.
    """

    def __init__(
        self,
        *,
        min_interval_s: float,
        max_wait_s: float,
        device_health_trip_threshold: int = DEFAULT_DEVICE_HEALTH_TRIP_THRESHOLD,
        device_recovery_max_wait_s: float = DEFAULT_DEVICE_RECOVERY_MAX_WAIT_S,
        burst_size: int = DEFAULT_CDP_BURST_SIZE,
        burst_cooldown_s: float = DEFAULT_CDP_BURST_COOLDOWN_S,
    ) -> None:
        self._min_interval_s = max(0.0, min_interval_s)
        self._max_wait_s = max(0.0, max_wait_s)
        self._device_health_trip_threshold = max(0, device_health_trip_threshold)
        self._device_recovery_max_wait_s = max(0.0, device_recovery_max_wait_s)
        self._burst_size = max(0, burst_size)
        self._burst_cooldown_s = max(0.0, burst_cooldown_s)
        self._last_dispatch_at: float | None = None
        self._consecutive_health_failures: int = 0
        self._dispatches_since_cooldown: int = 0
        # Adaptive slowdown -- see `_wait_for_device_recovery`'s docstring.
        # Multiplies `min_interval_s` for the REST of the run once a recovery
        # wait gives up without confirming the device is back; never shrinks
        # mid-run (a run that needed to slow down once stays slowed down --
        # this is a single archive call's own defensive posture, not a
        # reusable connection-health model).
        self._slowdown_multiplier: float = 1.0

        # Cumulative diagnostics -- surfaced in `manifest["pacing"]`, never
        # load-bearing for correctness.
        self.tier_checks: int = 0
        self.backpressure_events: int = 0
        self.total_paused_s: float = 0.0
        self.device_health_signals: int = 0
        self.device_health_trips: int = 0
        self.device_health_recoveries: int = 0
        self.burst_cooldowns: int = 0

    @property
    def slowdown_multiplier(self) -> float:
        """Read-only view of the current adaptive-slowdown factor -- surfaced
        in `manifest["pacing"]["cdp_slowdown_multiplier"]` so a caller can
        tell, after the fact, whether this run ever had to permanently ease
        off `cdp_pace_s` (see `_wait_for_device_recovery`'s docstring)."""
        return self._slowdown_multiplier

    async def before_dispatch(
        self,
        client: _ArchiveClient,
        device_id: str,
        *,
        on_event: Callable[[dict[str, Any]], None],
    ) -> None:
        effective_interval = self._min_interval_s * self._slowdown_multiplier
        if self._last_dispatch_at is not None and effective_interval > 0:
            remaining = effective_interval - (time.monotonic() - self._last_dispatch_at)
            if remaining > 0:
                await asyncio.sleep(remaining)

        if (
            self._device_health_trip_threshold > 0
            and self._consecutive_health_failures >= self._device_health_trip_threshold
        ):
            await self._wait_for_device_recovery(client, device_id, on_event=on_event)

        if self._burst_size > 0 and self._dispatches_since_cooldown >= self._burst_size:
            self.burst_cooldowns += 1
            on_event(
                {
                    "event": "burst_cooldown",
                    "device_id": device_id,
                    "burst_size": self._burst_size,
                    "paused_s": self._burst_cooldown_s,
                }
            )
            if self._burst_cooldown_s > 0:
                self.total_paused_s += self._burst_cooldown_s
                await asyncio.sleep(self._burst_cooldown_s)
            self._dispatches_since_cooldown = 0

        await self._wait_for_recovery(client, device_id, on_event=on_event)
        self._last_dispatch_at = time.monotonic()
        self._dispatches_since_cooldown += 1

    async def _wait_for_recovery(
        self, client: _ArchiveClient, device_id: str, *, on_event: Callable[[dict[str, Any]], None]
    ) -> None:
        if self._max_wait_s <= 0:
            return
        deadline = time.monotonic() + self._max_wait_s
        backoff = max(self._min_interval_s, 0.5)
        announced = False
        while True:
            self.tier_checks += 1
            try:
                devices = await client.list_devices()
            except HubError:
                return  # advisory only -- never block correctness on this check
            record = next((d for d in devices if d.get("device_id") == device_id), None)
            tier = (record or {}).get("tier", "live")
            if tier == "live":
                if announced:
                    on_event({"event": "backpressure_resumed", "device_id": device_id})
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if not announced:
                announced = True
                self.backpressure_events += 1
                on_event({"event": "backpressure_waiting", "device_id": device_id, "tier": tier})
            wait_s = min(backoff, remaining)
            self.total_paused_s += wait_s
            await asyncio.sleep(wait_s)
            backoff = min(backoff * 2, self._max_wait_s)

    async def _wait_for_device_recovery(
        self, client: _ArchiveClient, device_id: str, *, on_event: Callable[[dict[str, Any]], None]
    ) -> None:
        """Escalated recovery wait, triggered once `record_result` has counted
        `device_health_trip_threshold` consecutive CDP-paced failures matching
        a GENUINE device-level disconnect signature (`_is_device_disconnect_error`
        -- deliberately excludes the narrower per-tab `-32603`/`Detached while
        handling command` faults `_is_per_tab_cdp_session_error` handles; see
        module docstring's "Per-tab CDP isolation" section) -- i.e. the
        device itself appears to be in trouble, not merely one tab. Uses
        `device_recovery_max_wait_s` (deliberately much larger than
        `max_wait_s`/`_wait_for_recovery` above) because a genuine reconnect
        needs real time for the extension to re-establish its websocket and
        `hello`, not just for a stale tier reading to catch up.

        Resets the consecutive-failure counter unconditionally on return (a
        fresh run of failures should retrip from zero, not carry over a stale
        count). If tier is confirmed `live` again before the budget runs out,
        dispatch resumes at the ordinary cadence. If the budget is exhausted
        without confirmation, this is treated as a real signal that the
        device cannot keep up even after waiting -- `_slowdown_multiplier` is
        doubled (capped at 8x) so the REST of the run paces itself more
        gently, rather than resuming at the exact cadence that just
        overwhelmed the device. Either way, dispatch always eventually
        proceeds -- this is advisory pacing, not a correctness gate; a tab
        that still cannot be captured afterward is recorded as an honest
        per-tab failure like any other (module docstring's "No silent partial
        success" section).
        """
        self.device_health_trips += 1
        on_event(
            {
                "event": "device_health_trip",
                "device_id": device_id,
                "consecutive_failures": self._consecutive_health_failures,
            }
        )
        if self._device_recovery_max_wait_s <= 0:
            self._consecutive_health_failures = 0
            return
        deadline = time.monotonic() + self._device_recovery_max_wait_s
        backoff = max(self._min_interval_s, 0.5)
        recovered = False
        while True:
            self.tier_checks += 1
            try:
                devices = await client.list_devices()
            except HubError:
                break  # advisory only -- can't confirm recovery; fall through and resume
            record = next((d for d in devices if d.get("device_id") == device_id), None)
            tier = (record or {}).get("tier", "live")
            if tier == "live":
                recovered = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait_s = min(backoff, remaining)
            self.total_paused_s += wait_s
            await asyncio.sleep(wait_s)
            backoff = min(backoff * 2, self._device_recovery_max_wait_s)

        self._consecutive_health_failures = 0
        if recovered:
            self.device_health_recoveries += 1
            self._slowdown_multiplier = 1.0
            on_event({"event": "device_recovered", "device_id": device_id})
        else:
            self._slowdown_multiplier = min(self._slowdown_multiplier * 2, 8.0)
            on_event(
                {
                    "event": "device_recovery_gave_up",
                    "device_id": device_id,
                    "slowdown_multiplier": self._slowdown_multiplier,
                }
            )

    def record_result(self, result: Any, *, on_event: Callable[[dict[str, Any]], None]) -> None:
        """Feeds a just-completed CDP-paced capture's OWN outcome back into this
        pacer -- called by `_safe_command` only for `is_cdp=True` dispatches,
        after `_resolve_queued` has produced a terminal result. A genuine
        DEVICE-level disconnect (`_is_device_disconnect_error` --
        `disconnected mid-command`/`could not reach hub` ONLY, see module
        docstring's "Per-tab CDP isolation" correction) increments the
        consecutive-failure counter `before_dispatch` checks on the NEXT CDP
        dispatch; anything else -- success, an ordinary per-page content
        failure, OR a per-TAB CDP session fault (`-32603`/`Detached while
        handling command`; see `_is_per_tab_cdp_session_error`, checked
        separately by `_capture_tab` for tab-local isolation, never here) --
        resets it. Only a genuine RUN of device-level disconnect signals trips
        the escalation; a per-tab fault, however many tabs it touches, never
        does.
        """
        ok = isinstance(result, dict) and result.get("ok") is True
        error = result.get("error") if isinstance(result, dict) else None
        if not ok and _is_device_disconnect_error(error):
            self._consecutive_health_failures += 1
            self.device_health_signals += 1
            on_event(
                {
                    "event": "device_health_signal",
                    "consecutive_failures": self._consecutive_health_failures,
                    "error": error,
                }
            )
        else:
            self._consecutive_health_failures = 0

    async def wait_until_live(
        self,
        client: _ArchiveClient,
        device_id: str,
        *,
        budget_s: float,
        on_event: Callable[[dict[str, Any]], None],
    ) -> bool:
        """Bounded wait for `device_id` to report tier `"live"`, reusing the
        exact same `client.list_devices()` tier-check call and bounded-
        exponential-backoff shape `_wait_for_recovery`/`_wait_for_device_recovery`
        above already use -- module docstring's "Chunking" section. Used by
        `run_archive`'s inter-chunk settle (`_settle_between_chunks`), NOT by
        the reactive per-DISPATCH pacing those two methods implement -- this
        is a proactive, between-chunks checkpoint, not a before-a-single-
        dispatch one.

        Returns whether tier was confirmed `"live"` within `budget_s`. UNLIKE
        `_wait_for_recovery`/`_wait_for_device_recovery`, this is NOT purely
        advisory from the caller's point of view: `run_archive` treats a
        `False` return as a reason to stop dispatching further chunks
        entirely (module docstring's "Chunking" section) rather than
        proceeding anyway. Accordingly, a `HubError` from `list_devices()`
        here is treated as an INCONCLUSIVE read -- retried on the next
        backoff tick, same as a non-`"live"` tier -- rather than the
        immediate "give up and proceed anyway" those two methods use: we
        would rather keep checking, within budget, than let a caller
        silently resume dispatch into a device we were never able to
        confirm as healthy.

        `budget_s <= 0` means a single, immediate check with no waiting --
        the same "0 disables the wait" convention every other budget in this
        module already follows -- while still performing that one tier read
        (never skipped outright).
        """
        deadline = time.monotonic() + max(0.0, budget_s)
        backoff = max(self._min_interval_s, 0.5)
        while True:
            self.tier_checks += 1
            tier: str | None
            try:
                devices = await client.list_devices()
            except HubError:
                tier = None  # inconclusive -- retried below, same as non-live
            else:
                record = next((d for d in devices if d.get("device_id") == device_id), None)
                tier = (record or {}).get("tier", "live")
            if tier == "live":
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            wait_s = min(backoff, remaining)
            self.total_paused_s += wait_s
            await asyncio.sleep(wait_s)
            backoff = min(backoff * 2, max(budget_s, 0.5))


async def _resolve_queued(
    client: _ArchiveClient,
    device_id: str,
    result: Any,
    *,
    command: str,
    support: _RunSupport,
) -> Any:
    """Follows a `{"status": "queued", ...}` response with `client.poll()` until
    it resolves to a real terminal result, or gives up after `poll_max_wait_s`
    and returns an honest failure -- see module docstring's "Queued means wait,
    not fail" section. Passes any non-queued `result` through UNCHANGED (the
    overwhelmingly common case: a `live` device's immediate response).

    Never hangs indefinitely: `poll_max_wait_s` is a hard budget on THIS ONE
    command's wait, checked before every poll. A queued response missing
    `command_id` (a protocol violation) cannot be polled at all and is returned
    unresolved -- `_command_outcome`'s defensive fallback reports that honestly.
    """
    if not isinstance(result, dict) or result.get("status") != "queued":
        return result
    command_id = result.get("command_id")
    if not isinstance(command_id, str) or not command_id:
        return result
    started = time.monotonic()
    deadline = started + support.poll_max_wait_s
    tier = result.get("tier", "unknown")
    support.queued_waits += 1
    support.emit(
        {
            "event": "queued_wait_started",
            "device_id": device_id,
            "command": command,
            "command_id": command_id,
            "tier": tier,
        }
    )
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            waited_s = time.monotonic() - started
            support.queued_timeouts += 1
            support.queued_wait_total_s += waited_s
            support.emit(
                {
                    "event": "queued_wait_gave_up",
                    "device_id": device_id,
                    "command": command,
                    "command_id": command_id,
                    "waited_s": waited_s,
                }
            )
            return {
                "ok": False,
                "error": (
                    f"gave up waiting for queued command {command_id!r} ({command!r}) to resolve "
                    f"after {support.poll_max_wait_s:.0f}s (device tier last observed: {tier!r}). The "
                    "device may still complete this command later -- this archive treats an "
                    "unresolved wait as a failure rather than hanging indefinitely. Raise "
                    "poll_max_wait_s to wait longer."
                ),
            }
        await asyncio.sleep(min(support.poll_interval_s, remaining))
        try:
            polled = await client.poll(device_id, command_id)
        except HubError as e:
            support.queued_wait_total_s += time.monotonic() - started
            return {"ok": False, "error": str(e)}
        if not isinstance(polled, dict):
            support.queued_wait_total_s += time.monotonic() - started
            return {"ok": False, "error": f"unexpected poll response: {polled!r}"}
        status = polled.get("status")
        if status in ("queued", "pending"):
            tier = polled.get("tier", tier)
            continue
        waited_s = time.monotonic() - started
        support.queued_wait_total_s += waited_s
        support.emit(
            {
                "event": "queued_wait_resolved",
                "device_id": device_id,
                "command": command,
                "command_id": command_id,
                "waited_s": waited_s,
                "ok": polled.get("ok"),
            }
        )
        return polled


async def _safe_command(
    client: _ArchiveClient,
    target: Target,
    command: str,
    args: dict[str, Any],
    support: _RunSupport,
    *,
    is_cdp: bool = False,
) -> dict[str, Any]:
    """The single choke point every per-capture/per-profile-item `client.command()`
    call in this module goes through, so a TRANSPORT-level failure (`HubError` --
    e.g. a connection refused, a timeout, or a device/hub/client rejecting an
    oversized message -- see client.py's/hub.py's/protocol.py's "WebSocket
    message-size ceiling" section) becomes an ordinary `{"ok": False, "error":
    ...}` result instead of an exception, and a queued response is followed to
    its eventual real result rather than treated as a failure (module
    docstring's "Queued means wait, not fail" section).

    Real-world finding: `client.command()` previously raised `HubError` straight
    out of `_capture_tab`/`_capture_profile`/`run_archive`'s own top-level
    `windows`/`tabs` calls, uncaught -- one pathological tab (a real page whose
    MHTML capture tripped the client's websocket size cap) aborted the ENTIRE
    archive run partway through, discarding every already-captured tab's
    manifest entry (`manifest.json` is only ever written once, at the very end
    of `run_archive`) even though the files themselves were already safely on
    disk. `_command_outcome` already normalizes a dict-shaped
    `{"ok": false, ...}` result into a per-capture failure that lets the run
    continue (module docstring's "No silent partial success" section); this
    function is what makes a raised `HubError` reach `_command_outcome` in that
    same shape, rather than skipping it entirely. Deliberately narrow: only
    `HubError` (the one exception type this codebase's own transport layer is
    documented to raise for a connection-level failure) is caught here -- any
    other exception is a genuine bug and must keep propagating loudly, per this
    project's fail-loud convention (CONTRIBUTING.md).

    `is_cdp`, if True, routes this dispatch through `support.pacer` first (see
    module docstring's "Pacing" section) -- set by the caller for `mhtml`,
    `nav_history`, and `screenshot` when it is using `capture_hidden`; every
    other command (JS-injection captures, browser-wide profile-data commands,
    the top-level `windows`/`tabs` inventory) is never paced. For that same
    `is_cdp` subset, the RESOLVED result (after the queued-resolution above)
    is also fed back into `support.pacer.record_result` -- see module
    docstring's "Device health" section and `_CdpPacer.record_result`'s
    docstring -- so a run of device-health failures changes how the NEXT CDP
    dispatch is paced. This never changes the result itself or how it's
    recorded in the per-tab manifest; it is a pacing signal only.
    """
    if is_cdp:
        await support.pacer.before_dispatch(client, target.device_id, on_event=support.emit)
    try:
        result = await client.command(target, command, args)
    except HubError as e:
        result = {"ok": False, "error": str(e)}
    else:
        result = await _resolve_queued(client, target.device_id, result, command=command, support=support)
    if is_cdp:
        support.pacer.record_result(result, on_event=support.emit)
    return result


def _inventoried_count(entry: dict[str, Any]) -> int | None:
    """Pulls a definitive INVENTORY count out of a manifest capture entry
    (`manifest["windows"]`/`["tab_groups"]`/`["tabs_inventory"]`) for `summary`.
    Returns `None` -- never a misleading bare `0` -- when the underlying capture
    itself failed, so a reader of `summary` can tell "we never learned how many
    exist" apart from "zero exist" (module docstring's "No silent partial
    success" section)."""
    return entry.get("count") if entry.get("status") == "ok" else None


def _profile_summary(profile: dict[str, Any] | None) -> dict[str, int] | None:
    """Item-level capture accounting for the L5-only `profile` axis (history,
    bookmarks, sessions, top_sites, reading_list, cookies) -- `None` below L5,
    mirroring `manifest["profile"]` itself, never a misleading `0` standing in
    for "not attempted". `items_skipped` counts the intentional, non-failure
    cookies opt-out (see module docstring's "Cookies are opt-in" section), not
    a degraded run."""
    if profile is None:
        return None
    entries = list(profile.values())
    return {
        "items_total": len(entries),
        "items_captured": sum(1 for e in entries if e.get("status") == "ok"),
        "items_skipped": sum(1 for e in entries if e.get("status") == "skipped"),
        "items_failed": sum(1 for e in entries if e.get("status") == "failed"),
    }


def _record_json_capture(
    dir_path: Path, filename: str, result: Any, *, count_of: str | None = None
) -> dict[str, Any]:
    """Writes a JSON-shaped command result straight to disk and returns its
    manifest entry -- the general-purpose capture recorder used for every
    capture in this module EXCEPT the ones with a more specific shape
    (text/DOM/screenshot/mhtml, below), which get their own recorders so the
    interesting payload (text, HTML, image bytes, MHTML) lands in its own
    file rather than base64-wrapped inside a JSON blob."""
    ok, data, error = _command_outcome(result)
    if not ok:
        return _capture_failed(error or "unknown error")
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / filename
    size = _write_json(path, data)
    entry: dict[str, Any] = {"status": "ok", "path": str(path), "bytes": size}
    if isinstance(data, list):
        entry["count"] = len(data)
    elif count_of is not None and isinstance(data, dict) and isinstance(data.get(count_of), list):
        entry["count"] = len(data[count_of])
    return entry


def _record_read_capture(tab_dir: Path, result: Any) -> dict[str, Any]:
    """`read`'s result shape differs depending on whether `args.all_frames` was
    used: the common (top-frame-only, default) case is a flat `{url, title,
    text}`; `all_frames=true` produces `{url, title, frame_count, frames: [...],
    unconfirmed_frames}` with no top-level `text` at all (see combine_frames.mjs).
    Both are handled here rather than assuming one shape."""
    ok, data, error = _command_outcome(result)
    if not ok:
        return _capture_failed(error or "unknown error")
    if not isinstance(data, dict):
        return _capture_failed(f"expected `read` result to be a dict, got {type(data).__name__}: {data!r}")
    tab_dir.mkdir(parents=True, exist_ok=True)
    if "frames" in data:
        path = tab_dir / "text.json"
        size = _write_json(path, data)
        return {"status": "ok", "path": str(path), "bytes": size, "frame_count": data.get("frame_count")}
    text = data.get("text")
    if not isinstance(text, str):
        return _capture_failed(
            f"expected `read` result to include a string 'text' field, got keys {sorted(data)}"
        )
    path = tab_dir / "text.txt"
    path.write_text(text, encoding="utf-8")
    return {"status": "ok", "path": str(path), "bytes": len(text.encode("utf-8")), "chars": len(text)}


def _record_page_state_capture(tab_dir: Path, result: Any) -> dict[str, Any]:
    """`page_state`'s outerHTML and everything else are written to separate
    files -- an `outer_html` string can be multi-megabyte, and there is no
    reason to force a caller inspecting form/storage/scroll data to load that
    alongside it."""
    ok, data, error = _command_outcome(result)
    if not ok:
        return _capture_failed(error or "unknown error")
    if not isinstance(data, dict):
        return _capture_failed(
            f"expected `page_state` result to be a dict, got {type(data).__name__}: {data!r}"
        )
    tab_dir.mkdir(parents=True, exist_ok=True)
    html = data.get("outer_html")
    if not isinstance(html, str):
        return _capture_failed("expected `page_state` result to include a string 'outer_html' field")
    html_path = tab_dir / "dom.html"
    html_path.write_text(html, encoding="utf-8")
    metadata = {k: v for k, v in data.items() if k != "outer_html"}
    metadata_path = tab_dir / "page_state.json"
    metadata_bytes = _write_json(metadata_path, metadata)
    return {
        "status": "ok",
        "html_path": str(html_path),
        "html_bytes": len(html.encode("utf-8")),
        "html_truncated": bool(data.get("outer_html_truncated", False)),
        "metadata_path": str(metadata_path),
        "metadata_bytes": metadata_bytes,
    }


def _record_screenshot_capture(tab_dir: Path, result: Any) -> dict[str, Any]:
    ok, data, error = _command_outcome(result)
    if not ok:
        return _capture_failed(error or "unknown error")
    if not isinstance(data, dict) or not isinstance(data.get("base64"), str):
        got = sorted(data) if isinstance(data, dict) else type(data).__name__
        return _capture_failed(f"expected `screenshot` result to include a 'base64' string, got {got}")
    tab_dir.mkdir(parents=True, exist_ok=True)
    raw = base64.b64decode(data["base64"])
    ext = data.get("format") or "jpg"
    path = tab_dir / f"screenshot.{ext}"
    path.write_bytes(raw)
    return {"status": "ok", "path": str(path), "bytes": len(raw), "via": data.get("via")}


def _record_mhtml_capture(tab_dir: Path, result: Any) -> dict[str, Any]:
    ok, data, error = _command_outcome(result)
    if not ok:
        return _capture_failed(error or "unknown error")
    if not isinstance(data, dict) or not isinstance(data.get("data"), str):
        got = sorted(data) if isinstance(data, dict) else type(data).__name__
        return _capture_failed(f"expected `mhtml` result to include a string 'data' field, got {got}")
    tab_dir.mkdir(parents=True, exist_ok=True)
    mhtml_text = data["data"]
    path = tab_dir / "page.mhtml"
    path.write_text(mhtml_text, encoding="utf-8")
    return {"status": "ok", "path": str(path), "bytes": len(mhtml_text.encode("utf-8"))}


def _tab_status(captures: dict[str, Any]) -> str:
    """Rolls up a tab's per-capture statuses (`text`/`dom`/`screenshot`/`mhtml`/
    `nav_history`, whichever ran at this depth) into ONE tab-level status --
    never binary. `"ok"` only when every ATTEMPTED capture succeeded, `"failed"`
    only when every attempted capture failed, and `"partial"` -- the middle
    state neither of the other two can honestly claim -- when some succeeded and
    some failed (module docstring's "No silent partial success" section). This
    is the direct fix for the bug observed live: a tab on a browser error page
    had `text`/`dom` (JS injection) fail outright while `mhtml`/`screenshot`/
    `nav_history` (CDP-based, see the depth-ladder note on independent capture
    routes) all succeeded -- reporting that tab `"failed"` (the prior, binary
    behavior) discarded the ~147KB of real artifacts already on disk for it.
    Assumes at least one capture was attempted; `_capture_tab` never calls this
    for a `"skipped"` tab, which returns before any capture runs.

    A capture entry with `status == "skipped"` (the caller's explicit
    `captures` argument excluded it -- see `_capture_skipped_by_config`) is
    excluded from the ok/failed accounting entirely, the same way a whole
    `"skipped"` (no-wake) or `"not_found"` TAB is excluded one level up: it is
    neither a success nor a failure, so it must never pull `"ok"` down to
    `"partial"` or inflate `"failed"`'s denominator.
    """
    attempted = {name: c for name, c in captures.items() if c.get("status") != "skipped"}
    if not attempted:
        # Every capture reachable at this depth was excluded via `captures` --
        # `_validate_captures` is meant to make this unreachable (it rejects,
        # pre-flight, a `captures` set with nothing reachable at all at the
        # requested depth), but this is the defensive fallback: never
        # silently report "ok" (nothing succeeded) or "failed" (nothing
        # failed either) for zero attempted captures.
        return "skipped"
    ok_count = sum(1 for c in attempted.values() if c.get("status") == "ok")
    if ok_count == len(attempted):
        return "ok"
    if ok_count == 0:
        return "failed"
    return "partial"


_NOT_FOUND_REASON = (
    "tab_id was explicitly requested (via tab_ids) but is absent from the live tabs "
    "inventory -- most likely closed between when the caller read the inventory and "
    "when this archive ran. This is a benign, expected outcome, not a capture failure "
    "(there is no tab left to capture), but it must not be invisible: every id in "
    "tab_ids gets an entry in manifest['tabs'], one way or another."
)


def _not_found_tab_entry() -> dict[str, Any]:
    """Synthetic `manifest["tabs"][tab_id]` entry for a `tab_ids` id that no longer
    exists in the live inventory -- the FIFTH per-tab state (module docstring's "No
    silent partial success" section), distinct from `"ok"`/`"partial"`/`"failed"`/
    `"skipped"`. Unlike those four, there is no tab record to pull `url`/`title`/
    `window_id` from and no `captures` dict to populate -- the tab was never found in
    the first place, so this entry carries only `status` and `reason`.
    """
    return {"status": "not_found", "reason": _NOT_FOUND_REASON}


_SKIP_ASLEEP_REASON = (
    "tab is discarded/asleep; the archive orchestrator never wakes a tab implicitly -- pass "
    "wake=True to allow this (reloading a discarded tab to satisfy read/page_state destroys "
    "unsaved in-page state; attaching CDP for screenshot/mhtml/nav_history implicitly wakes a "
    "discarded tab as a side effect of the attach itself -- see docs/PROTOCOL.md's 'Discarded "
    "tabs' section)"
)


def _capture_skipped_by_config(name: str) -> dict[str, Any]:
    """Manifest entry for a per-tab capture the depth ladder would otherwise
    have attempted at this depth, but that the caller's explicit `captures`
    argument excluded (module docstring's "Injection budget and explicit
    capture selection" section). `status: "skipped"` -- the SAME status
    string an intentional no-wake tab skip and an opt-out `cookies_list`
    profile skip already use (both equally benign, equally non-failure) --
    with a `reason` naming exactly why, so it is never confused with a
    captured-and-failed outcome and never silently absorbed into a plain
    `"ok"` capture. Never added to `manifest["failures"]` (see
    `_capture_tab`'s `record`), mirroring how those other two skip kinds are
    excluded from `failures` too.
    """
    return {
        "status": "skipped",
        "reason": (
            f"'{name}' capture excluded by the caller's explicit `captures` argument -- not "
            "attempted at all (distinct from a captured-and-failed outcome; the depth ladder "
            "would otherwise have included it at this depth). See docs/PROTOCOL.md's "
            "'Browser-state archive' section."
        ),
    }


def _chunk_tabs(tabs: list[dict[str, Any]], chunk_size: int | None) -> list[list[dict[str, Any]]]:
    """Splits `tabs` into sequential sub-batches of at most `chunk_size` tabs each
    -- module docstring's "Chunking" section. `chunk_size` being `None` or `<= 0`
    means chunking is DISABLED: the whole list becomes a single chunk, so
    `run_archive`'s per-tab capture loop is byte-for-byte the pre-chunking single
    continuous loop (no inter-chunk settle ever runs, since there is only ever one
    chunk to finish) -- the same "0/None disables" convention this module already
    established for `cdp_burst_size`/`device_health_trip_threshold`. An empty
    `tabs` list produces zero chunks either way, so callers can iterate the result
    unconditionally without a separate empty-list special case.
    """
    if not tabs:
        return []
    if chunk_size is None or chunk_size <= 0:
        return [tabs]
    return [tabs[i : i + chunk_size] for i in range(0, len(tabs), chunk_size)]


_CHUNK_ABORTED_REASON = (
    "this tab was never attempted: the device did not confirm 'live' again within "
    "device_recovery_max_wait_s after a prior chunk finished, so run_archive stopped "
    "dispatching further chunks rather than throwing more load at a device that could not "
    "be confirmed healthy (module docstring's 'Chunking' section). This is a real failure, "
    "not a benign skip -- unlike a 'skipped' (no-wake) or 'not_found' tab, it is counted in "
    "manifest['failures']."
)


def _chunk_aborted_tab_entry(tab: dict[str, Any]) -> dict[str, Any]:
    """Synthetic `manifest["tabs"][tab_id]` entry for a tab in a chunk that was
    never even started because `_settle_between_chunks` gave up waiting for the
    device to confirm `"live"` again -- module docstring's "Chunking" section.
    Deliberately reuses the ordinary `"failed"` status (not a new sixth state):
    from the manifest reader's point of view this tab simply failed, for an
    honestly-named reason, exactly like any other capture failure -- the
    distinction that matters (proactively stopped vs. attempted-and-failed) lives
    in `reason`/`error`, not in a new status string a caller would have to learn.
    """
    return {
        "url": tab.get("url"),
        "title": tab.get("title"),
        "window_id": tab.get("window_id"),
        "captures": {},
        "status": "failed",
        "reason": _CHUNK_ABORTED_REASON,
    }


async def _settle_between_chunks(
    client: _ArchiveClient,
    device_id: str,
    support: _RunSupport,
    *,
    cooldown_s: float,
    recovery_budget_s: float,
) -> bool:
    """The inter-chunk settle itself (module docstring's "Chunking" section):
    an unconditional `cooldown_s` pause, THEN a bounded tier-confirmation wait
    via `_CdpPacer.wait_until_live` (reusing the same `client.list_devices()`
    tier-check path the reactive recovery waits already use). Returns whether
    the device confirmed `"live"` within `recovery_budget_s` -- UNLIKE the
    reactive waits, this return value IS a correctness gate for the caller:
    `run_archive` stops dispatching further chunks if it is `False`.

    Every second spent here (the fixed cooldown AND any backoff waiting inside
    `wait_until_live`) is accounted into `support.chunk_settle_wait_total_s`,
    surfaced in `manifest["pacing"]` for a caller who only reads the manifest
    after the fact.
    """
    started = time.monotonic()
    if cooldown_s > 0:
        await asyncio.sleep(cooldown_s)
    support.chunk_settles += 1
    recovered = await support.pacer.wait_until_live(
        client, device_id, budget_s=recovery_budget_s, on_event=support.emit
    )
    support.chunk_settle_wait_total_s += time.monotonic() - started
    support.emit(
        {
            "event": "chunk_settle_recovered" if recovered else "chunk_settle_gave_up",
            "device_id": device_id,
        }
    )
    if not recovered:
        support.chunk_gave_up = True
    return recovered


async def _capture_tab(
    client: _ArchiveClient,
    device_id: str,
    tab: dict[str, Any],
    *,
    depth_idx: int,
    archive_dir: Path,
    wake: bool,
    all_frames: bool,
    use_capture_hidden: bool,
    timeout_s: float | None,
    injection_timeout_s: float | None,
    captures: frozenset[str] | None,
    failures: list[dict[str, Any]],
    support: _RunSupport,
) -> dict[str, Any]:
    tab_id = tab.get("tab_id")
    entry: dict[str, Any] = {
        "url": tab.get("url"),
        "title": tab.get("title"),
        "window_id": tab.get("window_id"),
        "captures": {},
    }

    if bool(tab.get("discarded") or tab.get("asleep")) and not wake:
        entry["status"] = "skipped"
        entry["reason"] = _SKIP_ASLEEP_REASON
        return entry

    tab_dir = archive_dir / "tabs" / str(tab_id)
    target = Target(device_id=device_id, tab_id=tab_id, window_id=tab.get("window_id"))
    base_args: dict[str, Any] = {}
    if wake:
        base_args["wake"] = True
    if timeout_s is not None:
        base_args["timeout_s"] = timeout_s

    # Injection-based captures (`read`/`page_state` -- module docstring's
    # "Injection budget" section) get their OWN, optionally tighter, timeout
    # budget than CDP-based captures -- a caller archiving many tabs can bound
    # the wall-clock cost of a hung/heavy SPA's JS-injection captures without
    # ever reducing what the depth ladder attempts. `injection_timeout_s=None`
    # (the default) means "no override" -- `injection_args` is then IDENTICAL
    # to `base_args`, so behavior is byte-for-byte unchanged from before this
    # argument existed.
    injection_args: dict[str, Any] = dict(base_args)
    if injection_timeout_s is not None:
        injection_args["timeout_s"] = injection_timeout_s

    def allowed(name: str) -> bool:
        return captures is None or name in captures

    def record(name: str, capture_entry: dict[str, Any]) -> None:
        entry["captures"][name] = capture_entry
        if capture_entry.get("status") not in ("ok", "skipped"):
            failures.append(
                {"scope": "tab", "tab_id": tab_id, "capture": name, "error": capture_entry.get("error")}
            )

    # Per-tab CDP isolation -- see module docstring's "Per-tab CDP isolation" section.
    # Set to True whenever a just-completed CDP-based capture's OWN result matched a
    # per-tab session fault (`_is_per_tab_cdp_session_error` -- `-32603`/`Detached
    # while handling command`, NEVER a genuine device disconnect, which is a
    # separate, device-scoped signal `_CdpPacer` handles). Consumed (and cleared) by
    # `_reattach_if_needed` immediately before the NEXT CDP-based capture for THIS
    # tab, so one tab's broken session cannot cascade into another capture on the
    # same tab. `nonlocal` rather than a class: this state is scoped to one
    # `_capture_tab` call (one tab), never shared across tabs.
    needs_reattach = False

    def note_cdp_result(result: Any) -> None:
        nonlocal needs_reattach
        if (
            isinstance(result, dict)
            and result.get("ok") is not True
            and _is_per_tab_cdp_session_error(result.get("error"))
        ):
            needs_reattach = True

    async def reattach_if_needed() -> None:
        """Best-effort, explicit `attach` for THIS tab -- issued only when a PRIOR
        CDP-based capture on it observed a per-tab session fault, immediately before
        the next one dispatches. This is orchestrator-level recovery bookkeeping, not
        itself a paced/recorded capture: `is_cdp=False` (never counted toward
        `support.pacer`'s device-health trip accounting -- that budget is reserved
        for genuine device-level disconnects, not a single tab's re-attach) and its
        result is never written into `entry["captures"]`. A failed re-attach is not
        raised or treated specially here -- the next capture attempt simply proceeds
        and, if the tab truly cannot be reached, fails and is recorded as an honest
        per-tab failure like any other (module docstring's "No silent partial
        success" section)."""
        nonlocal needs_reattach
        if not needs_reattach:
            return
        await _safe_command(client, target, "attach", {}, support)
        needs_reattach = False

    if depth_idx >= _DEPTH_INDEX["L1"]:
        if allowed("text"):
            read_args = {**injection_args}
            if all_frames:
                read_args["all_frames"] = True
            result = await _safe_command(client, target, "read", read_args, support)
            record("text", _record_read_capture(tab_dir, result))
        else:
            record("text", _capture_skipped_by_config("text"))

    if depth_idx >= _DEPTH_INDEX["L2"]:
        if allowed("dom"):
            result = await _safe_command(client, target, "page_state", dict(injection_args), support)
            record("dom", _record_page_state_capture(tab_dir, result))
        else:
            record("dom", _capture_skipped_by_config("dom"))

    # `mhtml` (L4) and `nav_history` (L5) dispatch BEFORE `screenshot` (L3) here --
    # deliberately NOT the depth ladder's own introduction order (see module
    # docstring's "Per-tab CDP isolation" section). `Page.captureScreenshot` is the
    # capture observed to throw CDP `-32603` on a tab with no paintable surface
    # (e.g. woken from discard); `Page.captureSnapshot`/`Page.getNavigationHistory`
    # need no painted frame and are not implicated. Capturing the structurally safer,
    # higher-value snapshots FIRST means a screenshot-induced detach can no longer
    # cascade into them -- only `screenshot` itself, last, is ever at risk of being
    # doomed by an earlier failure on this tab (there is nothing scheduled after it).
    # Depth-ladder gating is unchanged; only DISPATCH ORDER moved.
    if depth_idx >= _DEPTH_INDEX["L4"]:
        if allowed("mhtml"):
            result = await _safe_command(client, target, "mhtml", dict(base_args), support, is_cdp=True)
            note_cdp_result(result)
            record("mhtml", _record_mhtml_capture(tab_dir, result))
        else:
            record("mhtml", _capture_skipped_by_config("mhtml"))

    if depth_idx >= _DEPTH_INDEX["L5"]:
        if allowed("nav_history"):
            await reattach_if_needed()
            result = await _safe_command(client, target, "nav_history", dict(base_args), support, is_cdp=True)
            note_cdp_result(result)
            record("nav_history", _record_json_capture(tab_dir, "nav_history.json", result))
        else:
            record("nav_history", _capture_skipped_by_config("nav_history"))

    if depth_idx >= _DEPTH_INDEX["L3"]:
        if allowed("screenshot"):
            await reattach_if_needed()
            screenshot_args = {**base_args}
            if use_capture_hidden:
                screenshot_args["capture_hidden"] = True
            # `screenshot` is CDP-based only when it carries `capture_hidden` --
            # see module docstring's "Pacing" section and `_CdpPacer`. Dispatched
            # LAST among this tab's CDP captures (see above) -- nothing else this
            # run schedules for this tab can be doomed by ITS failure.
            result = await _safe_command(
                client, target, "screenshot", screenshot_args, support, is_cdp=use_capture_hidden
            )
            record("screenshot", _record_screenshot_capture(tab_dir, result))
        else:
            record("screenshot", _capture_skipped_by_config("screenshot"))

    entry["status"] = _tab_status(entry["captures"])
    return entry


# (profile-data key, command, filename, the list-shaped field to count -- or None
# for sessions_list, whose two lists (recently_closed/devices) don't collapse to a
# single meaningful count).
_PROFILE_SPECS: tuple[tuple[str, str, str, str | None], ...] = (
    ("history", "history_list", "history.json", "entries"),
    ("bookmarks", "bookmarks_list", "bookmarks.json", "entries"),
    ("sessions", "sessions_list", "sessions.json", None),
    ("top_sites", "top_sites", "top_sites.json", "entries"),
    ("reading_list", "reading_list", "reading_list.json", "entries"),
)


async def _capture_profile(
    client: _ArchiveClient,
    device_id: str,
    archive_dir: Path,
    *,
    include_cookies: bool,
    failures: list[dict[str, Any]],
    support: _RunSupport,
) -> dict[str, Any]:
    profile_dir = archive_dir / "profile"
    target = Target(device_id=device_id)
    profile: dict[str, Any] = {}

    for key, command, filename, count_of in _PROFILE_SPECS:
        result = await _safe_command(client, target, command, {}, support)
        capture_entry = _record_json_capture(profile_dir, filename, result, count_of=count_of)
        profile[key] = capture_entry
        if capture_entry.get("status") != "ok":
            failures.append({"scope": "profile", "item": key, "error": capture_entry.get("error")})

    if include_cookies:
        result = await _safe_command(client, target, "cookies_list", {}, support)
        capture_entry = _record_json_capture(profile_dir, "cookies.json", result, count_of="entries")
        profile["cookies"] = capture_entry
        if capture_entry.get("status") != "ok":
            failures.append({"scope": "profile", "item": "cookies", "error": capture_entry.get("error")})
    else:
        profile["cookies"] = {
            "status": "skipped",
            "reason": (
                "include_cookies=False (default) -- cookies are opt-in only and are never "
                "included even at the deepest archive level unless explicitly requested"
            ),
        }

    return profile


async def run_archive(
    client: _ArchiveClient,
    device_id: str,
    dest_dir: str | Path,
    *,
    depth: str = DEFAULT_DEPTH,
    tab_ids: list[int] | None = None,
    include_cookies: bool = False,
    wake: bool = False,
    all_frames: bool = False,
    timeout_s: float | None = None,
    injection_timeout_s: float | None = None,
    captures: list[str] | None = None,
    cdp_pace_s: float = DEFAULT_CDP_PACE_S,
    cdp_backpressure_max_wait_s: float = DEFAULT_CDP_BACKPRESSURE_MAX_WAIT_S,
    device_health_trip_threshold: int = DEFAULT_DEVICE_HEALTH_TRIP_THRESHOLD,
    device_recovery_max_wait_s: float = DEFAULT_DEVICE_RECOVERY_MAX_WAIT_S,
    cdp_burst_size: int = DEFAULT_CDP_BURST_SIZE,
    cdp_burst_cooldown_s: float = DEFAULT_CDP_BURST_COOLDOWN_S,
    cdp_chunk_size: int | None = DEFAULT_CDP_CHUNK_SIZE,
    cdp_chunk_cooldown_s: float = DEFAULT_CDP_CHUNK_COOLDOWN_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    poll_max_wait_s: float = DEFAULT_POLL_MAX_WAIT_S,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Capture `device_id`'s browser state at `depth` (see module docstring's depth
    ladder), writing every payload under a fresh timestamped directory inside
    `dest_dir`, and return `{"ok": True, "result": <manifest>}`.

    `tab_ids`, if given, restricts per-tab CAPTURE (L1+) to that subset -- the L0
    windows/groups/tabs inventory is always captured in full regardless (it is
    already cheap and has no per-tab cost). A requested id absent from the live
    inventory (e.g. the tab was closed between the caller reading it and this
    call, or it never existed at all) gets its own `manifest["tabs"][tab_id] =
    {"status": "not_found", ...}` entry -- see module docstring's "No silent
    partial success" section -- at every depth, INCLUDING L0: this accounting is
    computed once against the live inventory and does not depend on whether any
    per-tab capture ran, so every id in `tab_ids` is accounted for regardless of
    `depth`, never silently dropped. `wake`, if True, allows per-tab
    capture to reload/attach-wake a discarded or asleep tab -- see module
    docstring's "no-wake guarantee" section; the DEFAULT is to skip such tabs
    entirely rather than disturb them. `all_frames`, if True, is forwarded to the
    `read` (L1) capture only (`page_state` does not support multi-frame
    gathering in this phase -- see docs/PROTOCOL.md's "Documented narrower
    limitation"). `include_cookies` gates `cookies_list` at L5 -- see module
    docstring's "Cookies are opt-in" section; the default is False at every
    depth, with no exception.

    `injection_timeout_s`, if given, overrides `timeout_s` for JUST the two
    JS-injection-based per-tab captures (`read`/`page_state`, L1/L2) -- CDP-based
    captures (`screenshot`/`mhtml`/`nav_history`) keep using `timeout_s` (or the
    hub's own default) unchanged. See module docstring's "Injection budget and
    explicit capture selection" section: on a heavy hydrated SPA, injection
    captures can time out at the FULL command-timeout budget while CDP-based
    captures on the same tab succeed in seconds -- for an archive spanning many
    tabs, a caller can bound the wall-clock cost of that timeout without
    reducing what the depth ladder attempts. `None` (the default) means no
    override: `timeout_s` (or the hub default) applies uniformly to every
    capture, exactly as before this argument existed.

    `captures`, if given, is an explicit allow-list (from `CAPTURE_NAMES`:
    `"text"`, `"dom"`, `"screenshot"`, `"mhtml"`, `"nav_history"`) that narrows
    -- never widens -- which per-tab captures actually run at this depth. A
    capture the depth ladder would otherwise attempt, but that is excluded from
    `captures`, is recorded as `{"status": "skipped", "reason": ...}` (the SAME
    status a no-wake tab skip or an opt-out cookies skip already uses) rather
    than silently omitted -- never confused with a captured-and-failed outcome.
    `None` (the default) means no narrowing: every capture the depth ladder
    attempts runs, the pre-existing strict-superset behavior, unchanged. Raises
    `ArchiveError` pre-flight (see `_validate_captures`) for an empty list, an
    unrecognized name, or a `captures` set naming nothing reachable at all at
    the requested `depth` -- a caller must never silently get an archive that
    captured nothing because of a mismatched `captures`/`depth` combination.

    Raises `ArchiveError` for a pre-flight failure (unknown depth, unknown
    device, a depth that is impossible on this device -- e.g. L4 on a device
    without the `debugger` capability -- or an invalid `captures` argument)
    BEFORE anything is captured or written to disk. Never raises for an
    ordinary per-tab/profile-data capture failure; those are recorded in the
    returned manifest (`manifest["failures"]`, `manifest["status"]`) and the
    run continues.

    `cdp_pace_s`/`cdp_backpressure_max_wait_s`, `device_health_trip_threshold`/
    `device_recovery_max_wait_s`, `cdp_burst_size`/`cdp_burst_cooldown_s`, and
    `poll_interval_s`/`poll_max_wait_s` are the pacing/queued-resolution knobs
    from module docstring's "Pacing", "Device health", and "Queued means wait,
    not fail" sections:

        - `cdp_pace_s`: floor on the interval between successive CDP-requiring
          dispatches (`mhtml`, `nav_history`, `screenshot` with
          `capture_hidden`) -- 0 disables the floor entirely (still subject to
          the tier-check backpressure below unless `cdp_backpressure_max_wait_s`
          is also 0).
        - `cdp_backpressure_max_wait_s`: cap on how long a CDP dispatch will
          wait (bounded exponential backoff) for a device that has fallen off
          `live` to recover before proceeding anyway -- 0 disables tier
          checking entirely, leaving only the `cdp_pace_s` floor. Assumes the
          device is merely SLOW; see the next two knobs for a device that has
          genuinely disconnected.
        - `device_health_trip_threshold`: consecutive CDP-paced capture
          failures matching a device-health signature (`Detached while
          handling command`, CDP `-32603` `Internal error`, `disconnected
          mid-command`, `could not reach hub` -- see module docstring's
          "Device health" section) before the pacer escalates to
          `device_recovery_max_wait_s` instead of the ordinary
          `cdp_backpressure_max_wait_s` -- 0 disables this escalation
          entirely (every CDP-paced capture is still attempted and recorded
          normally, just without this extra layer).
        - `device_recovery_max_wait_s`: cap on the escalated recovery wait
          above -- deliberately much larger than `cdp_backpressure_max_wait_s`,
          since a genuine reconnect needs real time for the extension to
          re-establish its websocket and `hello`. If this budget is exhausted
          without the device confirming `live` again, the pacer doubles its
          own effective `cdp_pace_s` (capped at 8x) for the rest of the run,
          rather than resuming at the exact cadence that just overwhelmed the
          device -- an adaptive slowdown, not a retry.
        - `cdp_burst_size`/`cdp_burst_cooldown_s`: proactive bound on burst
          load, independent of any failure signal -- after `cdp_burst_size`
          consecutive CDP dispatches, a mandatory `cdp_burst_cooldown_s` pause
          is inserted regardless of tier or device health, so a single large
          archive cannot throw unlimited burst load at the device in the
          first place. `cdp_burst_size<=0` disables burst cooldowns entirely.
          Never triggers on a small archive -- the default (25) is well above
          any run this fix is not concerned with.
        - `poll_interval_s`/`poll_max_wait_s`: how often, and for how long in
          total, a single queued command is polled before this archive gives
          up on it and records a failure -- never an unbounded/silent hang.

    `cdp_chunk_size`/`cdp_chunk_cooldown_s` are the CHUNKING knobs from module
    docstring's "Chunking" section -- a PROACTIVE bound, applied ONE LEVEL UP
    from every knob above (which all pace individual dispatches or bursts
    WITHIN one continuous per-tab capture loop): they split `tab_ids`'s
    per-tab capture loop itself into sequential sub-batches, so a large
    archive never fires a burst of CDP-heavy dispatches large enough to reach
    the device's drop threshold in the first place, rather than only reacting
    once it has:

        - `cdp_chunk_size`: max tabs processed per chunk before an inter-chunk
          settle. `<= 0` or `None` (default `8`) disables chunking entirely --
          `tab_ids` is then processed as a single continuous loop, byte-for-
          byte the pre-chunking behavior. Never engages for a run whose
          selected tab count is already `<= cdp_chunk_size` -- a small run is
          exactly one chunk.
        - `cdp_chunk_cooldown_s` (default 5.0s): unconditional pause inserted
          BETWEEN chunks (never within one), before the tier-confirmation
          wait below.

    Between each pair of chunks (never after the last one), `run_archive`
    inserts `cdp_chunk_cooldown_s`, THEN waits (bounded by
    `device_recovery_max_wait_s` -- the SAME budget the reactive mid-chunk
    recovery above uses, reusing the exact same `client.list_devices()`
    tier-check path via `_CdpPacer.wait_until_live`) for the device to
    confirm `"live"` again. UNLIKE every wait above, this one is NOT merely
    advisory: if the device never confirms `"live"` within that budget, this
    function stops dispatching further chunks entirely, and every tab in
    every not-yet-started chunk gets an honest `"failed"` entry in
    `manifest["tabs"]` (added to `manifest["failures"]` like any other real
    failure) rather than being silently dropped or attempted into a device
    that could not be confirmed healthy. A device-health trip and recovery
    INSIDE a single chunk is unaffected by chunking and handled exactly as
    described above -- chunking composes with that reactive machinery rather
    than replacing it.

    `on_progress`, if given, is called synchronously with a structured event
    dict (`{"event": ..., ...}`) as the run progresses -- per-tab completion,
    pacing/backpressure pauses, and queued-command waits -- rather than only
    reflected in the manifest once the whole run finishes. A raised exception
    from the callback itself is NOT caught (a broken callback is a caller
    bug, per this project's fail-loud convention). The same activity is also
    summarized, cumulatively, in the returned `manifest["pacing"]` for a
    caller that only inspects the manifest after the fact.
    """
    depth_idx = _depth_index(depth)
    captures_set = _validate_captures(captures, depth=depth, depth_idx=depth_idx)

    devices = await client.list_devices()
    record = next((d for d in devices if d.get("device_id") == device_id), None)
    if record is None:
        raise ArchiveError(f"unknown device: {device_id!r} (call browser_devices first)")
    capabilities: dict[str, Any] = record.get("capabilities") or {}

    if depth_idx >= _DEPTH_INDEX[_CDP_REQUIRED_FROM_DEPTH] and not capabilities.get(_CDP_CAPABILITY):
        lower_depths = ", ".join(DEPTHS[: _DEPTH_INDEX[_CDP_REQUIRED_FROM_DEPTH]])
        raise ArchiveError(
            f"archive depth {depth!r} is impossible on device {device_id!r}: MHTML capture (L4) and "
            "navigation history (L5) are unconditionally CDP-requiring, with no injection-only "
            f"fallback, and this device reports the '{_CDP_CAPABILITY}' capability unavailable "
            "(e.g. Edge Android genuinely lacks chrome.debugger). This never silently degrades to a "
            f"lower depth -- request one of {lower_depths} instead, or use a device with CDP support."
        )

    started_at = datetime.now(UTC)
    dest_root = Path(dest_dir).expanduser()
    archive_dir = (
        dest_root / f"archive_{_sanitize_component(device_id)}_{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    )
    archive_dir.mkdir(parents=True, exist_ok=True)

    failures: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "device_id": device_id,
        "depth": depth,
        # `None` (the default) means "no narrowing -- every capture the depth
        # ladder attempts ran", never silently omitted here just because it
        # wasn't narrowed -- a caller reading the manifest later must be able
        # to tell exactly what was asked for, not just infer it from `depth`.
        "captures_requested": sorted(captures_set) if captures_set is not None else None,
        "archive_dir": str(archive_dir),
        "started_at": started_at.isoformat(),
    }

    device_target = Target(device_id=device_id)
    support = _RunSupport(
        pacer=_CdpPacer(
            min_interval_s=cdp_pace_s,
            max_wait_s=cdp_backpressure_max_wait_s,
            device_health_trip_threshold=device_health_trip_threshold,
            device_recovery_max_wait_s=device_recovery_max_wait_s,
            burst_size=cdp_burst_size,
            burst_cooldown_s=cdp_burst_cooldown_s,
        ),
        poll_interval_s=poll_interval_s,
        poll_max_wait_s=poll_max_wait_s,
        on_progress=on_progress,
    )

    windows_result = await _safe_command(client, device_target, "windows", {}, support)
    ok, windows_data, error = _command_outcome(windows_result)
    if ok and isinstance(windows_data, dict):
        windows_path = archive_dir / "windows.json"
        size = _write_json(windows_path, windows_data)
        manifest["windows"] = {
            "status": "ok",
            "path": str(windows_path),
            "bytes": size,
            "count": len(windows_data.get("windows", [])),
        }
        manifest["tab_groups"] = {
            "status": "ok",
            "path": str(windows_path),
            "count": len(windows_data.get("tab_groups", [])),
        }
    else:
        windows_error = error if ok else (error or f"unexpected `windows` result: {windows_data!r}")
        manifest["windows"] = _capture_failed(windows_error or "unknown error")
        manifest["tab_groups"] = _capture_failed(windows_error or "unknown error")
        failures.append({"scope": "windows", "error": windows_error})

    tabs_result = await _safe_command(client, device_target, "tabs", {}, support)
    ok, tab_list, error = _command_outcome(tabs_result)
    if ok and isinstance(tab_list, list):
        tabs_path = archive_dir / "tabs.json"
        size = _write_json(tabs_path, tab_list)
        manifest["tabs_inventory"] = {
            "status": "ok",
            "path": str(tabs_path),
            "bytes": size,
            "count": len(tab_list),
        }
    else:
        tabs_error = error if ok else (error or f"unexpected `tabs` result: {tab_list!r}")
        manifest["tabs_inventory"] = _capture_failed(tabs_error or "unknown error")
        failures.append({"scope": "tabs_inventory", "error": tabs_error})
        tab_list = []

    all_tabs = [t for t in tab_list if isinstance(t, dict)]
    missing: list[int] = []
    if tab_ids is not None:
        selected_tabs = [t for t in all_tabs if t.get("tab_id") in tab_ids]
        found_ids = {t.get("tab_id") for t in selected_tabs}
        missing = sorted(set(tab_ids) - found_ids)
        if missing:
            manifest["requested_tab_ids_not_found"] = missing
    else:
        selected_tabs = all_tabs

    tab_manifest: dict[str, Any] = {}
    if depth_idx >= _DEPTH_INDEX["L1"]:
        use_capture_hidden = bool(capabilities.get(_CDP_CAPABILITY))
        # See module docstring's "Chunking" section: `selected_tabs` is split into
        # sequential sub-batches (a no-op split -- one chunk containing the whole
        # list -- when chunking is disabled or the run is already small; see
        # `_chunk_tabs`), with a settle BETWEEN chunks (never within one) so a
        # large archive never fires a burst of CDP-heavy dispatches large enough
        # to reach the device's drop threshold in the first place.
        chunks = _chunk_tabs(selected_tabs, cdp_chunk_size)
        support.chunks_total = len(chunks)
        for chunk_index, chunk in enumerate(chunks):
            for tab in chunk:
                tab_id = tab.get("tab_id")
                if tab_id is None:
                    continue
                tab_manifest[str(tab_id)] = await _capture_tab(
                    client,
                    device_id,
                    tab,
                    depth_idx=depth_idx,
                    archive_dir=archive_dir,
                    wake=wake,
                    all_frames=all_frames,
                    use_capture_hidden=use_capture_hidden,
                    timeout_s=timeout_s,
                    injection_timeout_s=injection_timeout_s,
                    captures=captures_set,
                    failures=failures,
                    support=support,
                )
                support.emit(
                    {
                        "event": "tab_done",
                        "tab_id": tab_id,
                        "status": tab_manifest[str(tab_id)].get("status"),
                        "tabs_done": len(tab_manifest),
                        "tabs_total": len(selected_tabs),
                    }
                )
            support.chunks_completed += 1
            is_last_chunk = chunk_index == len(chunks) - 1
            if is_last_chunk:
                continue
            support.emit(
                {
                    "event": "chunk_completed",
                    "chunk_index": chunk_index,
                    "chunks_total": support.chunks_total,
                }
            )
            recovered = await _settle_between_chunks(
                client,
                device_id,
                support,
                cooldown_s=cdp_chunk_cooldown_s,
                recovery_budget_s=device_recovery_max_wait_s,
            )
            if recovered:
                continue
            # The device never confirmed `live` again within the recovery budget --
            # stop dispatching further chunks entirely and record every tab in
            # every not-yet-started chunk as an honest failure (module docstring's
            # "Chunking" section): never a silent drop, never a hang, and never a
            # one-by-one cascade through tabs that were never going to succeed.
            for remaining_chunk in chunks[chunk_index + 1 :]:
                for tab in remaining_chunk:
                    tab_id = tab.get("tab_id")
                    if tab_id is None:
                        continue
                    entry = _chunk_aborted_tab_entry(tab)
                    tab_manifest[str(tab_id)] = entry
                    failures.append(
                        {"scope": "tab", "tab_id": tab_id, "capture": None, "error": entry["reason"]}
                    )
            break
    # A requested tab_id that vanished (closed) between inventory and capture --
    # or simply never existed at all -- is NEVER just absent; see module
    # docstring's "not_found" bullet under "No silent partial success". This
    # accounting is DEPTH-INDEPENDENT and deliberately lives OUTSIDE the `if`
    # above: `missing` (computed unconditionally, above, from the same
    # `all_tabs`/`tab_ids` comparison at every depth) already tells us which
    # requested ids are absent before any per-tab CAPTURE is even considered.
    # Nesting this inside the L1+ capture gate was itself the bug this commit
    # fixes -- it conflated "L0 does no page contact" (correct, by design) with
    # "L0 ignores what you asked for" (not correct): a caller requesting an
    # explicit tab_id that does not exist, at L0, got back a clean "ok" with
    # that id never mentioned anywhere in the manifest. Every id in `tab_ids`
    # gets an entry here, alongside every other tab this run touched, at every
    # depth including L0 -- not just living in the top-level
    # `requested_tab_ids_not_found` convenience list.
    for tab_id in missing:
        tab_manifest[str(tab_id)] = _not_found_tab_entry()
    manifest["tabs"] = tab_manifest

    profile_result: dict[str, Any] | None
    if depth_idx >= _DEPTH_INDEX["L5"]:
        profile_result = await _capture_profile(
            client,
            device_id,
            archive_dir,
            include_cookies=include_cookies,
            failures=failures,
            support=support,
        )
    else:
        profile_result = None
    manifest["profile"] = profile_result

    finished_at = datetime.now(UTC)
    manifest["finished_at"] = finished_at.isoformat()
    manifest["duration_s"] = (finished_at - started_at).total_seconds()
    manifest["failures"] = failures

    tabs_skipped = sum(1 for t in tab_manifest.values() if t.get("status") == "skipped")
    tabs_failed = sum(1 for t in tab_manifest.values() if t.get("status") == "failed")
    tabs_partial = sum(1 for t in tab_manifest.values() if t.get("status") == "partial")
    tabs_captured = sum(1 for t in tab_manifest.values() if t.get("status") == "ok")
    tabs_not_found = sum(1 for t in tab_manifest.values() if t.get("status") == "not_found")

    # See module docstring's "No silent partial success" section: the INVENTORY axis
    # (what actually exists in the browser -- populated at every depth, including L0)
    # is never collapsed into the CAPTURE axis (what had its page content pulled down
    # -- legitimately all zero at L0 by design). This is the direct fix for the bug
    # where an L0 archive of 735 real tabs reported `tabs_total: 0`: that field was
    # silently reading `len(tab_manifest)` (a capture count) where a caller expected
    # a true inventory count.
    #
    # `tabs_partial` is the same fix one level down: a tab's own status is not
    # binary (see `_tab_status`), so a tab where SOME captures succeeded and some
    # failed must be counted on its own, never folded into `tabs_captured` (which
    # would hide the real failures) or into `tabs_failed` (which would discard the
    # real artifacts already on disk for it -- observed live, a tab with 3 of 5
    # captures succeeding was previously counted as zero captures).
    #
    # `tabs_not_found` counts requested-but-vanished tab_ids (see `_not_found_tab_entry`).
    # `tabs_capture_attempted` is computed as the sum of the four outcomes a real
    # capture attempt can produce -- NOT `len(tab_manifest)` -- specifically so it
    # excludes `tabs_not_found`: a tab_id that no longer exists was never actually
    # attempted, and folding it into "attempted" would just recreate this same bug in
    # a new shape (claiming an attempt happened when there was nothing to attempt).
    manifest["summary"] = {
        "windows_inventoried": _inventoried_count(manifest["windows"]),
        "tab_groups_inventoried": _inventoried_count(manifest["tab_groups"]),
        "tabs_inventoried": _inventoried_count(manifest["tabs_inventory"]),
        "tabs_capture_attempted": tabs_captured + tabs_partial + tabs_failed + tabs_skipped,
        "tabs_captured": tabs_captured,
        "tabs_partial": tabs_partial,
        "tabs_skipped": tabs_skipped,
        "tabs_failed": tabs_failed,
        "tabs_not_found": tabs_not_found,
        "profile": _profile_summary(profile_result),
        "has_failures": bool(failures),
    }

    # `status` is the ONE key a caller scanning quickly cannot miss -- "ok" is
    # reserved for a run with zero failures AND zero skips; a degraded run (any
    # failure, or any tab intentionally skipped to honor the no-wake guarantee)
    # is never reported as plain "ok" (module docstring's "No silent partial
    # success" section). A tab counted in `tabs_partial` (or `tabs_failed`)
    # always contributed at least one entry to `failures` via `_capture_tab`'s
    # `record`, so a run containing any partial tab lands in the `failures`
    # branch below, never the plain-"ok" branch. A `tabs_not_found` tab never adds
    # to `failures` (it is benign, not a failure -- see `_NOT_FOUND_REASON`), so it
    # is checked alongside `tabs_skipped` here: both are "something benign didn't
    # get captured" outcomes, and neither is ever silently absorbed into plain "ok".
    if failures:
        manifest["status"] = "ok_with_failures"
    elif tabs_skipped > 0 or tabs_not_found > 0:
        manifest["status"] = "ok_with_skips"
    else:
        manifest["status"] = "ok"

    # Pacing/backpressure/queued-wait diagnostics for this run -- see module
    # docstring's "Pacing" and "Queued means wait, not fail" sections. Purely
    # observational: never consulted by any correctness decision above, only
    # surfaced so an operator reading the manifest after the fact (or an
    # `on_progress` consumer, live) can tell whether this run had to slow down
    # for the device, and whether any capture had to wait on a queued command.
    manifest["pacing"] = {
        "cdp_pace_s": cdp_pace_s,
        "cdp_backpressure_max_wait_s": cdp_backpressure_max_wait_s,
        "cdp_tier_checks": support.pacer.tier_checks,
        "cdp_backpressure_events": support.pacer.backpressure_events,
        "cdp_backpressure_paused_s": round(support.pacer.total_paused_s, 3),
        # Device health -- see module docstring's "Device health" section.
        # `device_health_signals` is the raw count of CDP-paced failures
        # classified as device-health (not per-page) signatures;
        # `device_health_trips` is how many times a RUN of those (per
        # `device_health_trip_threshold`) actually triggered the escalated
        # recovery wait; `device_health_recoveries` is how many of those
        # trips confirmed the device back to `live` before giving up.
        # `cdp_slowdown_multiplier` reports the pacer's own final adaptive
        # slowdown factor (1.0 if never needed) -- a caller can see, after the
        # fact, whether this run had to permanently ease off.
        "device_health_trip_threshold": device_health_trip_threshold,
        "device_recovery_max_wait_s": device_recovery_max_wait_s,
        "device_health_signals": support.pacer.device_health_signals,
        "device_health_trips": support.pacer.device_health_trips,
        "device_health_recoveries": support.pacer.device_health_recoveries,
        "cdp_slowdown_multiplier": support.pacer.slowdown_multiplier,
        # Proactive burst bound -- see module docstring's "Device health"
        # section, priority 2. `cdp_burst_cooldowns` is how many times this
        # run actually paused for a burst cooldown; 0 on any archive smaller
        # than `cdp_burst_size`.
        "cdp_burst_size": cdp_burst_size,
        "cdp_burst_cooldown_s": cdp_burst_cooldown_s,
        "cdp_burst_cooldowns": support.pacer.burst_cooldowns,
        # Chunking -- see module docstring's "Chunking" section.
        # `chunks_total`/`chunks_completed` let a caller see, after the fact,
        # whether this run engaged chunking at all (`chunks_total <= 1` means
        # it did not -- either disabled, or the run was already small enough
        # to be one chunk) and whether it ran to completion
        # (`chunks_completed == chunks_total`) or stopped early because the
        # device never confirmed "live" again (`chunk_settle_gave_up: true`).
        # `chunk_settle_waits` counts every inter-chunk settle attempted;
        # `chunk_settle_wait_total_s` is the cumulative wall-clock cost of all
        # of them (fixed cooldowns plus any tier-confirmation backoff).
        "cdp_chunk_size": cdp_chunk_size,
        "cdp_chunk_cooldown_s": cdp_chunk_cooldown_s,
        "chunks_total": support.chunks_total,
        "chunks_completed": support.chunks_completed,
        "chunk_settle_waits": support.chunk_settles,
        "chunk_settle_wait_total_s": round(support.chunk_settle_wait_total_s, 3),
        "chunk_settle_gave_up": support.chunk_gave_up,
        "poll_interval_s": poll_interval_s,
        "poll_max_wait_s": poll_max_wait_s,
        "queued_waits": support.queued_waits,
        "queued_wait_total_s": round(support.queued_wait_total_s, 3),
        "queued_timeouts": support.queued_timeouts,
    }

    manifest_path = archive_dir / "manifest.json"
    _write_json(manifest_path, manifest)

    support.emit({"event": "archive_finished", "status": manifest["status"], "archive_dir": str(archive_dir)})

    return {"ok": True, "result": manifest}
