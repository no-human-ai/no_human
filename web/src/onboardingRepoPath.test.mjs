import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// Guards the REPOSITORIES step's discovered-repo list (.ob-repolist in
// Onboarding.jsx): two checkouts of the same repo used to render as two
// identical rows with no path anywhere. There is no DOM renderer in this
// project, so - like onboardingRoster.test.mjs - this reads the source and
// asserts on it directly.

const SRC = dirname(fileURLToPath(import.meta.url));
const jsx = readFileSync(join(SRC, "Onboarding.jsx"), "utf8");
const css = readFileSync(join(SRC, "styles.css"), "utf8");

// Pull just the discovered-repo list block (the first .ob-repolist - the
// project-summary list further down has its own, unrelated block) so
// assertions don't accidentally match unrelated JSX elsewhere in the file.
const repoListBlock = jsx.match(
  /<div className="ob-repolist ph-no-capture">([\s\S]*?)\n {14}<\/div>/,
)?.[1];
assert.ok(repoListBlock, "could not locate the discovered-repo .ob-repolist block in Onboarding.jsx");

test("the repositories step computes the ambiguous-name set from the discovered rows", () => {
  assert.match(
    jsx,
    /import\s*\{[^}]*\bambiguousNames\b[^}]*\}\s*from\s*"\.\/discoveredRepos\.js"/,
    "ambiguousNames must be imported from ./discoveredRepos.js",
  );
  assert.match(
    jsx,
    /ambiguousNames\(detected\)/,
    "the ambiguous-name set must be derived from `detected`, not discovery.repos, so the manual single-root scan gets the same treatment",
  );
});

test("a colliding row renders its full path, a unique row does not", () => {
  assert.match(
    repoListBlock,
    /<span className="ob-repo-path">\{r\.path\}<\/span>/,
    "expected an .ob-repo-path span rendering the row's full path",
  );
  // The path span must never appear unconditionally - it has to be guarded by
  // the ambiguous-name check, otherwise unique rows would render a path too.
  const pathLine = repoListBlock.match(/\{[^{}]*ambiguous[^{}]*&&\s*\(\s*\n\s*<span className="ob-repo-path">/);
  assert.ok(pathLine, "the .ob-repo-path span must be conditionally rendered on the ambiguous-name set");
});

test("selection still keys on the full path", () => {
  assert.match(repoListBlock, /key=\{r\.path\}/);
  assert.match(repoListBlock, /checked=\{selectedRepos\.has\(r\.path\)\}/);
  assert.match(repoListBlock, /onChange=\{\(\) => toggleRepo\(r\.path\)\}/);
});

test("the path style exists and is not a dead selector", () => {
  assert.match(css, /\.ob-repo-path\s*\{/, "expected an .ob-repo-path rule in styles.css");
  assert.match(jsx, /className="ob-repo-path"/, "the class must actually be used in the JSX");
  assert.match(
    css,
    /\.ob-repo\s*\{[^}]*flex-wrap:\s*wrap/,
    ".ob-repo must wrap so the full-width path span can break onto its own line",
  );
});

test("the empty state delegates to searchEmptyMessage", () => {
  assert.match(
    jsx,
    /import\s*\{[^}]*\bsearchEmptyMessage\b[^}]*\}\s*from\s*"\.\/discoveredRepos\.js"/,
    "searchEmptyMessage must be imported from ./discoveredRepos.js",
  );
  assert.match(
    jsx,
    /searchEmptyMessage\(discovery, searchedPath\)/,
    "the empty-result state must delegate its wording to searchEmptyMessage, so a refused root is never misreported as an empty folder",
  );
  assert.ok(
    !jsx.includes("no git repositories there"),
    "the absence wording must live in discoveredRepos.js, not be duplicated as a literal in Onboarding.jsx",
  );
});
