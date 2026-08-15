import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";

import { digestShippedEntries } from "./build_stamp.mjs";

function entry(name, text) {
  return { name, bytes: new TextEncoder().encode(text) };
}

// The exact byte layout amplifier_browser_bridge.build_stamp.compute_build_stamp
// reproduces on the hub side (sorted name, then name + NUL + bytes + NUL for every
// entry, hashed with SHA-256) -- a from-scratch reimplementation using Node's own
// crypto module, deliberately NOT importing digestShippedEntries, so this test can
// never pass merely because both sides share a bug.
function referenceDigest(entries) {
  const hasher = createHash("sha256");
  const sorted = [...entries].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  for (const { name, bytes } of sorted) {
    hasher.update(Buffer.from(name, "utf-8"));
    hasher.update(Buffer.from([0]));
    hasher.update(Buffer.from(bytes));
    hasher.update(Buffer.from([0]));
  }
  return hasher.digest("hex");
}

test("identical file sets produce identical stamps", async () => {
  const entries = [entry("a.js", "console.log(1);"), entry("b.js", "console.log(2);")];
  const first = await digestShippedEntries(entries);
  const second = await digestShippedEntries(entries.map((e) => ({ ...e })));
  assert.equal(first, second);
});

test("input order does not matter -- entries are sorted before hashing", async () => {
  const forward = [entry("a.js", "1"), entry("b.js", "2"), entry("c.js", "3")];
  const shuffled = [forward[2], forward[0], forward[1]];
  assert.equal(await digestShippedEntries(forward), await digestShippedEntries(shuffled));
});

test("changing a single shipped byte changes the stamp", async () => {
  const before = [entry("options.js", "const x = 1;")];
  const after = [entry("options.js", "const x = 2;")];
  assert.notEqual(await digestShippedEntries(before), await digestShippedEntries(after));
});

test("adding or removing a shipped file changes the stamp", async () => {
  const smaller = [entry("a.js", "1")];
  const larger = [entry("a.js", "1"), entry("b.js", "2")];
  assert.notEqual(await digestShippedEntries(smaller), await digestShippedEntries(larger));
});

test("renaming a file (same bytes, different name) changes the stamp", async () => {
  const asA = [entry("a.js", "same content")];
  const asB = [entry("b.js", "same content")];
  assert.notEqual(await digestShippedEntries(asA), await digestShippedEntries(asB));
});

test("matches an independent from-scratch reference implementation", async () => {
  const entries = [
    entry("manifest.json", '{"name": "x"}'),
    entry("background.js", "// hello"),
    // Genuinely binary (non-UTF8-text) bytes, including embedded NUL -- proves
    // the digest is a true byte hash, not something that only works for text.
    { name: "icons/icon-16.png", bytes: new Uint8Array([0, 1, 2, 137, 80, 78, 71]) },
  ];
  assert.equal(await digestShippedEntries(entries), referenceDigest(entries));
});

test("empty byte content is hashed, not skipped", async () => {
  const withEmpty = [entry("empty.js", ""), entry("a.js", "1")];
  const withoutEmpty = [entry("a.js", "1")];
  assert.notEqual(await digestShippedEntries(withEmpty), await digestShippedEntries(withoutEmpty));
});
