// download_claim.mjs -- pure helpers for the `wait_download` command's
// baseline-max-id + filename-pattern matching logic. Deliberately has ZERO
// chrome.* usage (the real chrome.downloads.search() call lives in
// background.js) so this logic can be unit-tested directly with plain
// `node --test` (see download_claim.test.mjs), the same pattern frame_refs.mjs
// and combine_frames.mjs established.
//
// ## Why "baseline + pattern" and not just "the download I just triggered"
//
// `download` (chrome.downloads.download()) gives its caller the download's own
// definite id -- no ambiguity there. `wait_download` exists for the OTHER real
// case: a download triggered INDIRECTLY, e.g. the agent clicks a page's own
// "Download" control and the browser starts a native download the agent never
// called chrome.downloads.download() for. In that case there is no id to hand
// back from the triggering action itself.
//
// The naive fix -- "poll chrome.downloads.search() and grab whatever's newest"
// -- silently claims a download the HUMAN started themselves in the same
// window, which is a real, not hypothetical, failure mode on a live profile
// with hundreds of tabs and an actual person still using the browser. This is
// exactly the reference implementation's `maxDownloadId` + filename-pattern
// idea (see the design doc's evidence base) carried forward: a caller
// captures a baseline (the highest existing download id, from `downloads_list`)
// BEFORE the triggering action, then asks only for a completed download with
// an id STRICTLY GREATER than that baseline -- one the human's own downloads,
// already in progress or completed before the baseline was taken, can never
// satisfy.
//
// This module owns only the pure selection logic (given a list of
// `chrome.downloads.DownloadItem`-shaped objects, which one -- if any --
// satisfies the caller's request). Retrying/waiting/polling and the real
// chrome.downloads.search() call are background.js's job.

/**
 * Validate `wait_download`'s args shape before any polling starts. Fails loud
 * (returns an error string) rather than silently defaulting to "grab whatever
 * is newest" -- that default is exactly the unsafe behavior this module
 * exists to avoid.
 *
 * @param {object} args
 * @returns {string | null} an error message, or null if args are valid.
 */
export function validateWaitDownloadArgs(args) {
  const hasDownloadId = typeof args?.download_id === "number";
  const hasSinceId = typeof args?.since_id === "number";
  if (!hasDownloadId && !hasSinceId) {
    return (
      "wait_download requires args.download_id (to wait for a SPECIFIC download you already " +
      "triggered via the `download` command) or args.since_id (a baseline max_download_id from " +
      "`downloads_list`, taken BEFORE the action that triggers a native/indirect download -- e.g. " +
      "clicking a page's own Download control -- so a download the human started themselves is " +
      "never mistaken for the agent's)"
    );
  }
  if (args.pattern !== undefined && typeof args.pattern !== "string") {
    return `args.pattern must be a string (a regex), got: ${JSON.stringify(args.pattern)}`;
  }
  return null;
}

/**
 * Select the one DownloadItem (if any) that satisfies a `wait_download`
 * request from a fresh `chrome.downloads.search({})` snapshot.
 *
 * Two mutually exclusive modes:
 *   - `download_id` mode: the caller already knows exactly which download it
 *     wants (it triggered it itself via `download`) -- match that id only.
 *   - `since_id` mode: the caller only knows a baseline -- match the LOWEST
 *     id strictly greater than the baseline (the first new download to
 *     appear after the baseline was taken), optionally narrowed by a
 *     filename regex. Never matches an id <= since_id, no matter how new
 *     that download's startTime is -- id ordering, not time, is the
 *     baseline guarantee chrome.downloads.download() ids provide.
 *
 * @param {Array<{id: number, filename?: string, state?: string}>} items
 * @param {{downloadId?: number, sinceId?: number, pattern?: RegExp}} opts
 * @returns {object | undefined} the matching item, or undefined if none yet.
 */
export function pickCompletedDownload(items, { downloadId, sinceId, pattern } = {}) {
  if (typeof downloadId === "number") {
    return items.find((d) => d.id === downloadId && d.state === "complete");
  }
  if (typeof sinceId === "number") {
    return items
      .filter((d) => d.id > sinceId && d.state === "complete")
      .filter((d) => !pattern || pattern.test(d.filename || ""))
      .sort((a, b) => a.id - b.id)[0];
  }
  return undefined;
}

/**
 * Detect an INTERRUPTED (failed) download matching the same request, so
 * `wait_download` can fail loud immediately instead of polling all the way
 * to its timeout for a download that has already, definitively, failed.
 *
 * @param {Array<{id: number, filename?: string, state?: string}>} items
 * @param {{downloadId?: number, sinceId?: number, pattern?: RegExp}} opts
 * @returns {object | undefined}
 */
export function pickInterruptedDownload(items, { downloadId, sinceId, pattern } = {}) {
  if (typeof downloadId === "number") {
    return items.find((d) => d.id === downloadId && d.state === "interrupted");
  }
  if (typeof sinceId === "number") {
    return items
      .filter((d) => d.id > sinceId && d.state === "interrupted")
      .filter((d) => !pattern || pattern.test(d.filename || ""))
      .sort((a, b) => a.id - b.id)[0];
  }
  return undefined;
}
