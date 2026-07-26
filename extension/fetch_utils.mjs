// fetch_utils.mjs -- pure helpers for `fetch_bytes`/`grab_image`: byte-size cap
// enforcement and base64 encoding. Deliberately has ZERO chrome.* usage so it
// can be unit-tested directly with plain `node --test` (see
// fetch_utils.test.mjs) -- the same pattern frame_refs.mjs established. The
// real `fetch()` calls (extension context for fetch_bytes, page MAIN world for
// grab_image) live in background.js; this module only owns the size-cap
// decision and the bytes-to-wire encoding, both of which are pure functions of
// their inputs.
//
// ## Why these two commands exist (see docs/designs/browser-bridge.md's
// "Mechanism, not policy" section)
//
// A real SharePoint policy page embeds a .docx in a Word Online viewer, which
// renders the document to <canvas> -- the rendered page's ENTIRE DOM text is a
// page-chrome string like "PAGE 1 OF 5 | CONFIDENTIAL...", not the document
// body. There is no DOM text to read, full stop -- `read`/`snapshot` cannot
// reach content that was never placed in the DOM as text. Two, and only two,
// paths reach that content: fetch the underlying file directly (`fetch_bytes`,
// riding the user's real authenticated session), or capture pixels
// (`screenshot`). This module backs the first path.
//
// `fetch_bytes` (extension context, credentials: "include") and `grab_image`
// (the page's own MAIN-world script context, carrying the page's real
// Referer/cookie context) are deliberately TWO DISTINCT mechanisms, not one
// command with an internal fallback -- some origins accept an extension's
// cross-origin fetch just fine; others gate on Referer/Origin in a way only a
// same-page-context fetch defeats. Silently trying one then falling back to
// the other would hide from the caller which path actually worked, and would
// be exactly the kind of policy substitution this round's governing principle
// forbids. The caller picks; this module just enforces a byte-size cap and
// encodes whichever bytes actually came back.

// 25MB: generous for a real .docx/.pdf/image, bounded so a caller can't
// accidentally pull an enormous file through a single command result. This is
// a PAYLOAD-SIZE mechanism (refuse and say so), never a silent truncation of
// the file's actual bytes -- a truncated .docx/.pdf is corrupt, not useful.
export const DEFAULT_MAX_FETCH_BYTES = 25 * 1024 * 1024;

/**
 * Check a byte length against a cap. Returns an error string (never throws)
 * so callers can build an actionable `Error` message with their own context
 * (URL, content-type) -- see background.js's fetchBytes()/grabImage().
 *
 * @param {number} byteLength
 * @param {number} maxBytes
 * @returns {string | null}
 */
export function checkSizeCap(byteLength, maxBytes) {
  if (byteLength > maxBytes) {
    return (
      `response body is ${byteLength} bytes, exceeding the ${maxBytes}-byte cap ` +
      "(pass a larger args.max_bytes to raise it)"
    );
  }
  return null;
}

/**
 * Encode an ArrayBuffer/Uint8Array as base64, chunked to avoid blowing the
 * call stack on `String.fromCharCode(...hugeArray)` for large files (a real
 * failure mode above roughly a few MB in V8).
 *
 * @param {ArrayBuffer | Uint8Array} bufferLike
 * @returns {string}
 */
export function bytesToBase64(bufferLike) {
  const bytes = bufferLike instanceof Uint8Array ? bufferLike : new Uint8Array(bufferLike);
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
  }
  // btoa is available in both the extension's background (service worker)
  // context and the page's MAIN world -- no Buffer/Node dependency needed.
  return btoa(binary);
}
