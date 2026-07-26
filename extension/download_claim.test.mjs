// download_claim.test.mjs -- unit tests for the pure wait_download selection
// and arg-validation logic.
//
// Run with: node --test extension/download_claim.test.mjs

import assert from "node:assert/strict";
import { test } from "node:test";
import {
  pickCompletedDownload,
  pickInterruptedDownload,
  validateWaitDownloadArgs,
} from "./download_claim.mjs";

// ---------------------------------------------------------------------------
// validateWaitDownloadArgs -- fail loud rather than defaulting to "newest".
// ---------------------------------------------------------------------------

test("validateWaitDownloadArgs requires download_id or since_id", () => {
  const error = validateWaitDownloadArgs({});
  assert.ok(error);
  assert.match(error, /requires args\.download_id/);
  assert.match(error, /args\.since_id/);
});

test("validateWaitDownloadArgs accepts download_id alone", () => {
  assert.equal(validateWaitDownloadArgs({ download_id: 42 }), null);
});

test("validateWaitDownloadArgs accepts since_id alone, with or without pattern", () => {
  assert.equal(validateWaitDownloadArgs({ since_id: 10 }), null);
  assert.equal(validateWaitDownloadArgs({ since_id: 10, pattern: "\\.docx$" }), null);
});

test("validateWaitDownloadArgs rejects a non-string pattern", () => {
  const error = validateWaitDownloadArgs({ since_id: 10, pattern: 123 });
  assert.ok(error);
  assert.match(error, /args.pattern must be a string/);
});

// ---------------------------------------------------------------------------
// pickCompletedDownload -- the baseline-max-id + pattern correctness property:
// the human's own concurrent downloads must never be claimed as the agent's.
// ---------------------------------------------------------------------------

test("download_id mode matches only the exact id, and only once complete", () => {
  const items = [
    { id: 100, filename: "a.txt", state: "complete" },
    { id: 101, filename: "b.txt", state: "in_progress" },
  ];
  assert.deepEqual(pickCompletedDownload(items, { downloadId: 100 }), items[0]);
  assert.equal(pickCompletedDownload(items, { downloadId: 101 }), undefined); // still in progress
  assert.equal(pickCompletedDownload(items, { downloadId: 999 }), undefined); // doesn't exist
});

test("since_id mode NEVER matches a download at or below the baseline -- the core correctness property", () => {
  const items = [
    { id: 50, filename: "human-started-this-one.pdf", state: "complete" }, // pre-existing
    { id: 51, filename: "human-started-this-one-too.pdf", state: "complete" }, // pre-existing
  ];
  // Baseline == the highest existing id -- nothing here is "new".
  assert.equal(pickCompletedDownload(items, { sinceId: 51 }), undefined);
});

test("since_id mode matches the lowest NEW completed download above the baseline", () => {
  const items = [
    { id: 50, filename: "old.pdf", state: "complete" }, // below baseline -- must be ignored
    { id: 52, filename: "agents-file.docx", state: "complete" }, // new
    { id: 53, filename: "another-new-one.txt", state: "complete" }, // also new, but later
  ];
  const picked = pickCompletedDownload(items, { sinceId: 50 });
  assert.equal(picked.id, 52); // the earliest new one, not just "any" new one
});

test("since_id mode ignores a new download that is not yet complete", () => {
  const items = [{ id: 60, filename: "still-downloading.docx", state: "in_progress" }];
  assert.equal(pickCompletedDownload(items, { sinceId: 50 }), undefined);
});

test("since_id mode narrows by filename pattern when given", () => {
  const items = [
    { id: 61, filename: "unrelated-notification-sound.mp3", state: "complete" },
    { id: 62, filename: "quarterly-report.docx", state: "complete" },
  ];
  const picked = pickCompletedDownload(items, { sinceId: 50, pattern: /\.docx$/ });
  assert.equal(picked.id, 62);
});

test("since_id mode with a pattern that matches nothing returns undefined (never a wrong guess)", () => {
  const items = [{ id: 61, filename: "something.pdf", state: "complete" }];
  const picked = pickCompletedDownload(items, { sinceId: 50, pattern: /\.docx$/ });
  assert.equal(picked, undefined);
});

test("neither downloadId nor sinceId given -- returns undefined rather than guessing 'newest'", () => {
  const items = [{ id: 999, filename: "whatever.txt", state: "complete" }];
  assert.equal(pickCompletedDownload(items, {}), undefined);
  assert.equal(pickCompletedDownload(items), undefined);
});

// ---------------------------------------------------------------------------
// pickInterruptedDownload -- lets wait_download fail fast instead of polling
// to its full timeout for a download that has already, definitively, failed.
// ---------------------------------------------------------------------------

test("pickInterruptedDownload detects a failed download in download_id mode", () => {
  const items = [{ id: 70, filename: "x.docx", state: "interrupted" }];
  assert.deepEqual(pickInterruptedDownload(items, { downloadId: 70 }), items[0]);
});

test("pickInterruptedDownload detects a failed download in since_id mode, respecting the baseline", () => {
  const items = [
    { id: 40, filename: "old-failed.docx", state: "interrupted" }, // below baseline, ignored
    { id: 71, filename: "new-failed.docx", state: "interrupted" },
  ];
  const picked = pickInterruptedDownload(items, { sinceId: 50 });
  assert.equal(picked.id, 71);
});
