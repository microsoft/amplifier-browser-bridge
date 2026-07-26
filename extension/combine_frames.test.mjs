// combine_frames.test.mjs -- unit tests for the pure frame-combine helpers.
//
// Run with: node --test extension/combine_frames.test.mjs

import assert from "node:assert/strict";
import { test } from "node:test";
import { combineRead, combineSnapshot, READ_FRAME_TEXT_CAP } from "./combine_frames.mjs";

// ---------------------------------------------------------------------------
// Task 1 (mechanism, not policy): combineRead must never pick a "winner" frame
// for its content -- every frame's text must be present in the result.
// ---------------------------------------------------------------------------

test("combineRead returns every frame's content -- no frame is dropped or singled out as THE answer", () => {
  const frames = [
    { frameId: 0, url: "https://sp.example/page", title: "Policy Page", text: "nav chrome" },
    { frameId: 860, url: "https://o365.example/bootstrap.js", title: "", text: "x".repeat(3608) },
    { frameId: 862, url: "https://ppc-word-view.example/doc", title: "Global-Travel-Policy.docx", text: "PAGE 1 OF 5" },
    { frameId: 861, url: "https://sharepoint.example/w/r/doc", title: "", text: "" },
    { frameId: 864, url: "https://auth.example/oauth", title: "WacOAuth", text: "abcdef" },
  ];
  const result = combineRead(frames, []);

  assert.equal(result.frame_count, 5);
  assert.equal(result.frames.length, 5);
  // Every frame_id from the input is present in the output -- none dropped.
  const idsOut = new Set(result.frames.map((f) => f.frame_id));
  assert.deepEqual(idsOut, new Set([0, 860, 862, 861, 864]));
  // The "richest" (auth bootstrap JS, 3608 chars) frame is NOT promoted to any
  // top-level primary text/url/title field -- there is no such field at all.
  assert.equal("text" in result, false);
  assert.equal("frame_id" in result, false);
  assert.equal("other_frames" in result, false);
  // The thin, real-content frame (862 -- the actual policy document viewer)
  // is present with its own full text, un-demoted.
  const docFrame = result.frames.find((f) => f.frame_id === 862);
  assert.ok(docFrame);
  assert.equal(docFrame.text, "PAGE 1 OF 5");
  assert.equal(docFrame.chars, 11);
});

test("combineRead orders frames by frame_id ascending -- predictable, not ranked by content", () => {
  const frames = [
    { frameId: 862, url: "u1", title: "t1", text: "short" },
    { frameId: 0, url: "u2", title: "t2", text: "y".repeat(1000) },
    { frameId: 12, url: "u3", title: "t3", text: "z" },
  ];
  const result = combineRead(frames, []);
  assert.deepEqual(
    result.frames.map((f) => f.frame_id),
    [0, 12, 862]
  );
});

test("combineRead's top-level url/title identify the tab (frame 0), not a content pick", () => {
  const frames = [
    { frameId: 0, url: "https://sp.example/page", title: "Policy Page", text: "chrome only" },
    { frameId: 3, url: "https://embed.example/doc", title: "Doc", text: "y".repeat(5000) },
  ];
  const result = combineRead(frames, []);
  assert.equal(result.url, "https://sp.example/page");
  assert.equal(result.title, "Policy Page");
});

test("combineRead falls back to the first frame present when frame 0 didn't produce a result", () => {
  const frames = [
    { frameId: 3, url: "https://embed.example/doc", title: "Doc", text: "hi" },
    { frameId: 7, url: "https://other.example", title: "Other", text: "bye" },
  ];
  const result = combineRead(frames, []);
  assert.equal(result.url, "https://embed.example/doc");
});

test("combineRead caps each frame's text independently and reports truncated honestly", () => {
  const longText = "a".repeat(READ_FRAME_TEXT_CAP + 500);
  const frames = [
    { frameId: 0, url: "u0", title: "t0", text: "short" },
    { frameId: 1, url: "u1", title: "t1", text: longText },
  ];
  const result = combineRead(frames, []);
  const short = result.frames.find((f) => f.frame_id === 0);
  const long = result.frames.find((f) => f.frame_id === 1);

  assert.equal(short.truncated, false);
  assert.equal(short.chars, 5);
  assert.equal(short.text, "short");

  assert.equal(long.truncated, true);
  // chars reports the REAL (untruncated) length, so a caller knows how much
  // was cut -- only `text` itself is capped.
  assert.equal(long.chars, READ_FRAME_TEXT_CAP + 500);
  assert.equal(long.text.length, READ_FRAME_TEXT_CAP);
});

test("combineRead passes unconfirmed_frames through unmodified", () => {
  const frames = [{ frameId: 0, url: "u", title: "t", text: "" }];
  const result = combineRead(frames, ["https://ad.example/frame1", "https://tracker.example/frame2"]);
  assert.deepEqual(result.unconfirmed_frames, ["https://ad.example/frame1", "https://tracker.example/frame2"]);
});

test("combineRead handles a single-frame page (the common case) with no frames array surprises", () => {
  const frames = [{ frameId: 0, url: "https://simple.example", title: "Simple", text: "hello world" }];
  const result = combineRead(frames, []);
  assert.equal(result.frame_count, 1);
  assert.equal(result.frames.length, 1);
  assert.equal(result.frames[0].text, "hello world");
});

// ---------------------------------------------------------------------------
// combineSnapshot -- unchanged in spirit (never picked a winner), verified
// alongside combineRead now that both live in this module.
// ---------------------------------------------------------------------------

test("combineSnapshot qualifies every node's ref with its owning frame and keeps all frames", () => {
  const frames = [
    { frameId: 0, url: "u0", title: "t0", nodes: [{ ref: "e1", role: "button", name: "Go" }] },
    { frameId: 7, url: "u7", title: "t7", nodes: [{ ref: "e1", role: "link", name: "Doc" }] },
  ];
  const result = combineSnapshot(frames, []);
  assert.equal(result.frame_count, 2);
  assert.equal(result.nodes.length, 2);
  const refs = result.nodes.map((n) => n.ref);
  assert.deepEqual(refs, ["f0.e1", "f7.e1"]);
  // Same bare ref ("e1") from two different frames must not collide.
  assert.notEqual(result.nodes[0].ref, result.nodes[1].ref);
});

test("combineSnapshot reports a per-frame manifest ordered by frame_id", () => {
  const frames = [
    { frameId: 5, url: "u5", title: "t5", nodes: [{ ref: "e1", role: "button", name: "" }] },
    { frameId: 0, url: "u0", title: "t0", nodes: [] },
  ];
  const result = combineSnapshot(frames, []);
  assert.deepEqual(
    result.frames.map((f) => f.frame_id),
    [0, 5]
  );
  assert.equal(result.frames.find((f) => f.frame_id === 5).node_count, 1);
});
