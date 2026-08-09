import { test } from "node:test";
import assert from "node:assert/strict";

import { parsePairingCode, buildDeviceWsUrl, buildRedeemUrl } from "./pairing_code.mjs";

test("parsePairingCode accepts the canonical formatted shape", () => {
  const result = parsePairingCode("7F3K9-QXTM2@100.124.126.19:8900");
  assert.equal(result.valid, true);
  assert.equal(result.ticket, "7F3K9QXTM2");
  assert.equal(result.host, "100.124.126.19");
  assert.equal(result.port, 8900);
  assert.equal(result.error, null);
});

test("parsePairingCode is tolerant of lowercase and no dash", () => {
  const result = parsePairingCode("7f3k9qxtm2@100.124.126.19:8900");
  assert.equal(result.valid, true);
  assert.equal(result.ticket, "7F3K9QXTM2");
});

test("parsePairingCode trims surrounding whitespace", () => {
  const result = parsePairingCode("  7F3K9-QXTM2@100.124.126.19:8900  ");
  assert.equal(result.valid, true);
  assert.equal(result.host, "100.124.126.19");
});

test("parsePairingCode rejects empty input", () => {
  const result = parsePairingCode("");
  assert.equal(result.valid, false);
  assert.match(result.error, /required/i);
});

test("parsePairingCode rejects null/undefined", () => {
  assert.equal(parsePairingCode(null).valid, false);
  assert.equal(parsePairingCode(undefined).valid, false);
});

test("parsePairingCode rejects a bare ws:// URL (the raw-paste shape this replaces)", () => {
  const result = parsePairingCode("ws://100.124.126.19:8900/device");
  assert.equal(result.valid, false);
  assert.match(result.error, /Not a valid pairing code/);
});

test("parsePairingCode rejects a missing @host:port", () => {
  const result = parsePairingCode("7F3K9-QXTM2");
  assert.equal(result.valid, false);
});

test("parsePairingCode rejects a missing port", () => {
  const result = parsePairingCode("7F3K9-QXTM2@100.124.126.19");
  assert.equal(result.valid, false);
});

test("parsePairingCode rejects an out-of-range port", () => {
  const result = parsePairingCode("7F3K9-QXTM2@100.124.126.19:70000");
  assert.equal(result.valid, false);
  assert.match(result.error, /out-of-range port/);
});

test("parsePairingCode accepts a hostname (not just an IP literal) for the host component", () => {
  // This parser doesn't itself enforce "IP literal, not MagicDNS name" -- that
  // policy lives in the operator's choice of what `amplifier-browser-bridge pair`
  // prints (always the tailnet IP the hub resolved -- see cli.py's `pair` command,
  // which reuses the same host:port it just talked to over /agent). This parser's
  // only job is splitting the three fields out of whatever string was pasted.
  const result = parsePairingCode("7F3K9-QXTM2@my-hub-host:8900");
  assert.equal(result.valid, true);
  assert.equal(result.host, "my-hub-host");
});

test("buildDeviceWsUrl builds the /device route", () => {
  assert.equal(buildDeviceWsUrl("100.124.126.19", 8900), "ws://100.124.126.19:8900/device");
});

test("buildRedeemUrl builds the /pair/redeem route over plain http", () => {
  assert.equal(buildRedeemUrl("100.124.126.19", 8900), "http://100.124.126.19:8900/pair/redeem");
});
