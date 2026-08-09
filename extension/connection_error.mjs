// connection_error.mjs -- pure classification of "why isn't the hub connection
// up right now" into one of a small, closed set of reason codes background.js
// can report over amplifier_browser_bridge_get_status and options.js/popup.js can
// render distinct, actionable text for. ZERO chrome.*/WebSocket usage, unit-tested
// directly with `node --test` (connection_error.test.mjs) -- same pattern
// config_validate.mjs/bundled_config.mjs already established.
//
// Why this exists (craft-inspector / human-advocate findings, both councils):
// before this module, options.js's status line could only ever say "Connected."
// or a single generic "Configured, but not currently connected -- is the hub
// running?" -- collapsing "the hub rejected this device's token" and "nothing at
// this address is listening" into the exact same sentence. A user staring at
// that page had no way to tell which of those two entirely different problems
// (with entirely different fixes) they were looking at, and no error text at all
// identified WHICH failure occurred -- a WCAG 3.3.1 (Error Identification) gap.
// This module is the single place that turns the two real signals background.js
// already has (an explicit `{"type":"error",...}` frame from the hub, and the
// WebSocket `close` event's own `code`) into one of a closed set of reason codes.

/** @typedef {"auth_rejected"|"unreachable"|"hub_error"} ConnectionErrorCode */

/**
 * @typedef {Object} ConnectionError
 * @property {ConnectionErrorCode} code
 * @property {string} message - human-readable, safe to render directly.
 * @property {number} at - `Date.now()` when this was classified.
 */

/**
 * Classify an explicit `{"type": "error", "error": "..."}` frame the hub sent
 * BEFORE closing the connection (see hub.py's `_handle_device_message`'s `hello`
 * branch: it sends this exact frame, then closes, on a bad token). This is
 * always the MORE specific signal when available -- a close event alone cannot
 * distinguish "the hub actively rejected us" from "the socket never reached the
 * hub at all," but an explicit error frame proves the hub was reached and chose
 * to refuse.
 *
 * @param {{error?: unknown}} msg - a parsed device-protocol message with type "error".
 * @param {number} [now]
 * @returns {ConnectionError}
 */
export function classifyHubErrorMessage(msg, now = Date.now()) {
  const text = msg && typeof msg.error === "string" ? msg.error : "unknown error";
  // hub.py's auth.py-backed token check sends exactly this literal string --
  // see hub.py's `_handle_device_message`'s `hello` branch and `_handle_agent_ws`'s
  // token check. Matched verbatim rather than loosely (e.g. substring/regex) so a
  // future, differently-worded hub error is never misclassified as this one.
  if (text === "unauthorized") {
    return {
      code: "auth_rejected",
      message:
        "The hub rejected this device's token. Re-pair (`amplifier-browser-bridge pair`, then " +
        "\"Pair with a hub\" below) for a fresh token, or check the Manual configuration token " +
        "against the hub's token file -- `amplifier-browser-bridge doctor` on the hub host names " +
        "the exact mismatch.",
      at: now,
    };
  }
  return {
    code: "hub_error",
    message: `The hub returned an error: ${text}`,
    at: now,
  };
}

/**
 * Classify a WebSocket `close` event that arrived WITHOUT any preceding explicit
 * error frame -- the only signal available is the close event's own `code`/
 * `reason`. Per the WHATWG WebSocket spec, code 1006 ("abnormal closure") is
 * reported precisely when the connection never completed its handshake at all --
 * i.e. nothing at that host:port ever answered. Any other code without a prior
 * explicit error message is honestly reported as "closed" rather than guessed at.
 *
 * @param {{code?: number, reason?: string}} event
 * @param {number} [now]
 * @returns {ConnectionError}
 */
export function classifyCloseEvent(event, now = Date.now()) {
  const code = event && typeof event.code === "number" ? event.code : null;
  const reason = event && typeof event.reason === "string" && event.reason ? event.reason : null;
  if (code === 1006) {
    return {
      code: "unreachable",
      message:
        "Could not reach the hub -- the connection never completed its handshake (close code 1006). " +
        "Is `amplifier-browser-bridge hub` (or the service) running at this address, and is this " +
        "device on the same tailnet? Run `amplifier-browser-bridge doctor` on the hub host for a full check.",
      at: now,
    };
  }
  return {
    code: "unreachable",
    message: `Connection closed${reason ? `: ${reason}` : ""} (code ${code ?? "unknown"}) before completing setup.`,
    at: now,
  };
}

/**
 * Human-readable next-step text for a badge/tooltip, keyed off the same reason
 * codes -- kept short (tooltips truncate), unlike the options page's full message.
 *
 * @param {ConnectionErrorCode} code
 * @returns {string}
 */
export function badgeTitleForErrorCode(code) {
  if (code === "auth_rejected") {
    return "Amplifier Browser Bridge: hub rejected this device's token -- click the toolbar icon to re-pair.";
  }
  if (code === "unreachable") {
    return "Amplifier Browser Bridge: could not reach the hub -- click the toolbar icon for details.";
  }
  return "Amplifier Browser Bridge: connection error -- click the toolbar icon for options.";
}
