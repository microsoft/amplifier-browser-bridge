// options.js -- the extension's ONLY user-facing UI. Reads/writes the runtime hub
// configuration (Hub URL + token) directly to chrome.storage.local; background.js's
// storage.onChanged listener picks up a save immediately and reconnects. See
// config_validate.mjs for the shared validation logic and background.js's "Runtime
// configuration" section for why this replaced the old tracked extension/config.js.
//
// One ladder, two screens (docs/designs/onboarding-ux.md): this page and the hub's
// `/setup` page render the SAME three-step ladder off the same copy-pasted CSS token
// block (see options.html's <style> and onboarding.py's `_TOKENS_CSS` --
// tests/test_shared_design_tokens.py guards the two staying byte-identical). Step 1
// ("Extension installed") is always done -- this page running proves it. Step 2
// ("Connect it" / "Connected to <host>") collapses to a single done line the moment
// this device has ever been paired. Step 3 is the dynamic slot: the same four-class
// status vocabulary this file has always used (ok/pending/alert), now expressed as
// this step's own marker/title/context, with its body holding either the pairing
// controls (while unpaired) or the post-pairing payload -- the moment the product
// finally says what it's for: what the agent can see, what it can do, and "you can
// close this tab." `renderLadder` below is the single place that decides all of this;
// `renderStatus`'s prior job (four-class status text) is now folded into it.
//
// Status-query fail-loud discipline (bug report, 2026-08): this file used to have two
// silent `return`s in its status query -- one in a bare `catch`, one on `!response` --
// plus a fixed three-poll retry (500/2000/5000ms) after Save. If EVERY attempt took a
// silent path, the optimistic "Saved. Connecting..."/"Checking status..." string stood
// forever, even while the hub-side connection was perfectly healthy (root cause: a
// module-import bug in background.js -- see setup.py's _EXTENSION_FILES fix in the same
// commit -- meant a fresh service-worker instantiation could fail entirely, silently
// dropping every message including this one). The fix here is structural, not "more
// retries": `queryStatusOnce` never throws and never returns a value the caller can
// mistake for success, and `pollStatusUntilKnown` guarantees that once its retry budget
// is exhausted, the page renders an HONEST "couldn't determine status" state rather than
// leaving whatever optimistic string was showing. A stale optimistic string must never be
// a terminal state.
//
// Pairing (design/product council review, 2026-08): the primary configuration path is
// now "Pair with a hub" -- a single pairing code (from `amplifier-browser-bridge pair`)
// replaces hand-transcribing a raw ws:// URL and a 32-hex token into two separate
// fields. The Hub URL/Token fields still exist (collapsed under "Manual setup" in
// options.html) for scripting, dev hubs with no running pairing session, or an operator
// who prefers typing values directly -- pairing does not remove that capability, it
// just stops being the default path. See pairing_code.mjs for the code parser and
// src/amplifier_browser_bridge/pairing.py for the server-side ticket design
// (entropy/lifetime/threat-model reasoning).
//
// Zero-copy-paste pairing (real-run maintainer feedback, 2026-08): typing/pasting a
// code at all is a step this extension can usually skip entirely. The `tabs`
// permission this extension already holds can see an already-open
// `/setup#pair=...` tab's URL (fragment included), which is exactly what a user who
// just followed the setup link is looking at. `runPairingDiscovery` below tries, in
// order, until one works: (1) an open pairing tab (`pair_discovery.mjs` -- see that
// module's docstring for the origin-check security invariant this relies on), (2) the
// system clipboard (`navigator.clipboard.readText()`, gated on the `clipboardRead`
// permission), (3) a "Check again" button the user can click to retry both with a
// real user gesture (some clipboard-permission policies require one). Only the last
// resort -- typing/pasting a code into the "Enter a code by hand" field -- is
// unchanged from before. Never runs at all once this device is already configured:
// an auto-redeem is only appropriate for a brand-new, never-paired install, never a
// silent re-pair against whatever `/setup` tab happens to be open later.
//
// Auto-pair provenance (docs/designs/onboarding-ux.md section 6.1): a new storage flag,
// `amplifier_browser_bridge_paired_auto`, is set true ONLY by the auto-discovery path
// (`tryAutoRedeem`) -- never by the manual "Pair"/"Save" buttons. It drives the ONE
// conditional line under step 2 ("Paired automatically -- nothing to copy.") that
// replaces the old, always-visible provenance paragraph (deleted -- it explained a
// mechanism to a user who watched it happen, in a state where it may not even be true).
// The full provenance description still exists, computed live from CURRENT storage
// state, but moved into the "Connection details" disclosure (see
// `renderConnectionDetails`) -- useful diagnostic info once actually connected, no
// longer a paragraph sitting on the path before anything has happened.

import { validateHubUrl, validateHubToken } from "./config_validate.mjs";
import { describeConfigProvenance, CONFIG_SOURCE_MANUAL, CONFIG_SOURCE_PAIRED } from "./bundled_config.mjs";
import { parsePairingCode, buildDeviceWsUrl, buildRedeemUrl } from "./pairing_code.mjs";
import { discoverPairingCandidate } from "./pair_discovery.mjs";

const step2El = document.getElementById("step-2");
const step2TitleEl = document.getElementById("step-2-title");
const step2AutoLineEl = document.getElementById("step-2-auto-line");
const step3El = document.getElementById("step-3");
const step3MarkerEl = document.getElementById("step-3-marker");
const step3TitleEl = document.getElementById("step-3-title");
const step3LineEl = document.getElementById("step-3-line");
const step3BodyEl = document.getElementById("step-3-body");
const pairingControlsTpl = document.getElementById("tpl-pairing-controls");
const readyPayloadTpl = document.getElementById("tpl-ready-payload");

// Storage key mirrored from persistConfigAndPoll below -- checked BEFORE any
// auto-discovery attempt runs. An already-paired device must never be
// silently re-pointed at a different hub just because some `/setup` tab
// happens to be open; auto-discovery is for first-run only. See this file's
// module docstring.
const SETUP_COMPLETED_STORAGE_KEY = "amplifier_browser_bridge_setup_completed";
const PAIRED_AUTO_STORAGE_KEY = "amplifier_browser_bridge_paired_auto";

async function loadCurrentValues() {
  const stored = await chrome.storage.local.get([
    "amplifier_browser_bridge_hub_url",
    "amplifier_browser_bridge_hub_token",
  ]);
  const urlInput = document.getElementById("hub-url");
  const tokenInput = document.getElementById("hub-token");
  if (urlInput) urlInput.value = stored.amplifier_browser_bridge_hub_url || "";
  if (tokenInput) tokenInput.value = stored.amplifier_browser_bridge_hub_token || "";
}

// ---------------------------------------------------------------------------
// Device identity -- read/created directly against chrome.storage.local, the
// SAME key background.js's ensureIdentity()/getOrCreateId() use. Duplicated
// (not imported from background.js, which is chrome.*-API-entangled and not a
// pure module) rather than requiring a round trip through the service worker:
// this needs to work even on a brand-new, never-configured install, where
// background.js's connect() has never run ensureIdentity() at all yet (it only
// runs once `configured` is true -- see background.js's connectLocked()). A
// pairing redemption is exactly the case that must work BEFORE that's true.
// ---------------------------------------------------------------------------

const DEVICE_ID_STORAGE_KEY = "amplifier_browser_bridge_device_id";

async function getOrCreateDeviceId() {
  const stored = await chrome.storage.local.get(DEVICE_ID_STORAGE_KEY);
  if (stored[DEVICE_ID_STORAGE_KEY]) return stored[DEVICE_ID_STORAGE_KEY];
  const id = crypto.randomUUID();
  await chrome.storage.local.set({ [DEVICE_ID_STORAGE_KEY]: id });
  return id;
}

function platformLabel() {
  const ua = (typeof navigator !== "undefined" && navigator.userAgent) || "";
  if (/Android/i.test(ua)) return "edge-android";
  if (/Macintosh/i.test(ua)) return "edge-macos";
  if (/Windows/i.test(ua)) return "edge-windows";
  if (/Linux/i.test(ua)) return "edge-linux";
  return "edge-unknown";
}

// Extracts "host:port" from a stored ws://host:port/device URL, for step 2's
// "Connected to <host>" title -- falls back to the raw URL if parsing fails
// for any reason (never throws, never shows a blank title).
function hostPortFromHubUrl(hubUrl) {
  try {
    const parsed = new URL(hubUrl);
    return parsed.port ? `${parsed.hostname}:${parsed.port}` : parsed.hostname;
  } catch {
    return hubUrl;
  }
}

// Sends the status query exactly once. NEVER throws and NEVER returns a bare
// value the caller could confuse with a real answer -- always `{ ok: true,
// response }` (a real answer arrived) or `{ ok: false, error }` (it didn't, for
// whatever reason: the background script isn't running, sendMessage rejected, or
// it resolved with nothing). This is the single chokepoint between "did we hear
// back" and "what does it mean", so every caller gets an explicit,
// un-ignorable failure signal instead of a value that's merely falsy.
async function queryStatusOnce() {
  let response;
  try {
    response = await chrome.runtime.sendMessage({ type: "amplifier_browser_bridge_get_status" });
  } catch (err) {
    // Most commonly "Could not establish connection. Receiving end does not exist."
    // -- the background service worker isn't running and didn't wake up (or woke up
    // and failed to load -- e.g. a broken import; see this file's module docstring).
    return { ok: false, error: err instanceof Error ? err : new Error(String(err)) };
  }
  if (!response) {
    // Chrome resolves (does not reject) sendMessage with undefined when a listener
    // matched but never called sendResponse -- distinct from the reject case above,
    // but equally "we learned nothing." Never treated as a value to render.
    return { ok: false, error: new Error("the extension's background script returned no status") };
  }
  return { ok: true, response };
}

// ---------------------------------------------------------------------------
// The ladder -- one function decides step 2 and step 3's entire visible state.
// Replaces the old, single `#status` div's four-class vocabulary; the classes
// are the same four (ok/pending/alert -- there was never a fourth "unknown"
// CSS class, see renderUnknown below), just now expressed through the shared
// step component's marker/title/context instead of a standalone div.
// ---------------------------------------------------------------------------

function clearStep3Body() {
  step3BodyEl.replaceChildren();
}

function mountPairingControls() {
  clearStep3Body();
  step3BodyEl.appendChild(pairingControlsTpl.content.cloneNode(true));
  wirePairingControls();
}

function mountReadyPayload(response) {
  clearStep3Body();
  step3BodyEl.appendChild(readyPayloadTpl.content.cloneNode(true));
  wireReadyPayload(response);
}

/** Renders a REAL response. This is the only place allowed to show a "we know the
 * truth" status -- every branch here is an honest description of what the
 * background script just told us, never an assumption about what it's probably
 * still doing. See docs/designs/onboarding-ux.md section 6.3's table -- this
 * function implements that table exactly, one row per branch below. */
function renderLadder(response) {
  const pairedBefore = !!(response && response.configured);

  if (!response.configured) {
    if (response.legacyConfigDetected) {
      step3El.setAttribute("data-marker-class", "alert");
      step3TitleEl.textContent = "Settings need re-pairing";
      step3LineEl.textContent =
        "Configuration key names changed in this version -- your previous Hub URL/token are " +
        "no longer read. Re-enter them below and click Save.";
    } else {
      step3El.setAttribute("data-marker-class", "pending");
      step3TitleEl.textContent = "Not connected yet";
      step3LineEl.textContent = "Open your hub's setup link, or enter a code below.";
    }
    step3MarkerEl.textContent = response.legacyConfigDetected ? "!" : "3";
    mountPairingControls();
  } else if (response.connected) {
    step3El.setAttribute("data-marker-class", "ok");
    step3MarkerEl.textContent = "\u2713";
    step3TitleEl.textContent = "You're ready";
    step3LineEl.textContent = "Your agent can use this browser now.";
    mountReadyPayload(response);
  } else {
    const lastError = response.lastError;
    if (lastError && lastError.code === "auth_rejected") {
      step3El.setAttribute("data-marker-class", "alert");
      step3MarkerEl.textContent = "!";
      step3TitleEl.textContent = "Hub refused this browser";
      step3LineEl.textContent = `${lastError.message} Pair again to get a fresh code.`;
      mountPairingControls();
    } else if (lastError && lastError.code === "unreachable") {
      step3El.setAttribute("data-marker-class", "alert");
      step3MarkerEl.textContent = "!";
      step3TitleEl.textContent = `Can't reach ${hostPortFromHubUrl(response.hubUrl)}`;
      step3LineEl.textContent = `${lastError.message} Check the hub is running.`;
      mountPairingControls();
    } else if (lastError && lastError.code === "hub_error") {
      step3El.setAttribute("data-marker-class", "alert");
      step3MarkerEl.textContent = "!";
      step3TitleEl.textContent = "Hub refused this browser";
      step3LineEl.textContent = lastError.message;
      mountPairingControls();
    } else {
      // Configured, not yet connected, no concrete error yet -- the window right
      // after Save/Pair while the first attempt is still in flight. Expected and
      // transient, not a confirmed problem -- pending, not alert.
      step3El.setAttribute("data-marker-class", "pending");
      step3MarkerEl.textContent = "3";
      step3TitleEl.textContent = "Connecting\u2026";
      step3LineEl.textContent = "Give it a moment.";
      mountPairingControls();
    }
  }

  // Step 2 collapses to "done" the moment this device has EVER been paired --
  // independent of whether the LIVE socket is up right now (that's step 3's
  // job). "Connected to <host>" stays visible even mid-error, since pairing
  // itself already happened; only the connectivity axis is in question.
  if (pairedBefore) {
    step2El.setAttribute("data-state", "done");
    step2TitleEl.textContent = `Connected to ${hostPortFromHubUrl(response.hubUrl)}`;
  } else {
    step2El.setAttribute("data-state", "next");
    step2TitleEl.textContent = "Connect it";
  }

  renderAutoPairLine(pairedBefore);
}

/** The ONE line that replaces the old always-visible provenance paragraph --
 * renders ONLY when auto-discovery actually won (docs/designs/onboarding-ux.md
 * section 6.1). If the user pasted the code by hand (or used Manual setup),
 * this line is omitted entirely -- never shown speculatively. */
async function renderAutoPairLine(pairedBefore) {
  if (!pairedBefore) {
    step2AutoLineEl.style.display = "none";
    return;
  }
  const stored = await chrome.storage.local.get(PAIRED_AUTO_STORAGE_KEY);
  step2AutoLineEl.style.display = stored[PAIRED_AUTO_STORAGE_KEY] ? "block" : "none";
}

/** The honest "we tried, and we still don't know" terminal state -- what a caller
 * lands on when every retry in pollStatusUntilKnown's budget failed. */
function renderUnknown(lastError) {
  step3El.setAttribute("data-marker-class", "alert");
  step3MarkerEl.textContent = "!";
  step3TitleEl.textContent = "Couldn't determine status";
  const reason = lastError && lastError.message ? ` (${lastError.message})` : "";
  step3LineEl.textContent =
    `Couldn't determine connection status${reason} -- the extension's background script ` +
    "may not be running. Try reloading this page or the extension (edge://extensions), " +
    "or run `amplifier-browser-bridge doctor` from the CLI for a full check.";
  mountPairingControls();
}

// Polls queryStatusOnce on the given delay schedule (each entry is a delay in ms BEFORE
// that attempt) until a real response arrives, rendering it immediately and stopping.
// If every attempt in the schedule fails, renders the honest "couldn't determine" state
// -- this is the guarantee that closes off the bug this file used to have: no matter how
// many attempts it takes or whether every single one fails, the page always ends on a
// truthful terminal state, never a stale optimistic one.
async function pollStatusUntilKnown(delaysMs) {
  let lastError = null;
  for (const delay of delaysMs) {
    if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
    const result = await queryStatusOnce();
    if (result.ok) {
      renderLadder(result.response);
      return;
    }
    lastError = result.error;
  }
  renderUnknown(lastError);
}

// Widened + backoff schedule for the initial page load (first attempt immediate, then
// growing gaps) -- covers a service worker that needs a moment to wake, without
// retrying forever.
const LOAD_STATUS_POLL_DELAYS_MS = [0, 300, 800, 2000, 4000];

// Post-save schedule: a real hub round trip (device auth + hello) is not instant, so the
// first attempt waits a beat; growing gaps after that cover slower reconnects.
const SAVE_STATUS_POLL_DELAYS_MS = [500, 1500, 3000, 6000, 10000];

// ---------------------------------------------------------------------------
// Wiring for step-3-body's two templates -- called each time one is mounted,
// since a <template> clone's elements are fresh nodes with no listeners yet.
// ---------------------------------------------------------------------------

function wirePairingControls() {
  const toggleTokenLink = document.getElementById("toggle-token");
  const tokenInput = document.getElementById("hub-token");
  if (toggleTokenLink && tokenInput) {
    toggleTokenLink.addEventListener("click", () => {
      const showing = tokenInput.type === "text";
      tokenInput.type = showing ? "password" : "text";
      toggleTokenLink.textContent = showing ? "show" : "hide";
    });
  }
  loadCurrentValues();

  const saveButton = document.getElementById("save");
  if (saveButton) {
    saveButton.addEventListener("click", async () => {
      const errorEl = document.getElementById("error");
      const urlInput = document.getElementById("hub-url");
      errorEl.textContent = "";
      const urlValidation = validateHubUrl(urlInput.value);
      if (!urlValidation.valid) {
        errorEl.textContent = urlValidation.error;
        return;
      }
      const tokenValidation = validateHubToken(tokenInput.value);
      if (!tokenValidation.valid) {
        errorEl.textContent = tokenValidation.error;
        return;
      }
      // A deliberate Save is the user affirmatively taking ownership of these values.
      await persistConfigAndPoll(urlValidation.normalized, tokenInput.value, CONFIG_SOURCE_MANUAL, {
        auto: false,
      });
    });
  }

  const pairCodeInput = document.getElementById("pair-code");
  const pairErrorEl = document.getElementById("pair-error");
  const pairButton = document.getElementById("pair");
  if (pairButton) {
    pairButton.addEventListener("click", async () => {
      pairErrorEl.textContent = "";
      pairButton.disabled = true;
      const originalLabel = pairButton.textContent;
      pairButton.textContent = "Pairing...";
      try {
        const ok = await redeemCode(pairCodeInput.value, {
          auto: false,
          onError: (message) => {
            pairErrorEl.textContent = message;
          },
        });
        if (ok) pairCodeInput.value = "";
      } finally {
        pairButton.disabled = false;
        pairButton.textContent = originalLabel;
      }
    });
  }

  const pairRetryButton = document.getElementById("pair-retry");
  if (pairRetryButton) {
    pairRetryButton.addEventListener("click", async () => {
      pairRetryButton.disabled = true;
      try {
        // The click itself is a real user gesture -- the best chance a stricter
        // clipboard-permission policy has of allowing discoverFromClipboard to
        // succeed, even if the automatic attempt above could not.
        await runPairingDiscovery();
      } finally {
        pairRetryButton.disabled = false;
      }
    });
  }

  runPairingDiscovery();
}

/** Human-readable provenance -- moved here (from an always-visible top-of-page
 * paragraph) into the "Connection details" disclosure, computed live from
 * CURRENT storage state so it's always accurate once actually read, never a
 * stale claim sitting on the path before anything has happened (see this
 * file's module docstring). */
async function wireReadyPayload(response) {
  const disconnectButton = document.getElementById("disconnect");
  if (disconnectButton) {
    disconnectButton.addEventListener("click", disconnect);
  }
  const detailsBody = document.getElementById("connection-details-body");
  if (!detailsBody) return;
  const stored = await chrome.storage.local.get([
    "amplifier_browser_bridge_hub_url",
    "amplifier_browser_bridge_config_source",
    "amplifier_browser_bridge_config_bundled_at",
    DEVICE_ID_STORAGE_KEY,
  ]);
  const provenance = describeConfigProvenance({
    configSource: stored.amplifier_browser_bridge_config_source,
    configBundledAt: stored.amplifier_browser_bridge_config_bundled_at,
  });
  const lines = [];
  lines.push(`<div><code>${stored.amplifier_browser_bridge_hub_url || response.hubUrl || ""}</code></div>`);
  if (response.deviceId || stored[DEVICE_ID_STORAGE_KEY]) {
    lines.push(`<div>Device ID: <code>${response.deviceId || stored[DEVICE_ID_STORAGE_KEY]}</code></div>`);
  }
  if (provenance) lines.push(`<div>${provenance}</div>`);
  detailsBody.innerHTML = lines.join("");
}

/** Disconnect -- the counterweight to broad access, one click from the top
 * level (never behind a disclosure -- see options.html). Reverts steps 2 and
 * 3 to their pre-pair state and reveals the pairing controls again. Does not
 * touch the device identity (DEVICE_ID_STORAGE_KEY) -- re-pairing later
 * reuses the same device_id, matching how the hub's own token file already
 * keys per-device tokens. */
async function disconnect() {
  await chrome.storage.local.set({
    amplifier_browser_bridge_hub_url: "",
    amplifier_browser_bridge_hub_token: "",
    amplifier_browser_bridge_config_source: null,
    amplifier_browser_bridge_config_bundled_at: null,
    amplifier_browser_bridge_setup_completed: false,
    [PAIRED_AUTO_STORAGE_KEY]: false,
  });
  step3LineEl.textContent = "Disconnected. Pair again to reconnect.";
  pollStatusUntilKnown([0]);
}

// ---------------------------------------------------------------------------
// Pairing -- see this file's module docstring and pairing_code.mjs.
//
// `redeemCode` is the ONE place a pairing code is ever turned into a
// configured hub -- the manual "Pair" button, the auto-discovered tab, and
// the clipboard rung all call this same function, so the three paths can
// never drift apart on what counts as success or how an error is worded.
// ---------------------------------------------------------------------------

async function persistConfigAndPoll(hubUrl, token, configSource, { auto } = { auto: false }) {
  await chrome.storage.local.set({
    amplifier_browser_bridge_hub_url: hubUrl,
    amplifier_browser_bridge_hub_token: token,
    amplifier_browser_bridge_config_source: configSource,
    amplifier_browser_bridge_config_bundled_at: null,
    amplifier_browser_bridge_setup_completed: true,
    [PAIRED_AUTO_STORAGE_KEY]: !!auto,
  });
  // storage.onChanged (background.js) reconnects immediately; pollStatusUntilKnown
  // either catches that success or -- if it never arrives -- lands on the honest
  // "couldn't determine status" state instead of leaving a stale line up forever.
  pollStatusUntilKnown(SAVE_STATUS_POLL_DELAYS_MS);
}

/**
 * Parse and redeem a pairing code against its hub, persisting the resulting
 * hub URL/token on success. Never throws -- every failure is reported via
 * `onError` (a callback, not an exception) so callers with different UI
 * surfaces (a visible error line vs. a quiet auto-discovery status line) can
 * each render it their own way.
 *
 * @param {string} rawCode
 * @param {{onError?: (message: string) => void, auto?: boolean}} [opts]
 * @returns {Promise<boolean>} true iff redeemed and persisted.
 */
async function redeemCode(rawCode, { onError, auto = false } = {}) {
  const fail = (message) => {
    if (onError) onError(message);
    return false;
  };

  const parsed = parsePairingCode(rawCode);
  if (!parsed.valid) return fail(parsed.error);

  const deviceId = await getOrCreateDeviceId();
  const redeemUrl = buildRedeemUrl(parsed.host, parsed.port);
  let response;
  try {
    // Rides the extension's own <all_urls> host_permissions -- fetches from an
    // extension page are not subject to the same-origin/CORS restrictions a
    // normal web page's fetch would be, the same reason background.js's
    // fetchBytes() can reach an arbitrary origin (see that function's comment).
    response = await fetch(redeemUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ticket: parsed.ticket,
        device_id: deviceId,
        label: platformLabel(),
        platform: (typeof navigator !== "undefined" && navigator.platform) || "unknown",
      }),
    });
  } catch (err) {
    return fail(
      `Could not reach the hub at ${parsed.host}:${parsed.port} -- is \`amplifier-browser-bridge hub\` ` +
        `(or the service) running, and is this device on the same tailnet? (${(err && err.message) || err})`
    );
  }

  let data;
  try {
    data = await response.json();
  } catch {
    return fail(`Hub at ${parsed.host}:${parsed.port} returned an unreadable response (HTTP ${response.status}).`);
  }

  if (!response.ok || !data || !data.ok) {
    return fail((data && data.error) || `Pairing failed (HTTP ${response.status}).`);
  }

  const hubUrl = buildDeviceWsUrl(parsed.host, parsed.port);
  await persistConfigAndPoll(hubUrl, data.token || "", CONFIG_SOURCE_PAIRED, { auto });
  return true;
}

// ---------------------------------------------------------------------------
// Zero-copy-paste auto-discovery -- see this file's module docstring.
// ---------------------------------------------------------------------------

function setAutoStatus(text, { showRetry = false } = {}) {
  const pairAutoStatusEl = document.getElementById("pair-auto-status");
  const pairRetryButton = document.getElementById("pair-retry");
  if (pairAutoStatusEl) pairAutoStatusEl.textContent = text;
  if (pairRetryButton) pairRetryButton.style.display = showRetry ? "" : "none";
}

/**
 * Rung 1: scan every open tab for a redeemable, origin-checked pairing code.
 * Never throws -- `chrome.tabs.query` failing (should not happen given this
 * extension's `tabs` permission, but defensive regardless) is treated as
 * "found nothing," not a fatal error. Rejected candidates (see
 * pair_discovery.mjs) are logged quietly, never surfaced as a user warning --
 * a hostile or malformed tab is not something the user did wrong.
 *
 * @returns {Promise<string|null>}
 */
async function discoverFromTabs() {
  let tabs;
  try {
    tabs = await chrome.tabs.query({});
  } catch (err) {
    console.debug("pairing auto-discovery: chrome.tabs.query failed", err);
    return null;
  }
  const { candidate, rejected } = discoverPairingCandidate(tabs);
  for (const r of rejected) {
    console.debug(`pairing auto-discovery: rejected tab ${r.tabId} (${r.url}): ${r.reason}`);
  }
  return candidate ? candidate.code : null;
}

/**
 * Rung 2: read a pairing code straight out of the system clipboard, if the
 * `clipboardRead` permission is granted and the API is usable right now.
 * `navigator.clipboard.readText()` can reject for reasons that have nothing
 * to do with whether a code is actually there -- no permission, the
 * document not focused, or (on browsers stricter than Chromium's own
 * extension-permission model) no transient user activation -- all of which
 * this treats identically as "nothing found," never a user-facing error.
 * The retry button below supplies a real user gesture for the stricter case.
 *
 * @returns {Promise<string|null>}
 */
async function discoverFromClipboard() {
  if (typeof navigator === "undefined" || !navigator.clipboard || typeof navigator.clipboard.readText !== "function") {
    return null;
  }
  let text;
  try {
    text = await navigator.clipboard.readText();
  } catch (err) {
    console.debug("pairing auto-discovery: clipboard read unavailable (permission/focus/gesture)", err);
    return null;
  }
  const parsed = parsePairingCode(text);
  return parsed.valid ? text.trim() : null;
}

async function tryAutoRedeem(rawCode, foundMessage) {
  setAutoStatus(`${foundMessage} Connecting...`);
  const ok = await redeemCode(rawCode, {
    auto: true,
    onError: (message) => setAutoStatus(`Found a pairing code, but couldn't use it: ${message}`, { showRetry: true }),
  });
  if (ok) setAutoStatus(`${foundMessage} Paired automatically.`);
}

/**
 * The full ladder, in order: an open pairing tab, then the clipboard, then
 * (if neither worked) a "Check again" button -- the last, always-available
 * rung is the "Enter a code by hand" details already in options.html.
 * Skipped entirely once this device is already configured (see this file's
 * module docstring) and skipped in tests via the same test flag the status
 * poll uses below.
 */
async function runPairingDiscovery() {
  const stored = await chrome.storage.local.get(SETUP_COMPLETED_STORAGE_KEY);
  if (stored[SETUP_COMPLETED_STORAGE_KEY]) {
    setAutoStatus("Already paired. Pairing again below replaces the current connection.");
    return;
  }

  setAutoStatus("Looking for a pairing code...");

  const tabCode = await discoverFromTabs();
  if (tabCode) return tryAutoRedeem(tabCode, "Found an open pairing tab.");

  const clipCode = await discoverFromClipboard();
  if (clipCode) return tryAutoRedeem(clipCode, "Found a pairing code in your clipboard.");

  setAutoStatus("No pairing code found nearby.", { showRetry: true });
}

// Auto-run on real page load, skipped when options.test.mjs sets this flag before
// importing -- tests drive queryStatusOnce/pollStatusUntilKnown/runPairingDiscovery
// directly with their own fake chrome.*/deterministic inputs instead of the real ones.
if (!globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__) {
  pollStatusUntilKnown(LOAD_STATUS_POLL_DELAYS_MS);
}

// Exported for extension/options.test.mjs only -- not used by any other runtime file.
export {
  loadCurrentValues,
  queryStatusOnce,
  renderLadder,
  renderUnknown,
  pollStatusUntilKnown,
  getOrCreateDeviceId,
  runPairingDiscovery,
  redeemCode,
  disconnect,
  hostPortFromHubUrl,
  LOAD_STATUS_POLL_DELAYS_MS,
  SAVE_STATUS_POLL_DELAYS_MS,
  PAIRED_AUTO_STORAGE_KEY,
};
