// Tests for config_validate.mjs -- the shared validation between the options page (validate
// on save) and background.js (validate on load, so a corrupted/partial storage value fails
// loud instead of feeding a broken URL into `new WebSocket(...)`).

import { test } from "node:test";
import assert from "node:assert/strict";
import { validateHubUrl, validateHubToken, isConfigured } from "./config_validate.mjs";

test("validateHubUrl accepts a full ws:// URL with a path unchanged", () => {
  const result = validateHubUrl("ws://100.124.126.19:8900/device");
  assert.equal(result.valid, true);
  assert.equal(result.normalized, "ws://100.124.126.19:8900/device");
  assert.equal(result.error, null);
});

test("validateHubUrl appends /device to a bare host:port URL", () => {
  const result = validateHubUrl("ws://100.124.126.19:8900");
  assert.equal(result.valid, true);
  assert.equal(result.normalized, "ws://100.124.126.19:8900/device");
});

test("validateHubUrl accepts wss://", () => {
  const result = validateHubUrl("wss://spark-1.tailnet.ts.net:8900/device");
  assert.equal(result.valid, true);
});

test("validateHubUrl trims surrounding whitespace", () => {
  const result = validateHubUrl("  ws://127.0.0.1:8900/device  ");
  assert.equal(result.valid, true);
  assert.equal(result.normalized, "ws://127.0.0.1:8900/device");
});

test("validateHubUrl rejects empty/missing input", () => {
  assert.equal(validateHubUrl("").valid, false);
  assert.equal(validateHubUrl("   ").valid, false);
  assert.equal(validateHubUrl(null).valid, false);
  assert.equal(validateHubUrl(undefined).valid, false);
  assert.match(validateHubUrl("").error, /required/i);
});

test("validateHubUrl rejects a non-URL string", () => {
  const result = validateHubUrl("not a url at all");
  assert.equal(result.valid, false);
  assert.match(result.error, /not a valid url/i);
});

test("validateHubUrl rejects http(s) -- must be ws/wss", () => {
  const result = validateHubUrl("https://100.124.126.19:8900/device");
  assert.equal(result.valid, false);
  assert.match(result.error, /ws:\/\/ or wss:\/\//);
});

test("validateHubToken accepts empty/missing (hub may run with auth disabled)", () => {
  assert.equal(validateHubToken("").valid, true);
  assert.equal(validateHubToken(null).valid, true);
  assert.equal(validateHubToken(undefined).valid, true);
});

test("validateHubToken accepts an ordinary token string", () => {
  assert.equal(validateHubToken("a1b2c3d4e5f6").valid, true);
});

test("validateHubToken rejects a value with stray leading/trailing whitespace", () => {
  const result = validateHubToken("a1b2c3 \n");
  assert.equal(result.valid, false);
  assert.match(result.error, /whitespace/i);
});

test("isConfigured is true only when hubUrl validates", () => {
  assert.equal(isConfigured({ hubUrl: "ws://127.0.0.1:8900/device", hubToken: "" }), true);
  assert.equal(isConfigured({ hubUrl: "", hubToken: "abc" }), false);
  assert.equal(isConfigured({}), false);
  assert.equal(isConfigured(null), false);
  assert.equal(isConfigured(undefined), false);
});
