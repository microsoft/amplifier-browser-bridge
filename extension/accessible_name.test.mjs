// Tests for accessible_name.mjs -- the B1 fix (aria-labelledby extraction).
// Run with: node --test extension/accessible_name.test.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import { joinLabelledByTexts, computeAccessibleName } from "./accessible_name.mjs";

test("joinLabelledByTexts: joins non-empty entries with a single space, trimmed", () => {
  assert.equal(joinLabelledByTexts(["  Elevate  ", "to Administrator"]), "Elevate to Administrator");
});

test("joinLabelledByTexts: drops empty/whitespace-only entries", () => {
  assert.equal(joinLabelledByTexts(["Elevate", "  ", null, "to Administrator"]), "Elevate to Administrator");
});

test("joinLabelledByTexts: non-array input is empty string, not a throw", () => {
  assert.equal(joinLabelledByTexts(null), "");
  assert.equal(joinLabelledByTexts(undefined), "");
});

test("computeAccessibleName: aria-labelledby-only icon button (the reported gap) extracts the referenced text", () => {
  // An icon-only button: <button aria-labelledby="lbl"><svg .../></button>
  // with <span id="lbl">Elevate to Administrator</span> elsewhere on the
  // page. No aria-label, no text content of its own -- this previously
  // extracted as empty.
  const name = computeAccessibleName({
    ariaLabelledbyTexts: ["Elevate to Administrator"],
    ariaLabel: null,
    isImg: false,
    altText: null,
    textContent: "",
  });
  assert.equal(name, "Elevate to Administrator");
});

test("computeAccessibleName: aria-labelledby referencing multiple ids joins them in order", () => {
  const name = computeAccessibleName({
    ariaLabelledbyTexts: ["Elevate", "to Administrator"],
    ariaLabel: null,
    isImg: false,
    altText: null,
    textContent: "",
  });
  assert.equal(name, "Elevate to Administrator");
});

test("computeAccessibleName: aria-labelledby wins over aria-label when both present", () => {
  const name = computeAccessibleName({
    ariaLabelledbyTexts: ["Elevate to Administrator"],
    ariaLabel: "Continue",
    isImg: false,
    altText: null,
    textContent: "",
  });
  assert.equal(name, "Elevate to Administrator");
});

test("computeAccessibleName: falls back to aria-label when no labelledby text resolves", () => {
  const name = computeAccessibleName({
    ariaLabelledbyTexts: [],
    ariaLabel: "Elevate to Administrator",
    isImg: false,
    altText: null,
    textContent: "",
  });
  assert.equal(name, "Elevate to Administrator");
});

test("computeAccessibleName: falls back to alt text for an img with neither labelledby nor aria-label", () => {
  const name = computeAccessibleName({
    ariaLabelledbyTexts: [],
    ariaLabel: null,
    isImg: true,
    altText: "Elevate to Administrator",
    textContent: "ignored",
  });
  assert.equal(name, "Elevate to Administrator");
});

test("computeAccessibleName: falls back to trimmed, collapsed text content as the last resort", () => {
  const name = computeAccessibleName({
    ariaLabelledbyTexts: [],
    ariaLabel: null,
    isImg: false,
    altText: null,
    textContent: "  Elevate   to Administrator  ",
  });
  assert.equal(name, "Elevate to Administrator");
});

test("computeAccessibleName: caps to 120 characters, same as the pre-existing nameOf() contract", () => {
  const long = "x".repeat(200);
  const name = computeAccessibleName({
    ariaLabelledbyTexts: [],
    ariaLabel: null,
    isImg: false,
    altText: null,
    textContent: long,
  });
  assert.equal(name.length, 120);
});

test("computeAccessibleName: no facts at all -> empty string, never a throw", () => {
  assert.equal(computeAccessibleName(undefined), "");
  assert.equal(computeAccessibleName({}), "");
});
