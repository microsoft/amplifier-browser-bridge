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
  return { value: "", textContent: "", className: "", type: "text", disabled: false, addEventListener() {}, ...initial };
}

function installFakeDom() {
  const elements = {
    "hub-url": makeElement(),
    "hub-token": makeElement({ type: "password" }),
    "toggle-token": makeElement(),
    error: makeElement(),
    status: makeElement({ className: "unknown", textContent: "Checking status..." }),
    save: makeElement(),
    "pair-code": makeElement(),
    "pair-error": makeElement(),
    pair: makeElement({ textContent: "Pair" }),
  };
  globalThis.document = { getElementById: (id) => elements[id] };
  return elements;
}

// Node 20+ defines `globalThis.crypto` as a getter-only accessor (its lazy-loaded
// WebCrypto implementation), so a plain `globalThis.crypto = {...}` assignment
// throws "Cannot set property crypto of #<Object> which has only a getter" --
// it never fails on Node 18 (no built-in global crypto getter there), so this
// only surfaced once CI actually ran on Node 20. `Object.defineProperty` replaces
// the accessor outright (the built-in descriptor is configurable), which works
// on every Node version this project's matrix supports.
function setFakeCrypto(randomUUIDValue) {
  Object.defineProperty(globalThis, "crypto", {
    value: { randomUUID: () => randomUUIDValue },
    configurable: true,
    writable: true,
  });
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

// --- Connection-status detail: distinguishing "unreachable" / "token rejected" /
// "connected" (craft-inspector / human-advocate review) ---

test("renderStatus shows the auth_rejected message verbatim when the hub rejected this device's token", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = { storage: { local: { get: async () => ({}), set: async () => {} } }, runtime: {} };
  const mod = await importOptionsFresh();

  mod.renderStatus({
    configured: true,
    connected: false,
    hubUrl: "ws://100.1.2.3:8900/device",
    lastError: { code: "auth_rejected", message: "The hub rejected this device's token. Re-pair for a fresh one." },
  });

  assert.equal(elements.status.className, "warn");
  assert.match(elements.status.textContent, /rejected this device's token/);
});

test("renderStatus shows the unreachable message verbatim when nothing answered at the configured address", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = { storage: { local: { get: async () => ({}), set: async () => {} } }, runtime: {} };
  const mod = await importOptionsFresh();

  mod.renderStatus({
    configured: true,
    connected: false,
    hubUrl: "ws://100.1.2.3:8900/device",
    lastError: { code: "unreachable", message: "Could not reach the hub -- is it running?" },
  });

  assert.equal(elements.status.className, "warn");
  assert.match(elements.status.textContent, /Could not reach the hub/);
});

test("renderStatus falls back to a calm PENDING message (not warn/red) when lastError is null (attempt still in flight)", async () => {
  // craft-inspector/emotion-reader fix: the window right after Save/Pair, before the
  // hub round trip has had time to succeed or fail, is expected and transient -- not a
  // confirmed problem. Must render `.pending`, never `.warn` (which is reserved for a
  // real, named lastError).
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = { storage: { local: { get: async () => ({}), set: async () => {} } }, runtime: {} };
  const mod = await importOptionsFresh();

  mod.renderStatus({
    configured: true,
    connected: false,
    hubUrl: "ws://100.1.2.3:8900/device",
    lastError: null,
  });

  assert.equal(elements.status.className, "pending");
  assert.match(elements.status.textContent, /connecting/i);
});

test("renderStatus shows a calm PENDING message (not warn/red) for a brand-new, never-configured install", async () => {
  // The bug report this fixes: the pre-pair state -- the FIRST thing a new user sees --
  // rendered with the same red styling as a genuine hub-unreachable/token-rejected
  // error, even though nothing has gone wrong yet.
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = { storage: { local: { get: async () => ({}), set: async () => {} } }, runtime: {} };
  const mod = await importOptionsFresh();

  mod.renderStatus({ configured: false, connected: false, legacyConfigDetected: false });

  assert.equal(elements.status.className, "pending");
  assert.match(elements.status.textContent, /not paired yet/i);
});

test("renderStatus keeps the red WARN style for the legacy-config case -- that IS a real, actionable problem", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = { storage: { local: { get: async () => ({}), set: async () => {} } }, runtime: {} };
  const mod = await importOptionsFresh();

  mod.renderStatus({ configured: false, connected: false, legacyConfigDetected: true });

  assert.equal(elements.status.className, "warn");
  assert.match(elements.status.textContent, /configuration key names changed/i);
});

test("renderStatus still shows the connected message when connected is true, regardless of any stale lastError", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = { storage: { local: { get: async () => ({}), set: async () => {} } }, runtime: {} };
  const mod = await importOptionsFresh();

  mod.renderStatus({
    configured: true,
    connected: true,
    hubUrl: "ws://100.1.2.3:8900/device",
    deviceId: "abc-123",
    lastError: { code: "auth_rejected", message: "stale" },
  });

  assert.equal(elements.status.className, "ok");
  assert.match(elements.status.textContent, /Connected to/);
});

// --- Device identity helper (used by the pairing flow before background.js has
// ever run ensureIdentity -- see options.js's getOrCreateDeviceId docstring) ---

test("getOrCreateDeviceId creates and persists a fresh id when none is stored", async () => {
  installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  const stored = {};
  setFakeCrypto("generated-uuid");
  globalThis.chrome = {
    storage: {
      local: {
        get: async () => ({ ...stored }),
        set: async (values) => Object.assign(stored, values),
      },
    },
    runtime: {},
  };

  const mod = await importOptionsFresh();
  const id = await mod.getOrCreateDeviceId();
  assert.equal(id, "generated-uuid");
  assert.equal(stored.amplifier_browser_bridge_device_id, "generated-uuid");
});

test("getOrCreateDeviceId reuses an existing stored id", async () => {
  installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = {
    storage: {
      local: {
        get: async () => ({ amplifier_browser_bridge_device_id: "existing-id" }),
        set: async () => {
          throw new Error("must not regenerate an existing id");
        },
      },
    },
    runtime: {},
  };

  const mod = await importOptionsFresh();
  const id = await mod.getOrCreateDeviceId();
  assert.equal(id, "existing-id");
});

// --- Pairing flow (the "Pair" button click handler) ---

function installFakeChromeForPairing(stored = {}) {
  setFakeCrypto("device-uuid");
  globalThis.chrome = {
    storage: {
      local: {
        get: async () => ({ ...stored }),
        set: async (values) => Object.assign(stored, values),
      },
    },
    runtime: { sendMessage: async () => ({ configured: false, connected: false }) },
  };
  return stored;
}

// The stub `addEventListener` in makeElement() is a no-op, so exercising the
// actual click handler needs a capturing variant for the "pair" element
// specifically -- installed fresh per test, before the module (which reads
// `document.getElementById("pair")` at import time and calls
// `.addEventListener("click", ...)` on it) is imported.
function installFakeDomCapturingPairClick() {
  const elements = installFakeDom();
  let captured = null;
  elements.pair.addEventListener = (eventName, handler) => {
    if (eventName === "click") captured = handler;
  };
  return { elements, getClickHandler: () => captured };
}

test("Pair button: invalid code shows the parse error and never calls fetch", async () => {
  const { elements, getClickHandler } = installFakeDomCapturingPairClick();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  installFakeChromeForPairing();
  let fetchCalled = false;
  globalThis.fetch = async () => {
    fetchCalled = true;
    throw new Error("must not be called");
  };

  await importOptionsFresh();
  elements["pair-code"].value = "not-a-code";
  await getClickHandler()();

  assert.equal(fetchCalled, false);
  assert.match(elements["pair-error"].textContent, /Not a valid pairing code/);
});

test("Pair button: hub unreachable shows a specific network-failure message", async () => {
  const { elements, getClickHandler } = installFakeDomCapturingPairClick();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  installFakeChromeForPairing();
  globalThis.fetch = async () => {
    throw new Error("network error: connection refused");
  };

  await importOptionsFresh();
  elements["pair-code"].value = "7F3K9-QXTM2@100.124.126.19:8900";
  await getClickHandler()();

  assert.match(elements["pair-error"].textContent, /Could not reach the hub at 100\.124\.126\.19:8900/);
});

test("Pair button: hub rejects the ticket (expired/unknown) shows the hub's own error text", async () => {
  const { elements, getClickHandler } = installFakeDomCapturingPairClick();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  installFakeChromeForPairing();
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    json: async () => ({ ok: false, error: "unknown or already-used pairing code" }),
  });

  await importOptionsFresh();
  elements["pair-code"].value = "7F3K9-QXTM2@100.124.126.19:8900";
  await getClickHandler()();

  assert.match(elements["pair-error"].textContent, /unknown or already-used pairing code/);
});

test("Pair button: success stores the hub URL/token, CONFIG_SOURCE_PAIRED, and clears the code field", async () => {
  const { elements, getClickHandler } = installFakeDomCapturingPairClick();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  const stored = installFakeChromeForPairing();
  let capturedBody = null;
  globalThis.fetch = async (url, init) => {
    capturedBody = JSON.parse(init.body);
    assert.equal(url, "http://100.124.126.19:8900/pair/redeem");
    return { ok: true, status: 200, json: async () => ({ ok: true, token: "a".repeat(32), device_id: "device-uuid" }) };
  };

  await importOptionsFresh();
  elements["pair-code"].value = "7F3K9-QXTM2@100.124.126.19:8900";
  await getClickHandler()();

  assert.equal(capturedBody.ticket, "7F3K9QXTM2");
  assert.equal(capturedBody.device_id, "device-uuid");
  assert.equal(stored.amplifier_browser_bridge_hub_url, "ws://100.124.126.19:8900/device");
  assert.equal(stored.amplifier_browser_bridge_hub_token, "a".repeat(32));
  assert.equal(stored.amplifier_browser_bridge_config_source, "paired");
  assert.equal(stored.amplifier_browser_bridge_setup_completed, true);
  assert.equal(elements["pair-code"].value, "");
  assert.equal(elements["pair-error"].textContent, "");
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
