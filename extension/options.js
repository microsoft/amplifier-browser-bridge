// options.js -- the extension's ONLY user-facing UI. Reads/writes the runtime hub
// configuration (Hub URL + token) directly to chrome.storage.local; background.js's
// storage.onChanged listener picks up a save immediately and reconnects. See
// config_validate.mjs for the shared validation logic and background.js's "Runtime
// configuration" section for why this replaced the old tracked extension/config.js.
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
// fields. The Hub URL/Token fields still exist (collapsed under "Manual configuration
// (advanced)" in options.html) for scripting, dev hubs with no running pairing session,
// or an operator who prefers typing values directly -- pairing does not remove that
// capability, it just stops being the default path. See pairing_code.mjs for the code
// parser and src/amplifier_browser_bridge/pairing.py for the server-side ticket design
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
// Connection-status detail (craft-inspector / human-advocate review, 2026-08): the
// status line used to collapse EVERY "configured but not connected" cause into one
// generic sentence -- a hub that's unreachable and a hub that rejected this device's
// token rendered identically, and there was no error text identifying which. That is a
// WCAG 3.3.1 (Error Identification) gap: an error occurred with no error identified.
// `renderStatus` below now branches on `response.lastError.code` (background.js,
// connection_error.mjs) so "hub unreachable", "token rejected", and "connected and
// working" are each their own distinct, actionable message.
//
// Pre-pair state authorship (real-run bug report + craft-inspector/emotion-reader
// review, 2026-08): "not configured yet" is the FIRST thing a brand-new user sees --
// and until this fix it rendered with the identical red `.warn` styling as a genuine
// error (hub unreachable, token rejected). A user who has done nothing wrong yet saw
// what looks like a failure. The fix is a THIRD authored state, `.pending` (calm,
// neutral blue, options.html) -- for "expected, not done yet" rather than "something
// is wrong". It is used here for the fresh-install case and for "configured but no
// lastError yet" (the first moment after Save/Pair, before the hub round trip has had
// time to succeed OR fail -- also expected, also not an error). `.warn` remains
// reserved for states that name an actual, confirmed problem: a stale pre-rename
// config (legacyConfigDetected -- something really did break), and every branch that
// carries a concrete `lastError` code.

import { validateHubUrl, validateHubToken } from "./config_validate.mjs";
import { describeConfigProvenance, CONFIG_SOURCE_MANUAL, CONFIG_SOURCE_PAIRED } from "./bundled_config.mjs";
import { parsePairingCode, buildDeviceWsUrl, buildRedeemUrl } from "./pairing_code.mjs";
import { discoverPairingCandidate } from "./pair_discovery.mjs";

const urlInput = document.getElementById("hub-url");
const tokenInput = document.getElementById("hub-token");
const toggleTokenLink = document.getElementById("toggle-token");
const errorEl = document.getElementById("error");
const statusEl = document.getElementById("status");
const saveButton = document.getElementById("save");
const provenanceEl = document.getElementById("provenance");
const pairCodeInput = document.getElementById("pair-code");
const pairErrorEl = document.getElementById("pair-error");
const pairButton = document.getElementById("pair");
const pairAutoStatusEl = document.getElementById("pair-auto-status");
const pairRetryButton = document.getElementById("pair-retry");

// Storage key mirrored from persistConfigAndPoll below -- checked BEFORE any
// auto-discovery attempt runs. An already-paired device must never be
// silently re-pointed at a different hub just because some `/setup` tab
// happens to be open; auto-discovery is for first-run only. See this file's
// module docstring.
const SETUP_COMPLETED_STORAGE_KEY = "amplifier_browser_bridge_setup_completed";

// Renders the "where did these values come from" line -- see bundled_config.mjs's
// describeConfigProvenance for why this exists: a field silently pre-filled with a baked
// hub URL/token, with no indication it wasn't typed by a human, is exactly how someone
// ends up debugging a stale token for an hour with no idea where it came from.
function renderProvenance({ configSource, configBundledAt }) {
  if (!provenanceEl) return; // defensive -- absent only if options.html and this file drift apart
  const text = describeConfigProvenance({ configSource, configBundledAt });
  provenanceEl.textContent = text || "";
}

async function loadCurrentValues() {
  const stored = await chrome.storage.local.get([
    "amplifier_browser_bridge_hub_url",
    "amplifier_browser_bridge_hub_token",
    "amplifier_browser_bridge_config_source",
    "amplifier_browser_bridge_config_bundled_at",
  ]);
  if (urlInput) urlInput.value = stored.amplifier_browser_bridge_hub_url || "";
  if (tokenInput) tokenInput.value = stored.amplifier_browser_bridge_hub_token || "";
  renderProvenance({
    configSource: stored.amplifier_browser_bridge_config_source,
    configBundledAt: stored.amplifier_browser_bridge_config_bundled_at,
  });
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

// Renders a REAL response. This is the only place allowed to show a "we know the truth"
// status -- every branch here is an honest description of what the background script
// just told us, never an assumption about what it's probably still doing.
//
// Three distinguishable "not connected" causes (craft-inspector / human-advocate
// review, see this file's module docstring): `response.lastError.code` is one of
// "auth_rejected" (the hub rejected this device's token), "unreachable" (nothing
// answered at the configured address), or "hub_error" (the hub returned some other
// error) -- see connection_error.mjs for exactly how background.js derives this.
// `lastError` is `null` when the most recent attempt is still in flight (e.g. right
// after a fresh Save/Pair) -- that case falls through to the pre-existing generic
// message, which remains honest ("not connected yet," not "here's why").
function renderStatus(response) {
  if (!response.configured) {
    if (response.legacyConfigDetected) {
      // Distinct from the generic "never configured" message: this install HAD a working
      // config under the old (pre-rename) storage keys, which are no longer read. See
      // background.js's loadConfig()/legacyConfigDetected and MIGRATION.md. This IS a real
      // problem (something that used to work no longer does) -- .warn is correct here.
      statusEl.className = "warn";
      statusEl.textContent =
        "Configuration key names changed in this version -- your previous Hub URL/token are " +
        "no longer read. Re-enter them below and click Save.";
    } else {
      // Brand-new install, nothing pasted in yet -- expected, not an error. `.pending`
      // (calm/neutral), never `.warn` (red) -- see this file's module docstring.
      statusEl.className = "pending";
      statusEl.textContent = "Not paired yet -- pair with a hub below, or enter a Hub URL and click Save.";
    }
    return;
  }
  if (response.connected) {
    statusEl.className = "ok";
    statusEl.textContent = `Connected to ${response.hubUrl} as device ${response.deviceId || "(pending)"}.`;
    return;
  }

  const lastError = response.lastError;
  if (lastError && lastError.code === "auth_rejected") {
    statusEl.className = "warn";
    statusEl.textContent = lastError.message;
    return;
  }
  if (lastError && lastError.code === "unreachable") {
    statusEl.className = "warn";
    statusEl.textContent = lastError.message;
    return;
  }
  if (lastError && lastError.code === "hub_error") {
    statusEl.className = "warn";
    statusEl.textContent = lastError.message;
    return;
  }

  // Configured, not yet connected, and no concrete error reported yet -- this is the
  // window right after Save/Pair while the first connection attempt is still in
  // flight. Expected and transient, not a confirmed problem -- `.pending`, not `.warn`.
  // (A REAL problem lands in one of the three lastError branches above instead.)
  statusEl.className = "pending";
  statusEl.textContent =
    `Configured for ${response.hubUrl}, connecting... if this doesn't clear, is the hub ` +
    "running and reachable? Run `amplifier-browser-bridge doctor` from the CLI for a full check.";
}

// The honest "we tried, and we still don't know" terminal state -- what a caller lands
// on when every retry in pollStatusUntilKnown's budget failed. This is the answer to
// "what happens when the truth is unavailable": we say so, plainly, with the concrete
// reason, rather than leaving an optimistic string (e.g. "Saved. Connecting...") sitting
// on screen forever pretending to still be in progress.
function renderUnknown(lastError) {
  statusEl.className = "warn";
  const reason = lastError && lastError.message ? ` (${lastError.message})` : "";
  statusEl.textContent =
    `Couldn't determine connection status${reason} -- the extension's background script ` +
    "may not be running. Try reloading this page or the extension (edge://extensions), " +
    "or run `amplifier-browser-bridge doctor` from the CLI for a full check.";
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
      renderStatus(result.response);
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

if (toggleTokenLink) {
  toggleTokenLink.addEventListener("click", () => {
    const showing = tokenInput.type === "text";
    tokenInput.type = showing ? "password" : "text";
    toggleTokenLink.textContent = showing ? "show" : "hide";
  });
}

// Shared by both the Pair and Save handlers: write the resolved hub URL/token to
// storage under one config source, refresh the provenance line, and kick off the
// same post-write status poll -- the two paths differ only in WHERE hubUrl/token
// came from and which CONFIG_SOURCE_* to record.
async function persistConfigAndPoll(hubUrl, token, configSource) {
  await chrome.storage.local.set({
    amplifier_browser_bridge_hub_url: hubUrl,
    amplifier_browser_bridge_hub_token: token,
    amplifier_browser_bridge_config_source: configSource,
    amplifier_browser_bridge_config_bundled_at: null,
    amplifier_browser_bridge_setup_completed: true,
  });
  renderProvenance({ configSource, configBundledAt: null });
  statusEl.className = "unknown";
  statusEl.textContent = configSource === CONFIG_SOURCE_PAIRED ? "Paired. Connecting..." : "Saved. Connecting...";
  // storage.onChanged (background.js) reconnects immediately; pollStatusUntilKnown
  // either catches that success or -- if it never arrives -- lands on the honest
  // "couldn't determine status" state instead of leaving this line up forever.
  pollStatusUntilKnown(SAVE_STATUS_POLL_DELAYS_MS);
}

if (saveButton) {
  saveButton.addEventListener("click", async () => {
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
    // A deliberate Save is the user affirmatively taking ownership of these values --
    // whether they were blank, hand-typed from scratch, or started out pre-filled from a
    // bundled first-run default (see bundled_config.mjs). From this point on the config
    // is "manual": setup_completed blocks any future bundled-config adoption from ever
    // overwriting it again, even across a rebuild carrying a different baked token.
    await persistConfigAndPoll(urlValidation.normalized, tokenInput.value, CONFIG_SOURCE_MANUAL);
  });
}

// ---------------------------------------------------------------------------
// Pairing -- see this file's module docstring and pairing_code.mjs.
//
// `redeemCode` is the ONE place a pairing code is ever turned into a
// configured hub -- the manual "Pair" button, the auto-discovered tab, and
// the clipboard rung all call this same function, so the three paths can
// never drift apart on what counts as success or how an error is worded.
// ---------------------------------------------------------------------------

/**
 * Parse and redeem a pairing code against its hub, persisting the resulting
 * hub URL/token on success. Never throws -- every failure is reported via
 * `onError` (a callback, not an exception) so callers with different UI
 * surfaces (a visible error line vs. a quiet auto-discovery status line) can
 * each render it their own way.
 *
 * @param {string} rawCode
 * @param {{onError?: (message: string) => void}} [opts]
 * @returns {Promise<boolean>} true iff redeemed and persisted.
 */
async function redeemCode(rawCode, { onError } = {}) {
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
  await persistConfigAndPoll(hubUrl, data.token || "", CONFIG_SOURCE_PAIRED);
  return true;
}

if (pairButton) {
  pairButton.addEventListener("click", async () => {
    pairErrorEl.textContent = "";
    pairButton.disabled = true;
    const originalLabel = pairButton.textContent;
    pairButton.textContent = "Pairing...";
    try {
      const ok = await redeemCode(pairCodeInput.value, {
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

// ---------------------------------------------------------------------------
// Zero-copy-paste auto-discovery -- see this file's module docstring.
// ---------------------------------------------------------------------------

function setAutoStatus(text, { showRetry = false } = {}) {
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

// Auto-run on real page load, skipped when options.test.mjs sets this flag before
// importing -- tests drive queryStatusOnce/pollStatusUntilKnown/runPairingDiscovery
// directly with their own fake chrome.*/deterministic inputs instead of the real ones.
if (!globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__) {
  loadCurrentValues();
  pollStatusUntilKnown(LOAD_STATUS_POLL_DELAYS_MS);
  runPairingDiscovery();
}

// Exported for extension/options.test.mjs only -- not used by any other runtime file.
export {
  loadCurrentValues,
  queryStatusOnce,
  renderStatus,
  renderUnknown,
  pollStatusUntilKnown,
  getOrCreateDeviceId,
  runPairingDiscovery,
  LOAD_STATUS_POLL_DELAYS_MS,
  SAVE_STATUS_POLL_DELAYS_MS,
};
