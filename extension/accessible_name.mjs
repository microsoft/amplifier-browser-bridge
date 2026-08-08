// accessible_name.mjs -- pure, testable subset of the W3C Accessible Name and
// Description Computation (https://www.w3.org/TR/accname-1.2/), used to
// extract the "label" the confirmation-gate classifier scores (classify.py).
//
// B1 fix (security review finding, classifier extraction gap): `nameOf()` in
// injected.js previously computed a name from `aria-label` OR text content
// only -- an icon-only button whose accessible name comes from
// `aria-labelledby` (a reference to ANOTHER element's text, not a direct
// child text node) extracted as empty. An `<button aria-labelledby="lbl">`
// with `<span id="lbl">Elevate to Administrator</span>` elsewhere on the page
// is a completely ordinary, spec-compliant way to build an icon button, not
// an exotic evasion -- and it silently starved the classifier of the single
// most decisive signal it has.
//
// LOAD-BEARING SUBSET IMPLEMENTED HERE (priority order, matching the spec's
// own precedence): aria-labelledby > aria-label > alt (img) > text content.
// This covers the two concrete gaps named by the security review
// (aria-label, aria-labelledby) plus the pre-existing alt/text-content
// fallback `nameOf()` already had.
//
// NOT IMPLEMENTED (say so plainly, not implied-complete) -- the accname spec
// also covers, and this does NOT: native host-language labeling (`<label
// for>` / wrapping `<label>`), `placeholder` and `title` attributes,
// embedded-control recursion (an aria-labelledby target that is itself an
// input, whose VALUE should contribute), "presentational children" pruning
// of the text-content computation, table caption/summary handling, and
// aria-describedby (a distinct, lower-priority accessible name role this
// module does not compute at all). A caller relying on this for anything
// beyond confirmation-gate classification should not assume spec parity.
//
// injected.js cannot `import` this file (loaded as a classic script via
// `chrome.scripting.executeScript({files:...})`, no ES module support -- the
// same constraint documented in injected.js's own module docstring for
// ref_registry.mjs/action_descriptor.mjs). This is the TESTED twin;
// injected.js carries a hand-synced copy. Keep the two in sync by hand --
// same discipline CONTRIBUTING.md documents for protocol.py/background.js.

const NAME_MAX_CHARS = 120;

// Joins already-resolved aria-labelledby referent texts, per the spec's
// "name from content" step: each referenced element's text is trimmed
// individually, empty ones are dropped, then joined with a single space.
export function joinLabelledByTexts(texts) {
  if (!Array.isArray(texts)) return "";
  return texts
    .filter((t) => typeof t === "string" && t.trim().length > 0)
    .map((t) => t.trim())
    .join(" ")
    .trim();
}

// `facts` shape (all optional):
//   { ariaLabelledbyTexts: string[]|null, ariaLabel: string|null,
//     isImg: bool, altText: string|null, textContent: string|null }
//
// Returns the computed accessible name, capped to NAME_MAX_CHARS (matching
// the pre-existing `nameOf()` cap -- this module doesn't widen the payload
// contract, only what feeds it).
export function computeAccessibleName(facts) {
  const f = facts || {};

  const labelledBy = joinLabelledByTexts(f.ariaLabelledbyTexts);
  if (labelledBy) return labelledBy.slice(0, NAME_MAX_CHARS);

  if (typeof f.ariaLabel === "string" && f.ariaLabel.trim()) {
    return f.ariaLabel.slice(0, NAME_MAX_CHARS);
  }

  if (f.isImg) return (typeof f.altText === "string" ? f.altText : "").slice(0, NAME_MAX_CHARS);

  const text = (typeof f.textContent === "string" ? f.textContent : "").trim().replace(/\s+/g, " ");
  return text.slice(0, NAME_MAX_CHARS);
}

export { NAME_MAX_CHARS };
