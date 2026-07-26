// ref_registry.mjs -- pure, DOM-free reference implementation of the generation-bound
// ref bookkeeping algorithm `injected.js` uses for `snapshot`/`click`/`type`/`key`/
// `wait_for`. Deliberately has ZERO chrome.*/window/document usage so it can be
// unit-tested directly with plain `node --test` (see ref_registry.test.mjs) --
// the same pattern frame_refs.mjs/combine_frames.mjs established.
//
// ## Why this is a SEPARATE, hand-synced copy rather than something injected.js imports
//
// `injected.js` is injected via `chrome.scripting.executeScript({files: ['injected.js']})`
// as a plain CLASSIC script (no `type: module`) -- it cannot use static `import`.
// Dynamic `import()` was considered and rejected: `window.__abb` must be
// synchronously available the instant injection completes (background.js's very next
// `executeScript` call invokes `window.__abb.dispatch(...)` immediately afterward), and
// an async import introduces a real race between "file injected" and "__abb ready"
// for a savings that's purely about test convenience. So the bookkeeping algorithm
// below is mirrored, by hand, inside injected.js's `__abb` closure (its own `refFor`/
// `resolveRef`) -- exactly the discipline CONTRIBUTING.md already documents for
// protocol.py/background.js ("keep the two protocol implementations in sync by hand").
// This module exists so the ALGORITHM'S correctness (not injected.js's literal source)
// has real, fast, deterministic test coverage; the live-browser proof required for this
// bug additionally proves injected.js's own copy matches.
//
// ## Bug 1 (stale refs succeed silently) -- the design
//
// Every `snapshot()` call bumps a per-frame `generation` counter and re-stamps every
// ref it touches with the NEW generation. A ref is valid to resolve ONLY while its
// stamped generation still equals the CURRENT generation -- i.e., only refs from the
// MOST RECENT snapshot (or a `wait_for` that ran since then) resolve. A ref from a
// superseded snapshot is rejected outright, even if it happens to still point at a
// live, connected element -- see docs/designs/browser-bridge.md's "Mechanism, not
// policy" section: silently accepting it (because the element still "works") would be
// exactly the silent-substitution mistake that section forbids. `resolveRef` fails
// loud with one of four distinct, actionable causes:
//
//   1. unknown ref     -- never minted in the current page context (bad ref, or a
//                          navigation/reload reset the whole table)
//   2. stale ref       -- minted by an earlier, now-superseded snapshot generation
//   3. disconnected    -- same generation, but the element left the DOM
//   4. identity change -- same generation, still connected, but tag/accessible-name
//                          no longer match what was captured (e.g. a virtualized list
//                          recycled the DOM node for different content without any
//                          new snapshot happening) -- the same silent-failure class as
//                          a stale generation, just without a generation bump to catch
//                          it structurally. Cheap to check (a tag/string compare), so
//                          worth doing: an element "still there" but silently NOT the
//                          one the caller inspected is exactly the failure this bug
//                          report is about.

export class StaleRefError extends Error {}

/**
 * @returns {{
 *   beginSnapshot: () => number,
 *   currentGeneration: () => number,
 *   mintRef: (el: object, fingerprintOf: (el: object) => {tag: string, name: string}) => string,
 *   resolveRef: (ref: string, fingerprintOf?: (el: object) => {tag: string, name: string}) => object,
 * }}
 */
export function createRefRegistry() {
  let refCounter = 0;
  let generation = 0;
  const refToElement = new Map(); // ref string -> element (or duck-typed stand-in in tests)
  const elementToRef = new WeakMap(); // element -> ref string
  const refGeneration = new Map(); // ref string -> generation it was last (re)stamped in
  const refFingerprint = new Map(); // ref string -> {tag, name} captured at last stamp time

  /** Call once at the start of every `snapshot()` pass. Returns the new generation. */
  function beginSnapshot() {
    generation += 1;
    return generation;
  }

  function currentGeneration() {
    return generation;
  }

  /**
   * Mint (or re-confirm) a ref for `el`, stamping it with the CURRENT generation.
   * Called for every element a snapshot pass visits, and for `wait_for`'s found
   * element (which stamps with whatever generation is current without bumping it --
   * valid until the NEXT real snapshot supersedes it).
   */
  function mintRef(el, fingerprintOf) {
    let ref = elementToRef.get(el);
    if (!ref) {
      ref = `e${++refCounter}`;
      elementToRef.set(el, ref);
      refToElement.set(ref, el);
    }
    refGeneration.set(ref, generation);
    if (fingerprintOf) refFingerprint.set(ref, fingerprintOf(el));
    return ref;
  }

  /**
   * Resolve `ref` to its element, or throw `StaleRefError` with a specific,
   * actionable cause. `fingerprintOf`, if supplied, re-derives the element's
   * current {tag, name} for the identity-drift check (case 4 above); omit it to
   * skip that check (e.g. for a caller that never supplied one at mint time).
   */
  function resolveRef(ref, fingerprintOf) {
    const el = refToElement.get(ref);
    if (!el) {
      throw new StaleRefError(
        `unknown element ref: ${ref} -- never produced in the current page context. If a navigation or ` +
          "reload happened since you last took a snapshot, the ref table was reset -- take a fresh snapshot."
      );
    }
    const mintedGeneration = refGeneration.get(ref);
    if (mintedGeneration !== generation) {
      throw new StaleRefError(
        `stale ref: ${ref} was captured by an earlier snapshot (generation ${mintedGeneration}); the most ` +
          `recent snapshot on this page is generation ${generation}. Refs are only valid from the MOST ` +
          "RECENT snapshot -- take a fresh snapshot and use a ref from that result."
      );
    }
    if (!el.isConnected) {
      throw new StaleRefError(
        `element for ref ${ref} is no longer attached to the page (removed from the DOM since it was ` +
          `captured, still generation ${generation}) -- take a fresh snapshot.`
      );
    }
    if (fingerprintOf) {
      const fp = refFingerprint.get(ref);
      const now = fingerprintOf(el);
      if (fp && (fp.tag !== now.tag || fp.name !== now.name)) {
        throw new StaleRefError(
          `element for ref ${ref} no longer matches what was captured (expected tag=${fp.tag} ` +
            `name=${JSON.stringify(fp.name)}, now tag=${now.tag} name=${JSON.stringify(now.name)}) -- the ` +
            "DOM node may have been reused for different content since the snapshot. Take a fresh snapshot."
        );
      }
    }
    return el;
  }

  return { beginSnapshot, currentGeneration, mintRef, resolveRef };
}
