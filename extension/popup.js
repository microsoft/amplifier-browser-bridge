// popup.js -- the toolbar popup: a read-only STATUS surface.
//
// Why this exists at all, given options.html already renders a status line:
// this extension is a persistent background bridge, not a click-to-act tool.
// Nobody clicks it to make it do something -- an agent drives it remotely, so
// the only recurring question a human has is "is this thing actually connected,
// and which device am I?". Answering that today costs a full tab (the toolbar
// click opens options.html). This panel answers it in place, and hands off to
// options.html for anything that involves *changing* configuration.
//
// It is deliberately NOT modelled on the sibling page-to-markdown extensions'
// popups. Theirs are action triggers -- a button that performs the extension's
// one job. There is no equivalent action to trigger here, so a button-shaped
// popup would be a costume. This one is a status readout with a single
// navigation affordance.
//
// WHAT THIS FILE WILL NOT SHOW
// background.js's only message handler, `amplifier_browser_bridge_get_status`,
// returns exactly five fields: configured, connected, hubUrl, deviceId,
// legacyConfigDetected. That is the complete set of state this popup can
// honestly render. Notably absent, and NOT to be added by inventing a plausible
// value:
//   - pending/queued command count -- no such counter exists in background.js
//   - "last seen" / last-heartbeat time -- no such timestamp is recorded
//   - capabilities, effectsTier, reconnect attempt -- held in background.js's
//     module scope but never included in the status response
// Any of those would require widening the status response in background.js
// first. A popup that displays them without that change is displaying fiction.
//
// ACTIVATION (not done in this change): this file is INERT. Nothing loads it
// until the manifest points at it, and the three edits below have to land
// TOGETHER -- doing only the first breaks the build rather than half-enabling
// the feature:
//
//   1. manifest.json AND manifest.android.json: add
//      `"default_popup": "popup.html"` inside the existing `"action"` block.
//   2. src/amplifier_browser_bridge/setup.py: add "popup.html", "popup.css",
//      "popup.js" to _EXTENSION_FILES. That constant is the single source of
//      truth for what gets staged, and scripts/package.sh derives its file list
//      from it -- so with (1) but not (2), package.sh's integrity gate REFUSES
//      the build on a manifest reference that resolves to nothing in the staged
//      set. (`amplifier-browser-bridge init` would likewise stage a manifest
//      pointing at a file it never copied.)
//   3. background.js: (1) also SUPPRESSES chrome.action.onClicked, which
//      background.js currently uses to open the options page on toolbar click.
//      That handler becomes dead code, and the toolbar click no longer reaches
//      the options page at all -- which is precisely why the Settings button
//      below is not optional garnish. It is the replacement route to
//      configuration.
//
// Also outstanding: there is no popup.test.mjs, so this file is the one module
// here without the repo's test-per-module coverage. The logic was verified
// out-of-tree (12 assertions, options.test.mjs's stub-globals +
// cache-busting-dynamic-import pattern); that harness should be committed here
// when the popup is activated. And README.md still describes the toolbar icon
// as opening the options page and calls it "its only UI" -- true today, stale
// the moment (1) lands.
//
// Status-query discipline is inherited from options.js, and for the same
// reason: a single point query against an MV3 service worker that is mid-wake
// returns "not connected" for a healthy bridge. queryStatusOnce never throws
// and never returns a value that can be mistaken for an answer;
// pollStatusUntilKnown always terminates on either a real response or an honest
// "couldn't determine status" -- never on a stale optimistic string.

const statusEl = document.getElementById("status");
const detailsEl = document.getElementById("details");
const hubUrlEl = document.getElementById("hub-url");
const deviceIdEl = document.getElementById("device-id");
const openOptionsButton = document.getElementById("open-options");

function setStatus(className, text) {
  if (!statusEl) return; // defensive -- absent only if popup.html and this file drift apart
  statusEl.className = className;
  statusEl.textContent = text;
}

// Shows the Hub/Device rows. Only ever called with values that arrived in a real
// status response. `hubUrl` here is the CONFIGURED target, which is meaningful
// whether or not the socket is currently open -- the status line above it, not
// this row, is what claims anything about liveness.
function showDetails({ hubUrl, deviceId }) {
  if (!detailsEl) return;
  if (!hubUrl) {
    // Configured-but-no-URL should be impossible, but rendering an empty row
    // labelled "Hub" would imply we know something we don't.
    detailsEl.hidden = true;
    return;
  }
  if (hubUrlEl) hubUrlEl.textContent = hubUrl;
  // background.js generates deviceId asynchronously on wake; "(pending)" is the
  // same honest placeholder options.js uses rather than an invented id.
  if (deviceIdEl) deviceIdEl.textContent = deviceId || "(pending)";
  detailsEl.hidden = false;
}

function hideDetails() {
  if (detailsEl) detailsEl.hidden = true;
}

// Sends the status query exactly once. Never throws, and never returns a bare
// value a caller could confuse with a real answer -- always `{ ok: true,
// response }` or `{ ok: false, error }`. Mirrors options.js's queryStatusOnce
// deliberately: the two surfaces must not disagree about what "we heard back"
// means.
async function queryStatusOnce() {
  let response;
  try {
    response = await chrome.runtime.sendMessage({ type: "amplifier_browser_bridge_get_status" });
  } catch (err) {
    // Typically "Could not establish connection. Receiving end does not exist."
    // -- the background service worker is not running, or woke and failed to
    // load (a broken static import kills every listener; see options.js).
    return { ok: false, error: err instanceof Error ? err : new Error(String(err)) };
  }
  if (!response) {
    // Chrome resolves (does not reject) sendMessage with undefined when a
    // listener matched but never called sendResponse. Equally "we learned
    // nothing" -- never treated as a renderable value.
    return { ok: false, error: new Error("the extension's background script returned no status") };
  }
  return { ok: true, response };
}

// Renders a REAL response. Every branch is an honest description of what
// background.js just reported, never an assumption about what it is probably
// still doing.
function renderStatus(response) {
  if (!response.configured) {
    hideDetails();
    if (response.legacyConfigDetected) {
      // This install HAD a working config under the old (pre-rename) storage
      // keys, which are no longer read. See background.js's loadConfig() and
      // MIGRATION.md. This IS a real problem -- "warn" (red) is correct.
      setStatus(
        "warn",
        "Configuration key names changed in this version -- your previous Hub URL and token " +
          "are no longer read. Open Settings and re-enter them.",
      );
    } else {
      // Brand-new install, nothing configured yet -- expected, not an error.
      // "pending" (calm/neutral), never "warn" (red) -- mirrors options.js's
      // renderStatus; see that file's module docstring for the full reasoning.
      setStatus("pending", "Not paired yet -- open Settings to pair with a hub.");
    }
    return;
  }
  showDetails(response);
  if (response.connected) {
    setStatus("ok", "Connected.");
  } else {
    // Configured but not (yet) connected, with no further detail available here
    // (this panel is read-only status -- see this file's module docstring for
    // exactly which fields background.js supplies). Calm/neutral, not red: a
    // fresh pair/reconnect attempt in flight looks identical to this panel.
    setStatus(
      "pending",
      "Configured, connecting... if this doesn't clear, is the hub running and reachable?",
    );
  }
}

// The honest "we tried, and we still don't know" terminal state. Details are
// hidden rather than left showing a previous render's values, so nothing on
// screen outlives the confidence that produced it.
function renderUnknown(lastError) {
  hideDetails();
  const reason = lastError && lastError.message ? ` (${lastError.message})` : "";
  setStatus(
    "warn",
    `Couldn't determine connection status${reason} -- the extension's background script may ` +
      "not be running. Open Settings, or run `amplifier-browser-bridge doctor` from the CLI.",
  );
}

// Polls queryStatusOnce on the given schedule (each entry is a delay in ms
// BEFORE that attempt) until a real response arrives. If every attempt fails,
// renders the honest unknown state.
async function pollStatusUntilKnown(delaysMs) {
  let lastError = null;
  for (const delay of delaysMs) {
    if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
    const result = await queryStatusOnce();
    if (result.ok) {
      renderStatus(result.response);
      return;
    }
    lastError = result.error;
  }
  renderUnknown(lastError);
}

// Tighter than options.js's LOAD_STATUS_POLL_DELAYS_MS ([0, 300, 800, 2000,
// 4000], ~7.1s) on purpose: a popup is dismissed by any click outside it, so a
// retry budget that outlives the panel is a budget that never renders. ~2.5s
// still covers a service worker waking from idle, which is the case this exists
// to survive.
const POPUP_STATUS_POLL_DELAYS_MS = [0, 250, 750, 1500];

if (openOptionsButton) {
  openOptionsButton.addEventListener("click", () => {
    // The only write-capable route out of this panel. Once default_popup is set
    // in the manifest, chrome.action.onClicked no longer fires, making this the
    // sole in-extension path to configuration.
    chrome.runtime.openOptionsPage();
  });
}

// Auto-run on real popup open, skipped when a test sets this flag before
// importing so it can drive the functions below with its own fast schedules.
if (!globalThis.__AMPLIFIER_BROWSER_BRIDGE_POPUP_TEST__) {
  pollStatusUntilKnown(POPUP_STATUS_POLL_DELAYS_MS);
}

// Exported for tests only -- no other runtime file imports this module.
export {
  queryStatusOnce,
  renderStatus,
  renderUnknown,
  showDetails,
  hideDetails,
  pollStatusUntilKnown,
  POPUP_STATUS_POLL_DELAYS_MS,
};
