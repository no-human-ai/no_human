// The Updates panel must never offer an action it cannot perform.
//
// The two failures worth pinning: offering "Download" in a browser (where
// there is no shell to download into), and offering it in an unsigned build
// (where macOS will refuse the install). Both would look fine in a screenshot.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { TONES, updateBanner, updateNotice } from "./updateNotice.js";

test("every branch returns a known tone and a non-empty title", () => {
  const cases = [
    { inShell: false, current: "0.1.0" },
    { inShell: true, current: "0.1.0" },
    { inShell: true, current: "0.1.0", update: { mode: "available", latest: "0.2.0" } },
    { inShell: true, current: "0.1.0", update: { mode: "unavailable", latest: "0.2.0" } },
    { inShell: true, current: "0.1.0", update: { mode: "downloading", percent: 10 } },
    { inShell: true, current: "0.1.0", update: { mode: "downloaded", latest: "0.2.0" } },
    { inShell: true, current: "0.1.0", update: { mode: "up-to-date" } },
    { inShell: true, current: "0.1.0", update: { mode: "failed", error: "x" } },
  ];
  for (const c of cases) {
    const n = updateNotice(c);
    assert.ok(TONES.includes(n.tone), `unknown tone ${n.tone}`);
    assert.ok(n.title && n.title.trim().length > 0, "every state needs a title");
    assert.ok(Array.isArray(n.actions));
  }
});

test("Settings actually sources the version for the browser path", () => {
  // No React renderer in this harness (settingsOverlay.test.mjs), so the wiring
  // is read from the source: without this fetch the pure function above is
  // correct and the panel still says nothing useful.
  const src = readFileSync(fileURLToPath(new URL("./Settings.jsx", import.meta.url)), "utf8");
  assert.match(src, /fetchVersion/, "Settings must import and call fetchVersion");
  assert.match(src, /current:\s*desktop\?\.version\s*\?\?\s*versionInfo\?\.version/,
    "the shell's own version must still win; the server is the fallback");
  const api = readFileSync(fileURLToPath(new URL("./api.js", import.meta.url)), "utf8");
  assert.match(api, /\/api\/version/, "fetchVersion must call the endpoint that serves it");
});

test("an unpublished channel never prints an install command", () => {
  // No channel payload at all (older server, or the lookup failed) — the
  // softened copy, never a command that might 404.
  const n = updateNotice({ inShell: false, current: "0.4.2" });
  assert.doesNotMatch(n.detail, /pip install/);
  assert.deepEqual(n.actions, [],
    "there is no shell to install into — the only route is pip, and it isn't proven here");
  assert.equal(n.tone, "info");
});

test("a published channel names the real distribution", () => {
  const n = updateNotice({
    inShell: false, current: "0.4.2",
    channel: { distName: "no-human", published: true },
  });
  assert.match(n.detail, /pip install --upgrade no-human/);
});

test("a browser states the version it is actually running", () => {
  // There is no preload bridge outside the shell, so `current` used to be
  // undefined here and the panel printed "You are running no_human unknown in
  // a browser". Settings now sources it from GET /api/version.
  const n = updateNotice({ inShell: false, current: "0.4.2" });
  assert.match(n.detail, /You are running no_human 0\.4\.2 in a browser/);
  assert.equal(n.version, "0.4.2");
});

test("a browser that cannot learn its version says less, not 'unknown'", () => {
  // The remaining path is a failed lookup. "unknown" is a non-answer that reads
  // as a bug; the sentence is still true with the version left out.
  for (const current of [null, undefined, ""]) {
    const n = updateNotice({ inShell: false, current });
    assert.match(n.detail, /You are running no_human in a browser/);
    assert.equal(n.detail.includes("unknown"), false, "the word must be gone from the copy");
    assert.equal(n.detail.includes("null"), false);
    assert.equal(n.detail.includes("undefined"), false);
    // The structured field is unchanged — it still never invents a version.
    assert.equal(n.version, "unknown");
  }
});

test("an available update offers download AND later, and downloads nothing yet", () => {
  const n = updateNotice({
    inShell: true, current: "0.1.0",
    update: { mode: "available", latest: "0.2.0" },
  });
  assert.deepEqual(n.actions, ["download", "later"],
    "the operator asked to be informed and then choose");
  assert.match(n.title, /0\.2\.0/);
  assert.match(n.detail, /Nothing downloads until you choose/);
});

test("an UNSIGNED build still announces the update but offers no install", () => {
  const n = updateNotice({
    inShell: true, current: "0.1.0",
    update: { mode: "unavailable", latest: "0.2.0",
              message: "not code-signed, download manually" },
  });
  assert.match(n.title, /0\.2\.0 is available/,
    "the user must still learn a new version exists");
  assert.equal(n.actions.includes("download"), false,
    "offering an install macOS will refuse is worse than offering none");
  assert.equal(n.tone, "warn");
  assert.match(n.detail, /code-signed|manually/i, "it must say why");
});

test("a downloaded update offers install and later, never an auto-restart", () => {
  const n = updateNotice({
    inShell: true, current: "0.1.0",
    update: { mode: "downloaded", latest: "0.2.0" },
  });
  assert.deepEqual(n.actions, ["install", "later"]);
  assert.equal(n.tone, "ok");
});

test("download progress reports the real percentage, never a fabricated one", () => {
  const n = updateNotice({
    inShell: true, current: "0.1.0",
    update: { mode: "downloading", percent: 37 },
  });
  assert.match(n.title, /37%/);
  // A missing percent must read as 0, not as NaN% or a guess.
  const missing = updateNotice({
    inShell: true, current: "0.1.0", update: { mode: "downloading" },
  });
  assert.match(missing.title, /0%/);
  assert.equal(missing.title.includes("NaN"), false);
});

test("an unknown version is rendered as unknown, never invented", () => {
  for (const current of [null, undefined, ""]) {
    const n = updateNotice({ inShell: true, current });
    assert.equal(n.version, "unknown");
    assert.equal(n.title.includes("null"), false);
    assert.equal(n.title.includes("undefined"), false);
  }
});

test("a failed check surfaces the reason and offers a retry", () => {
  const n = updateNotice({
    inShell: true, current: "0.1.0",
    update: { mode: "failed", error: "getaddrinfo ENOTFOUND" },
  });
  assert.equal(n.tone, "error");
  assert.match(n.detail, /ENOTFOUND/, "the real cause must reach the user");
  assert.deepEqual(n.actions, ["check"]);
});

test("the default state explains the policy rather than showing nothing", () => {
  const n = updateNotice({ inShell: true, current: "0.1.0" });
  assert.match(n.detail, /once a day/);
  assert.deepEqual(n.actions, ["check"]);
});

test("it never throws on a malformed payload", () => {
  for (const update of [null, {}, { mode: "nonsense" }, { mode: null }]) {
    assert.doesNotThrow(() => updateNotice({ inShell: true, current: "0.1.0", update }));
  }
  assert.doesNotThrow(() => updateNotice());
});

// updateBanner: the board-shell-level notice for the automatic startup check.
// This is the fix for the bug this whole module was named for — the startup
// check's one "nh:update" push used to fire before Settings' UpdatesPanel (its
// only subscriber) mounted, so nobody outside Settings ever saw it.

test("updateBanner shows an available update", () => {
  const b = updateBanner({ update: { mode: "available", latest: "0.2.1", current: "0.2.0" } });
  assert.ok(b, "an available update must produce a banner");
  assert.match(b.text, /0\.2\.1/);
  assert.equal(b.version, "0.2.1");
  assert.equal(b.role, "status");
});

test("updateBanner shows an unavailable one, which is the unsigned case", () => {
  // The exact incident reported: an unsigned build's automatic check reports
  // "unavailable" (can't self-update), and this is the mode that was silently
  // dropped — the amber card only ever appeared after a MANUAL check.
  const b = updateBanner({
    update: {
      mode: "unavailable", latest: "0.2.1", current: "0.2.0",
      message: "no_human 0.2.1 is available (you have 0.2.0), but this build"
        + " is not code-signed, so it cannot update itself. Download the new"
        + " version manually.",
    },
  });
  assert.ok(b, "an unavailable (unsigned) update must still produce a banner");
  assert.match(b.text, /0\.2\.1/);
  assert.equal(b.version, "0.2.1");
});

test("updateBanner is silent for up-to-date, failed, downloading, downloaded and null", () => {
  const silentModes = ["up-to-date", "failed", "downloading", "downloaded"];
  for (const mode of silentModes) {
    const b = updateBanner({ update: { mode, latest: "0.2.1", current: "0.2.0" } });
    assert.equal(b, null, `mode "${mode}" must not produce a board banner`);
  }
  assert.equal(updateBanner({ update: null }), null);
  assert.equal(updateBanner(), null);
});

test("updateBanner hides a version the user deferred", () => {
  const update = { mode: "available", latest: "0.2.1", current: "0.2.0" };
  assert.ok(updateBanner({ update }), "sanity: shows without a dismissal");
  const b = updateBanner({ update, dismissedVersion: "0.2.1" });
  assert.equal(b, null, "a version the user clicked Later on must not re-show this session");
  // A DIFFERENT (newer) version must still break through the old deferral.
  const b2 = updateBanner({
    update: { mode: "available", latest: "0.3.0", current: "0.2.0" },
    dismissedVersion: "0.2.1",
  });
  assert.ok(b2, "a newer version than the one dismissed must still show");
});

// Static source-analysis wiring assertions — this repo has no jsdom/React
// renderer (see settingsOverlay.test.mjs), so the mount-time wiring is read
// from the source rather than exercised through a mounted component.
const settingsSrc = readFileSync(fileURLToPath(new URL("./Settings.jsx", import.meta.url)), "utf8");
const appSrc = readFileSync(fileURLToPath(new URL("./App.jsx", import.meta.url)), "utf8");

test("UpdatesPanel seeds itself from getLastUpdate on mount", () => {
  const panel = settingsSrc.match(/function UpdatesPanel\(\)[\s\S]*?\n}\n/)?.[0] ?? "";
  assert.ok(panel, "UpdatesPanel not found in Settings.jsx");
  assert.match(panel, /getLastUpdate/,
    "UpdatesPanel must pull the retained result via desktop.getLastUpdate()");
  assert.match(panel, /setUpdate\(\s*\(cur\)\s*=>\s*cur\s*\?\?/,
    "the seed must use the non-clobbering functional form, so a live event that raced ahead is never overwritten");
});

test("the shell subscribes AND seeds, so an event fired before mount still shows", () => {
  assert.match(appSrc, /onUpdate\?\.\(/,
    "App.jsx must subscribe to the live nh:update push");
  assert.match(appSrc, /getLastUpdate\?\.\(\)/,
    "App.jsx must also pull the retained last result on mount");
  assert.match(appSrc, /updateBanner\(/,
    "App.jsx must call updateBanner to decide whether to render the notice");
  // Rendered at the same host as the connection banner, so it inherits the
  // shell's existing top-of-shell banner slot rather than a new one.
  const bannerSite = appSrc.match(/\{banner\s*&&\s*\([\s\S]*?\)\}\s*\n\s*\{updateBar[\s\S]*?\)\}/);
  assert.ok(bannerSite,
    "the update banner must be rendered immediately next to the connection banner's output");
});

test("Later defers through the existing bridge", () => {
  const laterButton = appSrc.match(/updateBar[\s\S]{0,400}?deferUpdate\?\.\([^)]*\)/);
  assert.ok(laterButton,
    "the Later button must call the existing deferUpdate bridge with the banner's version");
});
