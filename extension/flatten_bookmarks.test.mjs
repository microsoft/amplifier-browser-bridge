import { test } from "node:test";
import assert from "node:assert/strict";

import { flattenBookmarks } from "./flatten_bookmarks.mjs";

test("empty tree flattens to an empty list", () => {
  assert.deepEqual(flattenBookmarks([]), []);
});

test("a single folder with no children flattens to one row with url: null", () => {
  const tree = [{ id: "1", parentId: "0", title: "Bookmarks Bar", index: 0 }];
  assert.deepEqual(flattenBookmarks(tree), [
    { id: "1", parent_id: "0", title: "Bookmarks Bar", url: null, date_added: null, index: 0 },
  ]);
});

test("a single bookmark leaf carries its url and date_added", () => {
  const tree = [
    { id: "2", parentId: "1", title: "Example", url: "https://example.com", dateAdded: 12345, index: 0 },
  ];
  assert.deepEqual(flattenBookmarks(tree), [
    { id: "2", parent_id: "1", title: "Example", url: "https://example.com", date_added: 12345, index: 0 },
  ]);
});

test("parents are emitted BEFORE their own children (depth-first, folder-first order)", () => {
  const tree = [
    {
      id: "1",
      parentId: "0",
      title: "Folder",
      index: 0,
      children: [
        { id: "2", parentId: "1", title: "Child A", url: "https://a.example.com", index: 0 },
        { id: "3", parentId: "1", title: "Child B", url: "https://b.example.com", index: 1 },
      ],
    },
  ];
  const flat = flattenBookmarks(tree);
  assert.deepEqual(
    flat.map((n) => n.id),
    ["1", "2", "3"]
  );
});

test("deeply nested folders are fully recursed, not just one level", () => {
  const tree = [
    {
      id: "1",
      parentId: "0",
      title: "Root",
      index: 0,
      children: [
        {
          id: "2",
          parentId: "1",
          title: "Mid",
          index: 0,
          children: [{ id: "3", parentId: "2", title: "Leaf", url: "https://leaf.example.com", index: 0 }],
        },
      ],
    },
  ];
  const flat = flattenBookmarks(tree);
  assert.deepEqual(
    flat.map((n) => n.id),
    ["1", "2", "3"]
  );
  assert.equal(flat[2].url, "https://leaf.example.com");
});

test("multiple top-level roots (Bookmarks Bar + Other Bookmarks) are both walked", () => {
  const tree = [
    { id: "1", parentId: "0", title: "Bookmarks Bar", index: 0, children: [] },
    { id: "2", parentId: "0", title: "Other Bookmarks", index: 1, children: [] },
  ];
  const flat = flattenBookmarks(tree);
  assert.deepEqual(
    flat.map((n) => n.id),
    ["1", "2"]
  );
});

test("a node with an empty children array is not walked further and produces no extra rows", () => {
  const tree = [{ id: "1", parentId: "0", title: "Empty Folder", index: 0, children: [] }];
  assert.equal(flattenBookmarks(tree).length, 1);
});

test("missing/undefined input is treated as an empty tree rather than throwing", () => {
  assert.deepEqual(flattenBookmarks(undefined), []);
});
