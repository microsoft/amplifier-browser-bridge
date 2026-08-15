// flattenBookmarks -- pure logic, zero chrome.* usage, unit-tested with `node --test`
// (CONTRIBUTING.md's convention for logic extracted purely for testability).
//
// chrome.bookmarks.getTree() returns a NESTED tree (each node's own `children` array
// holds its subfolder's/bookmark's nodes) rooted at one or more synthetic top-level
// folders ("Bookmarks Bar", "Other Bookmarks", ...). The archive orchestrator
// (archive.py) and a human scanning a JSON file both find a flat list easier to work
// with than a tree they'd otherwise have to recurse into themselves -- this is the
// single, tested home for that flattening, imported directly by background.js (an ES
// module per manifest.json's `"type": "module"`, unlike injected.js which is loaded as
// a classic script and cannot use `import` -- see ref_registry.mjs's module docstring
// for that constraint).

/**
 * Flattens a chrome.bookmarks.getTree()-shaped node array into a single list, depth
 * first, parents before their own children. A folder node has no `url`; a bookmark
 * leaf has no `children`. Both shapes are handled uniformly -- there is no need to
 * distinguish "folder" from "bookmark" structurally, since the output row format is
 * identical either way (a `url: null` row IS how a folder is represented).
 *
 * @param {Array<object>} nodes - BookmarkTreeNode-shaped objects (id, parentId, title,
 *   url?, dateAdded?, index, children?).
 * @returns {Array<object>} flat list of {id, parent_id, title, url, date_added, index}.
 */
export function flattenBookmarks(nodes) {
  const out = [];
  const walk = (level) => {
    for (const node of level) {
      out.push({
        id: node.id,
        parent_id: node.parentId || null,
        title: node.title,
        url: node.url || null,
        date_added: node.dateAdded || null,
        index: node.index,
      });
      if (Array.isArray(node.children) && node.children.length > 0) {
        walk(node.children);
      }
    }
  };
  walk(nodes || []);
  return out;
}
