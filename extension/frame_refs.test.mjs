// frame_refs.test.js -- unit tests for the pure ref-qualification helpers.
//
// Run with: node --test extension/frame_refs.test.js
//
// There is no JS build/test framework in this repo (see CONTRIBUTING.md's
// "Extension JavaScript" section -- extension/ has no build step at all).
// Node 18+ ships a built-in test runner (`node --test`) and assertion module,
// so these tests need no new dependency -- consistent with the project's
// "avoid speculative complexity" stance (see docs/designs/browser-bridge.md's
// citations of IMPLEMENTATION_PHILOSOPHY.md).

import assert from "node:assert/strict";
import { test } from "node:test";
import { parseQualifiedRef, qualifyRef } from "./frame_refs.mjs";

test("qualifyRef produces the documented f<frameId>.<ref> shape", () => {
  assert.equal(qualifyRef(0, "e12"), "f0.e12");
  assert.equal(qualifyRef(7, "e1"), "f7.e1");
  assert.equal(qualifyRef(123456789, "e999"), "f123456789.e999");
});

test("qualifyRef rejects a non-integer or negative frameId", () => {
  assert.throws(() => qualifyRef(-1, "e1"), /non-negative integer/);
  assert.throws(() => qualifyRef(1.5, "e1"), /non-negative integer/);
  assert.throws(() => qualifyRef("0", "e1"), /non-negative integer/);
  assert.throws(() => qualifyRef(undefined, "e1"), /non-negative integer/);
});

test("qualifyRef rejects an empty or non-string rawRef", () => {
  assert.throws(() => qualifyRef(0, ""), /non-empty string/);
  assert.throws(() => qualifyRef(0, null), /non-empty string/);
  assert.throws(() => qualifyRef(0, 12), /non-empty string/);
});

test("parseQualifiedRef is the exact inverse of qualifyRef", () => {
  for (const [frameId, ref] of [
    [0, "e1"],
    [7, "e12"],
    [42, "e999"],
  ]) {
    assert.deepEqual(parseQualifiedRef(qualifyRef(frameId, ref)), { frameId, ref });
  }
});

test("parseQualifiedRef disambiguates the SAME bare ref from two different frames", () => {
  // This is the core Gap 1 correctness property: "e12" in frame 0 and "e12" in
  // frame 7 must never be treated as the same element.
  const fromFrameZero = parseQualifiedRef(qualifyRef(0, "e12"));
  const fromFrameSeven = parseQualifiedRef(qualifyRef(7, "e12"));
  assert.equal(fromFrameZero.ref, "e12");
  assert.equal(fromFrameSeven.ref, "e12");
  assert.notEqual(fromFrameZero.frameId, fromFrameSeven.frameId);
  assert.notEqual(qualifyRef(0, "e12"), qualifyRef(7, "e12"));
});

test("parseQualifiedRef rejects a bare (unqualified) ref -- fails loud rather than guessing frame 0", () => {
  assert.throws(() => parseQualifiedRef("e12"), /not a frame-qualified ref/);
  assert.throws(() => parseQualifiedRef(""), /not a frame-qualified ref/);
});

test("parseQualifiedRef rejects malformed frame-id prefixes", () => {
  assert.throws(() => parseQualifiedRef("fx.e12"), /invalid frame id/);
  assert.throws(() => parseQualifiedRef("f-1.e12"), /invalid frame id/);
  assert.throws(() => parseQualifiedRef("f.e12"), /invalid frame id/); // empty frame id part
});

test("parseQualifiedRef only treats the FIRST separator as the frame-id boundary", () => {
  // Real refs from injected.js are always "e<N>" and never contain a ".", but the
  // parser's contract is "split on the first separator only" -- anything after
  // it is opaque ref content, not re-parsed. Documented here so the boundary
  // behavior is a deliberate, tested choice rather than an accident.
  assert.deepEqual(parseQualifiedRef("f1.5.e12"), { frameId: 1, ref: "5.e12" });
});

test("parseQualifiedRef rejects a qualified ref with no ref after the separator", () => {
  assert.throws(() => parseQualifiedRef("f0."), /empty ref after separator/);
});

test("parseQualifiedRef rejects non-string input", () => {
  assert.throws(() => parseQualifiedRef(undefined), /not a frame-qualified ref/);
  assert.throws(() => parseQualifiedRef(42), /not a frame-qualified ref/);
});

test("qualifyRef/parseQualifiedRef round-trip through many frames without collision", () => {
  // Simulates a snapshot spanning many frames with overlapping local ref
  // counters (every frame's own window.__abb starts counting at e1).
  const seen = new Set();
  for (let frameId = 0; frameId < 30; frameId++) {
    for (let n = 1; n <= 5; n++) {
      const qualified = qualifyRef(frameId, `e${n}`);
      assert.ok(!seen.has(qualified), `duplicate qualified ref: ${qualified}`);
      seen.add(qualified);
      assert.deepEqual(parseQualifiedRef(qualified), { frameId, ref: `e${n}` });
    }
  }
  assert.equal(seen.size, 30 * 5);
});
