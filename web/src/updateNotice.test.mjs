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

// updateBanner() — the BOARD's strictly narrower view. AC1: an automatic
// startup check's failure must never reach the board, while Settings'
// updateNotice() (exercised above) still surfaces that SAME failure via the
// existing error card unchanged — this is the one test proving both halves
// at once, so a future edit cannot fix one surface by breaking the other.
test("a failed check produces no board banner, while Settings still surfaces it", () => {
  const failed = { mode: "failed", error: "Check your internet connection and try again.", rawError: "y" };

  assert.equal(updateBanner({ update: failed }), null,
    "an automatic check's failure must never become a board notice");

  const settingsView = updateNotice({ inShell: true, current: "0.1.0", update: failed });
  assert.equal(settingsView.tone, "error");
  assert.deepEqual(settingsView.actions, ["check"],
    "a manual 'Check for updates' must still be able to retry from the SAME error card");
  assert.equal(settingsView.detail, failed.error);
});

test("updateBanner never fabricates copy for modes it declines", () => {
  for (const update of [
    null, {}, { mode: "downloading", percent: 10 }, { mode: "downloaded", latest: "0.2.0" },
    { mode: "up-to-date" }, { mode: "skipped", reason: "deferred", latest: "0.2.0" },
  ]) {
    assert.equal(updateBanner({ update }), null, `mode ${update?.mode} must render nothing on the board`);
  }
});

// AC2: Settings' "Later" must clear the BOARD notice, not just Settings' own
// view. App.jsx converges both surfaces onto the SAME `dismissedVersion` (see
// updateBanner's second parameter) driven by the one nh:update push that
// follows a persisted defer — this pins the pure decision that click relies on.
test("a dismissed/deferred version clears the board banner for that version only", () => {
  const available = { mode: "available", latest: "0.2.1" };
  assert.ok(updateBanner({ update: available }), "sanity: the version is bannerable before any dismissal");
  assert.equal(updateBanner({ update: available, dismissedVersion: "0.2.1" }), null,
    "the exact version Later was clicked for must be hidden");

  // A newer release than the one that was dismissed must still get through —
  // dismissing 0.2.1 must not permanently silence the board.
  const newer = { mode: "available", latest: "0.3.0" };
  assert.ok(updateBanner({ update: newer, dismissedVersion: "0.2.1" }),
    "a version beyond the dismissed one must still be announced");
});

test("the persisted-deferral event itself (mode: skipped) never becomes a banner", () => {
  // main.mjs's nh:update-defer pushes {mode:"skipped", reason:"deferred", ...}
  // over the very same nh:update channel Settings and the board both read —
  // that push must not itself paint a banner while it is clearing one.
  const skipped = { mode: "skipped", reason: "deferred", latest: "0.2.1" };
  assert.equal(updateBanner({ update: skipped }), null);
});

// AC3: the notice must render in normal document flow after `.nh-main-bar`,
// and its CSS must contain none of `.nh-stale-banner`'s overlapping
// positioning. Read straight from source, the same pattern the "raw error is
// rendered only inside a collapsed <details>" test above already uses — there
// is no React renderer in this harness.
test("the update banner is wired in App.jsx as a flow sibling AFTER .nh-main-bar, not inside it", () => {
  const src = readFileSync(fileURLToPath(new URL("./App.jsx", import.meta.url)), "utf8");

  const mainBarIdx = src.indexOf('className="nh-main-bar"');
  assert.ok(mainBarIdx >= 0, "the top bar element must exist");

  const bannerIdx = src.indexOf("updateBar.className");
  assert.ok(bannerIdx >= 0, "the banner must render updateBar's own className, not a hardcoded one");
  assert.ok(bannerIdx > mainBarIdx,
    "the update banner must be wired AFTER .nh-main-bar in source order, as a following sibling");

  // The banner's own `<div>` must close before <Board> opens — i.e. it is not
  // nested inside the .nh-main-bar div (which is already closed by then).
  // (Searched from bannerIdx: an explanatory code comment right above the
  // banner also mentions "<Board>" in prose, before the real JSX tag.)
  const boardIdx = src.indexOf("<Board", bannerIdx);
  assert.ok(boardIdx > bannerIdx, "the banner must be rendered before <Board>");

  assert.doesNotMatch(src.slice(bannerIdx, boardIdx), /nh-stale-banner/,
    "the update banner must not reuse .nh-stale-banner's fixed-position styling");
});

test("the .nh-update-banner rule renders in flow — no fixed/absolute positioning, no z-index, no pointer-events gate", () => {
  const css = readFileSync(fileURLToPath(new URL("./styles.css", import.meta.url)), "utf8");

  const start = css.indexOf(".nh-update-banner {");
  assert.ok(start >= 0, "the .nh-update-banner rule must exist");
  const end = css.indexOf("}", start);
  const rule = css.slice(start, end);

  assert.doesNotMatch(rule, /position\s*:/, "must not be taken out of flow with position");
  assert.doesNotMatch(rule, /z-index\s*:/, "must not layer over the top bar");
  assert.doesNotMatch(rule, /pointer-events\s*:/,
    "must not borrow .nh-stale-banner's click-through trick");
});
