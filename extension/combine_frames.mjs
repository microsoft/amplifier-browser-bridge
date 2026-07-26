// combine_frames.mjs -- pure helpers for combining per-frame command results into
// one wire response. Deliberately has ZERO chrome.* usage so it can be
// unit-tested directly with plain `node --test` (see combine_frames.test.mjs),
// the same pattern frame_refs.mjs established.
//
// ## Mechanism, not policy (see docs/designs/browser-bridge.md's "Mechanism, not
// policy" section)
//
// `combineRead` used to rank frames by character count and return the "richest"
// frame's text as THE result, demoting everything else to an `other_frames`
// manifest. That was a policy decision baked into this mechanism layer -- and a
// bad one, proven live against a real SharePoint/Word Online policy page: the
// richest frame by character count was an O365 auth/bootstrap iframe's inlined
// JS config blob (3608 chars), not the actual policy document body (108 chars,
// rendered to canvas by Word Online -- see fetch_utils.mjs's module docstring
// and the design doc for why that content isn't reachable via DOM text at all).
// A char-count heuristic cannot tell "verbose bootstrap JS" from "the document
// that matters" -- it isn't equipped to, and it shouldn't be trying: that
// judgment belongs to the calling agent, which has context this layer does not.
//
// `combineRead` now returns EVERY frame's content, uniformly, with no frame
// singled out as "the" answer. The caller decides which frame's text matters --
// this layer's job is only to make every frame's content honestly available,
// in a predictable (frame-id-ascending) order, with a per-frame length cap so
// one enormous frame can't blow up the payload for every other frame in the
// same result (a length cap is a payload-size mechanism, not a content pick --
// it truncates, with an honest `truncated` flag, rather than choosing what NOT
// to return).

import { qualifyRef } from "./frame_refs.mjs";

// Per-frame character cap for `read`'s combined result. Sized generously for a
// real document body while keeping a many-frame result's total payload sane.
// This is NOT a content decision (it never discards a frame, never picks a
// "best" frame) -- it is a size bound applied UNIFORMLY to every frame, with
// `truncated` reported honestly so a caller who needs more can request a
// specific frame directly (frame_id, or the CDP/screenshot/fetch_bytes
// alternatives named in the timeout/error hints -- see hub.py).
export const READ_FRAME_TEXT_CAP = 50000;

/**
 * Combine `read` results gathered from every injectable frame of a tab.
 *
 * @param {Array<{frameId: number, url: string, title: string, text: string}>} frames
 * @param {string[]} unconfirmedFrames -- child frame `src` URLs that were declared
 *   but produced no result (sandboxed, cross-origin-blocked, or not yet loaded).
 * @returns {{url: string, title: string, frame_count: number, frames: Array<object>,
 *   unconfirmed_frames: string[]}}
 */
export function combineRead(frames, unconfirmedFrames) {
  const ordered = [...frames].sort((a, b) => a.frameId - b.frameId);
  const framesOut = ordered.map((f) => {
    const text = f.text || "";
    const truncated = text.length > READ_FRAME_TEXT_CAP;
    return {
      frame_id: f.frameId,
      url: f.url,
      title: f.title,
      chars: text.length,
      text: truncated ? text.slice(0, READ_FRAME_TEXT_CAP) : text,
      truncated,
    };
  });
  // Top-level url/title identify the TAB (frame 0's own metadata) -- this is
  // deterministic identity, not a content pick: frame 0 is always the tab's
  // own top-level document, never a heuristic guess about which frame's TEXT
  // matters most. Kept so hub.py's tab-host policy cache (`_ingest_result`'s
  // `_URL_BEARING_RESULT_COMMANDS`) still has a URL to note for `read`.
  const top = ordered.find((f) => f.frameId === 0) || ordered[0];
  return {
    url: top.url,
    title: top.title,
    frame_count: ordered.length,
    frames: framesOut,
    unconfirmed_frames: unconfirmedFrames,
  };
}

/**
 * Combine `snapshot` results gathered from every injectable frame of a tab.
 * Unchanged in spirit from before this round -- there was never a "richest
 * frame" pick here, since an interactive element an agent needs to click/type
 * into can legitimately live in any frame. Kept alongside combineRead so both
 * combine strategies live in one pure, tested module.
 *
 * @param {Array<{frameId: number, url: string, title: string, nodes: object[]}>} frames
 * @param {string[]} unconfirmedFrames
 */
export function combineSnapshot(frames, unconfirmedFrames) {
  const ordered = [...frames].sort((a, b) => a.frameId - b.frameId);
  const nodes = [];
  for (const f of ordered) {
    for (const node of f.nodes || []) {
      nodes.push({ ...node, ref: qualifyRef(f.frameId, node.ref), frame_id: f.frameId });
    }
  }
  const top = ordered.find((f) => f.frameId === 0) || ordered[0];
  return {
    url: top.url,
    title: top.title,
    nodes,
    frame_count: ordered.length,
    frames: ordered.map((f) => ({
      frame_id: f.frameId,
      url: f.url,
      title: f.title,
      node_count: (f.nodes || []).length,
    })),
    unconfirmed_frames: unconfirmedFrames,
  };
}
