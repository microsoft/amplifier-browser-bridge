// fetch_utils.test.mjs -- unit tests for the pure byte-cap and base64 helpers
// backing `fetch_bytes`/`grab_image`.
//
// Run with: node --test extension/fetch_utils.test.mjs

import assert from "node:assert/strict";
import { test } from "node:test";
import { DEFAULT_MAX_FETCH_BYTES, checkSizeCap, bytesToBase64 } from "./fetch_utils.mjs";

test("checkSizeCap allows a body under the cap", () => {
  assert.equal(checkSizeCap(100, 1000), null);
  assert.equal(checkSizeCap(1000, 1000), null); // exactly at the cap is fine
});

test("checkSizeCap refuses a body over the cap with an actionable message", () => {
  const error = checkSizeCap(1001, 1000);
  assert.ok(error);
  assert.match(error, /1001 bytes/);
  assert.match(error, /1000-byte cap/);
  assert.match(error, /args.max_bytes/);
});

test("DEFAULT_MAX_FETCH_BYTES is a sane, generous-but-bounded default", () => {
  assert.equal(DEFAULT_MAX_FETCH_BYTES, 25 * 1024 * 1024);
  assert.equal(checkSizeCap(DEFAULT_MAX_FETCH_BYTES + 1, DEFAULT_MAX_FETCH_BYTES) !== null, true);
  assert.equal(checkSizeCap(DEFAULT_MAX_FETCH_BYTES, DEFAULT_MAX_FETCH_BYTES), null);
});

test("bytesToBase64 round-trips small buffers correctly", () => {
  const original = new TextEncoder().encode("hello world");
  const encoded = bytesToBase64(original);
  const decoded = Buffer.from(encoded, "base64");
  assert.equal(decoded.toString("utf-8"), "hello world");
});

test("bytesToBase64 handles an ArrayBuffer input as well as a Uint8Array", () => {
  const bytes = new Uint8Array([80, 75, 3, 4]); // the real .docx/.zip magic bytes
  const fromTyped = bytesToBase64(bytes);
  const fromBuffer = bytesToBase64(bytes.buffer);
  assert.equal(fromTyped, fromBuffer);
  assert.equal(Buffer.from(fromTyped, "base64").equals(Buffer.from(bytes)), true);
});

test("bytesToBase64 handles a buffer larger than the chunking boundary (0x8000 bytes)", () => {
  const size = 0x8000 * 3 + 17; // spans multiple chunks with a remainder
  const bytes = new Uint8Array(size);
  for (let i = 0; i < size; i++) bytes[i] = i % 256;
  const encoded = bytesToBase64(bytes);
  const decoded = Buffer.from(encoded, "base64");
  assert.equal(decoded.length, size);
  assert.equal(Buffer.compare(decoded, Buffer.from(bytes)), 0);
});

test("bytesToBase64 handles an empty buffer", () => {
  assert.equal(bytesToBase64(new Uint8Array(0)), "");
});
