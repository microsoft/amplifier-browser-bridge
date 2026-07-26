// ref_registry.test.mjs -- unit tests for the generation-bound ref registry (Bug 1:
// stale refs succeed silently). Run with: node --test extension/ref_registry.test.mjs

import assert from "node:assert/strict";
import { test } from "node:test";
import { createRefRegistry, StaleRefError } from "./ref_registry.mjs";

// Plain duck-typed stand-ins for DOM elements -- ref_registry.mjs never touches
// window/document, so a plain object with `isConnected`/`tagName` is sufficient.
function fakeElement(tag, isConnected = true) {
  return { tagName: tag, isConnected };
}

function fingerprintOf(el) {
  return { tag: el.tagName, name: el.name || "" };
}

test("a ref resolves within the SAME snapshot generation that minted it", () => {
  const reg = createRefRegistry();
  reg.beginSnapshot();
  const el = fakeElement("BUTTON");
  el.name = "Revoke";
  const ref = reg.mintRef(el, fingerprintOf);

  assert.equal(reg.resolveRef(ref, fingerprintOf), el);
});

test("THE BUG: a ref from a SUPERSEDED snapshot generation is REJECTED, not silently accepted", () => {
  const reg = createRefRegistry();
  reg.beginSnapshot(); // generation 1
  const el = fakeElement("BUTTON");
  el.name = "Revoke";
  const staleRef = reg.mintRef(el, fingerprintOf);

  // A second snapshot runs -- even though `el` is untouched (still connected,
  // still the same object), the ref minted under generation 1 must not resolve
  // once generation 2 exists, because the caller's copy of it came from a
  // snapshot that has since been superseded.
  reg.beginSnapshot(); // generation 2

  assert.throws(() => reg.resolveRef(staleRef, fingerprintOf), StaleRefError);
  assert.throws(() => reg.resolveRef(staleRef, fingerprintOf), /stale ref/);
  assert.throws(() => reg.resolveRef(staleRef, fingerprintOf), /generation 1/);
  assert.throws(() => reg.resolveRef(staleRef, fingerprintOf), /generation 2/);
});

test("re-snapshotting the SAME element re-stamps its ref, so it resolves again in the new generation", () => {
  const reg = createRefRegistry();
  reg.beginSnapshot(); // generation 1
  const el = fakeElement("BUTTON");
  el.name = "Revoke";
  const ref1 = reg.mintRef(el, fingerprintOf);

  reg.beginSnapshot(); // generation 2
  const ref2 = reg.mintRef(el, fingerprintOf); // same element -> same ref string, re-stamped

  assert.equal(ref1, ref2);
  assert.equal(reg.resolveRef(ref2, fingerprintOf), el);
});

test("an unknown ref (never minted, or the table was reset by navigation) fails loud distinctly", () => {
  const reg = createRefRegistry();
  reg.beginSnapshot();
  assert.throws(() => reg.resolveRef("e999", fingerprintOf), StaleRefError);
  assert.throws(() => reg.resolveRef("e999", fingerprintOf), /unknown element ref/);
  assert.throws(() => reg.resolveRef("e999", fingerprintOf), /navigation or/);
});

test("an element removed from the DOM (same generation) fails loud distinctly from a stale generation", () => {
  const reg = createRefRegistry();
  reg.beginSnapshot();
  const el = fakeElement("BUTTON");
  el.name = "Revoke";
  const ref = reg.mintRef(el, fingerprintOf);
  el.isConnected = false; // removed from the page without any new snapshot

  assert.throws(() => reg.resolveRef(ref, fingerprintOf), StaleRefError);
  assert.throws(() => reg.resolveRef(ref, fingerprintOf), /no longer attached to the page/);
});

test("an element whose identity changed under a still-valid ref (same generation, still connected) fails loud", () => {
  const reg = createRefRegistry();
  reg.beginSnapshot();
  const el = fakeElement("BUTTON");
  el.name = "Revoke";
  const ref = reg.mintRef(el, fingerprintOf);

  // Simulate a virtualized-list DOM-node reuse: same node, same generation
  // (no new snapshot happened), but it now represents different content.
  el.name = "Approve";

  assert.throws(() => reg.resolveRef(ref, fingerprintOf), StaleRefError);
  assert.throws(() => reg.resolveRef(ref, fingerprintOf), /no longer matches what was captured/);
});

test("identity check is skipped when no fingerprintOf is supplied at resolve time", () => {
  const reg = createRefRegistry();
  reg.beginSnapshot();
  const el = fakeElement("BUTTON");
  el.name = "Revoke";
  const ref = reg.mintRef(el, fingerprintOf);
  el.name = "Approve"; // would trip the identity check if one were supplied

  assert.equal(reg.resolveRef(ref), el);
});

test("currentGeneration() reports the live counter, and beginSnapshot() returns the new value", () => {
  const reg = createRefRegistry();
  assert.equal(reg.currentGeneration(), 0);
  assert.equal(reg.beginSnapshot(), 1);
  assert.equal(reg.currentGeneration(), 1);
  assert.equal(reg.beginSnapshot(), 2);
  assert.equal(reg.currentGeneration(), 2);
});

test("a ref minted by wait_for (no beginSnapshot call) stays valid until the NEXT real snapshot", () => {
  const reg = createRefRegistry();
  reg.beginSnapshot(); // generation 1 (an initial snapshot)
  const found = fakeElement("DIV");
  found.name = "Loaded";
  const waitRef = reg.mintRef(found, fingerprintOf); // e.g. wait_for's own mint, same generation

  assert.equal(reg.resolveRef(waitRef, fingerprintOf), found);

  reg.beginSnapshot(); // generation 2 -- a fresh snapshot supersedes it
  assert.throws(() => reg.resolveRef(waitRef, fingerprintOf), /stale ref/);
});
