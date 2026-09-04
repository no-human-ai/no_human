import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { PHONE_QUERY, readIsPhone, subscribeIsPhone } from "./useIsPhone.js";

const here = fileURLToPath(new URL(".", import.meta.url));
const stylesCss = readFileSync(here + "styles.css", "utf8");

test("PHONE_QUERY is exactly the CSS mobile breakpoint — pins JS/CSS parity", () => {
  assert.equal(PHONE_QUERY, "(max-width: 640px)");
  assert.ok(
    stylesCss.includes("@media (max-width: 640px)"),
    "styles.css must still declare its mobile block at the same breakpoint string",
  );
});

test("readIsPhone reports phone when matchMedia matches", () => {
  const realWindow = globalThis.window;
  globalThis.window = { matchMedia: () => ({ matches: true }) };
  try {
    assert.equal(readIsPhone(), true);
  } finally {
    globalThis.window = realWindow;
  }
});

test("readIsPhone reports desktop when matchMedia does not match", () => {
  const realWindow = globalThis.window;
  globalThis.window = { matchMedia: () => ({ matches: false }) };
  try {
    assert.equal(readIsPhone(), false);
  } finally {
    globalThis.window = realWindow;
  }
});

test("readIsPhone falls back to false (desktop) when window.matchMedia is absent", () => {
  const realWindow = globalThis.window;
  globalThis.window = {};
  try {
    assert.equal(readIsPhone(), false);
  } finally {
    globalThis.window = realWindow;
  }
});

test("readIsPhone falls back to false (desktop) when window itself is absent", () => {
  const realWindow = globalThis.window;
  delete globalThis.window;
  try {
    assert.equal(readIsPhone(), false);
  } finally {
    globalThis.window = realWindow;
  }
});

test("subscribeIsPhone adds a change listener and the returned cleanup removes it", () => {
  const realWindow = globalThis.window;
  let added = 0;
  let removed = 0;
  let capturedListener = null;
  globalThis.window = {
    matchMedia: () => ({
      matches: true,
      addEventListener: (type, fn) => { assert.equal(type, "change"); added++; capturedListener = fn; },
      removeEventListener: (type, fn) => { assert.equal(type, "change"); assert.equal(fn, capturedListener); removed++; },
    }),
  };
  try {
    const unsubscribe = subscribeIsPhone(() => {});
    assert.equal(added, 1);
    assert.equal(removed, 0);
    unsubscribe();
    assert.equal(removed, 1);
  } finally {
    globalThis.window = realWindow;
  }
});

test("subscribeIsPhone falls back to addListener/removeListener when addEventListener is absent", () => {
  const realWindow = globalThis.window;
  let added = 0;
  let removed = 0;
  let capturedListener = null;
  globalThis.window = {
    matchMedia: () => ({
      matches: true,
      addListener: (fn) => { added++; capturedListener = fn; },
      removeListener: (fn) => { assert.equal(fn, capturedListener); removed++; },
    }),
  };
  try {
    const unsubscribe = subscribeIsPhone(() => {});
    assert.equal(added, 1);
    unsubscribe();
    assert.equal(removed, 1);
  } finally {
    globalThis.window = realWindow;
  }
});

test("subscribeIsPhone is a safe no-op when matchMedia is unavailable", () => {
  const realWindow = globalThis.window;
  globalThis.window = {};
  try {
    const unsubscribe = subscribeIsPhone(() => {});
    assert.doesNotThrow(unsubscribe);
  } finally {
    globalThis.window = realWindow;
  }
});
