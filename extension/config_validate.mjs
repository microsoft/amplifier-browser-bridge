// config_validate.mjs -- pure validation for the extension's runtime hub configuration
// (hub URL + shared token), read from chrome.storage.local (see options.js/background.js).
// ZERO chrome.* usage so it can be unit-tested directly with `node --test` (see
// config_validate.test.mjs), the same pattern args_bool.mjs/frame_refs.mjs established.
//
// Why this exists at all: the extension used to ship a real-looking credential
// (extension/config.js's HUB_URL/HUB_TOKEN) as tracked source that a user had to hand-edit,
// and that every `git pull`/file-copy update silently clobbered. Configuration now lives in
// chrome.storage.local (per-install, survives file updates -- see options.js), entered once
// through the options page. This module is the shared validation logic between that options
// page (validate on save) and background.js (validate on load, so a corrupted/partial
// storage value fails loud instead of feeding a broken URL into `new WebSocket(...)`).

/**
 * @typedef {Object} HubUrlValidation
 * @property {boolean} valid
 * @property {string|null} normalized - the URL with a trailing "/device" path appended if
 *   the caller supplied a bare "ws://host:port" with no path. Only set when valid.
 * @property {string|null} error - human-readable reason, only set when NOT valid.
 */

/**
 * Validate (and lightly normalize) a hub URL as entered on the options page.
 *
 * Deliberately permissive about MagicDNS vs IP literal -- that's a documented operator
 * choice (design doc section 4), not something this validator should block. It only
 * rejects shapes that could never work: missing, non-ws(s) scheme, or no host.
 *
 * @param {string|null|undefined} url
 * @returns {HubUrlValidation}
 */
export function validateHubUrl(url) {
  if (typeof url !== "string" || url.trim() === "") {
    return { valid: false, normalized: null, error: "Hub URL is required (e.g. ws://100.x.y.z:8900/device)." };
  }
  const trimmed = url.trim();

  let parsed;
  try {
    parsed = new URL(trimmed);
  } catch {
    return {
      valid: false,
      normalized: null,
      error: `Not a valid URL: "${trimmed}". Expected something like ws://100.x.y.z:8900/device.`,
    };
  }

  if (parsed.protocol !== "ws:" && parsed.protocol !== "wss:") {
    return {
      valid: false,
      normalized: null,
      error: `Hub URL must use ws:// or wss://, got "${parsed.protocol}//" -- this extension always dials OUT to the hub.`,
    };
  }

  if (!parsed.hostname) {
    return { valid: false, normalized: null, error: "Hub URL is missing a host." };
  }

  // Bare "ws://host:port" (no path) is a common paste mistake -- the device route is
  // "/device" (see docs/PROTOCOL.md). Append it rather than fail loud on something this
  // easy to correct automatically; still validated as a real URL either way.
  if (parsed.pathname === "/" || parsed.pathname === "") {
    parsed.pathname = "/device";
  }

  return { valid: true, normalized: parsed.toString(), error: null };
}

/**
 * Validate a hub token as entered on the options page. A token is optional (a hub may run
 * with auth disabled -- dev-only, loudly logged on the hub side, see auth.py), so an empty
 * value is valid; this only rejects shapes that are never legitimate.
 *
 * @param {string|null|undefined} token
 * @returns {{valid: boolean, error: string|null}}
 */
export function validateHubToken(token) {
  if (token === null || token === undefined || token === "") {
    return { valid: true, error: null };
  }
  if (typeof token !== "string") {
    return { valid: false, error: "Token must be a string." };
  }
  if (token !== token.trim()) {
    return { valid: false, error: "Token has leading/trailing whitespace -- check for a stray copy-paste newline." };
  }
  return { valid: true, error: null };
}

/**
 * True if a stored config object (as read from chrome.storage.local) is usable enough to
 * attempt a connection. Does not guarantee the hub will actually accept the token --
 * only that the extension has something coherent to try. Used by background.js to decide
 * "attempt to connect" vs. "fail loud as not-configured" (see its module docstring).
 *
 * @param {{hubUrl?: string|null, hubToken?: string|null}} stored
 * @returns {boolean}
 */
export function isConfigured(stored) {
  if (!stored) return false;
  return validateHubUrl(stored.hubUrl).valid;
}
