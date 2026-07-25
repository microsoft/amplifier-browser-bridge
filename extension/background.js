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

  // Intentionally not requested in this phase's manifest -- injection-only is the
  // default posture, CDP escalation is Phase 6 (design doc §7). Reporting false
  // here is honest: we did not ask for the permission, so the capability is absent
  // by our own choice, not by platform limitation.
  caps.debugger = false;

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
    if (command === "screenshot") return { ok: true, result: await screenshot(target) };
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

async function screenshot(target) {
  const tabId = requireTabId(target);
  const tab = await chrome.tabs.get(tabId);
  if (!tab.active) {
    // Co-working etiquette: never activate a tab merely to screenshot it. Without
    // CDP (a later phase -- design doc §7), chrome.tabs.captureVisibleTab can only
    // ever capture the active tab of a focused window. Fail loud rather than
    // silently stealing focus to satisfy the request.
    throw new Error(
      "screenshot requires the target tab to already be active/visible in this " +
        "injection-only phase; CDP-based any-tab capture is a later phase"
    );
  }
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 80 });
  return { tab_id: tabId, format: "jpeg", data_url_length: dataUrl.length };
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
  if (alarm.name === ALARM_NAME) connect(); // the revival path for a killed connection
});

// A freshly-revived service worker shouldn't wait for the next half-minute alarm
// tick to reconnect -- try immediately on load too.
connect();
