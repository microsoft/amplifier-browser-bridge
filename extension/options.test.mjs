// Tests for options.js's status-query fail-loud discipline (bug report, 2026-08).
//
// Root cause recap: the status query used to have two silent `return`s (a bare
// `catch`, and `if (!response) return;`) plus a fixed three-poll retry. If every
// attempt took a silent path, the page's optimistic "Saved. Connecting..." /
// "Checking status..." string stood forever -- even though the underlying
// connection could be perfectly healthy. These tests exercise the fixed
// implementation's guarantee: once its retry budget is exhausted, the page
// renders an honest "couldn't determine status" state -- never a stale
// optimistic string.
//
// options.js touches `document`/`chrome` at module scope, so each test provides
// its own fake globals and imports the module fresh via a cache-busting query
// string (Node ES module caching is per-specifier). `__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__`
// suppresses the file's own real (slow) auto-run poll so each test drives
// pollStatusUntilKnown directly with a fast, deterministic schedule.

import { test } from "node:test";
import assert from "node:assert/strict";

function makeElement(initial = {}) {
  return { value: "", textContent: "", className: "", type: "text", addEventListener() {}, ...initial };
}

function installFakeDom() {
  const elements = {
    "hub-url": makeElement(),
    "hub-token": makeElement({ type: "password" }),
    "toggle-token": makeElement(),
    error: makeElement(),
    status: makeElement({ className: "unknown", textContent: "Checking status..." }),
    save: makeElement(),
  };
  globalThis.document = { getElementById: (id) => elements[id] };
  return elements;
}

let importCounter = 0;
async function importOptionsFresh() {
  importCounter += 1;
  const url = new URL(`./options.js?test=${importCounter}`, import.meta.url).href;
  return import(url);
}

test("pollStatusUntilKnown renders the real status once a response arrives", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = {
    storage: { local: { get: async () => ({}), set: async () => {} } },
    runtime: {
      sendMessage: async () => ({
        configured: true,
        connected: true,
        hubUrl: "ws://100.1.2.3:8900/device",
        deviceId: "abc-123",
        legacyConfigDetected: false,
      }),
    },
  };

  const mod = await importOptionsFresh();
  await mod.pollStatusUntilKnown([0]);

  assert.equal(elements.status.className, "ok");
  assert.match(elements.status.textContent, /Connected to ws:\/\/100\.1\.2\.3:8900\/device as device abc-123/);
});

test("pollStatusUntilKnown lands on an honest 'couldn't determine status' state -- never a stale optimistic string -- when every attempt rejects", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  let calls = 0;
  globalThis.chrome = {
    storage: { local: { get: async () => ({}), set: async () => {} } },
    runtime: {
      // Simulates the real-world failure this bug report diagnosed: the
      // background service worker never responds (broken/missing import,
      // or genuinely not running) -- sendMessage rejects every single time.
      sendMessage: async () => {
        calls += 1;
        throw new Error("Could not establish connection. Receiving end does not exist.");
      },
    },
  };

  const mod = await importOptionsFresh();

  // Set the optimistic string exactly like the real Save handler does, then run
  // the retry budget to exhaustion with a fast, deterministic schedule.
  elements.status.className = "unknown";
  elements.status.textContent = "Saved. Connecting...";
  await mod.pollStatusUntilKnown([0, 0, 0]);

  assert.equal(calls, 3, "every attempt in the schedule must actually be tried");
  assert.notEqual(elements.status.textContent, "Saved. Connecting...", "must never remain on the stale optimistic string");
  assert.equal(elements.status.className, "warn");
  assert.match(elements.status.textContent, /couldn't determine connection status/i);
  assert.match(elements.status.textContent, /Could not establish connection/);
});

test("pollStatusUntilKnown lands on the honest state when sendMessage resolves with no response at all", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = {
    storage: { local: { get: async () => ({}), set: async () => {} } },
    runtime: {
      // Chrome resolves (does not reject) sendMessage with undefined when no
      // listener called sendResponse -- the second silent-return path this bug
      // report named explicitly.
      sendMessage: async () => undefined,
    },
  };

  const mod = await importOptionsFresh();
  elements.status.textContent = "Checking status...";
  await mod.pollStatusUntilKnown([0, 0]);

  assert.notEqual(elements.status.textContent, "Checking status...");
  assert.equal(elements.status.className, "warn");
  assert.match(elements.status.textContent, /couldn't determine connection status/i);
  assert.match(elements.status.textContent, /returned no status/);
});

test("queryStatusOnce never throws and always returns an explicit ok/error result", async () => {
  installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = {
    storage: { local: { get: async () => ({}), set: async () => {} } },
    runtime: {
      sendMessage: async () => {
        throw new Error("boom");
      },
    },
  };

  const mod = await importOptionsFresh();
  const result = await mod.queryStatusOnce();
  assert.equal(result.ok, false);
  assert.ok(result.error instanceof Error);
});
