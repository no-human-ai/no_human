// #332: the running version was readable in exactly one place — Settings >
// Updates — and About, the surface a user and a bug reporter open first, did
// not print it. Putting it on a second surface is only half the fix; the other
// half is that the two surfaces must not be able to disagree, which is what
// most of this file is about.
//
// The pure resolution is asserted directly, and the markup in a real renderer
// rather than by regexing source text — this repo has paid for that mistake.
// Two guards at the bottom do read source, and deliberately: "there is exactly
// one resolver" and "no surface holds a version literal" are facts ABOUT the
// source, and a second, stale copy of either renders perfectly.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { AboutVersion, aboutVersionView, runningVersion } from "./runningVersion.js";

const read = (name) => readFileSync(fileURLToPath(new URL(name, import.meta.url)), "utf8");

// --- the resolution ---------------------------------------------------------

test("inside the shell the shell's own version wins", () => {
  // The bridge reports the build the user actually installed; the server it
  // launched could be a different install on PATH, and after an upgrade that
  // is exactly when the two differ.
  const r = runningVersion({ desktopVersion: "0.2.3", serverVersion: "0.2.1" });
  assert.equal(r.version, "0.2.3");
  assert.equal(r.source, "shell");
});

test("in a plain browser the server answers, because there is no bridge", () => {
  const r = runningVersion({ desktopVersion: undefined, serverVersion: "0.2.3" });
  assert.equal(r.version, "0.2.3");
  assert.equal(r.source, "server");
});

test("neither source answered: null, never an invented version", () => {
  const r = runningVersion({});
  assert.equal(r.version, null);
  assert.equal(r.source, null);
  assert.equal(runningVersion().version, null, "called with nothing at all");
});

test("an empty or blank shell version loses to a server that really answered", () => {
  // The precedence used to be `desktop?.version ?? server`, and `??` passes ""
  // straight through — a blank `--nh-app-version=` reaches the preload as "".
  for (const blank of ["", "   ", "\n"]) {
    const r = runningVersion({ desktopVersion: blank, serverVersion: "0.2.3" });
    assert.equal(r.version, "0.2.3", `${JSON.stringify(blank)} must not win`);
    assert.equal(r.source, "server");
  }
});

test("a non-string from either source is ignored rather than rendered", () => {
  // A malformed /api/version body (or an older bridge) must not put "[object
  // Object]" or "null" on screen where a version belongs.
  for (const junk of [null, undefined, 0, 1, {}, [], true]) {
    assert.equal(runningVersion({ desktopVersion: junk, serverVersion: junk }).version, null);
  }
  assert.equal(runningVersion({ desktopVersion: {}, serverVersion: "0.2.3" }).version, "0.2.3");
});

test("surrounding whitespace is trimmed, not printed", () => {
  assert.equal(runningVersion({ desktopVersion: " 0.2.3\n" }).version, "0.2.3");
});

// --- the copy ---------------------------------------------------------------

test("a known version is named, with the reason a reader wants it", () => {
  const view = aboutVersionView({ version: "0.2.3" });
  assert.equal(view.known, true);
  assert.equal(view.line, "no_human 0.2.3");
  assert.match(view.detail, /bug/, "the point of it being on About is the bug report");
});

test("an unknown version says so and never prints the word 'unknown'", () => {
  // "You are running no_human unknown" reads as a bug rather than as an honest
  // gap — the same judgement updateNotice.js already made about that word.
  for (const v of [null, undefined, "", "   "]) {
    const view = aboutVersionView({ version: v });
    assert.equal(view.known, false);
    assert.doesNotMatch(view.line, /unknown/i);
    assert.doesNotMatch(view.line, /no_human \S/, "must not name a version it does not have");
    assert.ok(view.detail.trim().length > 0, "an empty state still has to explain itself");
  }
  assert.equal(aboutVersionView().known, false, "called with nothing at all");
});

// --- the markup, in a renderer ----------------------------------------------

test("About renders the version it was given", () => {
  const html = renderToStaticMarkup(React.createElement(AboutVersion, { version: "0.2.3" }));
  assert.match(html, /no_human 0\.2\.3/);
  assert.match(html, /<h2>Version<\/h2>/, "it needs a heading a reader can find");
});

test("About renders the honest empty state instead of a fabricated number", () => {
  const html = renderToStaticMarkup(React.createElement(AboutVersion, { version: null }));
  assert.doesNotMatch(html, /no_human \d/, "no version was resolved — none may be shown");
  assert.doesNotMatch(html, /unknown/i);
  assert.match(html, /Version unavailable/);
});

test("the block About renders is the block that was tested", () => {
  // `node --test` cannot parse JSX, so About.jsx itself cannot be imported and
  // rendered here (no test in this suite imports a .jsx). The renderable part
  // therefore lives in this module — the same split drainChip.js made for the
  // sidebar's pause indicator — and this pins that About still renders THAT
  // component, with the version the shared hook resolved. Without it the block
  // above could be perfect and unreachable.
  const about = read("./About.jsx");
  assert.match(about, /import \{ AboutVersion \} from "\.\/runningVersion\.js";/,
    "About must render the tested block, not a second copy of it");
  assert.match(about, /<AboutVersion version=\{version\} \/>/,
    "the block must be rendered, and fed the resolved version");
  assert.match(about, /const \{ version \} = useRunningVersion\(\);/,
    "that version must come from the one resolver");
});

// --- one source, so the two surfaces cannot drift ---------------------------

test("no surface carries a version literal of its own", () => {
  // The failure this guards is a hardcoded "0.2.3" in the JSX that survives the
  // release that moves it — exactly how /openapi.json sat on 0.1.0 through two
  // bumps (tests/test_api_version.py). A literal cannot be caught by rendering,
  // because a stale one renders perfectly.
  for (const name of ["./About.jsx", "./Settings.jsx", "./runningVersion.js"]) {
    const src = read(name).replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/[^\n]*/g, "$1");
    assert.doesNotMatch(src, /["'`]\s*v?\d+\.\d+\.\d+/,
      `${name} names a version literal — it must read the running one instead`);
  }
});

test("both version surfaces resolve through the one module", () => {
  // Settings > Updates and About are the two places a version is printed. A
  // second copy of the shell-then-server precedence in either of them is the
  // divergence #332 asks to prevent, and it cannot be observed from markup:
  // two copies agree right up until the release that changes one.
  for (const name of ["./About.jsx", "./Settings.jsx"]) {
    const src = read(name);
    assert.match(src, /useRunningVersion/, `${name} must resolve the version through the hook`);
    assert.doesNotMatch(src, /nhDesktop\?\.version|nhDesktop\.version/,
      `${name} reads the bridge version directly instead of through the hook`);
    assert.doesNotMatch(src, /fetchVersion/,
      `${name} fetches the version itself instead of through the hook`);
  }
  assert.match(read("./useRunningVersion.js"), /fetchVersion/,
    "the hook is the one caller of the endpoint — if it stops calling, both surfaces go blank");
});
