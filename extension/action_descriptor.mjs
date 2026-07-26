// action_descriptor.mjs -- pure element-fact -> descriptor-field shaping. Zero
// chrome.* usage, zero DOM usage: takes already-extracted raw facts (a plain
// object of tag/attribute/text strings injected.js reads directly off a real
// DOM element) and normalizes them into the wire-shape fields
// docs/designs/confirmation-gate.md section 11.5 adds to `snapshot()` nodes:
// href/href_cross_origin, form_method/form_action/form_cross_origin, is_submit,
// nearest_heading, dialog_title.
//
// injected.js cannot `import` this file (it is loaded as a classic script via
// chrome.scripting.executeScript({files:...}), which has no ES module support --
// same constraint documented in injected.js's own module docstring for
// ref_registry.mjs). This file is the TESTED twin; injected.js carries a
// hand-synced copy of the same logic. Keep the two in sync by hand -- same
// discipline CONTRIBUTING.md documents for protocol.py/background.js.
//
// Carries ZERO site knowledge and ZERO policy (design doc section 14.3): no
// domain names, no category names, no thresholds. This module only shapes raw
// facts into the descriptor's field names; classification lives entirely in
// the hub's classify.py.

const HEADING_MAX_COUNT = 8;
const HEADING_MAX_CHARS = 200;

// True if `urlString` (relative or absolute) resolves to a different origin
// than `pageOrigin`. Returns `null` (not `false`) when either input can't be
// parsed -- an unparseable URL is an "unknown" cross-origin fact, not a
// confirmed same-origin one; callers must not silently treat null as false.
export function isCrossOrigin(urlString, pageOrigin) {
  if (typeof urlString !== "string" || !urlString) return null;
  if (typeof pageOrigin !== "string" || !pageOrigin) return null;
  try {
    const resolved = new URL(urlString, pageOrigin);
    return resolved.origin !== pageOrigin;
  } catch {
    return null;
  }
}

// `href`/`href_cross_origin` fields for an <a>-like element.
// `rawHref` is the element's raw `href` attribute (NOT the resolved
// `.href` property -- the raw attribute is what the page actually declared;
// resolution happens here, explicitly, against `pageOrigin`).
export function hrefFields(rawHref, pageOrigin) {
  if (typeof rawHref !== "string" || !rawHref) {
    return { href: null, href_cross_origin: null };
  }
  return { href: rawHref, href_cross_origin: isCrossOrigin(rawHref, pageOrigin) };
}

// `form_method`/`form_action`/`form_cross_origin` for an element's enclosing
// <form> (or all-null if the element has no enclosing form). `rawMethod`/
// `rawAction` are the form's raw attributes (default method is "get" per the
// HTML spec when the attribute is absent).
export function formFields(rawMethod, rawAction, pageOrigin) {
  if (rawMethod === undefined && rawAction === undefined) {
    return { form_method: null, form_action: null, form_cross_origin: null };
  }
  const method = (typeof rawMethod === "string" && rawMethod ? rawMethod : "get").toLowerCase();
  const action = typeof rawAction === "string" && rawAction ? rawAction : null;
  return {
    form_method: method,
    form_action: action,
    form_cross_origin: action ? isCrossOrigin(action, pageOrigin) : null,
  };
}

// True if the element is a submit control: a <button> with no explicit type
// or type="submit", or an <input type="submit">. `tag` is upper- or
// lower-case tolerant; `inputType`/`buttonType` are the raw attribute values
// (may be absent/undefined).
export function isSubmitControl(tag, inputType, buttonType) {
  const upper = typeof tag === "string" ? tag.toUpperCase() : "";
  if (upper === "INPUT") return inputType === "submit";
  if (upper === "BUTTON") return buttonType === undefined || buttonType === null || buttonType === "submit";
  return false;
}

// Caps a heading-text array to HEADING_MAX_COUNT entries, each truncated to
// HEADING_MAX_CHARS -- the same payload-size-bound-never-a-content-pick
// discipline docs/PROTOCOL.md's "Frames" section documents for
// READ_FRAME_TEXT_CAP. Filters out empty/whitespace-only entries.
export function capHeadings(headingTexts) {
  if (!Array.isArray(headingTexts)) return [];
  return headingTexts
    .map((t) => (typeof t === "string" ? t.trim() : ""))
    .filter((t) => t.length > 0)
    .slice(0, HEADING_MAX_COUNT)
    .map((t) => t.slice(0, HEADING_MAX_CHARS));
}

// A single heading/dialog-title string, trimmed and capped -- used for
// `nearest_heading`/`dialog_title`, which are singular (not an array like the
// top-level `headings`).
export function capSingleText(text) {
  if (typeof text !== "string") return null;
  const trimmed = text.trim();
  return trimmed.length > 0 ? trimmed.slice(0, HEADING_MAX_CHARS) : null;
}

// Assembles the full set of new descriptor fields this design adds to a
// snapshot node, from already-extracted raw facts. `facts` shape:
//   { rawHref, rawFormMethod, rawFormAction, tag, inputType, buttonType,
//     nearestHeadingText, dialogTitleText }
export function buildDescriptorFields(facts, pageOrigin) {
  const f = facts || {};
  return {
    ...hrefFields(f.rawHref, pageOrigin),
    ...formFields(f.rawFormMethod, f.rawFormAction, pageOrigin),
    is_submit: isSubmitControl(f.tag, f.inputType, f.buttonType),
    nearest_heading: capSingleText(f.nearestHeadingText),
    dialog_title: capSingleText(f.dialogTitleText),
  };
}
