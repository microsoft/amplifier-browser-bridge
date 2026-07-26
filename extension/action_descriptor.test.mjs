// Tests for action_descriptor.mjs -- the D1 descriptor-field shaping helpers.
// Run with: node --test extension/action_descriptor.test.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  isCrossOrigin,
  hrefFields,
  formFields,
  isSubmitControl,
  capHeadings,
  capSingleText,
  buildDescriptorFields,
} from "./action_descriptor.mjs";

const ORIGIN = "https://repos.opensource.microsoft.com";

test("isCrossOrigin: same-origin relative and absolute paths are not cross-origin", () => {
  assert.equal(isCrossOrigin("/orgs/x/repos/y", ORIGIN), false);
  assert.equal(isCrossOrigin("https://repos.opensource.microsoft.com/settings", ORIGIN), false);
});

test("isCrossOrigin: a different origin is cross-origin", () => {
  assert.equal(isCrossOrigin("https://evil.example/phish", ORIGIN), true);
});

test("isCrossOrigin: unparseable input is null, not false -- unknown is not the same as same-origin", () => {
  assert.equal(isCrossOrigin("https://[not-a-valid-host", ORIGIN), null);
  assert.equal(isCrossOrigin(null, ORIGIN), null);
  assert.equal(isCrossOrigin("https://example.com", null), null);
});

test("hrefFields: absent href is all-null", () => {
  assert.deepEqual(hrefFields(undefined, ORIGIN), { href: null, href_cross_origin: null });
  assert.deepEqual(hrefFields("", ORIGIN), { href: null, href_cross_origin: null });
});

test("hrefFields: present href reports the raw value and its cross-origin flag", () => {
  const result = hrefFields("https://evil.example/x", ORIGIN);
  assert.equal(result.href, "https://evil.example/x");
  assert.equal(result.href_cross_origin, true);
});

test("formFields: no enclosing form is all-null", () => {
  assert.deepEqual(formFields(undefined, undefined, ORIGIN), {
    form_method: null,
    form_action: null,
    form_cross_origin: null,
  });
});

test("formFields: method defaults to 'get' per the HTML spec when the attribute is absent", () => {
  const result = formFields(undefined, "/settings/permissions", ORIGIN);
  assert.equal(result.form_method, "get");
  assert.equal(result.form_action, "/settings/permissions");
  assert.equal(result.form_cross_origin, false);
});

test("formFields: an explicit POST to a cross-origin action is reported honestly", () => {
  const result = formFields("POST", "https://third-party.example/submit", ORIGIN);
  assert.equal(result.form_method, "post");
  assert.equal(result.form_cross_origin, true);
});

test("isSubmitControl: <button> with no type attribute is a submit control (HTML default)", () => {
  assert.equal(isSubmitControl("button", undefined, undefined), true);
  assert.equal(isSubmitControl("BUTTON", undefined, "submit"), true);
});

test("isSubmitControl: <button type=button> is NOT a submit control", () => {
  assert.equal(isSubmitControl("button", undefined, "button"), false);
});

test("isSubmitControl: <input type=submit> is a submit control; other input types are not", () => {
  assert.equal(isSubmitControl("input", "submit", undefined), true);
  assert.equal(isSubmitControl("input", "text", undefined), false);
});

test("isSubmitControl: unrelated tags are never submit controls", () => {
  assert.equal(isSubmitControl("a", undefined, undefined), false);
  assert.equal(isSubmitControl(undefined, undefined, undefined), false);
});

test("capHeadings: filters blanks, caps count at 8 and length at 200", () => {
  const many = Array.from({ length: 12 }, (_, i) => `Heading ${i}`);
  const capped = capHeadings([...many, "", "   ", null, undefined]);
  assert.equal(capped.length, 8);
  assert.equal(capped[0], "Heading 0");
  const long = capHeadings(["x".repeat(500)]);
  assert.equal(long[0].length, 200);
});

test("capHeadings: non-array input returns an empty array rather than throwing", () => {
  assert.deepEqual(capHeadings(null), []);
  assert.deepEqual(capHeadings(undefined), []);
});

test("capSingleText: trims, caps at 200 chars, and returns null for blank/absent text", () => {
  assert.equal(capSingleText("  Elevate bkrabach to Administrator  "), "Elevate bkrabach to Administrator");
  assert.equal(capSingleText("   "), null);
  assert.equal(capSingleText(null), null);
  assert.equal(capSingleText("y".repeat(300)).length, 200);
});

test("buildDescriptorFields: assembles the full additive field set from raw facts", () => {
  const fields = buildDescriptorFields(
    {
      rawHref: undefined,
      rawFormMethod: "post",
      rawFormAction: "/settings/permissions",
      tag: "button",
      inputType: undefined,
      buttonType: undefined,
      nearestHeadingText: "Just-in-time access",
      dialogTitleText: null,
    },
    ORIGIN,
  );
  assert.equal(fields.form_method, "post");
  assert.equal(fields.form_cross_origin, false);
  assert.equal(fields.is_submit, true);
  assert.equal(fields.nearest_heading, "Just-in-time access");
  assert.equal(fields.dialog_title, null);
});

test("buildDescriptorFields: tolerates a missing facts object entirely", () => {
  const fields = buildDescriptorFields(undefined, ORIGIN);
  assert.equal(fields.href, null);
  assert.equal(fields.form_method, null);
  assert.equal(fields.is_submit, false);
});
