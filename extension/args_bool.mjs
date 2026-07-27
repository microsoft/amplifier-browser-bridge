// args_bool.mjs -- tolerant boolean coercion for command args, shared by every
// call site in this codebase that interprets a caller-supplied arg as boolean
// intent. Deliberately has ZERO chrome.* usage so it can be unit-tested
// directly with plain `node --test` (see args_bool.test.mjs), the same
// pattern frame_refs.mjs/combine_frames.mjs/fetch_utils.mjs established.
//
// See src/amplifier_browser_bridge/args_bool.py -- the Python-side twin,
// kept in sync manually (same discipline as protocol.py/background.js).
//
// Real-world finding that motivated pulling this into ONE shared function:
// background.js's `wantsAllFrames()`/`wantsWake()` each independently
// reinvented the same `=== true || === "true" || === 1` check, while the
// HUB's CDP-escalation check (cdp.py's `requires_cdp`) used a STRICT `is
// True` identity check instead -- exactly the asymmetry that let `amplifier-browser-bridge cmd
// <target> screenshot --arg capture_hidden=true` (the CLI's escape hatch,
// which always sends STRING args) silently fail to escalate to CDP: the
// hub never saw a recognized "true", so it never set `_cdp` on the
// command, and the device's screenshot() failed loud with "requires the
// target tab to already be active" -- despite the caller passing exactly
// the flag meant to prevent that.
//
// `tabOpen()`'s prior `active: !!args.active` had a related, more severe
// bug: `!!` treats ANY non-empty string as true, so `--arg active=false`
// (a legitimate escape-hatch request to open a BACKGROUND tab) was silently
// treated as `active: true`. truthy() fixes this too.

/**
 * Coerce a caller-supplied arg value to a bool, tolerant of the shapes it can
 * arrive in (real boolean, numeric 1, or the strings "true"/"1",
 * case-insensitive, surrounding whitespace ignored). Anything else --
 * including `undefined`, `null`, `"false"`, `0`, or an unrecognized string --
 * is `false`. Mirrors args_bool.py's `truthy()` exactly.
 *
 * @param {*} value
 * @returns {boolean}
 */
export function truthy(value) {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value === 1;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    return normalized === "true" || normalized === "1";
  }
  return false;
}
