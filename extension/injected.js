// injected.js -- shared command-execution utilities, running in the PAGE's isolated
// world. Injected via chrome.scripting.executeScript({files: ['injected.js']}) before
// every page-world command dispatch. Idempotent: re-injection on an already-instrumented
// page is a safe no-op, because everything is guarded behind `if (!window.__abb)`.
//
// This is the ONE shared implementation of shadow-DOM-piercing traversal, element-ref
// bookkeeping, and per-command logic. The reference implementation this project
// supersedes copy-pasted an equivalent helper roughly fifteen times across separate
// scripts; this file exists so there is exactly one copy, everywhere.
//
// Element refs are stable only within the lifetime of this object -- i.e. within one
// page load. A navigation destroys `window.__abb` along with the rest of the page's
// JS context, so refs from a prior snapshot are correctly treated as gone (design doc
// §6.1: "Element refs are stable within a snapshot").

if (!window.__abb) {
  window.__abb = (() => {
    let refCounter = 0;
    const refToElement = new Map(); // ref string -> Element
    const elementToRef = new WeakMap(); // Element -> ref string

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

    function nameOf(el) {
      const aria = el.getAttribute("aria-label");
      if (aria) return aria;
      if (el.tagName === "IMG") return el.getAttribute("alt") || "";
      const text = (el.textContent || "").trim().replace(/\s+/g, " ");
      return text.slice(0, 120);
    }

    function refFor(el) {
      let ref = elementToRef.get(el);
      if (!ref) {
        ref = `e${++refCounter}`;
        elementToRef.set(el, ref);
        refToElement.set(ref, el);
      }
      return ref;
    }

    function resolveRef(ref) {
      const el = refToElement.get(ref);
      if (!el || !el.isConnected) {
        throw new Error(`stale or unknown element ref: ${ref}`);
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
        nodes.push(node);
      }
      // child_frames is frame-local (this frame's own iframe children); refs in
      // `nodes` are frame-local too (background.js qualifies them with this
      // frame's frameId when combining results from allFrames:true -- see
      // frame_refs.js and background.js's combineSnapshot()).
      return { url: location.href, title: document.title, nodes, child_frames: listChildFrames() };
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
      const el = resolveRef(ref);
      el.scrollIntoView({ block: "center", inline: "center" });
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, width: r.width, height: r.height, tag: el.tagName.toLowerCase() };
    }

    // Focus an element without dispatching a click -- used before CDP-backed
    // trusted typing (Input.insertText types into whatever currently has
    // focus; CDP has no notion of "ref").
    function focusFor(ref) {
      const el = resolveRef(ref);
      el.focus();
      return { ref };
    }

    function read() {
      return { url: location.href, title: document.title, text: document.body.innerText };
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
      switch (command) {
        case "snapshot":
          return snapshot();
        case "read":
          return read();
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
    }

    return { dispatch, resolveRef, snapshot, read, deepQueryAll, rectFor, focusFor };
  })();
}
