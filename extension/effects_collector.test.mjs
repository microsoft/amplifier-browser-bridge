// Tests for effects_collector.mjs -- the D3 fix's pure accumulation/windowing
// logic. Run with: node --test extension/effects_collector.test.mjs

import { test } from "node:test";
import assert from "node:assert/strict";
import { EffectsCollector, emptyEffectsReport, EFFECTS_WINDOW_MS, STATE_CHANGING_COMMANDS } from "./effects_collector.mjs";

test("a GET-only collector is not state-changing", () => {
  const c = new EffectsCollector();
  c.addRequest("GET", "https://example.com/page", "main_frame", false);
  c.addRequest("head", "https://example.com/favicon.ico", "image", false);
  assert.equal(c.isStateChanging(), false);
  const report = c.report("webrequest", EFFECTS_WINDOW_MS);
  assert.equal(report.state_changing, false);
  assert.equal(report.tier, "webrequest");
  assert.equal(report.attribution, "time_window");
});

test("a single non-GET request makes the report state-changing -- the measured case", () => {
  const c = new EffectsCollector();
  c.addRequest("POST", "https://repos.opensource.microsoft.com/api/orgs/x/repos/y/elevate", "xmlhttprequest", false);
  assert.equal(c.isStateChanging(), true);
  const report = c.report("webrequest", 1500);
  assert.equal(report.state_changing, true);
  assert.equal(report.requests.length, 1);
  assert.equal(report.requests[0].method, "POST");
});

test("a form_submit navigation is state-changing even with zero requests observed", () => {
  const c = new EffectsCollector();
  c.addNavigation("https://example.com/submit", "form_submit", false);
  assert.equal(c.isStateChanging(), true);
});

test("a non-form-submit navigation (link/reload) is NOT state-changing on its own", () => {
  const c = new EffectsCollector();
  c.addNavigation("https://example.com/next-page", "link", true);
  assert.equal(c.isStateChanging(), false);
});

test("a download makes the report state-changing", () => {
  const c = new EffectsCollector();
  c.addDownload("report.pdf");
  assert.equal(c.isStateChanging(), true);
});

test("a tab opened makes the report state-changing", () => {
  const c = new EffectsCollector();
  c.addTabOpened(42);
  assert.equal(c.isStateChanging(), true);
});

test("addRequest/addNavigation ignore malformed input rather than throwing or recording garbage", () => {
  const c = new EffectsCollector();
  c.addRequest(null, undefined, "x", false);
  c.addRequest("POST", "", "x", false); // empty url -- must not be recorded
  c.addNavigation(123, "link", false); // non-string url
  assert.equal(c.requests.length, 0);
  assert.equal(c.navigations.length, 0);
  assert.equal(c.isStateChanging(), false);
});

test("emptyEffectsReport is honestly 'none', not a silent gap", () => {
  const report = emptyEffectsReport();
  assert.equal(report.tier, "none");
  assert.equal(report.attribution, "none");
  assert.equal(report.state_changing, false);
  assert.deepEqual(report.requests, []);
});

test("emptyEffectsReport accepts an explicit tier (e.g. a capability that exists but wasn't used)", () => {
  const report = emptyEffectsReport("navigation");
  assert.equal(report.tier, "navigation");
});

test("STATE_CHANGING_COMMANDS matches the documented set", () => {
  assert.deepEqual([...STATE_CHANGING_COMMANDS].sort(), ["click", "key", "navigate", "type"]);
});

test("report() returns independent snapshots -- mutating the collector after report() doesn't retroactively change it", () => {
  const c = new EffectsCollector();
  c.addRequest("POST", "https://example.com/a", "xmlhttprequest", false);
  const first = c.report("webrequest", 1500);
  c.addRequest("POST", "https://example.com/b", "xmlhttprequest", false);
  assert.equal(first.requests.length, 1, "the earlier report snapshot must not grow");
});
