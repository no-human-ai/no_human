import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// One shared PathInput drives directory autocomplete everywhere a human types a
// repo path. Like taskComposerJira.test.mjs, there is no React renderer in this
// harness, so the .jsx assertions here pin WIRING only; the datalist BEHAVIOUR
// (options appearing as you type) is measured in a real browser by
// e2e/composer.mjs.

const here = fileURLToPath(new URL(".", import.meta.url));
const read = (f) => readFileSync(here + f, "utf8");

test("PathInput.jsx fetches suggestions and pairs the input with its datalist", () => {
  const src = read("PathInput.jsx");
  assert.match(src, /suggestPaths/);
  assert.match(src, /<datalist id=\{listId\} className="ph-no-capture">/);  // masked: options echo operator filesystem paths
  assert.match(src, /list=\{listId\}/);
});

test("the composer's free-text repository input is the shared PathInput", () => {
  const src = read("TaskComposer.jsx");
  assert.match(src, /import PathInput from "\.\/PathInput\.jsx"/);
  assert.match(src, /<PathInput[\s\S]{0,400}aria-label="Repository path"/);
});

test("every PathInput usage has its own datalist id (duplicate ids break the browser's input-datalist pairing)", () => {
  const composer = read("TaskComposer.jsx");
  const settings = read("Settings.jsx");
  const ids = [
    ...composer.matchAll(/listId="([^"]+)"/g),
    ...settings.matchAll(/listId="([^"]+)"/g),
  ].map((m) => m[1]);
  assert.ok(ids.length >= 3, `expected >=3 PathInput usages across composer+settings, saw ${ids.length}`);
  assert.equal(new Set(ids).size, ids.length, `datalist ids must be unique, saw: ${ids}`);
});

test("Settings uses the shared component instead of a private copy", () => {
  const settings = read("Settings.jsx");
  assert.doesNotMatch(settings, /function PathInputSettings/);
  assert.match(settings, /import PathInput from "\.\/PathInput\.jsx"/);
});

// The datalist-completion bug: a full folder name (no trailing separator) makes
// the server list the folder's CHILDREN; optionValue must be told so via a
// third `children` argument, or the option value never starts with the input
// and no completion shows. Both PathInput components must keep `res.prefix`
// and derive `children` the same way.

for (const file of ["PathInput.jsx", "Onboarding.jsx"]) {
  test(`${file} keeps the suggest response's prefix and wires children into optionValue`, () => {
    const src = read(file);
    assert.match(src, /res\.prefix/, `${file} must read res.prefix from the suggest response`);
    assert.match(src, /prefix\s*===\s*""/, `${file} must check prefix === "" for the children case`);
    // Raw source text of the trailing-separator guard: a character class
    // matching either backslash or forward slash, anchored at the end.
    assert.ok(src.includes('[\\\\/]$'), `${file} must guard against a value already ending in a separator`);
    assert.match(
      src,
      /optionValue\(\s*value\s*,\s*o\.name\s*,\s*children\s*\)/,
      `${file} must pass children as optionValue's third argument`,
    );
  });
}
