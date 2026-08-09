// Tests for pair_discovery.mjs -- the auto-discovery rung of the zero-copy-paste
// pairing ladder (options.js), plus the origin-check security invariant it
// exists to enforce (see that module's docstring).

import { test } from "node:test";
import assert from "node:assert/strict";
import { discoverPairingCandidate, extractPairFragment, tabOrigin } from "./pair_discovery.mjs";

// --- extractPairFragment -----------------------------------------------------

test("extractPairFragment reads the pair value out of a real setup-page URL fragment", () => {
  const url = "http://100.124.126.19:8900/setup#pair=7F3K9-QXTM2@100.124.126.19:8900&exp=1712345678";
  assert.equal(extractPairFragment(url), "7F3K9-QXTM2@100.124.126.19:8900");
});

test("extractPairFragment returns null for a URL with no fragment", () => {
  assert.equal(extractPairFragment("http://100.124.126.19:8900/setup"), null);
});

test("extractPairFragment returns null for a fragment with no pair field", () => {
  assert.equal(extractPairFragment("http://100.124.126.19:8900/setup#other=1"), null);
});

// --- tabOrigin ----------------------------------------------------------------

test("tabOrigin parses hostname and explicit port", () => {
  assert.deepEqual(tabOrigin("http://100.124.126.19:8900/setup"), { hostname: "100.124.126.19", port: "8900" });
});

test("tabOrigin returns null for an unparseable URL", () => {
  assert.equal(tabOrigin("not a url"), null);
});

// --- discoverPairingCandidate: the happy path --------------------------------

test("discoverPairingCandidate finds a real, already-open /setup#pair= tab and extracts a redeemable code", () => {
  const tabs = [
    { id: 1, url: "http://100.124.126.19:8900/" }, // unrelated tab, no fragment
    {
      id: 2,
      url: "http://100.124.126.19:8900/setup#pair=7F3K9-QXTM2@100.124.126.19:8900&exp=9999999999",
    },
    { id: 3, url: "chrome-extension://abcdefg/options.html" },
  ];

  const result = discoverPairingCandidate(tabs);

  assert.ok(result.candidate, "expected a candidate to be found");
  assert.equal(result.candidate.tabId, 2);
  assert.equal(result.candidate.host, "100.124.126.19");
  assert.equal(result.candidate.port, 8900);
  assert.equal(result.candidate.code, "7F3K9QXTM2@100.124.126.19:8900");
  assert.deepEqual(result.rejected, []);
});

test("discoverPairingCandidate returns no candidate when no open tab carries a pairing fragment", () => {
  const tabs = [{ id: 1, url: "https://example.com/" }, { id: 2, url: "http://100.124.126.19:8900/" }];
  const result = discoverPairingCandidate(tabs);
  assert.equal(result.candidate, null);
  assert.deepEqual(result.rejected, []);
});

test("discoverPairingCandidate tolerates tabs with no url (e.g. chrome://newtab with withheld url)", () => {
  const tabs = [{ id: 1 }, { id: 2, url: undefined }];
  const result = discoverPairingCandidate(tabs);
  assert.equal(result.candidate, null);
});

// --- discoverPairingCandidate: THE SECURITY INVARIANT ------------------------
// A hostile page cannot claim to speak for a hub it isn't served from -- see
// pair_discovery.mjs's module docstring.

test("discoverPairingCandidate REJECTS a hostile tab whose fragment claims a host the tab is not served from", () => {
  const tabs = [
    {
      id: 7,
      url: "http://evil.example.com/whatever#pair=7F3K9-QXTM2@100.124.126.19:8900",
    },
  ];

  const result = discoverPairingCandidate(tabs);

  assert.equal(result.candidate, null, "must never redeem a code whose claimed host does not match the tab's own origin");
  assert.equal(result.rejected.length, 1);
  assert.equal(result.rejected[0].tabId, 7);
  assert.match(result.rejected[0].reason, /origin mismatch/);
  assert.match(result.rejected[0].reason, /evil\.example\.com/);
  assert.match(result.rejected[0].reason, /100\.124\.126\.19:8900/);
});

test("discoverPairingCandidate rejects a mismatched PORT even when the hostname matches (attacker on the same host, different port)", () => {
  const tabs = [
    {
      id: 8,
      // Same hostname as the real hub, but a different port -- e.g. an
      // attacker-controlled service on the same machine listening elsewhere.
      url: "http://100.124.126.19:9999/whatever#pair=7F3K9-QXTM2@100.124.126.19:8900",
    },
  ];

  const result = discoverPairingCandidate(tabs);
  assert.equal(result.candidate, null);
  assert.match(result.rejected[0].reason, /origin mismatch/);
});

test("discoverPairingCandidate keeps scanning past a rejected hostile tab and still finds a real one", () => {
  const tabs = [
    { id: 7, url: "http://evil.example.com/whatever#pair=7F3K9-QXTM2@100.124.126.19:8900" },
    { id: 2, url: "http://100.124.126.19:8900/setup#pair=AAAAA-BBBBB@100.124.126.19:8900" },
  ];

  const result = discoverPairingCandidate(tabs);
  assert.ok(result.candidate);
  assert.equal(result.candidate.tabId, 2);
  assert.equal(result.rejected.length, 1);
  assert.equal(result.rejected[0].tabId, 7);
});

test("discoverPairingCandidate rejects (rather than crashes on) a malformed pair fragment", () => {
  const tabs = [{ id: 4, url: "http://100.124.126.19:8900/setup#pair=not-a-real-code" }];
  const result = discoverPairingCandidate(tabs);
  assert.equal(result.candidate, null);
  assert.equal(result.rejected.length, 1);
  assert.match(result.rejected[0].reason, /unparseable pairing code/);
});
