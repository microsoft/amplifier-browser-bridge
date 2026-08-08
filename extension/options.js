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

import { validateHubUrl, validateHubToken } from "./config_validate.mjs";
import { describeConfigProvenance, CONFIG_SOURCE_MANUAL } from "./bundled_config.mjs";

const urlInput = document.getElementById("hub-url");
const tokenInput = document.getElementById("hub-token");
const toggleTokenLink = document.getElementById("toggle-token");
const errorEl = document.getElementById("error");
const statusEl = document.getElementById("status");
const saveButton = document.getElementById("save");
const provenanceEl = document.getElementById("provenance");

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
  urlInput.value = stored.amplifier_browser_bridge_hub_url || "";
  tokenInput.value = stored.amplifier_browser_bridge_hub_token || "";
  renderProvenance({
    configSource: stored.amplifier_browser_bridge_config_source,
    configBundledAt: stored.amplifier_browser_bridge_config_bundled_at,
  });
}

// Sends the status query exactly once. NEVER throws and NEVER returns a bare value the
// caller could confuse with a real answer -- always `{ ok: true, response }` (a real
// answer arrived) or `{ ok: false, error }` (it didn't, for whatever reason: the
// background script isn't running, sendMessage rejected, or it resolved with nothing).
// This is the single chokepoint between "did we hear back" and "what does it mean",
// so every caller gets an explicit, un-ignorable failure signal instead of a value
// that's merely falsy.
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
function renderStatus(response) {
  if (!response.configured) {
    if (response.legacyConfigDetected) {
      // Distinct from the generic "never configured" message: this install HAD a working
      // config under the old (pre-rename) storage keys, which are no longer read. See
      // background.js's loadConfig()/legacyConfigDetected and MIGRATION.md.
      statusEl.className = "warn";
      statusEl.textContent =
        "Configuration key names changed in this version -- your previous Hub URL/token are " +
        "no longer read. Re-enter them below and click Save.";
    } else {
      statusEl.className = "warn";
      statusEl.textContent = "Not configured -- enter a Hub URL below and click Save.";
    }
    return;
  }
  if (response.connected) {
    statusEl.className = "ok";
    statusEl.textContent = `Connected to ${response.hubUrl} as device ${response.deviceId || "(pending)"}.`;
  } else {
    statusEl.className = "warn";
    statusEl.textContent =
      `Configured for ${response.hubUrl}, but not currently connected -- ` +
      "is the hub running and reachable? Run `amplifier-browser-bridge doctor` from the CLI for a full check.";
  }
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

toggleTokenLink.addEventListener("click", () => {
  const showing = tokenInput.type === "text";
  tokenInput.type = showing ? "password" : "text";
  toggleTokenLink.textContent = showing ? "show" : "hide";
});

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

  await chrome.storage.local.set({
    amplifier_browser_bridge_hub_url: urlValidation.normalized,
    amplifier_browser_bridge_hub_token: tokenInput.value,
    // A deliberate Save is the user affirmatively taking ownership of these values --
    // whether they were blank, hand-typed from scratch, or started out pre-filled from a
    // bundled first-run default (see bundled_config.mjs). From this point on the config
    // is "manual": setup_completed blocks any future bundled-config adoption from ever
    // overwriting it again, even across a rebuild carrying a different baked token.
    amplifier_browser_bridge_config_source: CONFIG_SOURCE_MANUAL,
    amplifier_browser_bridge_config_bundled_at: null,
    amplifier_browser_bridge_setup_completed: true,
  });
  renderProvenance({ configSource: CONFIG_SOURCE_MANUAL, configBundledAt: null });

  statusEl.className = "unknown";
  statusEl.textContent = "Saved. Connecting...";
  // storage.onChanged (background.js) reconnects immediately; pollStatusUntilKnown
  // either catches that success or -- if it never arrives -- lands on the honest
  // "couldn't determine status" state instead of leaving this line up forever.
  pollStatusUntilKnown(SAVE_STATUS_POLL_DELAYS_MS);
});

// Auto-run on real page load, skipped when options.test.mjs sets this flag before
// importing -- tests drive queryStatusOnce/pollStatusUntilKnown directly with their
// own fast, deterministic delay schedules instead of waiting out the real one.
if (!globalThis.__AMPLIFIER_BROWSER_BRIDGE_OPTIONS_TEST__) {
  loadCurrentValues();
  pollStatusUntilKnown(LOAD_STATUS_POLL_DELAYS_MS);
}

// Exported for extension/options.test.mjs only -- not used by any other runtime file.
export {
  loadCurrentValues,
  queryStatusOnce,
  renderStatus,
  renderUnknown,
  pollStatusUntilKnown,
  LOAD_STATUS_POLL_DELAYS_MS,
  SAVE_STATUS_POLL_DELAYS_MS,
};
