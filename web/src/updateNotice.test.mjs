// The Updates panel must never offer an action it cannot perform.
//
// The two failures worth pinning: offering "Download" in a browser (where
// there is no shell to download into), and offering it in an unsigned build
// (where macOS will refuse the install). Both would look fine in a screenshot.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { TONES, updateNotice } from "./updateNotice.js";

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
