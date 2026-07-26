// frame_refs.js -- pure helpers for frame-qualified element refs. Deliberately has
// ZERO chrome.* usage so it can be unit-tested directly with plain `node --test`
// (see frame_refs.test.js) instead of requiring a real browser.
//
// Why this exists: chrome.scripting.executeScript({allFrames: true}) injects
// injected.js independently into EVERY frame of a tab -- each frame gets its own
// `window.__abb` with its own ref counter starting at "e1". A bare "e12" in the
// top frame and "e12" in an embedded iframe are NOT the same element; nothing
// about the string itself says which frame it came from. Left alone, click/type/key
// would have no way to know which frame to route a ref back into, and two different
// elements could collide under the same ref string.
//
// background.js is the ONLY place that knows a frame's `frameId` (from
// chrome.scripting's `InjectionResult.frameId` -- injected.js itself has no API to
// learn its own frameId, by design). So qualification happens here, one level up
// from injected.js: every ref handed back to a caller (from `snapshot`) is
// rewritten as "f<frameId>.<bare ref>" before it leaves the extension; every ref a
// caller sends back in (to `click`/`type`/`key`) is parsed back into
// `{frameId, ref}` so background.js can target `chrome.scripting.executeScript`'s
// `frameIds: [frameId]` at the exact frame that owns it, instead of guessing frame 0.

const FRAME_PREFIX = "f";
const REF_SEP = ".";

/** Turn a frame-local ref ("e12") into a globally-unambiguous one ("f7.e12"). */
export function qualifyRef(frameId, rawRef) {
  if (typeof frameId !== "number" || !Number.isInteger(frameId) || frameId < 0) {
    throw new Error(`qualifyRef: frameId must be a non-negative integer, got: ${JSON.stringify(frameId)}`);
  }
  if (typeof rawRef !== "string" || rawRef.length === 0) {
    throw new Error(`qualifyRef: rawRef must be a non-empty string, got: ${JSON.stringify(rawRef)}`);
  }
  return `${FRAME_PREFIX}${frameId}${REF_SEP}${rawRef}`;
}

/** Reverse of qualifyRef: "f7.e12" -> {frameId: 7, ref: "e12"}. Throws with a
 * specific, actionable message on anything that isn't a well-formed qualified
 * ref -- addressing is load-bearing here, so fail loud rather than guess which
 * frame an unqualified/malformed ref might belong to (see design doc's "fail
 * loud" convention, CONTRIBUTING.md). */
export function parseQualifiedRef(qualifiedRef) {
  if (typeof qualifiedRef !== "string" || !qualifiedRef.startsWith(FRAME_PREFIX)) {
    throw new Error(
      `not a frame-qualified ref (expected "f<frameId>${REF_SEP}<ref>", e.g. "f0${REF_SEP}e12"): ` +
        `${JSON.stringify(qualifiedRef)}`
    );
  }
  const sepIndex = qualifiedRef.indexOf(REF_SEP);
  if (sepIndex === -1) {
    throw new Error(
      `not a frame-qualified ref (missing "${REF_SEP}" separator between frame id and ref): ` +
        `${JSON.stringify(qualifiedRef)}`
    );
  }
  const frameIdPart = qualifiedRef.slice(FRAME_PREFIX.length, sepIndex);
  const frameId = Number(frameIdPart);
  if (frameIdPart.length === 0 || !Number.isInteger(frameId) || frameId < 0 || String(frameId) !== frameIdPart) {
    throw new Error(
      `not a frame-qualified ref (invalid frame id "${frameIdPart}"): ${JSON.stringify(qualifiedRef)}`
    );
  }
  const rawRef = qualifiedRef.slice(sepIndex + 1);
  if (rawRef.length === 0) {
    throw new Error(`not a frame-qualified ref (empty ref after separator): ${JSON.stringify(qualifiedRef)}`);
  }
  return { frameId, ref: rawRef };
}
