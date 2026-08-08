// Tests for bundled_config.mjs -- the first-run-only adoption decision for a
// build-time-baked hub URL/token (Android zero-config install, see that module's
// docstring and docs/ANDROID.md's "Zero-configuration builds" section).
//
// The invariant under test throughout: a bundled value is a FIRST-RUN DEFAULT,
// never an override. Every "no adopt" case below models a way an install could
// already have real config -- current keys, legacy keys, or a recorded
// setupCompleted flag -- and must come back `null` (leave storage untouched).

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  resolveBundledConfigAdoption,
  describeConfigProvenance,
  CONFIG_SOURCE_BUNDLED,
  CONFIG_SOURCE_MANUAL,
  BUNDLED_CONFIG_RESOURCE,
} from "./bundled_config.mjs";

const EMPTY_STORED = {
  hubUrl: null,
  hubToken: null,
  legacyHubUrl: null,
  legacyHubToken: null,
  setupCompleted: false,
};

const VALID_BUNDLE = {
  hubUrl: "ws://100.124.126.19:8900/device",
  hubToken: "abc123def456",
  generatedAt: "2026-08-08T09:00:00Z",
};

test("adopts the bundle on a genuinely empty (never-configured) install", () => {
  const decision = resolveBundledConfigAdoption(EMPTY_STORED, VALID_BUNDLE);
  assert.deepEqual(decision, {
    hubUrl: "ws://100.124.126.19:8900/device",
    hubToken: "abc123def456",
    generatedAt: "2026-08-08T09:00:00Z",
  });
});

test("does NOT adopt when no bundle was shipped at all (desktop build, or fetch/parse failed)", () => {
  assert.equal(resolveBundledConfigAdoption(EMPTY_STORED, null), null);
  assert.equal(resolveBundledConfigAdoption(EMPTY_STORED, undefined), null);
});

test("does NOT adopt when a current-key hub URL is already stored", () => {
  const stored = { ...EMPTY_STORED, hubUrl: "ws://100.1.2.3:8900/device" };
  assert.equal(resolveBundledConfigAdoption(stored, VALID_BUNDLE), null);
});

test("does NOT adopt when a current-key hub token is already stored, even with no URL", () => {
  const stored = { ...EMPTY_STORED, hubToken: "some-existing-token" };
  assert.equal(resolveBundledConfigAdoption(stored, VALID_BUNDLE), null);
});

test("does NOT adopt when the OLD (pre-rename) legacy keys hold a value -- this is a real pre-existing install mid-migration, not a fresh one", () => {
  const storedUrl = { ...EMPTY_STORED, legacyHubUrl: "ws://100.1.2.3:8900/device" };
  const storedToken = { ...EMPTY_STORED, legacyHubToken: "old-token" };
  assert.equal(resolveBundledConfigAdoption(storedUrl, VALID_BUNDLE), null);
  assert.equal(resolveBundledConfigAdoption(storedToken, VALID_BUNDLE), null);
});

test("does NOT adopt once setupCompleted is recorded, even if storage is otherwise empty -- protects a deliberate 'clear my config' action from silently reverting", () => {
  const stored = { ...EMPTY_STORED, setupCompleted: true };
  assert.equal(resolveBundledConfigAdoption(stored, VALID_BUNDLE), null);
});

test("does NOT adopt a rebuild's different (e.g. rotated) token once this install already completed setup once", () => {
  // Models: install v1 (adopts bundle A, setupCompleted becomes true) -> rebuild produces
  // bundle B with a rotated token -> update installs v2 in place (storage persists) -> must
  // NOT silently swap the live token out from under an install that may have since been
  // hand-edited by the user.
  const stored = { ...EMPTY_STORED, hubUrl: "ws://100.1.2.3:8900/device", hubToken: "rotated-away", setupCompleted: true };
  const bundleB = { ...VALID_BUNDLE, hubToken: "brand-new-token" };
  assert.equal(resolveBundledConfigAdoption(stored, bundleB), null);
});

test("does NOT adopt a structurally invalid bundled hub URL -- fails closed, a broken build never silently applies", () => {
  const badBundle = { hubUrl: "not a url at all", hubToken: "abc" };
  assert.equal(resolveBundledConfigAdoption(EMPTY_STORED, badBundle), null);

  const httpBundle = { hubUrl: "http://100.1.2.3:8900/device", hubToken: "abc" };
  assert.equal(resolveBundledConfigAdoption(EMPTY_STORED, httpBundle), null);
});

test("adopted hubUrl is normalized exactly like a manually-entered one (validateHubUrl's own normalization)", () => {
  const bareBundle = { hubUrl: "ws://100.1.2.3:8900", hubToken: "abc" };
  const decision = resolveBundledConfigAdoption(EMPTY_STORED, bareBundle);
  assert.equal(decision.hubUrl, "ws://100.1.2.3:8900/device");
});

test("a bundle with no token (--allow-no-token dev build) adopts an empty-string token, never undefined/null", () => {
  const noTokenBundle = { hubUrl: "ws://100.1.2.3:8900/device" };
  const decision = resolveBundledConfigAdoption(EMPTY_STORED, noTokenBundle);
  assert.equal(decision.hubToken, "");
});

test("a bundle missing generatedAt adopts with generatedAt: null, never a fabricated timestamp", () => {
  const noDateBundle = { hubUrl: "ws://100.1.2.3:8900/device", hubToken: "abc" };
  const decision = resolveBundledConfigAdoption(EMPTY_STORED, noDateBundle);
  assert.equal(decision.generatedAt, null);
});

test("resolveBundledConfigAdoption never throws on a missing/undefined stored argument", () => {
  assert.equal(resolveBundledConfigAdoption(undefined, VALID_BUNDLE), null);
  assert.equal(resolveBundledConfigAdoption(null, VALID_BUNDLE), null);
});

// --- describeConfigProvenance -------------------------------------------------

test("describeConfigProvenance describes a bundled config, including the bake timestamp when present", () => {
  const text = describeConfigProvenance({ configSource: CONFIG_SOURCE_BUNDLED, configBundledAt: "2026-08-08T09:00:00Z" });
  assert.match(text, /arrived bundled with this install/);
  assert.match(text, /2026-08-08T09:00:00Z/);
});

test("describeConfigProvenance describes a bundled config with no recorded timestamp gracefully (no fabricated date)", () => {
  const text = describeConfigProvenance({ configSource: CONFIG_SOURCE_BUNDLED, configBundledAt: null });
  assert.match(text, /arrived bundled with this install/);
  assert.doesNotMatch(text, /baked at/);
});

test("describeConfigProvenance describes a manually-entered config", () => {
  const text = describeConfigProvenance({ configSource: CONFIG_SOURCE_MANUAL });
  assert.match(text, /entered manually/);
});

test("describeConfigProvenance returns null when there is nothing to say yet (never configured, or called with no state)", () => {
  assert.equal(describeConfigProvenance({}), null);
  assert.equal(describeConfigProvenance(undefined), null);
  assert.equal(describeConfigProvenance({ configSource: "something-unrecognized" }), null);
});

test("BUNDLED_CONFIG_RESOURCE is the exact extension-relative path background.js fetches", () => {
  assert.equal(BUNDLED_CONFIG_RESOURCE, "bundled_config.json");
});
