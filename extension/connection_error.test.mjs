import { test } from "node:test";
import assert from "node:assert/strict";

import { classifyHubErrorMessage, classifyCloseEvent, badgeTitleForErrorCode } from "./connection_error.mjs";

test("classifyHubErrorMessage recognizes the hub's exact 'unauthorized' string", () => {
  const result = classifyHubErrorMessage({ error: "unauthorized" }, 12345);
  assert.equal(result.code, "auth_rejected");
  assert.equal(result.at, 12345);
  assert.match(result.message, /rejected this device's token/);
});

test("classifyHubErrorMessage treats any other error text as a generic hub_error", () => {
  const result = classifyHubErrorMessage({ error: "hello missing device_id" }, 999);
  assert.equal(result.code, "hub_error");
  assert.match(result.message, /hello missing device_id/);
});

test("classifyHubErrorMessage handles a malformed/missing error field without throwing", () => {
  const result = classifyHubErrorMessage({});
  assert.equal(result.code, "hub_error");
  assert.match(result.message, /unknown error/);
});

test("classifyCloseEvent treats code 1006 (abnormal closure) as unreachable", () => {
  const result = classifyCloseEvent({ code: 1006 }, 111);
  assert.equal(result.code, "unreachable");
  assert.equal(result.at, 111);
  assert.match(result.message, /never completed its handshake/);
});

test("classifyCloseEvent handles a clean close (e.g. after an explicit hub close) honestly", () => {
  const result = classifyCloseEvent({ code: 1000, reason: "" });
  assert.equal(result.code, "unreachable");
  assert.match(result.message, /code 1000/);
});

test("classifyCloseEvent includes the close reason when present", () => {
  const result = classifyCloseEvent({ code: 1011, reason: "server error" });
  assert.match(result.message, /server error/);
});

test("classifyCloseEvent handles a missing/malformed event without throwing", () => {
  const result = classifyCloseEvent({});
  assert.equal(result.code, "unreachable");
  assert.match(result.message, /code unknown/);
});

test("badgeTitleForErrorCode gives a distinct title per code", () => {
  assert.match(badgeTitleForErrorCode("auth_rejected"), /re-pair/);
  assert.match(badgeTitleForErrorCode("unreachable"), /could not reach the hub/);
  assert.match(badgeTitleForErrorCode("hub_error"), /connection error/);
  assert.match(badgeTitleForErrorCode("something_else"), /connection error/); // safe fallback
});
