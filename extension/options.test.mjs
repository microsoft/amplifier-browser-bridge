// Tests for options.js -- the ladder (docs/designs/onboarding-ux.md section 6),
// the redemption-status-driven states, the auto-pair provenance line, Disconnect,
// and the pre-existing fail-loud status-query discipline (bug report, 2026-08).
//
// Root cause recap (fail-loud discipline): the status query used to have two
// silent `return`s (a bare `catch`, and `if (!response) return;`) plus a fixed
// three-poll retry. If every attempt took a silent path, the page's optimistic
// "Saved. Connecting..." / "Checking status..." string stood forever -- even
// though the underlying connection could be perfectly healthy. These tests
// exercise the fixed implementation's guarantee: once its retry budget is
// exhausted, the page renders an honest "couldn't determine status" state --
// never a stale optimistic string. That guarantee is unchanged by the ladder
// rewrite; only where the text lands (step-3-title/step-3-line instead of a
// single #status div) is new.
//
// options.js touches `document`/`chrome` at module scope, so each test provides
// its own fake globals and imports the module fresh via a cache-busting query
// string (Node ES module caching is per-specifier). `__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__`
// suppresses the file's own real (slow) auto-run poll so each test drives
// pollStatusUntilKnown/renderLadder directly with a fast, deterministic schedule.
//
// The ladder's step-3 body is populated by cloning a <template>'s `.content` --
// this fake DOM is intentionally FLAT (one id -> element map, not a real tree),
// so `tpl-*.content.cloneNode()`/`step3BodyEl.appendChild()` are harmless no-ops
// and the elements that would live inside the cloned template (pair-code,
// hub-url, disconnect, ...) are simply present in the same flat map already --
// exactly how the pre-rewrite tests already modeled a static DOM.

import { test } from "node:test";
import assert from "node:assert/strict";

function makeElement(initial = {}) {
  return {
    value: "",
    textContent: "",
    className: "",
    type: "text",
    disabled: false,
    style: {},
    attributes: {},
    addEventListener() {},
    setAttribute(name, value) {
      this.attributes[name] = value;
    },
    getAttribute(name) {
      return this.attributes[name];
    },
    ...initial,
  };
}

function makeTemplate() {
  return { content: { cloneNode: () => ({}) } };
}

function installFakeDom() {
  const elements = {
    "step-2": makeElement(),
    "step-2-title": makeElement(),
    "step-2-auto-line": makeElement({ style: { display: "none" } }),
    "step-3": makeElement(),
    "step-3-marker": makeElement(),
    "step-3-title": makeElement(),
    "step-3-line": makeElement(),
    "step-3-body": makeElement({ replaceChildren() {}, appendChild() {} }),
    "tpl-pairing-controls": makeTemplate(),
    "tpl-ready-payload": makeTemplate(),
    "hub-url": makeElement(),
    "hub-token": makeElement({ type: "password" }),
    "toggle-token": makeElement(),
    error: makeElement(),
    save: makeElement(),
    "pair-code": makeElement(),
    "pair-error": makeElement(),
    pair: makeElement({ textContent: "Pair" }),
    "pair-auto-status": makeElement(),
    "pair-retry": makeElement({ style: { display: "none" } }),
    disconnect: makeElement(),
    "connection-details-body": makeElement(),
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

function defaultFakeChrome(overrides = {}) {
  return {
    storage: { local: { get: async () => ({}), set: async () => {}, ...overrides.storage } },
    runtime: { sendMessage: async () => ({ configured: false, connected: false }), ...overrides.runtime },
    ...overrides,
  };
}

// --- pollStatusUntilKnown / renderLadder: the four-class vocabulary, now on step 3 ---

test("pollStatusUntilKnown renders 'You're ready' + ok marker once connected", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = defaultFakeChrome({
    runtime: {
      sendMessage: async () => ({
        configured: true,
        connected: true,
        hubUrl: "ws://100.1.2.3:8900/device",
        deviceId: "abc-123",
        legacyConfigDetected: false,
      }),
    },
  });

  const mod = await importOptionsFresh();
  await mod.pollStatusUntilKnown([0]);

  assert.equal(elements["step-3"].attributes["data-marker-class"], "ok");
  assert.equal(elements["step-3-title"].textContent, "You're ready");
  assert.match(elements["step-3-line"].textContent, /Your agent can use this browser now/);
  assert.equal(elements["step-2"].attributes["data-state"], "done");
  assert.match(elements["step-2-title"].textContent, /Paired with 100\.1\.2\.3:8900/);
});

test("pollStatusUntilKnown lands on an honest 'couldn't determine status' state -- never a stale optimistic string -- when every attempt rejects", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  let calls = 0;
  globalThis.chrome = defaultFakeChrome({
    runtime: {
      // Simulates the real-world failure this bug report diagnosed: the
      // background service worker never responds (broken/missing import,
      // or genuinely not running) -- sendMessage rejects every single time.
      sendMessage: async () => {
        calls += 1;
        throw new Error("Could not establish connection. Receiving end does not exist.");
      },
    },
  });

  const mod = await importOptionsFresh();

  elements["step-3-title"].textContent = "Saved. Connecting...";
  await mod.pollStatusUntilKnown([0, 0, 0]);

  assert.equal(calls, 3, "every attempt in the schedule must actually be tried");
  assert.notEqual(elements["step-3-title"].textContent, "Saved. Connecting...");
  assert.equal(elements["step-3"].attributes["data-marker-class"], "alert");
  assert.equal(elements["step-3-title"].textContent, "Couldn't determine status");
  assert.match(elements["step-3-line"].textContent, /couldn't determine connection status/i);
  assert.match(elements["step-3-line"].textContent, /Could not establish connection/);
});

test("pollStatusUntilKnown lands on the honest state when sendMessage resolves with no response at all", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = defaultFakeChrome({
    runtime: {
      // Chrome resolves (does not reject) sendMessage with undefined when no
      // listener called sendResponse -- the second silent-return path this bug
      // report named explicitly.
      sendMessage: async () => undefined,
    },
  });

  const mod = await importOptionsFresh();
  await mod.pollStatusUntilKnown([0, 0]);

  assert.equal(elements["step-3"].attributes["data-marker-class"], "alert");
  assert.match(elements["step-3-line"].textContent, /couldn't determine connection status/i);
  assert.match(elements["step-3-line"].textContent, /returned no status/);
});

// --- Transient vs terminal: the sustained watch (bug report, 2026-08) ----------
// A hub restart can put background.js's own reconnect/backoff loop into a state that
// legitimately takes minutes to resolve (one real run measured about six). Before this
// fix, `result.ok` from queryStatusOnce -- "the background script answered" -- was
// mistaken for "the connection settled": pollStatusUntilKnown returned on the FIRST
// real response even when it was "configured, not connected, no error yet" (exactly the
// shape of "still reconnecting"), leaving the page frozen on "Connecting... / Give it a
// moment." for the rest of the six minutes while the device was, underneath, perfectly
// healthy again. These tests exercise the fix: a transient response keeps the watch
// going, a connected/error/unconfigured response still settles immediately (unchanged
// from before), and a connection that never settles at all renders an honest, actionable
// message once the sustained watch's ceiling elapses -- never a repeated "Give it a
// moment."

test("isTerminalStatus: connected, each concrete error, never-configured, and legacy-config are all terminal", async () => {
  installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = defaultFakeChrome();
  const mod = await importOptionsFresh();

  assert.equal(mod.isTerminalStatus({ configured: true, connected: true }), true);
  assert.equal(
    mod.isTerminalStatus({ configured: true, connected: false, lastError: { code: "auth_rejected" } }),
    true
  );
  assert.equal(
    mod.isTerminalStatus({ configured: true, connected: false, lastError: { code: "unreachable" } }),
    true
  );
  assert.equal(mod.isTerminalStatus({ configured: true, connected: false, lastError: { code: "hub_error" } }), true);
  assert.equal(mod.isTerminalStatus({ configured: false, connected: false }), true);
  assert.equal(mod.isTerminalStatus({ configured: false, connected: false, legacyConfigDetected: true }), true);
});

test("isTerminalStatus: configured + not connected + no error yet is the ONE transient shape", async () => {
  installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = defaultFakeChrome();
  const mod = await importOptionsFresh();

  assert.equal(mod.isTerminalStatus({ configured: true, connected: false, lastError: null }), false);
});

test("pollStatusUntilKnown keeps watching a transient status instead of settling on the first real response", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  let calls = 0;
  globalThis.chrome = defaultFakeChrome({
    runtime: {
      sendMessage: async () => {
        calls += 1;
        // First call: still connecting -- exactly the shape that used to end the poll
        // early. Every call after: the reconnect finally succeeded.
        if (calls === 1) {
          return { configured: true, connected: false, hubUrl: "ws://100.1.2.3:8900/device", lastError: null };
        }
        return { configured: true, connected: true, hubUrl: "ws://100.1.2.3:8900/device", deviceId: "d1" };
      },
    },
  });

  const mod = await importOptionsFresh();
  // Burst schedule of one immediate attempt (the transient response above); the
  // sustained watch (interval 0, for a fast test) then picks up the real settle.
  await mod.pollStatusUntilKnown([0], { sustainedIntervalMs: 0, sustainedCeilingMs: 5000 });

  assert.ok(calls >= 2, "must have queried again instead of settling on the first (transient) response");
  assert.equal(elements["step-3-title"].textContent, "You're ready");
  assert.equal(elements["step-3"].attributes["data-marker-class"], "ok");
});

test("pollStatusUntilKnown settles immediately (no sustained watch) once a concrete error is known", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  let calls = 0;
  globalThis.chrome = defaultFakeChrome({
    runtime: {
      sendMessage: async () => {
        calls += 1;
        return {
          configured: true,
          connected: false,
          hubUrl: "ws://100.1.2.3:8900/device",
          lastError: { code: "unreachable", message: "Could not reach the hub -- is it running?" },
        };
      },
    },
  });

  const mod = await importOptionsFresh();
  await mod.pollStatusUntilKnown([0], { sustainedIntervalMs: 0, sustainedCeilingMs: 5000 });

  assert.equal(calls, 1, "a concrete error is terminal -- must not enter the sustained watch at all");
  assert.match(elements["step-3-title"].textContent, /Can't reach 100\.1\.2\.3:8900/);
});

test("pollStatusUntilKnown's sustained watch renders an honest, actionable message once its ceiling elapses -- never repeating 'Give it a moment'", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = defaultFakeChrome({
    runtime: {
      // Never resolves, ever -- a connection that never settles and never produces a
      // concrete named error either (the real reconnect-after-minutes scenario, taken
      // to its unbounded extreme).
      sendMessage: async () => ({
        configured: true,
        connected: false,
        hubUrl: "ws://100.124.126.19:8900/device",
        lastError: null,
      }),
    },
  });

  const mod = await importOptionsFresh();
  // Ceiling of 0ms: the sustained watch's `while` loop never runs a single iteration,
  // exercising "ceiling already elapsed" deterministically and fast.
  await mod.pollStatusUntilKnown([0], { sustainedIntervalMs: 0, sustainedCeilingMs: 0 });

  assert.equal(elements["step-3"].attributes["data-marker-class"], "alert");
  assert.equal(elements["step-3-title"].textContent, "Still not connected");
  assert.notEqual(elements["step-3-title"].textContent, "Connecting\u2026");
  assert.doesNotMatch(elements["step-3-line"].textContent, /give it a moment/i);
  assert.match(elements["step-3-line"].textContent, /100\.124\.126\.19:8900/);
  assert.match(elements["step-3-line"].textContent, /doctor/);
});

test("pollStatusUntilKnown's sustained watch stops cleanly (no further polling, no ceiling message) once the page is hidden", async () => {
  const elements = installFakeDom();
  globalThis.document.visibilityState = "hidden";
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  let calls = 0;
  globalThis.chrome = defaultFakeChrome({
    runtime: {
      sendMessage: async () => {
        calls += 1;
        return { configured: true, connected: false, hubUrl: "ws://100.1.2.3:8900/device", lastError: null };
      },
    },
  });

  const mod = await importOptionsFresh();
  await mod.pollStatusUntilKnown([0], { sustainedIntervalMs: 0, sustainedCeilingMs: 60000 });

  assert.equal(calls, 1, "must not poll again once the page is hidden");
  assert.equal(
    elements["step-3-title"].textContent,
    "Connecting\u2026",
    "still shows the last real (transient) answer, not a ceiling message nobody can see"
  );
});

test("pageIsVisible defaults true when document has no visibilityState (the test harness's flat fake DOM)", async () => {
  installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = defaultFakeChrome();
  const mod = await importOptionsFresh();

  assert.equal(mod.pageIsVisible(), true);
  globalThis.document.visibilityState = "hidden";
  assert.equal(mod.pageIsVisible(), false);
  globalThis.document.visibilityState = "visible";
  assert.equal(mod.pageIsVisible(), true);
});

// --- Connection-status detail: distinguishing "unreachable" / "token rejected" /
// "connected" (craft-inspector / human-advocate review) ---

test("renderLadder shows the auth_rejected message verbatim when the hub rejected this device's token", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = defaultFakeChrome();
  const mod = await importOptionsFresh();

  mod.renderLadder({
    configured: true,
    connected: false,
    hubUrl: "ws://100.1.2.3:8900/device",
    lastError: { code: "auth_rejected", message: "The hub rejected this device's token. Re-pair for a fresh one." },
  });

  assert.equal(elements["step-3"].attributes["data-marker-class"], "alert");
  assert.equal(elements["step-3-title"].textContent, "Hub refused this browser");
  assert.match(elements["step-3-line"].textContent, /rejected this device's token/);
  assert.match(elements["step-3-line"].textContent, /Pair again to get a fresh code/);
});

test("renderLadder shows the unreachable message verbatim when nothing answered at the configured address", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = defaultFakeChrome();
  const mod = await importOptionsFresh();

  mod.renderLadder({
    configured: true,
    connected: false,
    hubUrl: "ws://100.1.2.3:8900/device",
    lastError: { code: "unreachable", message: "Could not reach the hub -- is it running?" },
  });

  assert.equal(elements["step-3"].attributes["data-marker-class"], "alert");
  assert.match(elements["step-3-title"].textContent, /Can't reach 100\.1\.2\.3:8900/);
  assert.match(elements["step-3-line"].textContent, /Could not reach the hub/);
});

test("renderLadder falls back to a calm PENDING state (not alert) when lastError is null (attempt still in flight)", async () => {
  // craft-inspector/emotion-reader fix: the window right after Save/Pair, before the
  // hub round trip has had time to succeed or fail, is expected and transient -- not a
  // confirmed problem. Must render pending, never alert (reserved for a real, named lastError).
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = defaultFakeChrome();
  const mod = await importOptionsFresh();

  mod.renderLadder({
    configured: true,
    connected: false,
    hubUrl: "ws://100.1.2.3:8900/device",
    lastError: null,
  });

  assert.equal(elements["step-3"].attributes["data-marker-class"], "pending");
  assert.match(elements["step-3-title"].textContent, /connecting/i);
});

test("renderLadder shows a calm PENDING state (not alert) for a brand-new, never-configured install", async () => {
  // The bug report this fixes: the pre-pair state -- the FIRST thing a new user sees --
  // rendered with the same red styling as a genuine hub-unreachable/token-rejected
  // error, even though nothing has gone wrong yet.
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = defaultFakeChrome();
  const mod = await importOptionsFresh();

  mod.renderLadder({ configured: false, connected: false, legacyConfigDetected: false });

  assert.equal(elements["step-3"].attributes["data-marker-class"], "pending");
  assert.equal(elements["step-3-title"].textContent, "Not connected yet");
  assert.equal(elements["step-2"].attributes["data-state"], "next");
});

test("renderLadder keeps the ALERT style for the legacy-config case -- that IS a real, actionable problem", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = defaultFakeChrome();
  const mod = await importOptionsFresh();

  mod.renderLadder({ configured: false, connected: false, legacyConfigDetected: true });

  assert.equal(elements["step-3"].attributes["data-marker-class"], "alert");
  assert.equal(elements["step-3-title"].textContent, "Settings need re-pairing");
  assert.match(elements["step-3-line"].textContent, /configuration key names changed/i);
});

test("renderLadder still shows the connected message when connected is true, regardless of any stale lastError", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = defaultFakeChrome();
  const mod = await importOptionsFresh();

  mod.renderLadder({
    configured: true,
    connected: true,
    hubUrl: "ws://100.1.2.3:8900/device",
    deviceId: "abc-123",
    lastError: { code: "auth_rejected", message: "stale" },
  });

  assert.equal(elements["step-3"].attributes["data-marker-class"], "ok");
  assert.equal(elements["step-3-title"].textContent, "You're ready");
});

// --- hostPortFromHubUrl ---------------------------------------------------------

test("hostPortFromHubUrl extracts host:port from a ws:// device URL", async () => {
  installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = defaultFakeChrome();
  const mod = await importOptionsFresh();
  assert.equal(mod.hostPortFromHubUrl("ws://100.124.126.19:8900/device"), "100.124.126.19:8900");
});

// --- Auto-pair provenance line -- renders ONLY when auto-discovery won ---------

test("the auto-pair line is shown when paired_auto is true and this device is configured", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = defaultFakeChrome({
    storage: { local: { get: async () => ({ amplifier_browser_bridge_paired_auto: true }), set: async () => {} } },
  });
  const mod = await importOptionsFresh();

  mod.renderLadder({ configured: true, connected: true, hubUrl: "ws://h:1/device" });
  // renderLadder kicks off renderAutoPairLine asynchronously (a storage.get) --
  // give it a tick to resolve.
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(elements["step-2-auto-line"].style.display, "block");
});

test("the auto-pair line is omitted when the code was pasted by hand (paired_auto false/absent)", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = defaultFakeChrome({
    storage: { local: { get: async () => ({}), set: async () => {} } },
  });
  const mod = await importOptionsFresh();

  mod.renderLadder({ configured: true, connected: true, hubUrl: "ws://h:1/device" });
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(elements["step-2-auto-line"].style.display, "none");
});

test("the auto-pair line is never shown while unpaired, regardless of the stored flag", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  globalThis.chrome = defaultFakeChrome({
    storage: { local: { get: async () => ({ amplifier_browser_bridge_paired_auto: true }), set: async () => {} } },
  });
  const mod = await importOptionsFresh();

  mod.renderLadder({ configured: false, connected: false });
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(elements["step-2-auto-line"].style.display, "none");
});

// --- Disconnect -- one click from the top level, reverts to pre-pair state -----

test("disconnect clears stored config (including paired_auto) and reports the disconnected line", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  const stored = {
    amplifier_browser_bridge_hub_url: "ws://h:1/device",
    amplifier_browser_bridge_hub_token: "tok",
    amplifier_browser_bridge_setup_completed: true,
    amplifier_browser_bridge_paired_auto: true,
  };
  globalThis.chrome = defaultFakeChrome({
    storage: {
      local: {
        get: async () => ({ ...stored }),
        set: async (values) => Object.assign(stored, values),
      },
    },
    runtime: { sendMessage: async () => ({ configured: false, connected: false }) },
  });

  const mod = await importOptionsFresh();
  await mod.disconnect();

  assert.equal(stored.amplifier_browser_bridge_hub_url, "");
  assert.equal(stored.amplifier_browser_bridge_hub_token, "");
  assert.equal(stored.amplifier_browser_bridge_setup_completed, false);
  assert.equal(stored.amplifier_browser_bridge_paired_auto, false);
  assert.match(elements["step-3-line"].textContent, /Disconnected\. Pair again to reconnect\./);
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

// --- Pairing flow (redeemCode / the "Pair" button click handler) ---

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

test("redeemCode with auto=false (manual Pair) never sets paired_auto", async () => {
  installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  const stored = installFakeChromeForPairing();
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ ok: true, token: "a".repeat(32), device_id: "device-uuid" }),
  });

  const mod = await importOptionsFresh();
  const ok = await mod.redeemCode("7F3K9-QXTM2@100.124.126.19:8900", { auto: false });

  assert.equal(ok, true);
  assert.equal(stored.amplifier_browser_bridge_paired_auto, false);
  assert.equal(stored.amplifier_browser_bridge_config_source, "paired");
});

test("redeemCode with auto=true (auto-discovery) sets paired_auto", async () => {
  installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  const stored = installFakeChromeForPairing();
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ ok: true, token: "b".repeat(32), device_id: "device-uuid" }),
  });

  const mod = await importOptionsFresh();
  const ok = await mod.redeemCode("7F3K9-QXTM2@100.124.126.19:8900", { auto: true });

  assert.equal(ok, true);
  assert.equal(stored.amplifier_browser_bridge_paired_auto, true);
});

test("redeemCode: invalid code fails via onError and never calls fetch", async () => {
  installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  installFakeChromeForPairing();
  let fetchCalled = false;
  globalThis.fetch = async () => {
    fetchCalled = true;
    throw new Error("must not be called");
  };
  let error = null;

  const mod = await importOptionsFresh();
  const ok = await mod.redeemCode("not-a-code", { onError: (message) => (error = message) });

  assert.equal(ok, false);
  assert.equal(fetchCalled, false);
  assert.match(error, /Not a valid pairing code/);
});

test("redeemCode: hub unreachable reports a specific network-failure message", async () => {
  installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  installFakeChromeForPairing();
  globalThis.fetch = async () => {
    throw new Error("network error: connection refused");
  };
  let error = null;

  const mod = await importOptionsFresh();
  await mod.redeemCode("7F3K9-QXTM2@100.124.126.19:8900", { onError: (message) => (error = message) });

  assert.match(error, /Could not reach the hub at 100\.124\.126\.19:8900/);
});

test("redeemCode: hub rejects the ticket (expired/unknown) reports the hub's own error text", async () => {
  installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  installFakeChromeForPairing();
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    json: async () => ({ ok: false, error: "unknown or already-used pairing code" }),
  });
  let error = null;

  const mod = await importOptionsFresh();
  await mod.redeemCode("7F3K9-QXTM2@100.124.126.19:8900", { onError: (message) => (error = message) });

  assert.match(error, /unknown or already-used pairing code/);
});

test("redeemCode: success stores the hub URL/token and CONFIG_SOURCE_PAIRED", async () => {
  const stored = installFakeChromeForPairing();
  installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  let capturedBody = null;
  globalThis.fetch = async (url, init) => {
    capturedBody = JSON.parse(init.body);
    assert.equal(url, "http://100.124.126.19:8900/pair/redeem");
    return { ok: true, status: 200, json: async () => ({ ok: true, token: "a".repeat(32), device_id: "device-uuid" }) };
  };

  const mod = await importOptionsFresh();
  const ok = await mod.redeemCode("7F3K9-QXTM2@100.124.126.19:8900", { auto: false });

  assert.equal(ok, true);
  assert.equal(capturedBody.ticket, "7F3K9QXTM2");
  assert.equal(capturedBody.device_id, "device-uuid");
  assert.equal(stored.amplifier_browser_bridge_hub_url, "ws://100.124.126.19:8900/device");
  assert.equal(stored.amplifier_browser_bridge_hub_token, "a".repeat(32));
  assert.equal(stored.amplifier_browser_bridge_config_source, "paired");
  assert.equal(stored.amplifier_browser_bridge_setup_completed, true);
});

// --- Zero-copy-paste auto-discovery (runPairingDiscovery) --------------------

function installFakeChromeForDiscovery({ setupCompleted = false, tabs = [], clipboardText = null, clipboardThrows = null } = {}) {
  setFakeCrypto("device-uuid");
  const stored = { amplifier_browser_bridge_setup_completed: setupCompleted };
  globalThis.chrome = {
    storage: {
      local: {
        get: async () => ({ ...stored }),
        set: async (values) => Object.assign(stored, values),
      },
    },
    runtime: { sendMessage: async () => ({ configured: setupCompleted, connected: false }) },
    tabs: { query: async () => tabs },
  };
  if (clipboardThrows) {
    globalThis.navigator = {
      clipboard: {
        readText: async () => {
          throw clipboardThrows;
        },
      },
    };
  } else if (clipboardText !== null) {
    globalThis.navigator = { clipboard: { readText: async () => clipboardText } };
  } else {
    globalThis.navigator = {};
  }
  return stored;
}

test("runPairingDiscovery auto-redeems from an already-open, origin-matching /setup tab with zero user interaction, and sets paired_auto", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  const stored = installFakeChromeForDiscovery({
    tabs: [{ id: 9, url: "http://100.124.126.19:8900/setup#pair=7F3K9-QXTM2@100.124.126.19:8900&exp=9999999999" }],
  });
  globalThis.fetch = async (url) => {
    assert.equal(url, "http://100.124.126.19:8900/pair/redeem");
    return { ok: true, status: 200, json: async () => ({ ok: true, token: "b".repeat(32) }) };
  };

  const mod = await importOptionsFresh();
  await mod.runPairingDiscovery();

  assert.equal(stored.amplifier_browser_bridge_hub_url, "ws://100.124.126.19:8900/device");
  assert.equal(stored.amplifier_browser_bridge_hub_token, "b".repeat(32));
  assert.equal(stored.amplifier_browser_bridge_config_source, "paired");
  assert.equal(stored.amplifier_browser_bridge_paired_auto, true);
  assert.match(elements["pair-auto-status"].textContent, /paired automatically/i);
  assert.equal(elements["pair-retry"].style.display, "none", "no retry button needed on success");
});

test("runPairingDiscovery REJECTS a hostile tab's code (origin mismatch) and never calls fetch for it", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  installFakeChromeForDiscovery({
    tabs: [{ id: 66, url: "http://evil.example.com/whatever#pair=7F3K9-QXTM2@100.124.126.19:8900" }],
  });
  let fetchCalled = false;
  globalThis.fetch = async () => {
    fetchCalled = true;
    throw new Error("must not be called for a rejected origin-mismatched tab");
  };

  const mod = await importOptionsFresh();
  await mod.runPairingDiscovery();

  assert.equal(fetchCalled, false);
  assert.match(elements["pair-auto-status"].textContent, /no pairing code found nearby/i);
  assert.equal(elements["pair-retry"].style.display, "", "retry button shown when nothing redeemable was found");
});

test("runPairingDiscovery falls back to the clipboard when no tab carries a pairing code", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  const stored = installFakeChromeForDiscovery({
    tabs: [{ id: 1, url: "https://example.com/" }],
    clipboardText: "7F3K9-QXTM2@100.124.126.19:8900",
  });
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ ok: true, token: "c".repeat(32) }) });

  const mod = await importOptionsFresh();
  await mod.runPairingDiscovery();

  assert.equal(stored.amplifier_browser_bridge_hub_token, "c".repeat(32));
  assert.match(elements["pair-auto-status"].textContent, /clipboard/i);
});

test("runPairingDiscovery treats a clipboard read failure (permission/focus/gesture) as 'nothing found', never a crash or user-facing error", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  installFakeChromeForDiscovery({ tabs: [], clipboardThrows: new Error("Document is not focused") });
  globalThis.fetch = async () => {
    throw new Error("must not be called");
  };

  const mod = await importOptionsFresh();
  await mod.runPairingDiscovery();

  assert.match(elements["pair-auto-status"].textContent, /no pairing code found nearby/i);
  assert.equal(elements["pair-retry"].style.display, "");
});

test("runPairingDiscovery never runs auto-redeem at all when this device is already configured", async () => {
  const elements = installFakeDom();
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  installFakeChromeForDiscovery({
    setupCompleted: true,
    tabs: [{ id: 9, url: "http://100.124.126.19:8900/setup#pair=7F3K9-QXTM2@100.124.126.19:8900" }],
  });
  globalThis.fetch = async () => {
    throw new Error("must never redeem automatically once already configured");
  };

  const mod = await importOptionsFresh();
  await mod.runPairingDiscovery();

  assert.match(elements["pair-auto-status"].textContent, /already paired/i);
});

test("Check again button re-runs discovery on click", async () => {
  const elements = installFakeDom();
  let captured = null;
  elements["pair-retry"].addEventListener = (eventName, handler) => {
    if (eventName === "click") captured = handler;
  };
  globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__ = true;
  const stored = installFakeChromeForDiscovery({ tabs: [] }); // nothing found on the automatic pass
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({ ok: true, token: "d".repeat(32) }) });

  const mod = await importOptionsFresh();
  await mod.pollStatusUntilKnown([0]);
  mod.renderLadder({ configured: false, connected: false });
  // Simulate a /setup tab opening AFTER the automatic pass already ran and found nothing.
  globalThis.chrome.tabs.query = async () => [
    { id: 5, url: "http://100.124.126.19:8900/setup#pair=7F3K9-QXTM2@100.124.126.19:8900" },
  ];
  await captured();

  assert.equal(stored.amplifier_browser_bridge_hub_token, "d".repeat(32));
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
