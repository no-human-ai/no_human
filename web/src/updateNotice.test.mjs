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
    { inShell: true, current: "0.1.0", update: { mode: "failed", error: "x", rawError: "y" } },
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
  // desktop/updatePolicy.mjs has already classified the raw electron-updater
  // error into a short sentence by the time it reaches here — `error` is that
  // sentence, and the raw dump (headers, request id, stack) rides separately
  // in `rawError`. This panel must never re-classify or promote the raw text
  // onto the primary line.
  const n = updateNotice({
    inShell: true, current: "0.1.0",
    update: {
      mode: "failed",
      error: "Check your internet connection and try again.",
      rawError: "getaddrinfo ENOTFOUND github.com\n"
        + 'Headers: {"x-github-request-id":"ABCD:1234:56789"}',
    },
  });
  assert.equal(n.tone, "error");
  assert.equal(n.detail, "Check your internet connection and try again.");
  assert.doesNotMatch(n.detail, /ENOTFOUND|x-github-request-id/,
    "the raw dump must never appear on the primary line");
  assert.match(n.details, /ENOTFOUND/, "the raw text must still be reachable");
  assert.match(n.details, /x-github-request-id/);
  assert.deepEqual(n.actions, ["check"]);
});

test("a failed check with no rawError renders no collapsed details", () => {
  const n = updateNotice({
    inShell: true, current: "0.1.0",
    update: { mode: "failed", error: "The updater component is not available in this build." },
  });
  assert.equal(n.details, null);
});

test("the raw error is rendered only inside a collapsed <details>", () => {
  // No React renderer in this harness — read the source the same way the
  // "Settings actually sources the version" test above does.
  const src = readFileSync(fileURLToPath(new URL("./Settings.jsx", import.meta.url)), "utf8");
  assert.match(src, /<details className="update-raw">/,
    "the raw error must be wrapped in a collapsed <details> element");
  assert.match(src, /\{view\.details\}/, "the raw text must come from view.details");
  const detailsBlock = src.slice(src.indexOf('<details className="update-raw">'));
  const openTag = detailsBlock.slice(0, detailsBlock.indexOf(">") + 1);
  assert.doesNotMatch(openTag, /\bopen\b/,
    "the details element must be closed by default, never above the fold");
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

test("updateBanner offers a persisted Later only for \"available\"", () => {
  const available = updateBanner({ update: { mode: "available", latest: "0.2.1", current: "0.2.0" } });
  assert.deepEqual(available.actions, ["details", "later"],
    "an available update offers the same persisted defer Settings uses, plus a way to see more");

  const unavailable = updateBanner({
    update: { mode: "unavailable", latest: "0.2.1", current: "0.2.0" },
  });
  assert.deepEqual(unavailable.actions, ["downloads", "dismiss"],
    "the unsigned card has no persisted defer to offer — only the download page and a session-only dismiss");
  assert.equal(unavailable.actions.includes("later"), false);
});

test("the board notice is the in-flow callout, not the fixed connection strip", () => {
  const b = updateBanner({ update: { mode: "available", latest: "0.2.1", current: "0.2.0" } });
  assert.match(b.className, /\bnh-alarm\b/);
  assert.match(b.className, /\bupdate-notice\b/);
  assert.match(b.className, /\bnh-update-flow\b/);
  assert.doesNotMatch(b.className, /nh-stale-banner|nh-update-banner/,
    "must not reuse the fixed connection-strip primitive — that strip's pointer-events:none contract exists so it never eats clicks, which a clickable banner would defeat");

  const css = readFileSync(fileURLToPath(new URL("./styles.css", import.meta.url)), "utf8");
  assert.doesNotMatch(css, /\.nh-update-banner\b/,
    "the old fixed-overlay rule must be gone entirely");
  const flowRule = css.match(/\.nh-update-flow\s*\{[^}]*\}/)?.[0] ?? "";
  assert.ok(flowRule, ".nh-update-flow rule must exist");
  assert.doesNotMatch(flowRule, /position\s*:\s*fixed/,
    "the in-flow notice must never be position:fixed");
});

// Static source-analysis wiring assertions — this repo has no jsdom/React
// renderer (see settingsOverlay.test.mjs), so the mount-time wiring is read
// from the source rather than exercised through a mounted component.
const settingsSrc = readFileSync(fileURLToPath(new URL("./Settings.jsx", import.meta.url)), "utf8");
const appSrc = readFileSync(fileURLToPath(new URL("./App.jsx", import.meta.url)), "utf8");

// These two are static source-analysis, not a mount: there is no jsdom/React
// renderer in this repo (settingsOverlay.test.mjs), so they cannot prove an
// event delivered before mount actually reaches the screen. That proof lives
// in desktop/mainUpdateLast.test.mjs ("an update event with no subscriber is
// still retained" and friends), which exercises the real IPC retention path
// end to end. What these CAN and must prove is that the resolved payload is
// not silently discarded — a mutation that keeps the getLastUpdate() call but
// drops its argument (e.g. `cur ?? null`) would still pass a bare
// "getLastUpdate was called" check, so the pattern below requires the payload
// variable itself to reach setUpdate.

test("UpdatesPanel calls getLastUpdate and writes its payload into state", () => {
  const panel = settingsSrc.match(/function UpdatesPanel\(\)[\s\S]*?\n}\n/)?.[0] ?? "";
  assert.ok(panel, "UpdatesPanel not found in Settings.jsx");
  assert.match(panel, /getLastUpdate\?\.\(\)/,
    "UpdatesPanel must pull the retained result via desktop.getLastUpdate()");
  assert.match(panel, /\(p\)\s*=>\s*\{[\s\S]{0,80}setUpdate\(\s*\(cur\)\s*=>\s*cur\s*\?\?\s*p\s*\)/,
    "the resolved payload (p) must be the value written into state, not discarded");
});

test("the shell calls getLastUpdate and writes its payload into state", () => {
  assert.match(appSrc, /onUpdate\?\.\(/,
    "App.jsx must subscribe to the live nh:update push");
  assert.match(appSrc, /getLastUpdate\?\.\(\)/,
    "App.jsx must also pull the retained last result on mount");
  assert.match(appSrc, /\(p\)\s*=>\s*\{[\s\S]{0,80}setUpdate\(\s*\(cur\)\s*=>\s*cur\s*\?\?\s*p\s*\)/,
    "the resolved payload (p) must be the value written into state, not discarded");
  assert.match(appSrc, /updateBanner\(/,
    "App.jsx must call updateBanner to decide whether to render the notice");
  // Rendered as a normal block inside .nh-main, BELOW .nh-main-bar (not fixed
  // over the connection banner's slot, and not above the bar either — see
  // MAJOR-1/MAJOR-3: Windows' titleBarOverlay controls only clear .nh-main-bar).
  const flowSite = appSrc.match(/<h1 className="sr-only">[\s\S]*?<\/h1>[\s\S]{0,2500}?\{updateBar[\s\S]*?\)\}/);
  assert.ok(flowSite,
    "the update notice must be rendered in flow inside .nh-main, below the page heading");
  const mainBarThenUpdateBar = appSrc.match(/nh-main-bar[\s\S]{0,2000}?\{updateBar/);
  assert.ok(mainBarThenUpdateBar,
    "the notice must render AFTER .nh-main-bar in source order, so it sits below it — " +
    "above it, the notice's right-aligned buttons fall under Windows' titleBarOverlay " +
    "min/max/close controls, which only .nh-main-bar clears");
});

test("Later defers through the existing bridge", () => {
  const laterButton = appSrc.match(/updateBar[\s\S]{0,400}?deferUpdate\?\.\([^)]*\)/);
  assert.ok(laterButton,
    "the Later button must call the existing deferUpdate bridge with the banner's version");
});

test("Later's onClick calls deferUpdate; Dismiss's does not", () => {
  // MAJOR-2 (round 2, follow-up): a mutation that makes the unsigned card's
  // session-only "Dismiss" ALSO call deferUpdate would still leave the whole
  // web suite green, since nothing else here exercises the click. Assert the
  // lexical shape of each button's own onClick body — the only proof possible
  // without a DOM renderer (see the file-level comment above).
  // Locate each button by its actions.includes(...) gate rather than by the
  // exact shape of its onClick (single-expression vs block body), so a
  // reformat can't make this test merely fail to find its target instead of
  // exercising the assertion it exists to make.
  const laterIdx = appSrc.indexOf('includes("later")');
  assert.ok(laterIdx !== -1, "could not locate the Later button's actions gate");
  const laterBlock = appSrc.slice(laterIdx, laterIdx + 400);
  assert.match(laterBlock, /\bLater\b/, "sanity: the Later button's own text must be in range");
  assert.match(laterBlock, /deferUpdate\?\.\(/,
    "positive control: Later's onClick must call deferUpdate — proves the pattern below can fail");

  const dismissIdx = appSrc.indexOf('includes("dismiss")');
  assert.ok(dismissIdx !== -1, "could not locate the Dismiss button's actions gate");
  const dismissBlock = appSrc.slice(dismissIdx, dismissIdx + 400);
  assert.match(dismissBlock, /\bDismiss\b/, "sanity: the Dismiss button's own text must be in range");
  assert.doesNotMatch(dismissBlock, /deferUpdate/,
    "the unsigned card's session-only Dismiss must never call deferUpdate — it writes nothing");
});
