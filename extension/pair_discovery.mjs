// pair_discovery.mjs -- pure logic for the zero-copy-paste pairing ladder's first
// two rungs (options.js): find an already-open `/setup#pair=...` tab, or a
// pairing code sitting in the clipboard, without the user typing or pasting
// anything. ZERO chrome.* usage, unit-tested directly with `node --test`
// (pair_discovery.test.mjs) -- same pattern pairing_code.mjs/config_validate.mjs
// already established.
//
// ## SECURITY -- read this before touching the origin check below
//
// A pairing code names a host it should be redeemed against (`ticket@host:port`
// -- see pairing_code.mjs). The extension holds the `tabs` permission, so it CAN
// read every open tab's URL, including the fragment -- a real, already-open
// `/setup#pair=...` tab is exactly what this module looks for. But if this
// module trusted the host:port EMBEDDED IN THE CODE STRING found in some tab's
// URL, a hostile page could plant its own fragment -- `#pair=X@attacker-host:1234`
// -- on any page it controls, and this module would happily instruct the
// extension to POST a redemption request to attacker-host and adopt whatever
// hub URL/token it hands back. That is a full browser takeover: the extension
// would end up configured to obey commands from attacker-host, not the hub the
// user actually meant to pair with.
//
// The fix is structural, not a denylist of bad hosts: the real onboarding page
// IS SERVED BY the hub it advertises (hub.py's `GET /setup` route embeds ITS
// OWN host:port into both the page URL and the `#pair=` fragment it hands out --
// see onboarding.py and cli.py's `_setup_pair_url`). So `discoverPairingCandidate`
// never trusts the code string's host:port on its own -- it also parses the
// TAB'S OWN URL (via the standard `URL` parser, which the extension's `tabs`
// permission can read regardless of which page is active) and requires the two
// to match exactly. A page hosted at evil.com can put any text it wants after
// `#`, but it cannot make its OWN url's origin equal `100.124.126.19:8900` --
// that is simply not where evil.com is served from. A tab whose code claims a
// host it isn't actually served from is rejected, every time, with a reason
// (for debugging/audit) -- never redeemed.
//
// This is deliberately quiet, not alarming: a tab that fails this check is
// almost always just "an ordinary tab that happens to have a `#pair=` looking
// fragment" or, at worst, a page an attacker hoped would be auto-trusted -- not
// something the user did wrong. See options.js for how rejections are logged
// (console.debug, never a user-facing warning) versus how a genuine candidate
// is surfaced.

import { parsePairingCode } from "./pairing_code.mjs";

/**
 * @typedef {Object} PairingCandidate
 * @property {number} tabId
 * @property {string} code - the raw `ticket@host:port` string, ready for parsePairingCode.
 * @property {string} host
 * @property {number} port
 */

/**
 * @typedef {Object} RejectedTab
 * @property {number|undefined} tabId
 * @property {string} url
 * @property {string} reason
 */

/**
 * @typedef {Object} DiscoveryResult
 * @property {PairingCandidate|null} candidate
 * @property {RejectedTab[]} rejected
 */

/**
 * Extract a `pair` fragment field from a tab URL string, if present. Mirrors
 * onboarding.py's inline `_PAIR_SCRIPT`, which reads `location.hash` the same
 * way client-side on the `/setup` page itself.
 *
 * @param {string} url
 * @returns {string|null}
 */
export function extractPairFragment(url) {
  const hashIndex = url.indexOf("#");
  if (hashIndex === -1) return null;
  const hash = url.slice(hashIndex + 1);
  if (!hash) return null;
  const params = new URLSearchParams(hash);
  return params.get("pair");
}

/**
 * Parse a tab URL's OWN origin (hostname + port) via the standard `URL`
 * parser -- the only source of truth this module uses for "what host is this
 * tab really served from." Never derived from the code string itself (see
 * module docstring's SECURITY section).
 *
 * @param {string} url
 * @returns {{hostname: string, port: string}|null}
 */
export function tabOrigin(url) {
  try {
    const parsed = new URL(url);
    // Real hub-issued setup links always carry an explicit port (see
    // addressing.py/cli.py) -- the default-port fallback here is defensive,
    // not a shape this project's own links ever take.
    const port = parsed.port || (parsed.protocol === "https:" ? "443" : "80");
    return { hostname: parsed.hostname, port };
  } catch {
    return null;
  }
}

/**
 * Scan a list of `chrome.tabs.query({})` results for one carrying a
 * REDEEMABLE pairing code: a well-formed `#pair=ticket@host:port` fragment
 * whose host:port matches the tab's OWN origin. Returns the first match (tab
 * order from `chrome.tabs.query` is not meaningful here -- there is normally
 * at most one live pairing tab open) plus every tab that looked like a
 * candidate but was rejected, with a reason.
 *
 * @param {Array<{id?: number, url?: string}>} tabs
 * @returns {DiscoveryResult}
 */
export function discoverPairingCandidate(tabs) {
  const rejected = [];
  for (const tab of tabs || []) {
    if (!tab || typeof tab.url !== "string" || !tab.url) continue;
    const fragment = extractPairFragment(tab.url);
    if (!fragment) continue; // not a pairing link at all -- not worth logging

    const parsed = parsePairingCode(fragment);
    if (!parsed.valid) {
      rejected.push({ tabId: tab.id, url: tab.url, reason: `unparseable pairing code (${parsed.error})` });
      continue;
    }

    const origin = tabOrigin(tab.url);
    if (!origin) {
      rejected.push({ tabId: tab.id, url: tab.url, reason: "could not parse the tab's own URL" });
      continue;
    }

    // The core security check -- see module docstring. The code's host:port
    // must match where the TAB ITSELF is actually served from.
    if (origin.hostname !== parsed.host || String(origin.port) !== String(parsed.port)) {
      rejected.push({
        tabId: tab.id,
        url: tab.url,
        reason:
          `origin mismatch: this tab is served from ${origin.hostname}:${origin.port}, but its ` +
          `pairing code claims ${parsed.host}:${parsed.port} -- refusing to redeem against a host ` +
          "this page cannot prove it speaks for.",
      });
      continue;
    }

    return {
      candidate: {
        tabId: tab.id,
        code: `${parsed.ticket}@${parsed.host}:${parsed.port}`,
        host: parsed.host,
        port: parsed.port,
      },
      rejected,
    };
  }
  return { candidate: null, rejected };
}
