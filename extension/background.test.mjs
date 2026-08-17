// Tests for the CDP-capture wake-wait fix in background.js.
//
// Root cause (prior investigation, proven live): chrome.debugger.attach() on a
// discarded/asleep tab forces Edge to instantiate a renderer for it, but returns as soon
// as the debugger session is established -- well before that renderer has painted a
// frame or finished navigating. Before this fix, cdpAttach() returned immediately after
// attach(), and screenshot()/cdpCaptureMhtml()/cdpNavHistory() issued their real CDP
// capture (Page.captureScreenshot / Page.captureSnapshot / Page.getNavigationHistory)
// right after -- a race that fails on a cold-waked tab with CDP `-32603 Internal error`
// (screenshot, no paintable surface yet) or `Detached while handling command` (mhtml, the
// target is recreated mid-navigation). cdpAttach() now checks tab state BEFORE attaching
// and, if the tab was asleep, waits for load-complete (waitForTabAwake() -- the SAME
// helper the injection path's ensureAwake() already uses) before returning, so the
// capture is only issued once the tab has actually settled.
//
// These tests exercise the REAL background.js code (not a reimplementation) by
// dynamically importing it with a fake `chrome` global and asserting the ORDER its own
// chrome.tabs.get/chrome.debugger.attach/chrome.debugger.sendCommand calls happen in --
// they fail against the pre-fix code (capture issued immediately after attach, with zero
// tab-state check) and pass against the fix (attach, then a tab-state check confirms
// status==="complete" before Page.captureScreenshot/Page.captureSnapshot ever fires).
//
// background.js has real module-scope side effects intended only for a live browser
// (chrome.runtime.onInstalled.addListener(...), ..., a final connect() call) -- see that
// file's own comment on the `__AMPLIFIER_BROWSER_BRIDGE_BACKGROUND_TEST__` gate this test
// sets before every import. This is the exact same precedent options.test.mjs already
// established for options.js's own __AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ gate (see
// that file's header comment) -- the same pattern, applied to the other chrome.*-heavy
// entry-point file the extension ships.
//
// background.js is imported fresh (cache-busting query string) for every test so each
// gets its own module-scoped state (in particular `attachedTabs`, the Set that makes a
// second attach on an already-attached tab a no-op fast path) -- exactly the
// importOptionsFresh() pattern options.test.mjs uses for the same reason.

import { test } from "node:test";
import assert from "node:assert/strict";

let importCounter = 0;
async function importBackgroundFresh() {
  importCounter += 1;
  const url = new URL(`./background.js?test=${importCounter}`, import.meta.url).href;
  return import(url);
}

// Builds a fake `chrome` sufficient for executeCommand()'s CDP capture paths
// (mhtml/nav_history/screenshot) plus whatever cdpAttach()/markEngaged() touch along the
// way. `tabStates` is consumed in order by successive chrome.tabs.get(tabId) calls -- the
// LAST entry repeats for any call beyond the array's length, mirroring a tab that has
// settled and stays settled. `calls` is an ordered log every stubbed chrome.* call this
// test cares about appends to, so tests can assert ORDERING (not just "was called").
function makeFakeChrome(tabStates) {
  let getCallCount = 0;
  const calls = [];

  const chrome = {
    debugger: {
      attach: async () => {
        calls.push({ op: "debugger.attach" });
      },
      detach: async () => {},
      sendCommand: async (_target, method) => {
        calls.push({ op: "debugger.sendCommand", method });
        if (method === "Page.captureScreenshot") return { data: "ZmFrZS1zY3JlZW5zaG90" };
        if (method === "Page.captureSnapshot") return { data: "<html>fake mhtml</html>" };
        if (method === "Page.getNavigationHistory") return { currentIndex: 0, entries: [] };
        return {};
      },
      onDetach: { addListener() {} },
    },
    tabs: {
      get: async (tabId) => {
        const idx = Math.min(getCallCount, tabStates.length - 1);
        getCallCount += 1;
        const state = tabStates[idx];
        calls.push({ op: "tabs.get", state: { ...state } });
        return { id: tabId, windowId: 1, active: true, ...state };
      },
      update: async () => {},
      query: async () => [],
      onActivated: { addListener() {} },
      onUpdated: { addListener() {} },
    },
    runtime: {
      onInstalled: { addListener() {} },
      onStartup: { addListener() {} },
      onMessage: { addListener() {} },
    },
    action: { onClicked: { addListener() {} } },
    storage: { onChanged: { addListener() {} }, local: { get: async () => ({}), set: async () => {} } },
    alarms: { onAlarm: { addListener() {} } },
  };

  return { chrome, calls, getCallCount: () => getCallCount };
}

test("mhtml capture on a discarded tab: Page.captureSnapshot fires only after the post-wake wait observes status=complete", async () => {
  const { chrome, calls } = makeFakeChrome([
    { status: "unloaded", discarded: true }, // pre-attach check: tab is asleep
    { status: "complete", discarded: false }, // wake-wait's poll: settled
  ]);
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_BACKGROUND_TEST__ = true;
  globalThis.chrome = chrome;

  const mod = await importBackgroundFresh();
  const result = await mod.executeCommand("mhtml", { tab_id: 42 }, {});

  assert.equal(result.ok, true, `expected ok result, got ${JSON.stringify(result)}`);
  assert.equal(result.result.format, "mhtml");

  const ops = calls.map((c) => c.op);
  const attachIdx = ops.indexOf("debugger.attach");
  const captureIdx = ops.indexOf("debugger.sendCommand");
  const secondGetIdx = ops.lastIndexOf("tabs.get");

  assert.ok(attachIdx >= 0, "attach must have been called");
  assert.ok(captureIdx >= 0, "Page.captureSnapshot must have been called");
  // The real regression assertion: TWO tabs.get calls must happen (pre-attach state check,
  // then the wake-wait's poll) -- the pre-fix code made zero such calls and issued the
  // capture immediately after attach.
  assert.equal(
    calls.filter((c) => c.op === "tabs.get").length,
    2,
    "a discarded tab must be checked before attach AND polled again by the wake-wait before capture"
  );
  assert.ok(attachIdx < secondGetIdx, "attach must happen before the post-wake wait poll");
  assert.ok(secondGetIdx < captureIdx, "Page.captureSnapshot must be issued only AFTER the wake-wait observes status=complete");
});

test("screenshot capture on a discarded tab: Page.captureScreenshot fires only after the post-wake wait observes status=complete", async () => {
  const { chrome, calls } = makeFakeChrome([
    { status: "unloaded", discarded: true },
    { status: "complete", discarded: false },
  ]);
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_BACKGROUND_TEST__ = true;
  globalThis.chrome = chrome;

  const mod = await importBackgroundFresh();
  const result = await mod.executeCommand("screenshot", { tab_id: 43 }, { _cdp: true });

  assert.equal(result.ok, true, `expected ok result, got ${JSON.stringify(result)}`);
  assert.equal(result.result.via, "cdp");

  const ops = calls.map((c) => c.op);
  const attachIdx = ops.indexOf("debugger.attach");
  const captureIdx = ops.indexOf("debugger.sendCommand");
  const secondGetIdx = ops.lastIndexOf("tabs.get");

  assert.equal(
    calls.filter((c) => c.op === "tabs.get").length,
    2,
    "a discarded tab must be checked before attach AND polled again by the wake-wait before capture"
  );
  assert.ok(attachIdx < secondGetIdx, "attach must happen before the post-wake wait poll");
  assert.ok(secondGetIdx < captureIdx, "Page.captureScreenshot must be issued only AFTER the wake-wait observes status=complete");
});

test("mhtml capture on an already-awake tab: fast path unchanged -- exactly one state check, no extra wait poll", async () => {
  const { chrome, calls } = makeFakeChrome([{ status: "complete", discarded: false }]);
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_BACKGROUND_TEST__ = true;
  globalThis.chrome = chrome;

  const mod = await importBackgroundFresh();
  const result = await mod.executeCommand("nav_history", { tab_id: 44 }, {});

  assert.equal(result.ok, true, `expected ok result, got ${JSON.stringify(result)}`);
  assert.equal(
    calls.filter((c) => c.op === "tabs.get").length,
    1,
    "an already-awake tab needs exactly one state check -- no extra wait poll, no slowdown"
  );
  const ops = calls.map((c) => c.op);
  assert.deepEqual(ops, ["tabs.get", "debugger.attach", "debugger.sendCommand"]);
});

test("cdpAttach on an already-attached tab: no state check at all (idempotent fast path)", async () => {
  const { chrome, calls } = makeFakeChrome([{ status: "complete", discarded: false }]);
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_BACKGROUND_TEST__ = true;
  globalThis.chrome = chrome;

  const mod = await importBackgroundFresh();
  const first = await mod.cdpAttach(45);
  assert.equal(first.already, undefined);
  calls.length = 0; // only care about what the SECOND call does

  const second = await mod.cdpAttach(45);
  assert.equal(second.already, true);
  assert.equal(calls.length, 0, "an already-attached tab must not re-check state or re-attach");
});

test("waitForTabAwake times out with a specific, actionable error rather than hanging", async () => {
  const { chrome } = makeFakeChrome([{ status: "loading", discarded: false }]); // never reaches complete
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_BACKGROUND_TEST__ = true;
  globalThis.chrome = chrome;

  const mod = await importBackgroundFresh();
  await assert.rejects(() => mod.waitForTabAwake(46, 50), /did not finish reloading within 50ms/);
});
