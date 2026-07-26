// effects_collector.mjs -- pure effects accumulation, windowing, and
// state-changing determination. Zero chrome.* usage.
//
// This is the D3 fix (docs/designs/confirmation-gate.md, "attribution first,
// gating second"): background.js registers real chrome.webRequest/webNavigation/
// downloads/tabs listeners scoped to the acting tab around a
// STATE_CHANGING_COMMANDS dispatch, feeds every observed event into an
// EffectsCollector instance via its add*() methods, then calls report() after
// EFFECTS_WINDOW_MS to build the wire-shape `effects` block attached to the
// result envelope.
//
// Carries ZERO site knowledge and ZERO policy (design doc section 14.3): no
// domain names, no category names, no thresholds. This module only accumulates
// and windows raw observations; classification lives entirely in the hub's
// classify.py. Kept shape-compatible by hand with the Python-side twin,
// src/amplifier_browser_bridge/effects.py's EffectsReport -- the same
// keep-in-sync-by-hand discipline CONTRIBUTING.md documents for
// protocol.py/background.js.

const NON_MUTATING_METHODS = new Set(["GET", "HEAD"]);

export class EffectsCollector {
  constructor() {
    this.requests = [];
    this.navigations = [];
    this.downloads = [];
    this.tabsOpened = [];
  }

  addRequest(method, url, type, crossOrigin) {
    if (typeof method !== "string" || typeof url !== "string" || !url) return;
    this.requests.push({
      method: method.toUpperCase(),
      url,
      type: typeof type === "string" ? type : null,
      cross_origin: !!crossOrigin,
    });
  }

  addNavigation(url, transitionType, originChanged) {
    if (typeof url !== "string" || !url) return;
    this.navigations.push({
      url,
      transition_type: typeof transitionType === "string" ? transitionType : null,
      origin_changed: !!originChanged,
    });
  }

  addDownload(filename) {
    if (typeof filename === "string" && filename) this.downloads.push(filename);
  }

  addTabOpened(tabId) {
    if (typeof tabId === "number") this.tabsOpened.push(tabId);
  }

  // Browser-asserted throughout (design doc section 2): a page can add decoy
  // effects to trigger a false positive here, but cannot suppress a real one --
  // the correct failure direction for a safety signal.
  isStateChanging() {
    if (this.requests.some((r) => !NON_MUTATING_METHODS.has(r.method))) return true;
    if (this.navigations.some((n) => n.transition_type === "form_submit")) return true;
    if (this.downloads.length > 0) return true;
    if (this.tabsOpened.length > 0) return true;
    return false;
  }

  report(tier, windowMs) {
    return {
      tier,
      window_ms: windowMs,
      attribution: tier === "none" ? "none" : "time_window",
      state_changing: this.isStateChanging(),
      requests: this.requests.slice(),
      navigations: this.navigations.slice(),
      downloads: this.downloads.slice(),
      tabs_opened: this.tabsOpened.slice(),
    };
  }
}

// The collection window held open on the acting tab after a
// STATE_CHANGING_COMMANDS dispatch's own result, before the effects report is
// finalized. Mirrors effects.py's EFFECTS_WINDOW_MS -- one canonical number,
// referenced from both sides (kept in sync by hand, same as everything else
// this file mirrors).
export const EFFECTS_WINDOW_MS = 1500;

// The commands effects collection applies to -- mirrors
// effects.py's STATE_CHANGING_COMMANDS / policy.py's decision flow section 4.
export const STATE_CHANGING_COMMANDS = new Set(["click", "type", "key", "navigate"]);

// An absent/unsupported collection tier still returns a real, honestly-shaped
// report (`tier: "none"`) -- never `undefined`/omitted. See effects.py's
// module docstring: a caller must always be able to distinguish "observed
// nothing" from "could not observe."
export function emptyEffectsReport(tier = "none") {
  return {
    tier,
    window_ms: 0,
    attribution: "none",
    state_changing: false,
    requests: [],
    navigations: [],
    downloads: [],
    tabs_opened: [],
  };
}
