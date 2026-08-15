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

import { validateHubUrl, isConfigured } from "./config_validate.mjs";
import { parseQualifiedRef, qualifyRef, qualifySnapshotResult } from "./frame_refs.mjs";
import { combineRead, combineSnapshot } from "./combine_frames.mjs";
import { DEFAULT_MAX_FETCH_BYTES, checkSizeCap, bytesToBase64 } from "./fetch_utils.mjs";
import { truthy } from "./args_bool.mjs";
import {
  pickCompletedDownload,
  pickInterruptedDownload,
  validateWaitDownloadArgs,
} from "./download_claim.mjs";
import { EffectsCollector, EFFECTS_WINDOW_MS, emptyEffectsReport } from "./effects_collector.mjs";
import { resolveBundledConfigAdoption, CONFIG_SOURCE_BUNDLED, BUNDLED_CONFIG_RESOURCE } from "./bundled_config.mjs";
import { classifyHubErrorMessage, classifyCloseEvent, badgeTitleForErrorCode } from "./connection_error.mjs";

const PROTOCOL_VERSION = 1;
const HEARTBEAT_INTERVAL_MS = 15000; // measured to hold a desktop MV3 worker alive
// indefinitely (165 min, zero gaps) when paired with the hub's 20s ping.
const ALARM_NAME = "amplifier-browser-bridge-revive";
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;

let ws = null;
let deviceId = null;
let profileId = null;
let capabilities = null;
let reconnecting = false; // single-flight guard -- see scheduleReconnect()
let connectInFlight = false; // single-flight guard for connect() itself -- see its own comment
// below for why this exists (adoptBundledConfigIfNeeded() can trigger a *nested* connect() call
// via storage.onChanged the moment it writes a first-run adoption).
let reconnectAttempt = 0;
let heartbeatTimer = null;
let heartbeatSeq = 0;
// D3 (docs/designs/confirmation-gate.md): the effects-collection tier this
// device can actually support, determined once via probeEffectsTier() (a real
// invocation, never a typeof check -- same discipline as probeCapabilities()).
// "none" until probed.
let effectsTier = "none";
// Last classified connection failure reason (connection_error.mjs), or `null` if
// either never attempted yet or the most recent attempt is currently in flight /
// succeeded. Reset at the START of every new connect attempt (connectLocked())
// and set by either an explicit hub `error` frame (onMessage) or, absent that, the
// WebSocket `close` event itself -- see this module's "Connection lifecycle"
// section. Surfaced via amplifier_browser_bridge_get_status so options.js/popup.js
// can render WHICH of "hub unreachable" / "token rejected" / other this is,
// instead of one generic "not connected" sentence (craft-inspector / human-advocate
// findings: an undesigned state guaranteed to fire on every failed connection is
// not an edge case, and an unidentified error is a WCAG 3.3.1 gap).
let lastConnectError = null;

// ---------------------------------------------------------------------------
// Runtime configuration -- hub URL + shared token, read from chrome.storage.local
// (keys: amplifier_browser_bridge_hub_url, amplifier_browser_bridge_hub_token), entered once through options.html/options.js.
//
// This used to be a tracked source file (extension/config.js) with a real-looking
// placeholder credential (HUB_TOKEN = "dev-local-token-change-me") that a user had to
// hand-edit, and that every `git pull` / file-copy update silently overwrote -- see
// SCRATCH.md and the README's "Setup" section for the incident that motivated this.
// chrome.storage.local is keyed to the extension's install identity (stable as long as
// an unpacked install is loaded from the same directory path -- see `amplifier-browser-bridge init`'s staging
// directory), not to any file on disk, so re-copying extension/*.js over an existing
// install can never destroy a configured token/hub-url the way overwriting config.js did.
//
// `configured` gates whether connect() ever attempts a WebSocket at all (see connect()
// below and this module's "fail loud when unconfigured" section) -- this is deliberately
// NOT the same thing as "the hub will accept it"; that's `token_match`, checked by
// `amplifier-browser-bridge doctor`, not something the extension can know in advance of trying.
let hubUrl = null;
let hubToken = null;
let configured = false;
// True when the OLD (pre-rename) storage keys hold a value but the new ones don't --
// i.e. this is a pre-existing install whose config needs re-entering, not a fresh
// unconfigured install. See loadConfig() below and connect()'s "fail loud when
// unconfigured" section -- distinguishing these two cases is the whole point: a
// silent connect loop that looks identical to "never configured" would hide exactly
// the failure mode this project's fail-loud discipline exists to prevent. See
// MIGRATION.md.
let legacyConfigDetected = false;

async function loadConfig() {
  const stored = await chrome.storage.local.get([
    "amplifier_browser_bridge_hub_url",
    "amplifier_browser_bridge_hub_token",
    // Pre-rename key names (this extension used to be named "abb"). Read ONLY to
    // detect their presence for the migration message below -- NEVER used as the
    // actual config; see this file's module docstring on why config.js's old
    // hand-edited-placeholder incident makes any such silent fallback unacceptable.
    "abb_hub_url",
    "abb_hub_token",
  ]);
  const validation = validateHubUrl(stored.amplifier_browser_bridge_hub_url);
  configured = isConfigured({ hubUrl: stored.amplifier_browser_bridge_hub_url });
  legacyConfigDetected = !configured && !!(stored.abb_hub_url || stored.abb_hub_token);
  hubUrl = validation.valid ? validation.normalized : null;
  hubToken = typeof stored.amplifier_browser_bridge_hub_token === "string" ? stored.amplifier_browser_bridge_hub_token : "";
  return { configured, hubUrl, hubToken, error: validation.error, legacyConfigDetected };
}

// ---------------------------------------------------------------------------
// Bundled first-run config (Android zero-config install)
//
// The reachability problem this closes: chrome.runtime.openOptionsPage() (wired to
// both the toolbar click and onInstalled below) does nothing usable on Edge Android,
// and there is no way to type a 32-character extension ID by hand to reach
// chrome-extension://<id>/options.html directly. Without SOME other channel, a fresh
// Android sideload has NO reachable path to enter a hub URL/token at all. See
// bundled_config.mjs's own docstring for the full rationale and scripts/
// package-android.sh for how bundled_config.json gets baked into the packed CRX
// (written only into that script's temporary staging directory -- never into this
// tracked extension/ source tree) at pack time.
//
// Fetching a same-extension resource via chrome.runtime.getURL() + fetch() from the
// service worker's own execution context does NOT require declaring
// web_accessible_resources -- that manifest key only gates a WEB PAGE (a different
// origin) trying to load an extension resource; this file already IS the extension.
// ---------------------------------------------------------------------------

async function fetchBundledConfig() {
  let response;
  try {
    response = await fetch(chrome.runtime.getURL(BUNDLED_CONFIG_RESOURCE));
  } catch {
    return null; // most commonly: the desktop build, which never generates this file at all.
  }
  if (!response || !response.ok) {
    return null; // 404 on any build that didn't bake one in -- expected, not an error.
  }
  try {
    return await response.json();
  } catch (err) {
    // A file that exists but doesn't parse IS a build defect (unlike a plain 404) -- loud,
    // not silent, so a broken packaging run is noticed rather than quietly producing an
    // Android build that's no better off than before this feature existed.
    console.warn("amplifier-browser-bridge: bundled_config.json exists but is not valid JSON -- ignoring it.", err);
    return null;
  }
}

async function adoptBundledConfigIfNeeded() {
  const bundled = await fetchBundledConfig();
  if (!bundled) return; // nothing shipped (or it failed to load) -- resolveBundledConfigAdoption
  // would return null anyway, but skip the storage read entirely when there's nothing to adopt.

  const stored = await chrome.storage.local.get([
    "amplifier_browser_bridge_hub_url",
    "amplifier_browser_bridge_hub_token",
    "abb_hub_url",
    "abb_hub_token",
    "amplifier_browser_bridge_setup_completed",
  ]);
  const decision = resolveBundledConfigAdoption(
    {
      hubUrl: stored.amplifier_browser_bridge_hub_url,
      hubToken: stored.amplifier_browser_bridge_hub_token,
      legacyHubUrl: stored.abb_hub_url,
      legacyHubToken: stored.abb_hub_token,
      setupCompleted: !!stored.amplifier_browser_bridge_setup_completed,
    },
    bundled
  );
  if (!decision) {
    // Distinguish a real build defect (bundle present but structurally invalid) from every
    // other "do nothing" reason, which are all normal/expected and not worth logging.
    if (!validateHubUrl(bundled.hubUrl).valid) {
      console.warn(
        "amplifier-browser-bridge: bundled_config.json was shipped but its hubUrl is invalid " +
          `(${JSON.stringify(bundled.hubUrl)}) -- ignoring it. This indicates a packaging defect; ` +
          "re-run scripts/package-android.sh."
      );
    }
    return;
  }

  await chrome.storage.local.set({
    amplifier_browser_bridge_hub_url: decision.hubUrl,
    amplifier_browser_bridge_hub_token: decision.hubToken,
    amplifier_browser_bridge_config_source: CONFIG_SOURCE_BUNDLED,
    amplifier_browser_bridge_config_bundled_at: decision.generatedAt,
    // Marks this install as having completed setup so a future rebuild (carrying a
    // different, e.g. rotated, token) never re-adopts over it -- see
    // bundled_config.mjs's "one invariant" section.
    amplifier_browser_bridge_setup_completed: true,
  });
  console.info(
    `amplifier-browser-bridge: adopted a bundled first-run configuration (hub ${decision.hubUrl}) -- ` +
      "this was baked into the installed artifact at pack time, not typed by a human. Change it " +
      "any time on the options page; once you Save, this bundled default is never re-applied."
  );
}

// ---------------------------------------------------------------------------
// Status badge -- the extension's only UI surface besides options.html. Exists so
// "unconfigured" and "connection error" are LEGIBLE at a glance instead of a silent
// connect loop the user has no way to diagnose (the specific failure mode a real
// deployment hit: auth silently failed and the only symptom was the extension simply
// never showing up in `amplifier-browser-bridge devices`, with nothing in the UI explaining why).
// ---------------------------------------------------------------------------

const BADGE_STATE = {
  unconfigured: { text: "!", color: "#d9534f", title: "Amplifier Browser Bridge: NOT CONFIGURED -- click the toolbar icon to set the hub URL and token." },
  // Distinct from "unconfigured": the old (pre-rename) storage keys hold a value, but the
  // new ones don't. Telling the user to migrate here IS the fail-loud behavior -- see
  // connect()'s legacyConfigDetected branch and MIGRATION.md.
  legacy_config: { text: "!", color: "#d9534f", title: "Amplifier Browser Bridge: configuration key names changed -- click the toolbar icon to re-enter your hub URL and token (see MIGRATION.md)." },
  connecting: { text: "\u2026", color: "#f0ad4e", title: "Amplifier Browser Bridge: connecting..." },
  connected: { text: "", color: "#5cb85c", title: "Amplifier Browser Bridge: connected." },
  error: { text: "\u00d7", color: "#d9534f", title: "Amplifier Browser Bridge: connection error -- click the toolbar icon for options." },
};

// `overrideTitle`, if given, replaces BADGE_STATE[state]'s generic title with a
// reason-specific one (connection_error.mjs's badgeTitleForErrorCode) -- e.g.
// distinguishing "hub rejected this device's token" from "could not reach the
// hub" instead of one generic "connection error" tooltip for both. Text/color
// stay keyed off `state` (both distinct-error cases still use the same
// red/"\u00d7" visual -- only the accessible-name text differs).
function setBadge(state, overrideTitle) {
  const spec = BADGE_STATE[state] || BADGE_STATE.error;
  chrome.action.setBadgeText({ text: spec.text });
  chrome.action.setBadgeBackgroundColor({ color: spec.color });
  chrome.action.setTitle({ title: overrideTitle || spec.title });
}

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
  deviceId = await getOrCreateId("amplifier_browser_bridge_device_id");
  // profile_id is best-effort -- see addressing.py's module docstring for the full
  // honest accounting of what this field actually distinguishes today (not much,
  // yet, given the APIs an MV3 extension has available).
  profileId = await getOrCreateId("amplifier_browser_bridge_profile_id");
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

  // DELIBERATE EXCEPTION to the "behavioral probes, never typeof" rule.
  //
  // Every other capability here is probed by actually invoking it, because
  // Edge Android ships APIs that are present but silently non-functional
  // (design doc §2). chrome.debugger is the one API in this list handled
  // differently, but NOT because probing it would raise the banner: reading
  // Chromium's own source (chrome/browser/extensions/api/debugger/debugger_api.cc,
  // `ExtensionDevToolsClientHost::Attach()`) shows the "<Extension> started
  // debugging this browser" infobar is created ONLY from `Attach()` -- i.e.
  // only by a real `chrome.debugger.attach()` call. `DebuggerGetTargetsFunction::
  // Run()` (the nominally read-only getTargets()) does not route through
  // Attach() and creates no infobar. An earlier version of this comment
  // claimed otherwise ("calling ANY chrome.debugger method... raises... the
  // infobar") as a field observation; that claim does not match the source
  // and has been corrected here.
  //
  // The real reason presence-detection (not invocation) is still correct here:
  // this probe runs on startup, on every chrome.tabs.onActivated (i.e. every
  // tab switch), on every onUpdated:complete, and on the keepalive alarm --
  // there is no benign, side-effect-free chrome.debugger call to make that
  // often (getTargets() itself is harmless re: the banner, but still opens a
  // debugging surface far more frequently than this project's actual CDP
  // escalation needs, for zero additional signal beyond what `typeof` already
  // gives). chrome.debugger is genuinely absent (undefined) on Edge Android,
  // which is exactly the case presence-detection needs to distinguish. Real
  // functional confirmation happens on first actual attach -- an operation
  // the caller explicitly asked for (design doc §7), where the banner (now
  // correctly understood to come from THAT call, not from probing) is an
  // honest signal rather than a surprise.
  caps.debugger = typeof chrome.debugger?.attach === "function";

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
// Effects collection (D3, docs/designs/confirmation-gate.md) -- "attribution
// first, gating second." For every STATE_CHANGING_COMMANDS dispatch, records
// what the browser actually did in a bounded window after the command's own
// result, and attaches it as an `effects` block. Browser-asserted: the page
// cannot suppress a request it actually made (design doc section 2).
// ---------------------------------------------------------------------------

// Behavioral probe (never `typeof`) for the effects-collection tier this
// device can support. `webrequest` requires the `webRequest` manifest
// permission; falls back to `navigation` (webNavigation -- already granted
// on desktop, absent on Android until manifest.android.json adds it) if that
// throws; `none` if neither is usable.
async function probeEffectsTier() {
  try {
    const noop = () => {};
    chrome.webRequest.onBeforeRequest.addListener(noop, { urls: ["<all_urls>"] });
    chrome.webRequest.onBeforeRequest.removeListener(noop);
    return "webrequest";
  } catch {
    // fall through to the next tier
  }
  try {
    // getAllFrames rejecting with a "no such tab" style message still proves
    // chrome.webNavigation itself exists and is callable -- only a message
    // naming the API as undefined means it's genuinely absent.
    await chrome.webNavigation.getAllFrames({ tabId: -1 });
    return "navigation";
  } catch (err) {
    const message = String((err && err.message) || err || "");
    return /webNavigation/i.test(message) && /undefined|not a function/i.test(message) ? "none" : "navigation";
  }
}

// Runs `fn()` (a state-changing command's own dispatch) while collecting
// browser-asserted effects on `target.tab_id`, holding the collection window
// open EFFECTS_WINDOW_MS past `fn()`'s own completion (design doc section
// 11.5) before finalizing the report. Listener registration is best-effort
// and scoped to the acting tab where the API supports it (webRequest); where
// it does not (chrome.downloads has no tabId on DownloadItem, so downloads
// are collected globally during the window -- an honest, documented
// limitation, not a silent gap -- see design doc section 9.6).
async function withEffectsCollection(target, fn) {
  const tabId = target && typeof target.tab_id === "number" ? target.tab_id : null;
  if (tabId === null || effectsTier === "none") {
    const result = await fn();
    return { ok: true, result, effects: emptyEffectsReport(effectsTier) };
  }

  let pageOrigin = null;
  try {
    const tab = await chrome.tabs.get(tabId);
    if (tab && tab.url) pageOrigin = new URL(tab.url).origin;
  } catch {
    pageOrigin = null;
  }

  const collector = new EffectsCollector();

  const onRequest = (details) => {
    if (details.tabId !== tabId) return;
    let crossOrigin = false;
    if (pageOrigin) {
      try {
        crossOrigin = new URL(details.url).origin !== pageOrigin;
      } catch {
        crossOrigin = false;
      }
    }
    collector.addRequest(details.method, details.url, details.type, crossOrigin);
  };
  const onNavigation = (details) => {
    if (details.tabId !== tabId || details.frameId !== 0) return;
    let originChanged = false;
    if (pageOrigin) {
      try {
        originChanged = new URL(details.url).origin !== pageOrigin;
      } catch {
        originChanged = false;
      }
    }
    collector.addNavigation(details.url, details.transitionType || null, originChanged);
  };
  const onDownloadCreated = (item) => {
    collector.addDownload((item && (item.filename || item.url)) || null);
  };
  const onTabCreated = (tab) => {
    if (tab && tab.openerTabId === tabId) collector.addTabOpened(tab.id);
  };

  let webRequestActive = false;
  if (effectsTier === "webrequest") {
    try {
      chrome.webRequest.onBeforeRequest.addListener(onRequest, { urls: ["<all_urls>"], tabId });
      webRequestActive = true;
    } catch {
      webRequestActive = false;
    }
  }
  try {
    chrome.webNavigation.onCommitted.addListener(onNavigation);
  } catch {
    /* absent on this device -- collector simply records fewer navigations */
  }
  try {
    chrome.downloads.onCreated.addListener(onDownloadCreated);
  } catch {
    /* absent on this device */
  }
  try {
    chrome.tabs.onCreated.addListener(onTabCreated);
  } catch {
    /* absent on this device */
  }

  try {
    const result = await fn();
    await new Promise((resolve) => setTimeout(resolve, EFFECTS_WINDOW_MS));
    return { ok: true, result, effects: collector.report(effectsTier, EFFECTS_WINDOW_MS) };
  } finally {
    if (webRequestActive) {
      try {
        chrome.webRequest.onBeforeRequest.removeListener(onRequest);
      } catch {
        /* already gone */
      }
    }
    try {
      chrome.webNavigation.onCommitted.removeListener(onNavigation);
    } catch {
      /* already gone */
    }
    try {
      chrome.downloads.onCreated.removeListener(onDownloadCreated);
    } catch {
      /* already gone */
    }
    try {
      chrome.tabs.onCreated.removeListener(onTabCreated);
    } catch {
      /* already gone */
    }
  }
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
  if (connectInFlight) {
    // adoptBundledConfigIfNeeded() below can write a first-run adoption to
    // chrome.storage.local, which fires storage.onChanged, whose listener (bottom of this
    // file) calls connect() again -- a nested, concurrent call arriving before `ws` has been
    // assigned by THIS invocation. Without this guard, both calls could race past the
    // `ws && ...` check below (both seeing `ws === null`) and each open its own WebSocket.
    // This makes that nested call a harmless no-op; the in-flight call below still proceeds
    // using the very config the nested call would have re-read anyway.
    return;
  }
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return; // already connected/connecting -- connect() itself is also single-flight
  }

  connectInFlight = true;
  try {
    await connectLocked();
  } finally {
    connectInFlight = false;
  }
}

async function connectLocked() {
  await adoptBundledConfigIfNeeded();
  await loadConfig();
  if (!configured) {
    // Fail loud and legibly, never a silent connect loop against an undefined/invalid
    // URL. The real incident this guards against: auth silently failed and the only
    // symptom was the extension never appearing in `amplifier-browser-bridge devices`, with nothing in the
    // UI explaining why. See this module's "Runtime configuration" section above --
    // storage.onChanged (wired at the bottom of this file) re-triggers connect() the
    // moment the options page saves a valid config, so no manual reload is needed.
    //
    // legacyConfigDetected distinguishes "never configured" from "configured under the
    // OLD (pre-rename) key names" -- these are NOT the same failure and must not share a
    // message. The old keys are never read as config (no silent fallback -- see
    // MIGRATION.md); this is the fail-loud message telling the user exactly what to do.
    if (legacyConfigDetected) {
      console.warn(
        "amplifier-browser-bridge: configuration not found (this extension renamed its " +
          "chrome.storage.local keys in a recent version -- the old abb_hub_url/abb_hub_token " +
          "values are no longer read). Open the options page (click the toolbar icon) and " +
          "re-enter your Hub URL and token. See MIGRATION.md."
      );
      setBadge("legacy_config");
    } else {
      console.warn(
        "amplifier-browser-bridge: not configured (no valid Hub URL in chrome.storage.local). " +
          "Click the extension's toolbar icon to open its options page and set the Hub URL/token."
      );
      setBadge("unconfigured");
    }
    return;
  }

  // A fresh attempt starts with a clean slate -- a stale reason from a prior
  // attempt must never linger past a new one that hasn't resolved yet. See
  // this module's `lastConnectError` docstring.
  lastConnectError = null;
  setBadge("connecting");
  await ensureIdentity();
  if (!capabilities) capabilities = await probeCapabilities();
  effectsTier = await probeEffectsTier();

  try {
    ws = new WebSocket(hubUrl);
  } catch (err) {
    console.error("amplifier-browser-bridge: failed to open WebSocket to", hubUrl, err);
    setBadge("error");
    scheduleReconnect();
    return;
  }

  ws.addEventListener("open", () => {
    reconnectAttempt = 0;
    setBadge("connected");
    sendHello();
    startHeartbeat();
  });

  ws.addEventListener("message", (event) => {
    onMessage(event.data);
  });

  ws.addEventListener("close", (event) => {
    stopHeartbeat();
    // An explicit hub `error` frame (handled in onMessage, below) is the MORE
    // specific signal when one arrived on THIS attempt -- only fall back to
    // classifying the bare close event itself (connection_error.mjs's
    // classifyCloseEvent) when nothing more specific already explains why.
    if (!lastConnectError) {
      lastConnectError = classifyCloseEvent(event);
    }
    setBadge(
      lastConnectError.code === "auth_rejected" ? "auth_rejected" : "error",
      badgeTitleForErrorCode(lastConnectError.code)
    );
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
    token: hubToken,
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

  if (msg.type === "error") {
    // The hub sends this exact frame, then closes, on a bad hello token (see
    // hub.py's `_handle_device_message`) -- classify it NOW, before the `close`
    // event fires, so the close handler's fallback classification never
    // overwrites this more specific one. See this module's `lastConnectError`
    // docstring and connection_error.mjs.
    lastConnectError = classifyHubErrorMessage(msg);
    return;
  }

  if (msg.type === "command") {
    const result = await executeCommand(msg.command, msg.target || {}, msg.args || {});
    send({ v: PROTOCOL_VERSION, id: msg.id, type: "result", device_id: deviceId, ...result });
  }
}

const PAGE_WORLD_COMMANDS = new Set([
  "snapshot",
  "describe",
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

// Commands that gather results across EVERY frame of the tab (Bug 2, real-profile
// hardening: injection was top-frame-only, so any document rendered in an embedded
// viewer -- e.g. a SharePoint/M365 document body -- was invisible). See
// runMultiFrame()/combineRead()/combineSnapshot() below and docs/PROTOCOL.md's
// "Frames" section for the combine strategy and its rationale.
const MULTI_FRAME_COMMANDS = new Set(["snapshot", "read"]);

// OPT-IN, not default. Measured regression on the user's real browser: making
// multi-frame the default for read/snapshot turned a working ~2s read into a
// hang that did not return inside 190s on ordinary multi-frame pages (a docs
// site, an enterprise SPA). A single-frame page still returned fine, which
// isolated the cause to fanning out across frames.
//
// The cost is not linear in frame count: every frame must be injected,
// instrumented, and DOM-walked before ANY result comes back, and one slow or
// hostile frame stalls the whole command. Real pages routinely carry a dozen
// ad/auth/telemetry iframes that nobody wants read anyway.
//
// So: top frame by default (fast, predictable, what callers almost always
// mean), and `args.all_frames = true` when the caller actually wants embedded
// content -- e.g. a SharePoint/M365 document body rendered in a viewer frame.
// `args.frame_id` remains the precise escape hatch once a frame is known.
function wantsAllFrames(command, args) {
  if (!MULTI_FRAME_COMMANDS.has(command)) return false;
  return !!(args && truthy(args.all_frames));
}

// Commands that take an element `ref` and must route to the EXACT frame that ref
// was produced in (see frame_refs.js) rather than guessing frame 0. `key` is only
// routed this way when it names a ref -- a ref-less key press (to
// document.activeElement) has no frame to resolve and falls through to the
// top-frame-only default path, same as scroll/back/forward/wait_for/wait_text
// (a documented, narrower limitation than the multi-frame read/snapshot -- see
// docs/PROTOCOL.md's "Frames" section).
const FRAME_ROUTED_REF_COMMANDS = new Set(["click", "type", "key"]);

// D3 (docs/designs/confirmation-gate.md section 11.4): the commands effects
// collection applies to -- mirrors effects.py's STATE_CHANGING_COMMANDS. Only
// the untrusted (non-CDP) dispatch path is wrapped in this phase; the
// CDP-backed trusted-input path (cdpClick/cdpType/cdpKey) does not yet
// collect effects -- a documented scope limit, not a silent gap.
const STATE_CHANGING_COMMANDS = new Set(["click", "type", "key", "navigate"]);

async function executeCommand(command, target, args) {
  try {
    if (command === "tabs") return { ok: true, result: await listTabs(target) };
    if (command === "tab_open") return { ok: true, result: await tabOpen(args) };
    if (command === "tab_close") return { ok: true, result: await tabClose(target) };
    if (command === "tab_activate") return { ok: true, result: await tabActivate(target) };
    if (STATE_CHANGING_COMMANDS.has(command) && !(args && args._cdp)) {
      return await withEffectsCollection(target, async () => {
        if (command === "navigate") return await navigate(target, args);
        return await runInPage(target, command, args);
      });
    }
    if (command === "navigate") return { ok: true, result: await navigate(target, args) };
    if (command === "screenshot") return { ok: true, result: await screenshot(target, args) };
    // Self-service extension reload (Phase 5) -- see reloadExtension()'s own
    // comment for why the ack is sent before chrome.runtime.reload() actually
    // terminates the service worker.
    if (command === "reload") return { ok: true, result: await reloadExtension() };
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
    // Content-extraction mechanisms (see docs/designs/browser-bridge.md's
    // "Mechanism, not policy" section) -- each a distinct strategy the CALLER
    // picks; none of these substitute for another or escalate automatically.
    if (command === "fetch_bytes") return { ok: true, result: await fetchBytes(args) };
    if (command === "grab_image") return { ok: true, result: await grabImage(requireTabId(target), args) };
    if (command === "downloads_list") return { ok: true, result: await downloadsList(args) };
    if (command === "download") return { ok: true, result: await triggerDownload(args) };
    if (command === "wait_download") return { ok: true, result: await waitDownload(args) };
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

// Bug 1 fix, part 2 (real-profile hardening, discovered proving Bug 1 live):
// chrome.scripting.executeScript does NOT propagate an exception thrown
// inside the injected function back to this side as a thrown/rejected error
// -- it silently resolves that frame's InjectionResult with `result:
// undefined` instead. Measured live: BEFORE this fix, clicking a stale ref
// (or a flat-out bogus one) produced `{ok: true, result: null}` -- the exact
// silent-success failure mode this bug report is about -- because
// injected.js's resolveRef() threw, but nothing on this side ever saw it.
//
// The fix: injected.js's dispatch()/rectFor()/focusFor() catch their own
// errors and return an explicit `{__amplifierBrowserBridgeError: message}` sentinel instead of
// letting the promise reject (see injected.js's rectFor() comment).
// unwrapAmplifierBrowserBridgeResult() is the single choke point that turns that sentinel back
// into a real thrown Error on THIS side, so executeCommand()'s existing
// try/catch reports it as `{ok: false, error: <the real message>}` --
// preserving the specific, actionable cause (stale generation, disconnected,
// identity mismatch, unknown ref) rather than a generic fallback.
function unwrapAmplifierBrowserBridgeResult(result) {
  if (result && typeof result === "object" && typeof result.__amplifierBrowserBridgeError === "string") {
    throw new Error(result.__amplifierBrowserBridgeError);
  }
  return result;
}

// Bug 2 fix: qualifySnapshotResult() is imported from frame_refs.mjs (see that
// module for the full rationale) -- kept there, not here, so it's covered by
// frame_refs.test.mjs's plain `node --test` coverage instead of being
// untestable inline background.js logic.

async function runInPage(target, command, args) {
  const tabId = requireTabId(target);
  let tab = await ensureAwake(tabId, args);
  // Bug 3: `args.activate` (same tolerant truthy() coercion as wake/all_frames)
  // -- explicit, opt-in tab activation before a DOM-injecting command runs.
  // Real-world finding: a heavy enterprise SPA timed out on `snapshot` at 170s
  // while backgrounded, and completed in ~2s once foregrounded -- DOM
  // injection/traversal is viable-when-foreground, dead-when-background on a
  // heavy hydrated page. NEVER automatic (co-working etiquette, design doc
  // §6.3 -- this steals the human's focus exactly like `tab_activate`), and
  // the result reports it happened, same precedent as `wake`/`woke`. Only
  // acts (and only reports `activated: true`) when the tab wasn't already
  // active -- activating an already-active tab steals nothing and reports
  // nothing, since no engagement actually occurred.
  let activated = false;
  if (wantsActivate(args) && !tab.active) {
    await chrome.tabs.update(tabId, { active: true });
    activated = true;
    tab = { ...tab, active: true };
  }
  // Co-working etiquette (design doc §6.3/§4): a tab the agent is actively
  // working with should not be discarded out from under it mid-session --
  // applied here (a real, deliberate per-tab engagement), never blanket-
  // applied to every tab a `tabs` listing happens to enumerate.
  markEngaged(tabId);
  // Idempotent: injected.js guards its own definitions behind `if (!window.__amplifierBrowserBridge)`,
  // so re-injecting on a page (or frame) that already has it is a safe no-op.
  //
  // Performance finding (live, against a heavy real-world SPA): unconditionally
  // injecting with `allFrames: true` here -- regardless of which branch below
  // actually dispatches -- was measured to make even a TOP-FRAME-ONLY command
  // meaningfully slower/less reliable on a page with many frames, since Chrome
  // has to walk and inject into every frame before the real command even runs.
  // Scope the file injection to match what the dispatch step below will
  // actually use: only `MULTI_FRAME_COMMANDS` need every frame instrumented
  // up front.
  if (wantsAllFrames(command, args)) {
    await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, files: ["injected.js"] });
  } else if (FRAME_ROUTED_REF_COMMANDS.has(command) && args && typeof args.ref === "string") {
    const { frameId } = parseQualifiedRef(args.ref);
    await chrome.scripting.executeScript({ target: { tabId, frameIds: [frameId] }, files: ["injected.js"] });
  } else {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["injected.js"] });
  }

  let result;
  if (wantsAllFrames(command, args)) {
    result = await runMultiFrame(tabId, command, args);
  } else if (FRAME_ROUTED_REF_COMMANDS.has(command) && args && typeof args.ref === "string") {
    result = await runInFrame(tabId, command, args);
  } else if (args && args.frame_id !== undefined && args.frame_id !== null && `${args.frame_id}` !== "") {
    // Explicit single-frame targeting. This branch was MISSING: `args.frame_id`
    // was documented as the precise escape hatch for "I know which frame holds
    // the content, read that one" -- but for a ref-less command like `read` it
    // matched neither of the branches above and silently fell through to the
    // top-frame default. Every frame_id therefore returned frame 0's text,
    // which looks like success and is worse than an error: a caller drilling
    // into a specific embedded frame got the outer page chrome back and had no
    // way to tell.
    const frameId = Number(args.frame_id);
    if (!Number.isInteger(frameId) || frameId < 0) {
      throw new Error(`invalid frame_id ${JSON.stringify(args.frame_id)}: expected a non-negative integer frame id`);
    }
    await chrome.scripting.executeScript({ target: { tabId, frameIds: [frameId] }, files: ["injected.js"] });
    const results = await chrome.scripting.executeScript({
      target: { tabId, frameIds: [frameId] },
      func: (cmd, a) => window.__amplifierBrowserBridge.dispatch(cmd, a),
      args: [command, args],
    });
    const single = results[0];
    result = single && single.result;
    if (result && typeof result === "object") {
      if (typeof result.ref === "string") result = { ...result, ref: qualifyRef(single.frameId, result.ref) };
      // Bug 2: `snapshot`'s `nodes[].ref` values are bare per-frame refs
      // (injected.js has no notion of frameId) -- qualify each one the same
      // way the top-level `ref` above is qualified, so a caller can copy a
      // ref straight out of this result into click/type/key with no
      // hand-editing. See qualifySnapshotResult()'s own comment.
      result = qualifySnapshotResult({ ...result, frame_id: single.frameId }, single.frameId);
    }
  } else {
    // Default: top frame (frameId 0) only -- scroll/back/forward/wait_for/
    // wait_text, and a ref-less `key` (no frame to resolve for
    // document.activeElement). A documented, narrower limitation than the
    // multi-frame read/snapshot path -- see docs/PROTOCOL.md's "Frames" section.
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: (cmd, a) => window.__amplifierBrowserBridge.dispatch(cmd, a),
      args: [command, args],
    });
    const singleResult = results[0];
    result = singleResult && singleResult.result;
    // `wait_for` returns a bare (frame-local) ref -- qualify it with the
    // frame it actually ran in so it composes correctly with a later
    // click/type/key, which require a frame-qualified ref (frame_refs.js).
    if (result && typeof result === "object" && typeof result.ref === "string") {
      result = { ...result, ref: qualifyRef(singleResult.frameId, result.ref) };
    }
    // Bug 2: this is the COMMON case for `snapshot` (no all_frames, no
    // explicit frame_id) -- injected.js's plain snapshot() returns bare
    // `nodes[].ref` values ("e29"); qualify each one with frameId 0 (the
    // only frame this branch ever targets) so the result composes directly
    // into click/type/key, matching combineSnapshot's per-node shape.
    result = qualifySnapshotResult(result, singleResult && singleResult.frameId);
  }

  return attachEngagementInfo(result, tab, activated);
}

// Fail-loud-adjacent: a command only succeeded because we reloaded the tab
// first (in-page state -- unsaved form data, scroll position, ephemeral JS
// state -- was destroyed), or because we activated it (Bug 3: stole the
// human's focus). The caller asked for both via explicit opt-in args, but the
// result must still say so plainly (design doc §6.3: never mutate the
// human's session -- or its focus -- as a hidden side effect). Single choke
// point so every runInPage() branch (top-frame, frame-routed, multi-frame)
// reports this identically instead of separate copies of the same tagging logic.
function attachEngagementInfo(result, tab, activated) {
  let out = result;
  if (tab.__amplifierBrowserBridgeWoke && out && typeof out === "object") {
    out = { ...out, woke: true, wake_reason: "tab was discarded; reloaded to satisfy wake=true" };
  }
  if (activated && out && typeof out === "object") {
    out = { ...out, activated: true };
  }
  return out;
}

// Route a ref-bearing command (click/type/key-with-ref) to the EXACT frame
// that produced the ref -- see frame_refs.js's module docstring for why a
// bare "e12" can't be trusted to mean the same element in every frame.
// `frameIds: [frameId]` (as opposed to `allFrames: true`) targets exactly one
// frame, so this never touches (or re-numbers refs in) any other frame.
async function runInFrame(tabId, command, args) {
  const { frameId, ref: bareRef } = parseQualifiedRef(args.ref);
  const bareArgs = { ...args, ref: bareRef };
  const results = await chrome.scripting.executeScript({
    target: { tabId, frameIds: [frameId] },
    func: (cmd, a) => window.__amplifierBrowserBridge.dispatch(cmd, a),
    args: [command, bareArgs],
  });
  if (results.length === 0) {
    // The frame this ref was qualified with no longer exists in the tab --
    // navigated away, removed, or the tab itself was reloaded. Refs are only
    // ever valid within the page load that produced them (same rule as a
    // plain stale/unknown-ref error from injected.js's resolveRef) -- fail
    // loud with the frame id named, rather than a bare "stale ref".
    throw new Error(
      `frame ${frameId} is no longer present in tab ${tabId} (navigated away, reloaded, or removed) -- ` +
        "refs are only valid within the page load that produced them; take a fresh snapshot"
    );
  }
  // Bug 1 fix, part 2: chrome.scripting.executeScript does NOT propagate an
  // exception thrown inside the injected function (e.g. resolveRef's stale/
  // unknown/disconnected/identity-mismatch errors) back as a thrown error --
  // it silently resolves with `result: undefined` instead. Measured live:
  // clicking a stale OR a bogus ref both produced `{ok: true, result: null}`
  // before this fix. unwrapAmplifierBrowserBridgeResult() converts injected.js's `{__amplifierBrowserBridgeError}`
  // sentinel (see its module comment) back into a real thrown Error here, so
  // executeCommand()'s existing try/catch reports it as `{ok: false, error}`.
  const result = unwrapAmplifierBrowserBridgeResult(results[0].result);
  if (result && typeof result === "object" && typeof result.ref === "string") {
    // injected.js's click()/type() echo back the BARE ref it resolved
    // (e.g. "e12") -- report the qualified ref the caller actually used
    // (e.g. "f7.e12") instead, so the result is addressable the same way
    // the input was.
    return { ...result, ref: args.ref };
  }
  return result;
}

// Gather a page-world command's result from EVERY frame chrome.scripting can
// reach (Bug 2, real-profile hardening -- see docs/PROTOCOL.md's "Frames"
// section for the full rationale). Frames chrome.scripting could not inject
// into at all (sandboxed without allow-scripts, opaque-origin data:/about:blank
// frames, or removed mid-call) are simply absent from `results` -- Chrome does
// not report *why* a frame is missing, so this cannot always name the reason,
// but it never pretends a declared child frame doesn't exist: see
// `unconfirmedFrames` below, cross-referenced against each successfully-injected
// frame's own `child_frames` (injected.js's `listChildFrames()`).
//
// Real-world finding (live, against an embedded Word Online editor) that
// retired the prior "richest frame" design: char-count ranking is a proxy, not
// a guarantee -- an auth/bootstrap iframe's inlined JS config blob legitimately
// out-counted the actual rendered document text in a neighboring frame, and
// would have been silently returned as THE result. That was a policy decision
// (which frame's content the caller wants) baked into this mechanism layer --
// see combine_frames.mjs's module docstring and docs/designs/browser-bridge.md's
// "Mechanism, not policy" section for the full account. `combineRead` now
// returns every frame's content uniformly; the caller decides which matters.
// An optional `args.frame_id` (an integer the caller read off a prior
// `read`/`snapshot` result's `frames` entry) remains the precise escape hatch
// for going straight to one known frame without re-fetching every frame.
async function runMultiFrame(tabId, command, args) {
  // Accept a numeric string too (e.g. `amplifier-browser-bridge cmd ... --arg frame_id=762`, or any
  // caller that only has string-typed args to work with) -- coerced once,
  // here, rather than requiring every caller to send a real JS number.
  const requestedFrameId =
    typeof args.frame_id === "number"
      ? args.frame_id
      : typeof args.frame_id === "string" && args.frame_id !== "" && Number.isInteger(Number(args.frame_id))
        ? Number(args.frame_id)
        : undefined;
  if (requestedFrameId !== undefined) {
    const targeted = await chrome.scripting.executeScript({
      target: { tabId, frameIds: [requestedFrameId] },
      func: (cmd, a) => window.__amplifierBrowserBridge.dispatch(cmd, a),
      args: [command, args],
    });
    if (targeted.length === 0 || !targeted[0].result) {
      throw new Error(
        `${command}: frame ${requestedFrameId} produced no result for tab ${tabId} (no longer present, ` +
          "or chrome.scripting cannot inject into it)"
      );
    }
    return { ...targeted[0].result, frame_id: requestedFrameId };
  }

  const results = await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    func: (cmd, a) => window.__amplifierBrowserBridge.dispatch(cmd, a),
    args: [command, args],
  });
  const frames = results
    .filter((r) => r && r.result && typeof r.result === "object")
    .map((r) => ({ frameId: r.frameId, ...r.result }));

  if (frames.length === 0) {
    throw new Error(`${command}: no frame produced a result for tab ${tabId} (page may not be ready yet)`);
  }

  const declaredChildSrcs = new Set();
  for (const f of frames) {
    for (const child of f.child_frames || []) {
      if (child.src) declaredChildSrcs.add(child.src);
    }
  }
  const confirmedUrls = new Set(frames.map((f) => f.url));
  const unconfirmedFrames = [...declaredChildSrcs].filter((src) => !confirmedUrls.has(src));

  return command === "read" ? combineRead(frames, unconfirmedFrames) : combineSnapshot(frames, unconfirmedFrames);
}

// combineRead/combineSnapshot now live in combine_frames.mjs (pure, unit-tested
// with node --test) -- see this file's imports and combine_frames.mjs's module
// docstring for the "mechanism, not policy" rationale behind the current
// combineRead shape.

// ---------------------------------------------------------------------------
// Discarded-tab handling (Bug 1, real-profile hardening)
// ---------------------------------------------------------------------------
// With hundreds of tabs open, Edge's own memory-pressure management discards
// (unloads) most background tabs' renderers to reclaim RAM -- chrome.tabs.Tab
// exposes this as `discarded`/`status`. A discarded tab has no live renderer
// for chrome.scripting.executeScript to inject into; Edge reports this as
// "Cannot access contents of the page. Extension manifest must request
// permission..." which is genuinely misleading -- <all_urls> IS granted, the
// real cause is "there is no page here right now." We check `discarded`
// ourselves and never let that misleading error reach the caller unexplained.
//
// Waking a discarded tab means reloading it, which destroys in-page state
// (unsaved form data, scroll position, ephemeral JS state) -- co-working
// etiquette (design doc §6.3) requires this be an explicit, opt-in action
// (args.wake truthy), never an automatic/hidden side effect of a read.

function discardedTabError(tabId) {
  return new Error(
    `tab ${tabId} is discarded: Edge unloaded its renderer to reclaim memory (this is the real cause -- ` +
      "not a permissions problem, despite what the underlying chrome.scripting error would otherwise say). " +
      "Waking it requires reloading the tab, which destroys in-page state (unsaved form data, scroll " +
      "position, ephemeral JS state) -- co-working etiquette requires this be explicit, never automatic. " +
      "Pass args.wake=true to reload the tab and retry."
  );
}

function wantsWake(args) {
  return !!(args && truthy(args.wake));
}

// Bug 3: `args.activate` -- same tolerant truthy() coercion as wake/all_frames.
// Checked by runInPage() before any DOM injection happens; see that function's
// own comment for the full rationale and the co-working-etiquette constraints.
function wantsActivate(args) {
  return !!(args && truthy(args.activate));
}

// MEASURED on real Edge 150 (macOS), profile with 531 open tabs:
//
//   tab_id 1565892466  discarded=false  status="complete"  -> executeScript OK
//   tab_id 1565892223  discarded=false  status="unloaded"  -> executeScript FAILS
//   tab_id 1565892547  discarded=false  status="unloaded"  -> executeScript FAILS
//
// Chrome documents an unloaded background tab as `discarded: true`. Edge's
// "sleeping tabs" feature does NOT set that flag -- it leaves `discarded`
// false and reports `status: "unloaded"` instead. Checking only `discarded`
// therefore misses every sleeping tab on Edge, which in a real profile is
// nearly all of them.
//
// Check BOTH: `discarded` for Chrome-style discard and standards-compliance,
// `status === "unloaded"` for Edge's sleeping tabs. Either means "no live
// renderer, executeScript will fail."
function isAsleep(tab) {
  return !!tab && (tab.discarded === true || tab.status === "unloaded");
}

// Returns the (possibly post-reload) chrome.tabs.Tab; throws a specific,
// actionable error for a discarded tab when the caller did not opt into
// waking it. Sets `.__amplifierBrowserBridgeWoke` on the returned object (an extra field on our
// own local copy, never sent back to chrome.tabs) so callers can tell the
// result reflects a fresh reload.
async function ensureAwake(tabId, args) {
  let tab = await chrome.tabs.get(tabId);
  if (!isAsleep(tab)) return tab;
  if (!wantsWake(args)) throw discardedTabError(tabId);
  await chrome.tabs.reload(tabId);
  tab = await waitForTabAwake(tabId, 15000);
  tab.__amplifierBrowserBridgeWoke = true;
  return tab;
}

function waitForTabAwake(tabId, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const check = async () => {
      let tab;
      try {
        tab = await chrome.tabs.get(tabId);
      } catch (err) {
        reject(new Error(`tab ${tabId} disappeared while waking it: ${(err && err.message) || err}`));
        return;
      }
      if (tab.status === "complete" && !isAsleep(tab)) {
        resolve(tab);
        return;
      }
      if (Date.now() > deadline) {
        reject(
          new Error(
            `tab ${tabId} did not finish reloading within ${timeoutMs}ms ` +
              `(status=${tab.status}, discarded=${tab.discarded})`
          )
        );
        return;
      }
      setTimeout(check, 200);
    };
    check();
  });
}

// Best-effort: prevent Edge from re-discarding a tab the agent is actively
// engaged with (design doc §6.3/Bug 1 requirement 4). Never allowed to fail
// the calling command -- this is a courtesy, not a correctness requirement.
async function markEngaged(tabId) {
  try {
    await chrome.tabs.update(tabId, { autoDiscardable: false });
  } catch {
    // Tab may have closed, or the browser may not support this on this tab
    // type (e.g. a chrome:// page) -- harmless either way.
  }
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
    // Bug 1: surfaced so an agent can see which tabs are live vs. discarded
    // (unloaded by Edge to reclaim memory) BEFORE attempting a command
    // against one -- see ensureAwake()/discardedTabError() above.
    // Edge leaves `discarded` false for sleeping tabs and reports
    // status:"unloaded" instead -- report the raw fields AND our combined
    // verdict so an agent can see which tabs actually have a live renderer.
    discarded: !!t.discarded,
    asleep: isAsleep(t),
    status: t.status,
  }));
}

async function tabOpen(args) {
  // Co-working etiquette: a tab is opened ONLY because the command explicitly asked
  // for it, and defaults to background (active: false) unless the caller opts in --
  // never spawn something in front of the human uninvited.
  //
  // truthy() (not `!!args.active`): the old `!!` coercion treated ANY non-empty
  // string as true, so `amplifier-browser-bridge cmd <device> tab_open --arg active=false` (a
  // legitimate request to open a BACKGROUND tab via the escape hatch, which
  // always sends string args) was silently opened as the ACTIVE tab instead --
  // the same bug class as the capture_hidden/trusted CDP-escalation miss.
  const tab = await chrome.tabs.create({ url: args.url || "about:blank", active: truthy(args.active) });
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
  markEngaged(tabId);
  return { tab_id: tabId, activated: true };
}

async function navigate(target, args) {
  const tabId = requireTabId(target);
  await chrome.tabs.update(tabId, { url: args.url });
  markEngaged(tabId);
  return { tab_id: tabId, url: args.url };
}

// ---------------------------------------------------------------------------
// Self-service extension reload (Bug: unpacked-extension iteration friction)
// ---------------------------------------------------------------------------
// Unpacked extensions do not pick up file changes on disk automatically --
// normally this requires a human to click Reload in edge://extensions after
// every code update. Once THIS command itself is loaded (which still
// requires exactly one manual reload -- see docs/PROTOCOL.md), every
// subsequent iteration can self-serve via the hub/CLI/agent surface instead.

async function reloadExtension() {
  // chrome.runtime.reload() terminates the service worker close to
  // immediately -- give the `result` envelope onMessage() is about to send
  // a brief moment to actually flush over the websocket first, so the
  // caller gets a real ack rather than a "device disconnected mid-command"
  // error.
  setTimeout(() => chrome.runtime.reload(), 250);
  return { reloading: true };
}

// Bounds for args.max_pages (multi-page/scrolling capture -- see below). A
// hard ceiling so a runaway "infinite scroll" page can't turn one command
// into an unbounded loop; a caller who genuinely needs more pages raises
// args.max_pages explicitly rather than this silently guessing higher.
const DEFAULT_MAX_PAGES = 10;
const HARD_MAX_PAGES = 50;

function clampMaxPages(rawMaxPages) {
  const parsed = Number(rawMaxPages);
  if (!Number.isFinite(parsed) || parsed <= 0) return DEFAULT_MAX_PAGES;
  return Math.min(Math.floor(parsed), HARD_MAX_PAGES);
}

async function screenshot(target, args) {
  const tabId = requireTabId(target);
  const capturingHidden = !!(args && args._cdp);
  const wantsMultiPage = !!(args && truthy(args.multi_page));

  // Frame-targeted capture (design doc's "Mechanism, not policy" section --
  // a DISTINCT capability from full-tab capture, not a silent narrowing):
  // args.frame_id names which frame the caller cares about. What that BUYS
  // the caller depends on capture_hidden:
  //
  //   - capture_hidden=true: crops the capture to that frame's own on-screen
  //     region (computeFrameClip() below, via CDP's Page.captureScreenshot
  //     `clip` param -- chrome.tabs.captureVisibleTab has no equivalent).
  //   - capture_hidden=false (or omitted): no cropping is possible without
  //     CDP, so the capture stays whole-tab -- BUT for args.multi_page, the
  //     frame_id still selects which frame gets SCROLLED between captures
  //     (a nested document viewer's own internal scroll, not the top page's).
  //     This is a legitimate, honestly-reported use (region stays null in
  //     the result -- never silently cropped when it wasn't actually cropped).
  //
  // A single-shot (non-multi_page) capture with frame_id and no
  // capture_hidden has no scrolling to justify the frame_id's presence
  // either, so it still fails loud rather than silently ignoring it.
  let clip = null;
  let frameId = null;
  if (args && args.frame_id !== undefined && args.frame_id !== null && `${args.frame_id}` !== "") {
    const parsedFrameId = Number(args.frame_id);
    if (!Number.isInteger(parsedFrameId) || parsedFrameId < 0) {
      throw new Error(`invalid frame_id ${JSON.stringify(args.frame_id)}: expected a non-negative integer frame id`);
    }
    frameId = parsedFrameId;
    if (capturingHidden) {
      clip = await computeFrameClip(tabId, frameId);
    } else if (!wantsMultiPage) {
      throw new Error(
        "args.frame_id without args.multi_page requires args.capture_hidden=true -- cropping a single " +
          "capture to a frame's region uses CDP's Page.captureScreenshot 'clip' parameter, which " +
          "chrome.tabs.captureVisibleTab cannot do. With args.multi_page=true, frame_id can be used " +
          "without capture_hidden purely to target which frame gets scrolled between captures -- the " +
          "resulting images are then whole-tab, uncropped (result.region will be null)."
      );
    }
    // else: multi_page + frame_id + no capture_hidden -- frameId is used
    // below purely as the scroll target; clip stays null (uncropped).
  }

  if (!capturingHidden) {
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
  }

  markEngaged(tabId);

  if (wantsMultiPage) {
    return await captureMultiPage(tabId, {
      capturingHidden,
      clip,
      frameId,
      maxPages: clampMaxPages(args && args.max_pages),
      scrollSelector: (args && args.scroll_selector) || null,
      pageDelayMs: typeof (args && args.page_delay_ms) === "number" ? args.page_delay_ms : undefined,
    });
  }

  if (capturingHidden) {
    // Hub-authorized escalation (args.capture_hidden -> hub set _cdp=true
    // after attaching -- see hub.py's _ensure_cdp_attached). Page.
    // captureScreenshot works on minimized/occluded windows (design doc
    // §2/§7: measured 41-81ms, does not hang).
    const shot = await cdpScreenshotRegion(tabId, clip);
    return {
      tab_id: tabId,
      format: "jpeg",
      data_url_length: shot.data.length,
      base64: shot.data,
      via: "cdp",
      frame_id: frameId,
      region: clip,
    };
  }
  const tab = await chrome.tabs.get(tabId);
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 80 });
  const base64 = dataUrl.replace(/^data:image\/jpeg;base64,/, "");
  return { tab_id: tabId, format: "jpeg", data_url_length: dataUrl.length, base64 };
}

// ---------------------------------------------------------------------------
// Frame-targeted capture region (design doc's "Mechanism, not policy" section)
// ---------------------------------------------------------------------------
// A real SharePoint policy page renders its actual document body inside a
// nested Word Online viewer <iframe> -- capturing the whole tab includes nav
// chrome, ads, and unrelated frames the caller doesn't want. computeFrameClip
// resolves a `frame_id` (from a prior read/snapshot's `frames` entries) to an
// on-screen {x, y, width, height} region, which CDP's Page.captureScreenshot
// accepts as its `clip` parameter to crop the capture to just that frame.
//
// chrome.scripting has no API that reports "where is frame N rendered on
// screen" directly -- computing it requires walking the frame's ANCESTOR
// chain (chrome.webNavigation.getAllFrames gives frameId/parentFrameId/url,
// the actual containment hierarchy) and, at each level, finding the
// <iframe>/<frame> ELEMENT in the parent's own DOM whose rendered rect
// corresponds to that child. `window.frameElement` (read from inside the
// child frame itself) was considered and rejected: it returns null for a
// cross-origin child (a security restriction, not a bug) -- exactly our real
// case (a SharePoint top frame embedding an officeapps.live.com viewer).
// Reading the iframe element's getBoundingClientRect() from the PARENT's own
// DOM works regardless of the child's origin, since it's just measuring the
// parent's own layout.
async function computeFrameClip(tabId, frameId) {
  if (frameId === 0) return null; // top frame == full viewport, no clip needed

  let allFrames;
  try {
    allFrames = await chrome.webNavigation.getAllFrames({ tabId });
  } catch (err) {
    throw new Error(
      `cannot compute a capture region for frame_id ${frameId}: chrome.webNavigation.getAllFrames failed ` +
        `(${(err && err.message) || err})`
    );
  }
  if (!allFrames || allFrames.length === 0) {
    throw new Error(`chrome.webNavigation.getAllFrames returned no frames for tab ${tabId}`);
  }

  let x = 0;
  let y = 0;
  let width = null;
  let height = null;
  let currentId = frameId;
  const seen = new Set();
  while (true) {
    if (seen.has(currentId)) {
      throw new Error(`frame hierarchy loop detected while resolving frame_id ${frameId} (cycle at ${currentId})`);
    }
    seen.add(currentId);
    const info = allFrames.find((f) => f.frameId === currentId);
    if (!info) {
      throw new Error(
        `frame_id ${currentId} not found in tab ${tabId}'s current frame tree (the page may have navigated -- ` +
          "take a fresh read/snapshot with args.all_frames=true to get current frame ids)"
      );
    }
    if (info.parentFrameId === undefined || info.parentFrameId === -1) break; // reached the top frame
    const rect = await findChildFrameRect(tabId, info.parentFrameId, info.url);
    if (!rect) {
      throw new Error(
        `could not locate the <iframe>/<frame> element for frame_id ${currentId} (url ${info.url}) inside ` +
          `parent frame ${info.parentFrameId} -- cannot compute an on-screen capture region for it`
      );
    }
    x += rect.x;
    y += rect.y;
    if (width === null) {
      width = rect.width;
      height = rect.height;
    }
    currentId = info.parentFrameId;
  }
  if (width === null) return null; // frame_id resolved to the top frame after all
  return { x, y, width, height };
}

// Finds the <iframe>/<frame> element in `parentFrameId`'s own DOM whose `src`
// matches `childUrl` and returns its bounding rect. Best-effort: if no exact
// src match is found but there's exactly one child frame present, that one is
// unambiguous even if the src string doesn't match byte-for-byte (a redirect
// or URL normalization can change what's actually loaded vs. the declared
// src) -- the same src-matching heuristic already used by runMultiFrame's
// unconfirmedFrames cross-reference.
async function findChildFrameRect(tabId, parentFrameId, childUrl) {
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId, frameIds: [parentFrameId] },
    func: (url) => {
      const frames = document.querySelectorAll("iframe, frame");
      for (const el of frames) {
        const src = el.src || el.getAttribute("src") || "";
        if (src === url) {
          const r = el.getBoundingClientRect();
          return { x: r.x, y: r.y, width: r.width, height: r.height };
        }
      }
      if (frames.length === 1) {
        const r = frames[0].getBoundingClientRect();
        return { x: r.x, y: r.y, width: r.width, height: r.height };
      }
      return null;
    },
    args: [childUrl],
  });
  return result;
}

// ---------------------------------------------------------------------------
// Multi-page / scrolling capture (design doc's "Mechanism, not policy"
// section) -- a single viewport screenshot is not enough for a multi-page
// document rendered inside a scrollable viewer (the motivating real case: a
// 5-page .docx in a Word Online viewer frame). This scrolls the target
// frame/element, capturing a page at each stop, until it detects the bottom
// of the scrollable region or hits args.max_pages -- whichever comes first --
// and reports HONESTLY which one happened (`capped`/`stopped_reason`), never
// silently returning a partial result as if it were complete.
// ---------------------------------------------------------------------------

async function captureMultiPage(tabId, { capturingHidden, clip, frameId, maxPages, scrollSelector, pageDelayMs }) {
  const targetFrameId = frameId !== null && frameId !== undefined ? frameId : 0;
  const settleMs = typeof pageDelayMs === "number" ? pageDelayMs : 350;

  // Ensure the scroll-measurement helper below has a live execution context
  // in the target frame -- a bare executeScript with an inline `func` doesn't
  // need injected.js, but the frame itself must still have a live renderer
  // (see ensureAwake/discardedTabError elsewhere in this file for the
  // discarded-tab case, which this does not separately re-check here since
  // screenshot() is not currently a PAGE_WORLD_COMMAND / does not route
  // through ensureAwake -- a discarded tab simply fails the executeScript
  // call below with Chrome's own error).

  const pages = [];
  let capped = false;
  let stoppedReason = "reached end of scrollable content";

  for (let i = 0; i < maxPages; i++) {
    let base64;
    if (capturingHidden) {
      const shot = await cdpScreenshotRegion(tabId, clip);
      base64 = shot.data;
    } else {
      const tab = await chrome.tabs.get(tabId);
      const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 80 });
      base64 = dataUrl.replace(/^data:image\/jpeg;base64,/, "");
    }
    pages.push({ index: i, format: "jpeg", data_url_length: base64.length, base64 });

    if (i === maxPages - 1) {
      // Check whether this was ALSO genuinely the end, so a page count that
      // exactly matches max_pages isn't reported as capped when it wasn't.
      const info = await getScrollInfo(tabId, targetFrameId, scrollSelector);
      if (!info.atBottom) {
        capped = true;
        stoppedReason = `reached max_pages cap (${maxPages}) before the scrollable region's end`;
      }
      break;
    }

    const info = await getScrollInfo(tabId, targetFrameId, scrollSelector);
    if (info.atBottom) break;
    await scrollBy(tabId, targetFrameId, info.step, scrollSelector);
    await sleep(settleMs);
  }

  return {
    tab_id: tabId,
    format: "jpeg",
    via: capturingHidden ? "cdp" : "captureVisibleTab",
    frame_id: frameId,
    region: clip,
    page_count: pages.length,
    capped,
    stopped_reason: stoppedReason,
    pages,
  };
}

// Measures the scroll state of the target frame's scrollable element (or an
// explicit CSS selector, for a viewer that scrolls an inner container rather
// than its own document) -- `step` (how far to scroll per page) defaults to
// the element's own clientHeight, matching an ordinary "page down".
async function getScrollInfo(tabId, frameId, selector) {
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId, frameIds: [frameId] },
    func: (sel) => {
      const el = sel ? document.querySelector(sel) : document.scrollingElement || document.documentElement;
      if (!el) return { atBottom: true, step: 0, reason: "no scrollable element found" };
      const step = el.clientHeight || (typeof window !== "undefined" ? window.innerHeight : 0) || 800;
      const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 2;
      return {
        atBottom,
        step,
        scrollTop: el.scrollTop,
        scrollHeight: el.scrollHeight,
        clientHeight: el.clientHeight,
      };
    },
    args: [selector || null],
  });
  return result;
}

async function scrollBy(tabId, frameId, step, selector) {
  await chrome.scripting.executeScript({
    target: { tabId, frameIds: [frameId] },
    func: (sel, amount) => {
      const el = sel ? document.querySelector(sel) : document.scrollingElement || document.documentElement;
      if (el) el.scrollBy({ top: amount, left: 0, behavior: "instant" });
    },
    args: [selector || null, step],
  });
}

// ---------------------------------------------------------------------------
// Content-extraction mechanisms (design doc's "Mechanism, not policy" section)
// ---------------------------------------------------------------------------
// `read`/`snapshot` only ever see text that's actually in the DOM. Real-world
// finding, live against a SharePoint policy page: a .docx embedded in a Word
// Online viewer renders to <canvas> -- the frame's ENTIRE DOM text is a
// page-chrome string ("PAGE 1 OF 5 | CONFIDENTIAL..."), not the document
// body. There is no DOM text to read there, full stop. Two mechanisms reach
// that content instead -- fetch the underlying file directly (fetchBytes/
// grabImage, below), or capture pixels (screenshot, above). Neither is a
// silent fallback for the other or for `read`; each is a distinct, named
// command the CALLER chooses.

async function fetchBytes(args) {
  if (!args || typeof args.url !== "string" || !args.url) {
    throw new Error("fetch_bytes requires args.url");
  }
  const maxBytes = typeof args.max_bytes === "number" && args.max_bytes > 0 ? args.max_bytes : DEFAULT_MAX_FETCH_BYTES;
  let response;
  try {
    // credentials: "include" is the entire point -- this rides the user's
    // real cookies for the target origin (design doc \u00a71: "rides the user's
    // real logged-in sessions"). The extension's <all_urls> host permission
    // is what lets this succeed cross-origin without a CORS preflight failure.
    response = await fetch(args.url, { credentials: "include" });
  } catch (err) {
    throw new Error(
      `fetch_bytes failed to fetch ${args.url} from the extension's own context: ` +
        `${(err && err.message) || err}. If this URL requires the PAGE's own Referer/cookie ` +
        "context (some CDNs/hotlink protection check the request's origin), try grab_image " +
        "instead -- it fetches from the page's main-world script context."
    );
  }
  if (!response.ok) {
    throw new Error(
      `fetch_bytes got HTTP ${response.status} ${response.statusText} for ${args.url} ` +
        "(extension-context fetch, credentials included). If the target enforces a Referer/Origin " +
        "check that only a same-page fetch satisfies, try grab_image instead (requires a tab_id)."
    );
  }
  const contentType = response.headers.get("content-type") || "";
  const contentLengthHeader = response.headers.get("content-length");
  if (contentLengthHeader) {
    const declaredCapError = checkSizeCap(Number(contentLengthHeader), maxBytes);
    if (declaredCapError) {
      throw new Error(`fetch_bytes refused ${args.url}: declared Content-Length ${declaredCapError}`);
    }
  }
  const buf = await response.arrayBuffer();
  const capError = checkSizeCap(buf.byteLength, maxBytes);
  if (capError) {
    throw new Error(`fetch_bytes refused ${args.url}: ${capError} (Content-Type: ${contentType || "unknown"})`);
  }
  return { url: args.url, content_type: contentType, byte_length: buf.byteLength, base64: bytesToBase64(buf) };
}

// Runs the actual fetch inside the PAGE's own MAIN world (not the extension's
// isolated world injected.js uses) -- the request is indistinguishable from
// one the page's own script made, so it carries the page's real Referer and
// cookie context. This is how a hotlink-protected image/asset (blocked for an
// extension-context fetch) is retrievable at all. Requires a tab_id: unlike
// fetch_bytes, there is no context to run this in without a live page.
async function grabImage(tabId, args) {
  if (!args || typeof args.url !== "string" || !args.url) {
    throw new Error("grab_image requires args.url");
  }
  const maxBytes = typeof args.max_bytes === "number" && args.max_bytes > 0 ? args.max_bytes : DEFAULT_MAX_FETCH_BYTES;
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: async (url, cap) => {
      // Runs in the page's own MAIN world -- no access to this file's imports
      // (bytesToBase64/checkSizeCap), so the minimal equivalent is inlined
      // here rather than reaching across the world boundary.
      try {
        const response = await fetch(url);
        if (!response.ok) {
          return { ok: false, error: `HTTP ${response.status} ${response.statusText}` };
        }
        const contentType = response.headers.get("content-type") || "";
        const buf = await response.arrayBuffer();
        if (buf.byteLength > cap) {
          return {
            ok: false,
            error: `response body is ${buf.byteLength} bytes, exceeding the ${cap}-byte cap (pass a larger args.max_bytes to raise it)`,
            content_type: contentType,
          };
        }
        const bytes = new Uint8Array(buf);
        let binary = "";
        const chunkSize = 0x8000;
        for (let i = 0; i < bytes.length; i += chunkSize) {
          binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
        }
        return { ok: true, content_type: contentType, byte_length: buf.byteLength, base64: btoa(binary) };
      } catch (err) {
        return { ok: false, error: String((err && err.message) || err) };
      }
    },
    args: [args.url, maxBytes],
  });
  const inner = results[0] && results[0].result;
  if (!inner) {
    throw new Error(
      `grab_image produced no result for tab ${tabId} (the frame may have been removed mid-call, ` +
        "or the tab is discarded -- see docs/PROTOCOL.md's Discarded tabs section; args.wake is not " +
        "honored here since grab_image is not a PAGE_WORLD_COMMAND)"
    );
  }
  if (!inner.ok) {
    throw new Error(
      `grab_image failed fetching ${args.url} from the page's main-world context: ${inner.error}. ` +
        "If this URL doesn't need the page's own Referer/cookies, fetch_bytes (extension context, " +
        "no tab_id required) may succeed where this didn't."
    );
  }
  return { url: args.url, content_type: inner.content_type, byte_length: inner.byte_length, base64: inner.base64 };
}

// ---------------------------------------------------------------------------
// Downloads (chrome.downloads) -- see download_claim.mjs's module docstring
// for the baseline-max-id + filename-pattern rationale behind wait_download.
// ---------------------------------------------------------------------------

async function downloadsList(args) {
  const limit = typeof args?.limit === "number" && args.limit > 0 ? args.limit : 20;
  const recent = await chrome.downloads.search({ limit, orderBy: ["-startTime"] });
  // max_download_id is computed from this same recency-ordered result set
  // rather than a second query ordered by "-id" -- chrome.downloads.search's
  // orderBy only accepts a specific set of DownloadItem properties, and "id"
  // is not one of them (confirmed live: "Invalid orderBy field"). Download
  // ids are assigned monotonically as downloads are created, so the most
  // recent download by startTime is also, in every ordinary case, the
  // highest-id one -- exactly what a SAFE (not necessarily provably-maximal)
  // baseline for wait_download's since_id needs (see download_claim.mjs).
  const maxDownloadId = recent.reduce((max, d) => (typeof d.id === "number" && d.id > max ? d.id : max), 0);
  return {
    downloads: recent.map((d) => ({
      download_id: d.id,
      filename: d.filename,
      url: d.finalUrl || d.url,
      state: d.state,
      mime: d.mime,
      byte_length: d.totalBytes,
      start_time: d.startTime,
    })),
    max_download_id: maxDownloadId,
  };
}

async function triggerDownload(args) {
  if (!args || typeof args.url !== "string" || !args.url) {
    throw new Error("download requires args.url");
  }
  const options = { url: args.url };
  if (typeof args.filename === "string" && args.filename) options.filename = args.filename;
  const downloadId = await chrome.downloads.download(options);
  return { download_id: downloadId, url: args.url };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Poll-don't-sleep (design doc \u00a78), same discipline as injected.js's
// waitFor/waitText. See download_claim.mjs for the pure selection logic this
// wraps -- this function owns only the real chrome.downloads.search() call
// and the polling loop around it.
async function waitDownload(args) {
  const validationError = validateWaitDownloadArgs(args);
  if (validationError) throw new Error(`wait_download: ${validationError}`);

  const downloadId = typeof args.download_id === "number" ? args.download_id : undefined;
  const sinceId = typeof args.since_id === "number" ? args.since_id : undefined;
  const pattern = typeof args.pattern === "string" && args.pattern.length > 0 ? new RegExp(args.pattern) : undefined;
  const timeoutMs = typeof args.timeout_ms === "number" && args.timeout_ms > 0 ? args.timeout_ms : 30000;
  const opts = { downloadId, sinceId, pattern };

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const items = await chrome.downloads.search({});
    const failed = pickInterruptedDownload(items, opts);
    if (failed) {
      throw new Error(
        `wait_download: download ${failed.id} (${failed.filename || "unknown filename"}) was interrupted ` +
          "(failed) rather than completing"
      );
    }
    const completed = pickCompletedDownload(items, opts);
    if (completed) {
      return {
        download_id: completed.id,
        filename: completed.filename,
        url: completed.finalUrl || completed.url,
        mime: completed.mime,
        byte_length: completed.totalBytes,
        state: completed.state,
      };
    }
    await sleep(300);
  }
  const target =
    downloadId !== undefined
      ? `download_id=${downloadId}`
      : `since_id=${sinceId}${pattern ? `, pattern=${args.pattern}` : ""}`;
  throw new Error(`wait_download timed out after ${timeoutMs}ms waiting for a completed download (${target})`);
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
  // Bug 1 investigation finding: chrome.debugger.attach() on a DISCARDED tab
  // itself forces Edge to instantiate a live renderer for it -- observed live
  // against a real discarded background tab (attach succeeded, and a plain
  // injection-only `read` immediately afterward -- no explicit reload/wake --
  // then succeeded where it had failed before attaching). CDP does not need
  // its own separate discarded-tab check: attaching is itself an implicit
  // wake. This is NOT free of the same state-loss caveat as an explicit
  // wake=true reload (a discarded tab has no renderer at all, so making one
  // live is observably equivalent to a reload) -- it is simply automatic
  // rather than opt-in. See docs/PROTOCOL.md's CDP section.
  await chrome.debugger.attach({ tabId }, "1.3");
  attachedTabs.add(tabId);
  markEngaged(tabId);
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

// CDP's Input.dispatchMouseEvent/dispatchKeyEvent are tab-level (viewport
// coordinates already physically locate the right frame's rendered region --
// CDP has no per-frame targeting concept for input dispatch), but resolving a
// ref to a rect/focusing it still has to run inside the SPECIFIC frame that
// ref was qualified with (frame_refs.js) -- otherwise a frame-qualified ref
// from a non-zero frame would be looked up in the wrong frame's window.__amplifierBrowserBridge
// and reported as stale.

async function cdpClick(tabId, ref) {
  const { frameId, ref: bareRef } = parseQualifiedRef(ref);
  // Resolve the ref's viewport rect FIRST, before attaching CDP. Attaching
  // chrome.debugger to a tab can invalidate/recreate the isolated world's
  // execution context that chrome.scripting content scripts run in --
  // resolving the ref after attach intermittently raced a fresh (empty)
  // world and reported the ref as stale even immediately after a snapshot.
  // Order matters: rect resolution needs no CDP at all, so do it first.
  await chrome.scripting.executeScript({ target: { tabId, frameIds: [frameId] }, files: ["injected.js"] });
  const [{ result: rawRect }] = await chrome.scripting.executeScript({
    target: { tabId, frameIds: [frameId] },
    func: (r) => window.__amplifierBrowserBridge.rectFor(r),
    args: [bareRef],
  });
  // Bug 1 fix, part 2: rectFor() returns a `{__amplifierBrowserBridgeError}` sentinel (rather
  // than throwing) for exactly this reason -- see injected.js's rectFor()
  // comment. unwrapAmplifierBrowserBridgeResult() surfaces the REAL cause (stale generation,
  // disconnected, identity mismatch, unknown ref) instead of the old generic
  // "stale or unknown element ref" that discarded which of those it was.
  const rect = unwrapAmplifierBrowserBridgeResult(rawRect);
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
  const { frameId, ref: bareRef } = parseQualifiedRef(ref);
  await cdpAttach(tabId);
  await chrome.scripting.executeScript({ target: { tabId, frameIds: [frameId] }, files: ["injected.js"] });
  const [{ result: focusResult }] = await chrome.scripting.executeScript({
    target: { tabId, frameIds: [frameId] },
    func: (r) => window.__amplifierBrowserBridge.focusFor(r),
    args: [bareRef],
  });
  // Bug 1 fix, part 2: this previously ignored focusFor's result entirely --
  // a stale/unknown/disconnected ref would silently fall through to
  // Input.insertText typing into whatever (if anything) currently had focus,
  // the same silent-failure class as the click bug. unwrapAmplifierBrowserBridgeResult() fails
  // loud instead, before any CDP input is dispatched.
  unwrapAmplifierBrowserBridgeResult(focusResult);
  await chrome.debugger.sendCommand({ tabId }, "Input.insertText", { text });
  return { ref, trusted: true };
}

async function cdpKey(tabId, ref, keyName) {
  await cdpAttach(tabId);
  if (ref) {
    // A ref-less key press has no frame to resolve (same top-frame-only
    // limitation as the non-CDP path in runInPage) -- CDP's own key dispatch
    // below is tab-level regardless.
    const { frameId, ref: bareRef } = parseQualifiedRef(ref);
    await chrome.scripting.executeScript({ target: { tabId, frameIds: [frameId] }, files: ["injected.js"] });
    const [{ result: focusResult }] = await chrome.scripting.executeScript({
      target: { tabId, frameIds: [frameId] },
      func: (r) => window.__amplifierBrowserBridge.focusFor(r),
      args: [bareRef],
    });
    // Bug 1 fix, part 2 -- see cdpType()'s identical comment above.
    unwrapAmplifierBrowserBridgeResult(focusResult);
  }
  await chrome.debugger.sendCommand({ tabId }, "Input.dispatchKeyEvent", { type: "keyDown", key: keyName });
  await chrome.debugger.sendCommand({ tabId }, "Input.dispatchKeyEvent", { type: "keyUp", key: keyName });
  return { key: keyName, trusted: true };
}

// `clip`, if given, is `{x, y, width, height}` in viewport coordinates (see
// computeFrameClip() above) -- crops the capture to that on-screen region
// instead of the full viewport. `scale: 1` is required alongside `clip` by
// the CDP Page.captureScreenshot contract (an unset scale with a clip
// present is rejected by the protocol).
async function cdpScreenshotRegion(tabId, clip) {
  await cdpAttach(tabId);
  const params = { format: "jpeg", quality: 80, fromSurface: true };
  if (clip) {
    params.clip = { x: clip.x, y: clip.y, width: clip.width, height: clip.height, scale: 1 };
  }
  return await chrome.debugger.sendCommand({ tabId }, "Page.captureScreenshot", params);
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

chrome.runtime.onInstalled.addListener(async () => {
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: 0.5 });
  // Adopt a build-time-baked hub URL/token (Android zero-config installs -- see
  // "Bundled first-run config" section above) BEFORE opening the options page or
  // attempting to connect, so if the options page DOES render (reliable on Desktop;
  // unreliable on Edge Android, which is the whole reason this feature exists), it
  // shows the real adopted values on its very first paint instead of a blank one.
  // Idempotent and guarded by amplifier_browser_bridge_setup_completed -- connect()
  // below also calls this on every invocation (a worker restart without a fresh
  // onInstalled event still needs the same one-time adoption to happen eventually),
  // so this call is a deliberate, harmless duplicate of what would happen anyway.
  await adoptBundledConfigIfNeeded();
  // First-run UX: open the options page immediately so a fresh install's very first
  // screen is "set the hub URL/token", not a silently-failing connection attempt.
  // Harmless on a re-install/update of an already-configured install -- an extra tab
  // the user can close; it never touches the config already sitting in
  // chrome.storage.local (see this file's "Runtime configuration" section).
  chrome.runtime.openOptionsPage();
  connect();
});

chrome.runtime.onStartup.addListener(() => {
  connect();
});

// The toolbar icon has no default_popup (see manifest.json's "action" key) specifically
// so a click always reaches this handler -- the options page IS the extension's only UI.
chrome.action.onClicked.addListener(() => {
  chrome.runtime.openOptionsPage();
});

// Saving valid config on the options page re-triggers connect() immediately, rather than
// waiting for the next alarm tick (up to 30s) -- see options.js's save handler.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if ("amplifier_browser_bridge_hub_url" in changes || "amplifier_browser_bridge_hub_token" in changes) {
    reconnectAttempt = 0;
    if (ws) {
      try {
        ws.close();
      } catch {
        // already closed/closing -- connect() below handles re-establishing either way
      }
    }
    connect();
  }
});

// Status query for options.js -- never echoes the token back (options.js reads that
// directly from chrome.storage.local itself for prefill; this is purely "are we
// connected right now" for the options page's live status line).
//
// Defensive try/catch (bug report, 2026-08): this handler responds synchronously, so
// `return true` here is belt-and-suspenders (sendResponse has already fired either
// way) -- but if reading `ws`/`configured`/etc. ever threw for any reason, an
// uncaught exception here would leave the message channel open with no response
// ever sent, and the sender's `chrome.runtime.sendMessage` promise would hang until
// Chrome eventually closes the port with "message port closed before a response was
// received" -- indistinguishable, from options.js's side, from the background script
// never having run at all. Catching and reporting the error explicitly closes that
// gap; see options.js's queryStatusOnce()/pollStatusUntilKnown() for the client-side
// half of this fail-loud guarantee.
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.type === "amplifier_browser_bridge_get_status") {
    try {
      sendResponse({
        configured,
        connected: !!(ws && ws.readyState === WebSocket.OPEN),
        hubUrl,
        deviceId,
        legacyConfigDetected,
        // See this module's `lastConnectError` docstring -- null whenever the
        // most recent attempt is still in flight or the connection is currently
        // up; otherwise `{code, message, at}` naming exactly why it isn't.
        lastError: lastConnectError,
      });
    } catch (err) {
      sendResponse({ error: String((err && err.message) || err) });
    }
    return true;
  }
  return false;
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
