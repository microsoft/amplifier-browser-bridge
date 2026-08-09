// pairing_code.mjs -- pure parse/validate logic for the "Pair with a hub" flow
// (options.js). ZERO chrome.* usage, unit-tested directly with `node --test`
// (pairing_code.test.mjs) -- same pattern config_validate.mjs/bundled_config.mjs
// already established.
//
// Why this exists at all: replacing the raw-paste step (a bare ws:// URL with an
// IP literal, plus a separate 32-char hex token, each hand-typed into its own
// field) with a SINGLE pairing code the operator reads off `amplifier-browser-bridge
// pair`'s terminal output and enters once. See src/amplifier_browser_bridge/pairing.py's
// module docstring for the server-side ticket design (entropy/lifetime/threat
// model) this code redeems against.
//
// Code shape: `<ticket>@<host>:<port>` -- e.g. "7F3K9-QXTM2@100.124.126.19:8900".
// The ticket half may be typed with or without its cosmetic "AAAAA-BBBBB"
// grouping dash, and in either case. Deliberately NOT a `ws://`/URL-shaped
// string: this is not something dialed as a URL by this parser (options.js
// builds the actual `ws://host:port/device` URL separately, via
// config_validate.mjs's existing validateHubUrl, once the host/port here are
// known to be well-formed) -- keeping this parser's job to exactly one thing
// (split three fields out of one string) keeps it trivially testable.

/**
 * @typedef {Object} PairingCodeParseResult
 * @property {boolean} valid
 * @property {string|null} ticket - normalized (uppercased, dash-stripped) ticket, only set when valid.
 * @property {string|null} host - hostname/IP literal, only set when valid.
 * @property {number|null} port - only set when valid.
 * @property {string|null} error - human-readable reason, only set when NOT valid.
 */

// Ticket half: letters/digits only (dashes are cosmetic grouping, stripped before
// this regex ever sees them) -- deliberately permissive about length/alphabet
// here (the HUB is the sole authority on whether a ticket is valid at all; this
// parser's only job is "did the operator paste something shaped like a pairing
// code," not "is this ticket real"). A too-short/too-long/wrong-alphabet ticket
// still parses fine here and simply gets rejected by the hub's /pair/redeem with
// an honest "unknown or already-used pairing code" -- one error surface, not two.
const _PAIRING_CODE_RE = /^([A-Za-z0-9-]{4,32})@([^\s@:]+):(\d{1,5})$/;

/**
 * Parse a pairing code as entered on the options page.
 *
 * @param {string|null|undefined} raw
 * @returns {PairingCodeParseResult}
 */
export function parsePairingCode(raw) {
  if (typeof raw !== "string" || raw.trim() === "") {
    return {
      valid: false,
      ticket: null,
      host: null,
      port: null,
      error: "Pairing code is required (from `amplifier-browser-bridge pair`, e.g. 7F3K9-QXTM2@100.x.y.z:8900).",
    };
  }
  const trimmed = raw.trim();
  const match = _PAIRING_CODE_RE.exec(trimmed);
  if (!match) {
    return {
      valid: false,
      ticket: null,
      host: null,
      port: null,
      error: `Not a valid pairing code: "${trimmed}". Expected the exact text \`amplifier-browser-bridge pair\` printed, e.g. 7F3K9-QXTM2@100.x.y.z:8900.`,
    };
  }
  const [, rawTicket, host, rawPort] = match;
  const port = Number.parseInt(rawPort, 10);
  if (port < 1 || port > 65535) {
    return {
      valid: false,
      ticket: null,
      host: null,
      port: null,
      error: `Pairing code has an out-of-range port: ${rawPort}.`,
    };
  }
  const ticket = rawTicket.toUpperCase().replace(/-/g, "");
  return { valid: true, ticket, host, port, error: null };
}

/**
 * Build the `ws://host:port/device` URL the extension should store once a
 * pairing code's host/port have been extracted -- kept here (rather than
 * string-templated inline in options.js) so the exact shape used to compose it
 * has one home, matching the "one canonical place per concern" discipline this
 * file's siblings already follow.
 *
 * @param {string} host
 * @param {number} port
 * @returns {string}
 */
export function buildDeviceWsUrl(host, port) {
  return `ws://${host}:${port}/device`;
}

/**
 * Build the `http://host:port/pair/redeem` URL to POST a redemption request to.
 * Deliberately plain `http://`, matching this project's existing posture of never
 * layering TLS onto tailnet traffic (see hub.py/pairing.py -- the tailnet itself
 * is the transport-security boundary here, not TLS).
 *
 * @param {string} host
 * @param {number} port
 * @returns {string}
 */
export function buildRedeemUrl(host, port) {
  return `http://${host}:${port}/pair/redeem`;
}
