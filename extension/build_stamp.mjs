// build_stamp.mjs -- pure byte-hashing logic for the build-freshness handshake
// (docs/PROTOCOL.md's "hello" section, src/amplifier_browser_bridge/build_stamp.py).
//
// Zero chrome.* usage -- background.js's computeBuildStamp() is the thin wrapper
// that fetches each shipped file's bytes via chrome.runtime.getURL() and hands them
// here. Kept as its own dependency-free module for the same reason as every other
// zero-chrome.*-usage helper in this codebase (CONTRIBUTING.md's "Extension
// JavaScript" section): a real, Node-testable companion test
// (build_stamp.test.mjs) -- including a test that this produces the IDENTICAL
// digest Python's compute_build_stamp() does for the same file set, which is the
// entire point of a hub/device handshake that must never disagree with itself.
//
// This file is itself one of the files the build stamp hashes (it is a REAL
// shipped module background.js imports, added to setup.py's _EXTENSION_FILES and
// background.js's own SHIPPED_FILES mirror) -- that is NOT the forbidden
// circularity build_stamp.py's module docstring warns about. The forbidden case is
// a GENERATED file whose bytes embed the digest computed FROM the very set that
// includes it. This file is ordinary, tracked source: its bytes are fixed at commit
// time, hashed like any other shipped file, and it never contains a copy of the
// stamp it helps compute.

/**
 * SHA-256 over `entries` (`[{name, bytes}]`, any order, `bytes` a Uint8Array),
 * sorted by `name` for a fixed order, hex-encoded. Each entry's contribution is
 * its name (UTF-8 bytes) + a NUL separator + its raw bytes + a NUL separator --
 * the exact byte layout `amplifier_browser_bridge.build_stamp.compute_build_stamp`
 * reproduces on the hub side, so the two can never disagree over encoding or
 * ordering.
 *
 * @param {{name: string, bytes: Uint8Array}[]} entries
 * @returns {Promise<string>}
 */
export async function digestShippedEntries(entries) {
  const encoder = new TextEncoder();
  const separator = new Uint8Array([0]);
  const sorted = [...entries].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));

  const chunks = [];
  for (const { name, bytes } of sorted) {
    chunks.push(encoder.encode(name), separator, bytes, separator);
  }
  const combined = new Uint8Array(chunks.reduce((total, chunk) => total + chunk.length, 0));
  let offset = 0;
  for (const chunk of chunks) {
    combined.set(chunk, offset);
    offset += chunk.length;
  }

  const digest = await crypto.subtle.digest("SHA-256", combined);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}
