// injected.js -- shared command-execution utilities, running in the PAGE's isolated
// world. Injected via chrome.scripting.executeScript({files: ['injected.js']}) before
// every page-world command dispatch. Idempotent: re-injection on an already-instrumented
// page is a safe no-op, because everything is guarded behind `if (!window.__amplifierBrowserBridge)`.
//
// This is the ONE shared implementation of shadow-DOM-piercing traversal, element-ref
// bookkeeping, and per-command logic. The reference implementation this project
// supersedes copy-pasted an equivalent helper roughly fifteen times across separate
// scripts; this file exists so there is exactly one copy, everywhere.
//
// Element refs are stable only within the lifetime of this object -- i.e. within one
// page load. A navigation destroys `window.__amplifierBrowserBridge` along with the rest of the page's
// JS context, so refs from a prior snapshot are correctly treated as gone (design doc
// §6.1: "Element refs are stable within a snapshot").

if (!window.__amplifierBrowserBridge) {
  window.__amplifierBrowserBridge = (() => {
    let refCounter = 0;
    const refToElement = new Map(); // ref string -> Element
    const elementToRef = new WeakMap(); // Element -> ref string

    // ---------------------------------------------------------------------
    // Generation-bound ref bookkeeping (Bug 1: stale refs succeed silently)
    // ---------------------------------------------------------------------
    // Mirrors extension/ref_registry.mjs's algorithm exactly -- see that file's
    // module docstring for the full rationale and for why this is a hand-synced
    // copy rather than an import (injected.js is loaded as a classic script via
    // chrome.scripting.executeScript({files:...}), which cannot use `import`,
    // and `window.__amplifierBrowserBridge` must be synchronously ready the instant injection
    // completes -- a dynamic import would race background.js's very next
    // executeScript call). Keep the two in sync by hand, same discipline
    // CONTRIBUTING.md documents for protocol.py/background.js.
    //
    // Every `snapshot()` bumps `generation` and re-stamps every ref it touches
    // with the new value. A ref only resolves while its stamped generation
    // still equals the CURRENT generation -- i.e. only refs from the MOST
    // RECENT snapshot (or a `wait_for` that ran since) are valid. A ref from a
    // superseded snapshot is rejected outright, even if it still points at a
    // live, connected element (design doc's "Mechanism, not policy": silently
    // accepting it because the element "still works" would be exactly the
    // silent-substitution mistake that section forbids).
    let generation = 0;
    const refGeneration = new Map(); // ref string -> generation it was last (re)stamped in
    const refFingerprint = new Map(); // ref string -> {tag, name} captured at last stamp time

    function fingerprintOf(el) {
      return { tag: el.tagName, name: nameOf(el) };
    }

    // Depth-first traversal that pierces shadow roots (open shadow DOM only -- closed
    // shadow roots are, by design, unreachable from any script). Does NOT descend into
    // iframes -- injected.js runs independently in every frame chrome.scripting's
    // `allFrames: true` targets (background.js's `runInPage`/`runMultiFrame`), so
    // cross-frame traversal happens by injecting into each frame separately, not by
    // reaching across frame boundaries from here. See frame_refs.js for how refs
    // produced by different frames are kept unambiguous.
    function deepQueryAll(root) {
      const out = [];
      const walk = (node) => {
        if (!node) return;
        const children = node.children ? Array.from(node.children) : [];
        for (const el of children) {
          out.push(el);
          if (el.shadowRoot) walk(el.shadowRoot);
          walk(el);
        }
      };
      walk(root);
      return out;
    }

    function isVisible(el) {
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) return false;
      const style = window.getComputedStyle(el);
      return style.visibility !== "hidden" && style.display !== "none";
    }

    const IMPLICIT_ROLES = {
      a: "link",
      button: "button",
      input: "textbox",
      textarea: "textbox",
      select: "combobox",
      img: "img",
      h1: "heading",
      h2: "heading",
      h3: "heading",
      h4: "heading",
      h5: "heading",
      h6: "heading",
      nav: "navigation",
      form: "form",
      option: "option",
      label: "label",
      summary: "button",
    };

    function roleOf(el) {
      const explicit = el.getAttribute("role");
      if (explicit) return explicit;
      return IMPLICIT_ROLES[el.tagName.toLowerCase()] || el.tagName.toLowerCase();
    }

    // ---------------------------------------------------------------------
    // Action-descriptor field extraction (docs/designs/confirmation-gate.md
    // section 11.5, D1). Mirrors extension/action_descriptor.mjs's algorithm
    // by hand (same constraint as ref_registry.mjs above: this file cannot
    // `import` a module). These fields are PAGE-ASSERTED and therefore
    // advisory, never a security boundary -- see classify.py's module
    // docstring for the full contract.
    // ---------------------------------------------------------------------

    const HEADING_MAX_CHARS = 200;
    const HEADINGS_MAX_COUNT = 8;

    function capText(text) {
      if (typeof text !== "string") return null;
      const trimmed = text.trim();
      return trimmed.length > 0 ? trimmed.slice(0, HEADING_MAX_CHARS) : null;
    }

    function isCrossOrigin(urlString) {
      if (typeof urlString !== "string" || !urlString) return null;
      try {
        return new URL(urlString, location.href).origin !== location.origin;
      } catch {
        return null;
      }
    }

    function hrefInfoFor(el) {
      const raw = el.getAttribute && el.getAttribute("href");
      if (!raw) return { href: null, href_cross_origin: null };
      return { href: raw, href_cross_origin: isCrossOrigin(raw) };
    }

    function formInfoFor(el) {
      const form = el.closest && el.closest("form");
      if (!form) return { form_method: null, form_action: null, form_cross_origin: null };
      const method = (form.getAttribute("method") || "get").toLowerCase();
      const action = form.getAttribute("action") || null;
      return { form_method: method, form_action: action, form_cross_origin: action ? isCrossOrigin(action) : null };
    }

    function isSubmitControlFor(el) {
      const tag = el.tagName;
      if (tag === "INPUT") return el.type === "submit";
      if (tag === "BUTTON") return !el.hasAttribute("type") || el.getAttribute("type") === "submit";
      return false;
    }

    // Best-effort: walk up from `el`, checking each ancestor level's preceding
    // siblings (and their descendants) for a heading. This is a heuristic,
    // not an authoritative DOM-semantics computation -- see classify.py's
    // module docstring on why every page-asserted signal here is advisory.
    function nearestHeadingFor(el) {
      let node = el;
      while (node) {
        let sib = node.previousElementSibling;
        while (sib) {
          if (/^H[1-6]$/.test(sib.tagName)) return capText(sib.textContent);
          const found = sib.querySelector && sib.querySelector("h1,h2,h3,h4,h5,h6");
          if (found) return capText(found.textContent);
          sib = sib.previousElementSibling;
        }
        node = node.parentElement;
      }
      return null;
    }

    function dialogTitleFor(el) {
      const dialog = el.closest && el.closest('[role="dialog"], dialog');
      if (!dialog) return null;
      const aria = dialog.getAttribute("aria-label");
      if (aria) return capText(aria);
      const heading = dialog.querySelector("h1,h2,h3,h4,h5,h6");
      return heading ? capText(heading.textContent) : null;
    }

    // Assembles every additive descriptor field for one element -- called
    // from both snapshot() (per visible node) and describe() (on demand for
    // one ref, e.g. the `unknown`-classification recovery path).
    function descriptorFieldsFor(el) {
      return {
        ...hrefInfoFor(el),
        ...formInfoFor(el),
        is_submit: isSubmitControlFor(el),
        nearest_heading: nearestHeadingFor(el),
        dialog_title: dialogTitleFor(el),
      };
    }

    // h1/h2 text on the page, capped -- top-level `headings` field alongside
    // `page_title` (reuses the existing `title` field -- see snapshot()).
    function pageHeadings() {
      const seen = [];
      for (const h of document.querySelectorAll("h1, h2")) {
        const text = capText(h.textContent);
        if (text) seen.push(text);
        if (seen.length >= HEADINGS_MAX_COUNT) break;
      }
      return seen;
    }

    // B1 fix (security review finding, classifier extraction gap): an
    // element's accessible name can come from `aria-labelledby` -- a
    // space-separated list of OTHER elements' ids whose text is the name --
    // not just a direct `aria-label` attribute or the element's own text
    // content. An icon-only button with no `aria-label` but
    // `aria-labelledby="lbl"` pointing at `<span id="lbl">Elevate to
    // Administrator</span>` elsewhere on the page previously extracted as
    // empty. This mirrors accessible_name.mjs's `computeAccessibleName` by
    // hand (that file is the TESTED twin -- see its module docstring for
    // exactly which subset of the W3C accname spec this implements, and
    // which parts it deliberately does not).
    function resolveLabelledByTexts(el) {
      const attr = el.getAttribute && el.getAttribute("aria-labelledby");
      if (!attr) return [];
      const ids = attr.trim().split(/\s+/).filter(Boolean);
      if (ids.length === 0) return [];
      // Referenced ids are looked up in the element's own root (its shadow
      // root if it has one via getRootNode(), else the document) -- an
      // aria-labelledby reference does not cross shadow-DOM boundaries per
      // spec, and `getElementById` on `document` would silently miss an id
      // that only exists inside a shadow tree.
      const root = (el.getRootNode && el.getRootNode()) || document;
      return ids.map((id) => {
        const ref = (root.getElementById && root.getElementById(id)) || document.getElementById(id);
        return ref ? ref.textContent : null;
      });
    }

    function nameOf(el) {
      const labelledByTexts = resolveLabelledByTexts(el);
      const joined = labelledByTexts
        .filter((t) => typeof t === "string" && t.trim().length > 0)
        .map((t) => t.trim())
        .join(" ")
        .trim();
      if (joined) return joined.slice(0, 120);
      const aria = el.getAttribute("aria-label");
      if (aria) return aria;
      if (el.tagName === "IMG") return el.getAttribute("alt") || "";
      const text = (el.textContent || "").trim().replace(/\s+/g, " ");
      return text.slice(0, 120);
    }

    // Mints (or re-confirms) a ref for `el`, stamping it with the CURRENT
    // generation -- called for every element a snapshot pass visits, and for
    // wait_for's found element (which stamps with whatever generation is
    // current WITHOUT bumping it, valid until the next real snapshot
    // supersedes it). See ref_registry.mjs (this function's tested twin) for
    // the full rationale.
    function refFor(el) {
      let ref = elementToRef.get(el);
      if (!ref) {
        ref = `e${++refCounter}`;
        elementToRef.set(el, ref);
        refToElement.set(ref, el);
      }
      refGeneration.set(ref, generation);
      refFingerprint.set(ref, fingerprintOf(el));
      return ref;
    }

    // Bug 1 (stale refs succeed silently): fails loud with one of four
    // distinct, actionable causes rather than silently resolving a ref that
    // no longer means what the caller thinks it means. See ref_registry.mjs's
    // module docstring for the full rationale -- this mirrors that module's
    // `resolveRef` exactly.
    function resolveRef(ref) {
      const el = refToElement.get(ref);
      if (!el) {
        throw new Error(
          `unknown element ref: ${ref} -- never produced in the current page context. If a navigation or ` +
            "reload happened since you last took a snapshot, the ref table was reset -- take a fresh snapshot."
        );
      }
      const mintedGeneration = refGeneration.get(ref);
      if (mintedGeneration !== generation) {
        throw new Error(
          `stale ref: ${ref} was captured by an earlier snapshot (generation ${mintedGeneration}); the most ` +
            `recent snapshot on this page is generation ${generation}. Refs are only valid from the MOST ` +
            "RECENT snapshot -- take a fresh snapshot and use a ref from that result."
        );
      }
      if (!el.isConnected) {
        throw new Error(
          `element for ref ${ref} is no longer attached to the page (removed from the DOM since it was ` +
            `captured, still generation ${generation}) -- take a fresh snapshot.`
        );
      }
      const fp = refFingerprint.get(ref);
      if (fp) {
        const now = fingerprintOf(el);
        if (fp.tag !== now.tag || fp.name !== now.name) {
          throw new Error(
            `element for ref ${ref} no longer matches what was captured (expected tag=${fp.tag} ` +
              `name=${JSON.stringify(fp.name)}, now tag=${now.tag} name=${JSON.stringify(now.name)}) -- the ` +
              "DOM node may have been reused for different content since the snapshot. Take a fresh snapshot."
          );
        }
      }
      return el;
    }

    // Tags worth including in a snapshot even without an explicit role/aria attribute --
    // the common interactive and structural elements. Anything else must opt in via
    // role="..." or an onclick handler to show up.
    const INTERESTING_TAGS = new Set([
      "A",
      "BUTTON",
      "INPUT",
      "TEXTAREA",
      "SELECT",
      "IMG",
      "H1",
      "H2",
      "H3",
      "H4",
      "H5",
      "H6",
      "LABEL",
      "OPTION",
      "SUMMARY",
    ]);

    // Best-effort manifest of this frame's OWN direct <iframe>/<frame> children --
    // used by background.js's multi-frame combine (runMultiFrame/combineRead/
    // combineSnapshot) to report which declared child frames never produced a
    // result (sandboxed without allow-scripts, opaque/cross-origin-blocked, not
    // yet loaded, or removed mid-call). This is NOT a cross-frame reach: it only
    // ever looks at elements in THIS frame's own document, same as everything
    // else in this file.
    function listChildFrames() {
      return Array.from(document.querySelectorAll("iframe, frame")).map((f) => ({
        src: f.src || f.getAttribute("src") || null,
        name: f.getAttribute("name") || null,
      }));
    }

    function snapshot() {
      // Bug 1: every snapshot pass bumps the generation counter FIRST, then
      // (re)stamps every ref it touches with the new value via refFor() --
      // this is what makes a ref from THIS pass resolve, and a ref from any
      // earlier pass a rejected "stale ref" (see resolveRef() above).
      generation += 1;
      const myGeneration = generation;
      const elements = deepQueryAll(document.body);
      const nodes = [];
      for (const el of elements) {
        const interesting =
          INTERESTING_TAGS.has(el.tagName) || el.hasAttribute("role") || el.hasAttribute("onclick");
        if (!interesting || !isVisible(el)) continue;
        const node = { ref: refFor(el), role: roleOf(el), name: nameOf(el), tag: el.tagName.toLowerCase() };
        if ("value" in el) node.value = String(el.value ?? "");
        // input_type feeds the hub's file_upload gate detection (policy.py's
        // FILE_UPLOAD_INPUT_TYPES) -- an unambiguous alternative to fuzzy
        // label matching for input[type=file]. Only meaningful on <input>.
        if (el.tagName === "INPUT") node.input_type = el.type;
        // Action-descriptor fields (design doc section 11.5, D1) -- additive,
        // page-asserted, advisory. See descriptorFieldsFor()'s own comment.
        Object.assign(node, descriptorFieldsFor(el));
        nodes.push(node);
      }
      // child_frames is frame-local (this frame's own iframe children); refs in
      // `nodes` are frame-local too (background.js qualifies them with this
      // frame's frameId when combining results from allFrames:true -- see
      // frame_refs.js and background.js's combineSnapshot()). `generation` is
      // this frame's OWN counter (each frame gets its own window.__amplifierBrowserBridge) --
      // background.js surfaces it per-node/per-frame in the wire result so a
      // superseded ref fails loud instead of silently resolving (Bug 1).
      return {
        url: location.href,
        title: document.title,
        // page_title duplicates `title` under the descriptor's own field name
        // (design doc section 11.5: "snapshot()'s top-level result gains
        // page_title (already has title -- reuse it)") -- kept as a distinct
        // key so classify.py's ActionDescriptor.page_title reads consistently
        // whether the value came from a snapshot or a describe() call.
        page_title: document.title,
        headings: pageHeadings(),
        nodes,
        child_frames: listChildFrames(),
        generation: myGeneration,
      };
    }

    // On-demand full descriptor for one ref (design doc section 11.5) --
    // exists for the `unknown` classification recovery path: a caller that
    // gets `reason_code: "ref_not_observed"` can obtain a descriptor without
    // a full re-snapshot. Never auto-fires; the caller invokes it explicitly
    // (design doc section 13: no silent escalation).
    function describe(ref) {
      const el = resolveRef(ref);
      return {
        ref,
        role: roleOf(el),
        name: nameOf(el),
        tag: el.tagName.toLowerCase(),
        input_type: el.tagName === "INPUT" ? el.type : null,
        url: location.href,
        page_title: document.title,
        ...descriptorFieldsFor(el),
      };
    }

    // Resolve a ref to its viewport-space bounding rect, WITHOUT dispatching
    // any synthetic event -- used by background.js's CDP-backed trusted
    // input (Input.dispatchMouseEvent needs real viewport coordinates, not a
    // DOM event). Scrolls the element into view first so the rect is valid
    // even for an element currently outside the viewport; measured (design
    // doc §2/§7) that getBoundingClientRect() never returns zeros on Edge in
    // any window state (minimized, occluded), so this is reliable input to
    // CDP even for a hidden/minimized tab.
    function rectFor(ref) {
      try {
        const el = resolveRef(ref);
        el.scrollIntoView({ block: "center", inline: "center" });
        const r = el.getBoundingClientRect();
        return { x: r.x, y: r.y, width: r.width, height: r.height, tag: el.tagName.toLowerCase() };
      } catch (err) {
        // Bug 1 fix, part 2: chrome.scripting.executeScript does NOT propagate
        // an exception thrown inside the injected function back to the caller
        // as a rejected promise/thrown error -- it silently resolves that
        // frame's InjectionResult with `result: undefined` instead (measured
        // live: a bogus AND a stale ref both produced `{ok: true, result:
        // null}` before this fix, for exactly this reason). Returning an
        // explicit `{__amplifierBrowserBridgeError}` sentinel is the only way the real message
        // (e.g. "stale ref: ...") survives the executeScript boundary --
        // background.js's unwrapAmplifierBrowserBridgeResult() converts this back into a real
        // thrown Error on the extension side. See background.js's cdpClick().
        return { __amplifierBrowserBridgeError: String((err && err.message) || err) };
      }
    }

    // Focus an element without dispatching a click -- used before CDP-backed
    // trusted typing (Input.insertText types into whatever currently has
    // focus; CDP has no notion of "ref"). See rectFor()'s comment above for
    // why this catches internally and returns a sentinel rather than throwing.
    function focusFor(ref) {
      try {
        const el = resolveRef(ref);
        el.focus();
        return { ref };
      } catch (err) {
        return { __amplifierBrowserBridgeError: String((err && err.message) || err) };
      }
    }

    function read() {
      return { url: location.href, title: document.title, text: document.body.innerText };
    }

    // ---------------------------------------------------------------------
    // Browser-state archive capability -- per-tab page state (D2, archive.py)
    // ---------------------------------------------------------------------
    // outerHTML, form field values, localStorage/sessionStorage, and scroll
    // position -- the DOM/form/storage rung of the archive depth ladder
    // (L2). Top frame only by default, same documented narrower limitation
    // as scroll/wait_for/wait_text (docs/PROTOCOL.md's "Frames" section) --
    // background.js's runInPage() already supports args.frame_id for this
    // command generically (the same explicit single-frame-targeting branch
    // read/snapshot use), so a caller with a known frame id can still reach
    // an embedded document without this command needing its own
    // MULTI_FRAME_COMMANDS combine strategy.

    // A page's outerHTML can be arbitrarily large (a heavy SPA's fully
    // hydrated DOM easily exceeds several MB) -- capped the same way
    // combine_frames.mjs caps per-frame text (READ_FRAME_TEXT_CAP), with an
    // honest `truncated` flag rather than silently growing the archive
    // without bound. This caps in-memory/wire size on THIS side; the
    // archive orchestrator additionally never returns this payload as a
    // tool's return value at all (writes it straight to disk).
    const PAGE_STATE_HTML_CAP = 2_000_000;

    function capHtml(html) {
      if (html.length <= PAGE_STATE_HTML_CAP) return { html, truncated: false, chars: html.length };
      return { html: html.slice(0, PAGE_STATE_HTML_CAP), truncated: true, chars: html.length };
    }

    // Reads every key/value pair from a Storage object. Wrapped as a single
    // try/catch around the whole operation (not per-key) because merely
    // ACCESSING window.localStorage/sessionStorage can throw a SecurityError
    // in some contexts (an opaque-origin data:/about:blank frame, or storage
    // access blocked by the embedder) -- an honest empty dump in that case,
    // not a thrown error that would fail the whole page_state() call.
    function dumpStorage(getStorage) {
      try {
        const storage = getStorage();
        const out = {};
        for (let i = 0; i < storage.length; i++) {
          const key = storage.key(i);
          out[key] = storage.getItem(key);
        }
        return out;
      } catch {
        return {};
      }
    }

    function formFieldsFor(form) {
      const fields = [];
      for (const el of form.elements) {
        if (!el.name) continue;
        const isPassword = (el.type || "").toLowerCase() === "password";
        fields.push({
          name: el.name,
          type: el.type || el.tagName.toLowerCase(),
          // Password field VALUES are never captured, archived, or
          // transmitted here -- a deliberate, permanent exception (unlike
          // the cookies opt-in gate in archive.py, this is not a toggle:
          // there is no legitimate archival use for a raw password string,
          // and writing one to disk by default is a bad default regardless
          // of what chrome.scripting already permits).
          value: isPassword ? null : "value" in el ? String(el.value ?? "") : null,
        });
      }
      return fields;
    }

    function pageState() {
      const htmlCap = capHtml(document.documentElement.outerHTML);
      return {
        url: location.href,
        title: document.title,
        outer_html: htmlCap.html,
        outer_html_chars: htmlCap.chars,
        outer_html_truncated: htmlCap.truncated,
        forms: Array.from(document.forms).map((form, index) => ({
          index,
          id: form.id || null,
          name: form.getAttribute("name") || null,
          action: form.getAttribute("action") || null,
          method: (form.getAttribute("method") || "get").toLowerCase(),
          fields: formFieldsFor(form),
        })),
        local_storage: dumpStorage(() => window.localStorage),
        session_storage: dumpStorage(() => window.sessionStorage),
        scroll: { x: window.scrollX, y: window.scrollY },
      };
    }

    function click(ref) {
      const el = resolveRef(ref);
      el.scrollIntoView({ block: "center", inline: "center" });
      el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
      el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
      el.click();
      return { ref, tag: el.tagName.toLowerCase() };
    }

    function typeText(ref, text) {
      const el = resolveRef(ref);
      el.focus();
      if ("value" in el) {
        el.value = text;
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      } else {
        el.textContent = text;
      }
      return { ref };
    }

    function key(ref, keyName) {
      const el = ref ? resolveRef(ref) : document.activeElement || document.body;
      for (const type of ["keydown", "keypress", "keyup"]) {
        el.dispatchEvent(new KeyboardEvent(type, { key: keyName, bubbles: true, cancelable: true }));
      }
      return { key: keyName };
    }

    function scroll(x, y) {
      window.scrollBy(x || 0, y || 0);
      return { x: window.scrollX, y: window.scrollY };
    }

    function back() {
      history.back();
      return {};
    }

    function forward() {
      history.forward();
      return {};
    }

    // Poll-don't-sleep: check on an interval, return the instant the condition is
    // met, never block for the full timeout on the happy path.
    function sleep(ms) {
      return new Promise((resolve) => setTimeout(resolve, ms));
    }

    async function waitFor(selector, timeoutMs) {
      const deadline = Date.now() + (timeoutMs || 10000);
      while (Date.now() < deadline) {
        const found = deepQueryAll(document.body).find((el) => el.matches(selector));
        if (found) return { ref: refFor(found) };
        await sleep(150);
      }
      throw new Error(`wait_for timed out after ${timeoutMs || 10000}ms: ${selector}`);
    }

    async function waitText(text, timeoutMs) {
      const deadline = Date.now() + (timeoutMs || 10000);
      while (Date.now() < deadline) {
        if (document.body.innerText.includes(text)) return { found: true };
        await sleep(150);
      }
      throw new Error(`wait_text timed out after ${timeoutMs || 10000}ms: ${text}`);
    }

    async function dispatch(command, args) {
      args = args || {};
      // Bug 1 fix, part 2 (real-profile hardening, discovered proving Bug 1
      // live): chrome.scripting.executeScript does NOT propagate an exception
      // thrown in here back to background.js as a thrown/rejected error -- it
      // silently resolves with `result: undefined` instead. Measured live:
      // BEFORE this try/catch existed, clicking a stale ref (or a flat-out
      // bogus one) produced `{ok: true, result: null}` on the wire -- the
      // exact silent-success failure mode this bug is about -- because
      // resolveRef() threw here, but nothing on the extension side ever saw
      // it. Catching here and returning an explicit `{__amplifierBrowserBridgeError}` sentinel
      // is what lets background.js's unwrapAmplifierBrowserBridgeResult() (see its own comment)
      // turn this back into a real `{ok: false, error: ...}` for the caller.
      try {
        switch (command) {
          case "snapshot":
            return snapshot();
          case "describe":
            return describe(args.ref);
          case "read":
            return read();
          case "page_state":
            return pageState();
          case "click":
            return click(args.ref);
          case "type":
            return typeText(args.ref, args.text);
          case "key":
            return key(args.ref, args.key);
          case "scroll":
            return scroll(args.x, args.y);
          case "back":
            return back();
          case "forward":
            return forward();
          case "wait_for":
            return await waitFor(args.selector, args.timeout_ms);
          case "wait_text":
            return await waitText(args.text, args.timeout_ms);
          default:
            throw new Error(`unsupported page-world command: ${command}`);
        }
      } catch (err) {
        return { __amplifierBrowserBridgeError: String((err && err.message) || err) };
      }
    }

    return { dispatch, resolveRef, snapshot, read, deepQueryAll, rectFor, focusFor };
  })();
}
