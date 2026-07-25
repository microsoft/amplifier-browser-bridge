// background.js -- MV3 service worker. Owns: identity, capability probing, hub
// connection lifecycle, command dispatch, and co-working etiquette.
//
// This file is intentionally the ONLY place that touches chrome.* browser-level APIs
// (tabs, windows, downloads, alarms). Page-DOM logic lives entirely in injected.js --
// this keeps exactly one shared implementation of DOM traversal instead of copies
// scattered across command handlers.
//
// Carries ZERO site knowledge and ZERO policy (design doc §3.1). Every command is
// executed exactly as the hub asked; denylists/consent gates are a later phase and
// belong in the hub, not here.

import { HUB_URL, HUB_TOKEN } from "./config.js";

const PROTOCOL_VERSION = 1;
const HEARTBEAT_INTERVAL_MS = 15000; // measured to hold a desktop MV3 worker alive
// indefinitely (165 min, zero gaps) when paired with the hub's 20s ping.
const ALARM_NAME = "abb-revive";
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;

let ws = null;
let deviceId = null;
let profileId = null;
let capabilities = null;
let reconnecting = false; // single-flight guard -- see scheduleReconnect()
let reconnectAttempt = 0;
let heartbeatTimer = null;
let heartbeatSeq = 0;

// ---------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------

async function getOrCreateId(key) {
  const stored = await chrome.storage.local.get(key);
  if (stored[key]) return stored[key];
  const id = crypto.randomUUID();
  await chrome.storage.local.set({ [key]: id });
  return id;
}

async function ensureIdentity() {
  deviceId = await getOrCreateId("abb_device_id");
  // profile_id is best-effort -- see addressing.py's module docstring for the full
  // honest accounting of what this field actually distinguishes today (not much,
  // yet, given the APIs an MV3 extension has available).
  profileId = await getOrCreateId("abb_profile_id");
}

// ---------------------------------------------------------------------------
// Behavioral capability probe
// ---------------------------------------------------------------------------
// Every entry below is a REAL invocation in a try/catch, never a `typeof` check.
// Edge Android ships APIs that exist but are silently non-functional, and one
// (chrome.sidePanel.getLayout) is a confirmed browser crash. We never call that one,
// full stop -- it is not in this list and must never be added to it.

async function probeCapabilities() {
  const caps = {};

  // We are already using storage successfully by the time this runs (ensureIdentity
  // just used it), so this is a real behavioral fact, not an assumption.
  caps.storage = true;

  try {
    await chrome.windows.getAll({});
    caps.windows = true;
  } catch {
    caps.windows = false;
  }

  try {
    await chrome.tabGroups.query({});
    caps.tab_groups = true;
  } catch {
    caps.tab_groups = false;
  }

  // Real behavioral probe (Phase 4, design doc §7): chrome.debugger is
  // desktop-only on Edge -- genuinely undefined/throwing on Android even when
  // requested, so a plain try/catch is both correct AND sufficient (no
  // separate `typeof` pre-check needed -- accessing .getTargets on an
  // undefined chrome.debugger throws the same way a real API failure would).
  // getTargets() is a read-only, side-effect-free call -- safe to run on
  // every probe.
  try {
    await chrome.debugger.getTargets();
    caps.debugger = true;
  } catch {
    caps.debugger = false;
  }

  try {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tabs[0]) {
      await chrome.tabs.captureVisibleTab(tabs[0].windowId, { format: "jpeg", quality: 1 });
      caps.capture_visible_tab = true;
    } else {
      // No active tab to probe against at startup -- honestly "false", not a guess.
      caps.capture_visible_tab = false;
    }
  } catch {
    caps.capture_visible_tab = false;
  }

  try {
    await chrome.downloads.search({ limit: 1 });
    caps.downloads = true;
  } catch {
    caps.downloads = false;
  }

  try {
    await chrome.alarms.getAll();
    caps.alarms = true;
  } catch {
    caps.alarms = false;
  }

  try {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tabs[0]) {
      // Non-destructive no-op injection -- proves executeScript actually works
      // end-to-end rather than merely existing on the chrome.* namespace.
      await chrome.scripting.executeScript({ target: { tabId: tabs[0].id }, func: () => true });
      caps.scripting = true;
    } else {
      caps.scripting = false;
    }
  } catch {
    caps.scripting = false;
  }

  return caps;
}

// ---------------------------------------------------------------------------
// Connection lifecycle -- single-flight reconnect with exponential backoff + jitter
// ---------------------------------------------------------------------------
// The probe kit that informed this design fired three `hello`s in five seconds on
// reconnect, because an alarm timer, a backoff retry, and a startup handler all
// raced independently. In production that turns every laptop-lid-open into a
// reconnect storm against the hub. `reconnecting` is the single-flight guard that
// makes that structurally impossible: only one reconnect attempt is ever in flight,
// regardless of how many separate triggers fire.

function scheduleReconnect() {
  if (reconnecting) return;
  reconnecting = true;
  const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** reconnectAttempt);
  const jitter = Math.random() * delay * 0.3;
  reconnectAttempt += 1;
  setTimeout(() => {
    reconnecting = false;
    connect();
  }, delay + jitter);
}

async function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return; // already connected/connecting -- connect() itself is also single-flight
  }
  await ensureIdentity();
  if (!capabilities) capabilities = await probeCapabilities();

  try {
    ws = new WebSocket(HUB_URL);
  } catch {
    scheduleReconnect();
    return;
  }

  ws.addEventListener("open", () => {
    reconnectAttempt = 0;
    sendHello();
    startHeartbeat();
  });

  ws.addEventListener("message", (event) => {
    onMessage(event.data);
  });

  ws.addEventListener("close", () => {
    stopHeartbeat();
    scheduleReconnect();
  });

  ws.addEventListener("error", () => {
    // 'close' always follows 'error' for a WebSocket; reconnection is handled there
    // to avoid double-scheduling.
  });
}

function sendHello() {
  send({
    v: PROTOCOL_VERSION,
    id: crypto.randomUUID(),
    type: "hello",
    device_id: deviceId,
    profile_id: profileId,
    label: platformLabel(),
    platform: navigator.platform || "unknown",
    capabilities,
    protocol_version: PROTOCOL_VERSION,
    token: HUB_TOKEN,
  });
}

// ---------------------------------------------------------------------------
// Capability re-probe (Phase 1 fix, Phase 4 wiring): capture_visible_tab and
// scripting can under-report `false` at `hello` time if no real tab existed
// yet (design doc §2 -- a fresh browser launch can have zero tabs). Re-probe
// whenever a real tab becomes available and, if the result differs from what
// the hub was told, push a `capabilities_update` -- an under-reporting
// capability set is worse than none: agents route around capabilities that
// actually exist (see docs/PROTOCOL.md).
// ---------------------------------------------------------------------------

async function maybeReprobe() {
  if (!capabilities) return;
  const stale = capabilities.capture_visible_tab === false || capabilities.scripting === false;
  if (!stale) return;
  const fresh = await probeCapabilities();
  const changed = JSON.stringify(fresh) !== JSON.stringify(capabilities);
  capabilities = fresh;
  if (changed) {
    send({
      v: PROTOCOL_VERSION,
      id: crypto.randomUUID(),
      type: "capabilities_update",
      device_id: deviceId,
      capabilities,
    });
  }
}

function platformLabel() {
  const ua = navigator.userAgent || "";
  if (/Android/i.test(ua)) return "edge-android";
  if (/Macintosh/i.test(ua)) return "edge-macos";
  if (/Windows/i.test(ua)) return "edge-windows";
  if (/Linux/i.test(ua)) return "edge-linux";
  return "edge-unknown";
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
  }
}

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    heartbeatSeq += 1;
    send({ v: PROTOCOL_VERSION, id: crypto.randomUUID(), type: "heartbeat", device_id: deviceId, seq: heartbeatSeq });
  }, HEARTBEAT_INTERVAL_MS);
}

function stopHeartbeat() {
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

// ---------------------------------------------------------------------------
// Command handling
// ---------------------------------------------------------------------------

async function onMessage(raw) {
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch {
    return;
  }

  if (msg.type === "ping") {
    heartbeatSeq += 1;
    send({ v: PROTOCOL_VERSION, id: crypto.randomUUID(), type: "heartbeat", device_id: deviceId, seq: heartbeatSeq });
    return;
  }

  if (msg.type === "command") {
    const result = await executeCommand(msg.command, msg.target || {}, msg.args || {});
    send({ v: PROTOCOL_VERSION, id: msg.id, type: "result", device_id: deviceId, ...result });
  }
}

const PAGE_WORLD_COMMANDS = new Set([
  "snapshot",
  "read",
  "click",
  "type",
  "key",
  "scroll",
  "back",
  "forward",
  "wait_for",
  "wait_text",
]);

async function executeCommand(command, target, args) {
  try {
    if (command === "tabs") return { ok: true, result: await listTabs(target) };
    if (command === "tab_open") return { ok: true, result: await tabOpen(args) };
    if (command === "tab_close") return { ok: true, result: await tabClose(target) };
    if (command === "tab_activate") return { ok: true, result: await tabActivate(target) };
    if (command === "navigate") return { ok: true, result: await navigate(target, args) };
    if (command === "screenshot") return { ok: true, result: await screenshot(target, args) };
    // CDP escalation (Phase 4, design doc §7). `attach`/`detach` are explicit,
    // hub-issued commands (either agent-requested or hub auto-escalation --
    // see hub.py's _ensure_cdp_attached / soft_detach_idle_tabs). `_cdp` on
    // click/type/key is set ONLY by the hub, never by a caller directly (see
    // hub.py's send_command, which strips any caller-supplied `_cdp`).
    if (command === "attach") return { ok: true, result: await cdpAttach(requireTabId(target)) };
    if (command === "detach") return { ok: true, result: await cdpDetach(requireTabId(target)) };
    if (command === "click" && args && args._cdp) {
      return { ok: true, result: await cdpClick(requireTabId(target), args.ref) };
    }
    if (command === "type" && args && args._cdp) {
      return { ok: true, result: await cdpType(requireTabId(target), args.ref, args.text) };
    }
    if (command === "key" && args && args._cdp) {
      return { ok: true, result: await cdpKey(requireTabId(target), args.ref, args.key) };
    }
    if (PAGE_WORLD_COMMANDS.has(command)) return { ok: true, result: await runInPage(target, command, args) };
    return { ok: false, error: `unsupported command: ${command}` };
  } catch (err) {
    return { ok: false, error: String((err && err.message) || err) };
  }
}

function requireTabId(target) {
  if (!target || typeof target.tab_id !== "number") {
    throw new Error("command requires an explicit tab_id in target");
  }
  return target.tab_id;
}

async function runInPage(target, command, args) {
  const tabId = requireTabId(target);
  // Idempotent: injected.js guards its own definitions behind `if (!window.__abb)`,
  // so re-injecting on a page that already has it is a safe no-op.
  await chrome.scripting.executeScript({ target: { tabId }, files: ["injected.js"] });
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (cmd, a) => window.__abb.dispatch(cmd, a),
    args: [command, args],
  });
  return result;
}

async function listTabs(target) {
  const query = {};
  if (target && typeof target.window_id === "number") query.windowId = target.window_id;
  const tabs = await chrome.tabs.query(query);
  return tabs.map((t) => ({
    tab_id: t.id,
    window_id: t.windowId,
    url: t.url,
    title: t.title,
    active: t.active,
    index: t.index,
  }));
}

async function tabOpen(args) {
  // Co-working etiquette: a tab is opened ONLY because the command explicitly asked
  // for it, and defaults to background (active: false) unless the caller opts in --
  // never spawn something in front of the human uninvited.
  const tab = await chrome.tabs.create({ url: args.url || "about:blank", active: !!args.active });
  return { tab_id: tab.id, window_id: tab.windowId };
}

async function tabClose(target) {
  const tabId = requireTabId(target);
  await chrome.tabs.remove(tabId);
  return { tab_id: tabId, closed: true };
}

async function tabActivate(target) {
  const tabId = requireTabId(target);
  await chrome.tabs.update(tabId, { active: true });
  return { tab_id: tabId, activated: true };
}

async function navigate(target, args) {
  const tabId = requireTabId(target);
  await chrome.tabs.update(tabId, { url: args.url });
  return { tab_id: tabId, url: args.url };
}

async function screenshot(target, args) {
  const tabId = requireTabId(target);
  if (args && args._cdp) {
    // Hub-authorized escalation (args.capture_hidden -> hub set _cdp=true
    // after attaching -- see hub.py's _ensure_cdp_attached). Page.
    // captureScreenshot works on minimized/occluded windows (design doc
    // §2/§7: measured 41-81ms, does not hang).
    return await cdpScreenshot(tabId);
  }
  const tab = await chrome.tabs.get(tabId);
  if (!tab.active) {
    // Co-working etiquette: never activate a tab merely to screenshot it.
    // Without capture_hidden (which auto-escalates to CDP), chrome.tabs.
    // captureVisibleTab can only ever capture the active tab of a focused
    // window. Fail loud rather than silently stealing focus to satisfy the
    // request.
    throw new Error(
      "screenshot requires the target tab to already be active/visible unless " +
        "args.capture_hidden=true is set (auto-escalates to CDP any-tab capture, " +
        "requires the debugger capability on this device)"
    );
  }
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 80 });
  return { tab_id: tabId, format: "jpeg", data_url_length: dataUrl.length };
}

// ---------------------------------------------------------------------------
// CDP escalation (chrome.debugger) -- opt-in per tab, never speculative.
// ---------------------------------------------------------------------------
// design doc §7: injection-only by default; escalate to CDP per-tab only when
// trusted input or any-tab/hidden capture is genuinely requested by the hub
// (which sets args._cdp only after its own capability check + attach
// bookkeeping -- see hub.py's _ensure_cdp_attached). Soft-detach after idle
// (hub-driven `detach` command) so the banner clears while the human is just
// browsing (design doc §6.3).
//
// chrome.debugger is Edge-desktop-only -- measured genuinely absent on
// Android (design doc §2/§7). `hasDebuggerApi()` is a real presence check
// used only to produce a clear error message before attempting the call;
// every actual capability ANSWER (the `debugger` key in probeCapabilities)
// comes from a real invocation, never from this check alone.

const attachedTabs = new Set(); // tab_ids with a live chrome.debugger session held by THIS extension

function hasDebuggerApi() {
  return typeof chrome.debugger !== "undefined" && typeof chrome.debugger.attach === "function";
}

async function cdpAttach(tabId) {
  if (!hasDebuggerApi()) {
    throw new Error("CDP unavailable on this device: chrome.debugger is not present (e.g. Edge Android)");
  }
  if (attachedTabs.has(tabId)) return { tab_id: tabId, attached: true, already: true };
  await chrome.debugger.attach({ tabId }, "1.3");
  attachedTabs.add(tabId);
  return { tab_id: tabId, attached: true };
}

async function cdpDetach(tabId) {
  if (!attachedTabs.has(tabId)) return { tab_id: tabId, attached: false, already: true };
  try {
    await chrome.debugger.detach({ tabId });
  } finally {
    attachedTabs.delete(tabId);
  }
  return { tab_id: tabId, attached: false };
}

async function cdpClick(tabId, ref) {
  // Resolve the ref's viewport rect FIRST, before attaching CDP. Attaching
  // chrome.debugger to a tab can invalidate/recreate the isolated world's
  // execution context that chrome.scripting content scripts run in --
  // resolving the ref after attach intermittently raced a fresh (empty)
  // world and reported the ref as stale even immediately after a snapshot.
  // Order matters: rect resolution needs no CDP at all, so do it first.
  await chrome.scripting.executeScript({ target: { tabId }, files: ["injected.js"] });
  const [{ result: rect }] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (r) => window.__abb.rectFor(r),
    args: [ref],
  });
  if (!rect) throw new Error(`stale or unknown element ref: ${ref}`);
  await cdpAttach(tabId);
  const x = rect.x + rect.width / 2;
  const y = rect.y + rect.height / 2;
  // mouseMoved first -- some sites gate click handling on a preceding
  // pointer/mouse move (hover states, dropdown reveals).
  await chrome.debugger.sendCommand({ tabId }, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  await chrome.debugger.sendCommand({ tabId }, "Input.dispatchMouseEvent", {
    type: "mousePressed",
    x,
    y,
    button: "left",
    clickCount: 1,
  });
  await chrome.debugger.sendCommand({ tabId }, "Input.dispatchMouseEvent", {
    type: "mouseReleased",
    x,
    y,
    button: "left",
    clickCount: 1,
  });
  return { ref, tag: rect.tag, trusted: true };
}

async function cdpType(tabId, ref, text) {
  await cdpAttach(tabId);
  await chrome.scripting.executeScript({ target: { tabId }, files: ["injected.js"] });
  await chrome.scripting.executeScript({
    target: { tabId },
    func: (r) => window.__abb.focusFor(r),
    args: [ref],
  });
  await chrome.debugger.sendCommand({ tabId }, "Input.insertText", { text });
  return { ref, trusted: true };
}

async function cdpKey(tabId, ref, keyName) {
  await cdpAttach(tabId);
  if (ref) {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["injected.js"] });
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (r) => window.__abb.focusFor(r),
      args: [ref],
    });
  }
  await chrome.debugger.sendCommand({ tabId }, "Input.dispatchKeyEvent", { type: "keyDown", key: keyName });
  await chrome.debugger.sendCommand({ tabId }, "Input.dispatchKeyEvent", { type: "keyUp", key: keyName });
  return { key: keyName, trusted: true };
}

async function cdpScreenshot(tabId) {
  await cdpAttach(tabId);
  const { data } = await chrome.debugger.sendCommand({ tabId }, "Page.captureScreenshot", {
    format: "jpeg",
    quality: 80,
    fromSurface: true,
  });
  return { tab_id: tabId, format: "jpeg", data_url_length: data.length, via: "cdp" };
}

// Fires on ANY debugger detach -- including ones we didn't ask for: the
// human clicking Cancel on the yellow banner, opening DevTools (which
// force-detaches every session on the target), or the target tab crashing/
// closing. Push it to the hub as an unsolicited `event` so the hub's
// CdpRegistry stays truthful even when the detach wasn't hub-initiated
// (design doc §8: "surface real errors; recover by re-attaching where
// sensible").
if (typeof chrome.debugger !== "undefined" && chrome.debugger.onDetach) {
  chrome.debugger.onDetach.addListener((source, reason) => {
    const tabId = source && source.tabId;
    if (typeof tabId !== "number") return;
    attachedTabs.delete(tabId);
    send({
      v: PROTOCOL_VERSION,
      id: crypto.randomUUID(),
      type: "event",
      device_id: deviceId,
      event: "cdp_detached",
      data: { tab_id: tabId, reason: reason || "unknown" },
    });
  });
}

// ---------------------------------------------------------------------------
// Lifecycle wiring -- the service worker can be evicted at any moment (MV3), so
// every entry point independently attempts to (re)connect. `connect()` itself is
// idempotent/single-flight, so overlapping triggers here are harmless.
// ---------------------------------------------------------------------------

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: 0.5 });
  connect();
});

chrome.runtime.onStartup.addListener(() => {
  connect();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) {
    connect(); // the revival path for a killed connection
    maybeReprobe(); // periodic fallback re-probe (Phase 1 fix) -- reuses the
    // existing keepalive alarm rather than adding a second one.
  }
});

// Prompt re-probe as soon as a real tab becomes available, rather than
// waiting up to 30s for the next alarm tick -- the common case the Phase 1
// bug actually hits (a browser launched with zero tabs, then the user opens
// one).
chrome.tabs.onActivated.addListener(() => {
  maybeReprobe();
});
chrome.tabs.onUpdated.addListener((_tabId, info) => {
  if (info.status === "complete") maybeReprobe();
});

// A freshly-revived service worker shouldn't wait for the next half-minute alarm
// tick to reconnect -- try immediately on load too.
connect();
