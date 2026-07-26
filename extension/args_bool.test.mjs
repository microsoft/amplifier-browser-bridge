// Tests for args_bool.mjs -- the shared boolean-arg coercion, and the exact
// regression it fixes: background.js's wantsAllFrames()/wantsWake() each
// reinvented `=== true || === "true" || === 1` independently, and tabOpen()'s
// `!!args.active` treated ANY non-empty string (including "false") as true.

import { test } from "node:test";
import assert from "node:assert/strict";
import { truthy } from "./args_bool.mjs";

test("truthy recognizes every true-ish shape", () => {
  assert.equal(truthy(true), true);
  assert.equal(truthy("true"), true);
  assert.equal(truthy("TRUE"), true);
  assert.equal(truthy("  True  "), true);
  assert.equal(truthy(1), true);
  assert.equal(truthy("1"), true);
});

test("truthy defaults to false for everything else", () => {
  assert.equal(truthy(false), false);
  assert.equal(truthy("false"), false);
  assert.equal(truthy("FALSE"), false);
  assert.equal(truthy(0), false);
  assert.equal(truthy("0"), false);
  assert.equal(truthy(undefined), false);
  assert.equal(truthy(null), false);
  assert.equal(truthy(""), false);
  assert.equal(truthy(2), false);
  assert.equal(truthy("treu"), false);
});

test("truthy does not throw on a missing arg (undefined)", () => {
  const args = {};
  assert.equal(truthy(args.capture_hidden), false);
});

// The exact regression: `!!"false"` (the OLD tabOpen behavior) is `true`
// because "false" is a non-empty string -- truthy() must not repeat that bug.
test("truthy correctly rejects the string 'false' (the !!string bug)", () => {
  assert.notEqual(truthy("false"), true);
  assert.equal(!!"false", true); // documents WHY the old `!!args.active` was wrong
});
