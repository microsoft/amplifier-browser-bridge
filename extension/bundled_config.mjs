// bundled_config.mjs -- pure decision logic for adopting a build-time-BAKED hub
// URL/token as a FIRST-RUN DEFAULT, never an override. ZERO chrome.* usage, so it
// can be unit-tested directly with `node --test` (bundled_config.test.mjs), the same
// pattern config_validate.mjs/args_bool.mjs already established.
//
// Why this exists
// ----------------
// docs/ANDROID.md used to state plainly that configuration "lives only in this
// browser's local extension storage" and that packaging "no longer bakes in a URL."
// That was correct for Desktop, where the toolbar icon reliably reaches
// options.html (chrome.runtime.openOptionsPage() works there). On Edge Android that
// same click does nothing usable (2026-08 field report), and there is no way to
// type a 32-character extension ID by hand to reach
// chrome-extension://<id>/options.html directly, nor can a normal web page link to
// it without web_accessible_resources (unverified on Android -- see manifest.android.json
// and docs/ANDROID.md). Without some other channel, a fresh Android sideload has NO
// reachable path to enter a hub URL/token at all -- not an inconvenience, a dead end.
//
// The fix: scripts/package-android.sh bakes the hub URL + token it reads (read-only)
// from the same places the hub itself reads them into bundled_config.json, written
// ONLY inside that script's temporary staging directory -- never into this tracked
// extension/ source tree (see that script's own comments and SECURITY.md's "Baked-in
// credential" section). background.js fetches that file at startup (a same-extension
// resource fetch via chrome.runtime.getURL() -- no web_accessible_resources needed
// for that; see background.js's "Bundled first-run config" section) and hands both
// the fetched bundle and the current storage state to resolveBundledConfigAdoption()
// below to decide whether to adopt it.
//
// The one invariant this module exists to protect
// -------------------------------------------------
// A bundled value is a FIRST-RUN DEFAULT, never an override. The moment ANY real
// setup has happened for this install -- current-key config already present, an
// old (pre-rename) config still sitting under the legacy key names (a real
// pre-existing install mid-migration, not a fresh one -- see MIGRATION.md), or a
// prior bundled-config adoption already recorded via `setupCompleted` -- a baked
// value must never be written again, even by a later rebuild/reinstall of the SAME
// extension ID carrying a different (e.g. rotated) token. This is the direct JS-side
// analogue of setup.py's `ensure_token_file`, which never regenerates (and clobbers)
// an existing token without `--force`: an update must never destroy a working config.

import { validateHubUrl } from "./config_validate.mjs";

/** Storage-independent source tags -- shared between background.js (writer) and
 * options.js (reader/writer) so the two never drift out of agreement on the two
 * valid values. Declared up top since resolveBundledConfigAdoption's caller and
 * describeConfigProvenance below both reference them. */
export const CONFIG_SOURCE_BUNDLED = "bundled";
export const CONFIG_SOURCE_MANUAL = "manual";
/** Set by options.js's "Pair with a hub" flow (pairing_code.mjs) once a pairing
 * code has been redeemed for a real hub URL + per-device token -- distinct from
 * CONFIG_SOURCE_MANUAL (hand-typed into the Manual configuration fields) purely
 * for provenance display (describeConfigProvenance below); every write-once/
 * never-re-adopt guarantee that applies to CONFIG_SOURCE_MANUAL applies equally
 * here (see options.js's Save/Pair handlers, which both set
 * amplifier_browser_bridge_setup_completed). */
export const CONFIG_SOURCE_PAIRED = "paired";

/** The extension-relative resource path background.js fetches at startup. Exported
 * so tests (and, if ever needed, other modules) never have to re-type this literal. */
export const BUNDLED_CONFIG_RESOURCE = "bundled_config.json";

/**
 * @typedef {Object} StoredConfigState
 * @property {string|null|undefined} hubUrl - current amplifier_browser_bridge_hub_url value, if any.
 * @property {string|null|undefined} hubToken - current amplifier_browser_bridge_hub_token value, if any.
 * @property {string|null|undefined} legacyHubUrl - old (pre-rename) abb_hub_url value, if any.
 * @property {string|null|undefined} legacyHubToken - old (pre-rename) abb_hub_token value, if any.
 * @property {boolean} setupCompleted - true once this install has EVER completed setup,
 *   by any path (a prior bundled-config adoption, or a manual Save on the options page).
 *   Guards the edge case where a user deliberately clears their stored hub_url/hub_token
 *   (wanting to be unconfigured) -- without this flag, the next startup would look
 *   identical to "never configured" and silently re-adopt the bundled default.
 */

/**
 * @typedef {Object} BundledConfig
 * @property {string} hubUrl - as baked by scripts/package-android.sh; validated before use.
 * @property {string} [hubToken] - may be "" for a deliberate `--allow-no-token` dev build.
 * @property {string} [generatedAt] - ISO-8601 UTC timestamp, for display/diagnosis only.
 */

/**
 * @typedef {Object} AdoptionDecision
 * @property {string} hubUrl - validated + normalized (see config_validate.mjs).
 * @property {string} hubToken
 * @property {string|null} generatedAt
 */

/**
 * Decide whether background.js should adopt a build-time-baked hub URL/token into
 * chrome.storage.local. Returns `null` ("do nothing -- leave storage exactly as it
 * is") in every case except the one true first-run scenario: this install has NEVER
 * been configured, under ANY key name, by ANY path, AND a structurally valid bundled
 * config is present.
 *
 * @param {StoredConfigState} stored
 * @param {BundledConfig|null|undefined} bundled
 * @returns {AdoptionDecision|null}
 */
export function resolveBundledConfigAdoption(stored, bundled) {
  if (!bundled) return null; // no bundled_config.json shipped (desktop build never generates one,
  // or the fetch/parse failed -- see background.js's fetchBundledConfig) -- nothing to adopt.

  if (!stored || stored.setupCompleted) return null; // this install already made its one setup
  // decision -- never re-adopt, even across a rebuild that bakes in a different (e.g. rotated)
  // token. See module docstring's "one invariant" section.

  if (stored.hubUrl || stored.hubToken) return null; // already configured under the current key names.

  if (stored.legacyHubUrl || stored.legacyHubToken) return null; // configured under the OLD
  // (pre-rename) key names -- a real pre-existing install mid-migration (MIGRATION.md), not a
  // fresh one. Never paper over that with a baked default; the existing legacy-config UX
  // (background.js's legacyConfigDetected / options.js's warning) still applies unchanged.

  const validation = validateHubUrl(bundled.hubUrl);
  if (!validation.valid) return null; // a malformed bundle is a BUILD defect, never adopted --
  // fail closed here; background.js's caller distinguishes this from "no bundle shipped" and
  // logs a warning so a broken build is loud, not silently inert.

  return {
    hubUrl: validation.normalized,
    hubToken: typeof bundled.hubToken === "string" ? bundled.hubToken : "",
    generatedAt: typeof bundled.generatedAt === "string" ? bundled.generatedAt : null,
  };
}

/**
 * Human-readable provenance line for the options page -- the answer to "how can the
 * user tell where these values came from?" Silent magic (a field that's simply
 * pre-filled with no explanation) is exactly how someone ends up debugging a stale
 * baked token for an hour with no idea it was never typed by a human. Never
 * fabricates a claim: returns `null` only when there is genuinely nothing to say yet
 * (this install has no recorded config source at all).
 *
 * @param {{configSource?: string|null, configBundledAt?: string|null}} [state]
 * @returns {string|null}
 */
export function describeConfigProvenance(state) {
  const configSource = state && state.configSource;
  const configBundledAt = state && state.configBundledAt;
  if (configSource === CONFIG_SOURCE_BUNDLED) {
    const when = configBundledAt ? ` (baked at ${configBundledAt})` : "";
    return (
      `These values arrived bundled with this install${when} -- they were not typed by hand. ` +
      "Edit and click Save to make them your own; once you do, this bundled default is never " +
      "re-applied, even if you reinstall a rebuilt version of this extension."
    );
  }
  if (configSource === CONFIG_SOURCE_MANUAL) {
    return "These values were entered manually on this options page.";
  }
  if (configSource === CONFIG_SOURCE_PAIRED) {
    return (
      "These values were obtained by pairing with a hub -- the hub URL and token were fetched " +
      "automatically from a single pairing code, never typed or copied by hand."
    );
  }
  return null;
}
