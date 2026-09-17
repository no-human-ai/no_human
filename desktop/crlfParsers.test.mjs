// Repo guard: no SHIPPED desktop parser may drop a line just because a
// Windows editor or writer terminated it with CRLF.
//
// tokenStore.mjs's parseEnv was exactly this bug: `text.split("\n")` left a
// trailing "\r" on every line, and `/^\s*(KEY)\s*=\s*(.*)$/` — `.` excludes
// ALL line terminators and `$` is un-anchored by `m` — then failed to match
// the WHOLE line, silently dropping it. That one regex is fixed elsewhere
// (desktop/tokenStore.test.mjs). This file is the CLASS guard: it scans every
// shipped (`nhPackagedFiles.files`) desktop .mjs source for every `.split(`
// call and requires each site to be either CRLF-aware, already normalised
// before the split, or on an explicit allowlist of splits that are not
// line-ending splits over a Windows-writable file at all. A new bare
// `split("\n")` added to a packaged file in the future fails this test
// immediately, rather than waiting for another multi-week field report.
//
// Enumeration command this test encodes (see PLAN.md):
//   grep -n 'split(' desktop/*.mjs | grep -v '\.test\.'
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { configuredPort } from "./server.mjs";
import { configuredProfile, configuredAuthMode, setAuthMode } from "./tokenStore.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const pkg = JSON.parse(fs.readFileSync(path.join(here, "package.json"), "utf8"));

// The enumeration IS `nhPackagedFiles.files` — the same allowlist
// packagedFiles.test.mjs already enforces stays in sync with what main.mjs
// loads. Scanning anything wider (e.g. desktop/smoke.mjs, a dev-only script)
// would flag sites nobody ships.
const PACKAGED_MJS_FILES = pkg.nhPackagedFiles.files.filter((f) => f.endsWith(".mjs"));

// Sites where `.split(` does NOT split a Windows-writable file's line
// endings — each has an inline reason. Keyed by "file:lineNumber" from the
// CURRENT source, so a line-number drift makes this test fail loudly (the
// site moved and must be re-verified) rather than silently going blind.
const ALLOWLIST = {
  "tokenStore.mjs:93": "splits `icacls` STDOUT (a Windows command's own "
    + "output), not a file a user or editor writes; each resulting line is "
    + "already .trim()'d before use.",
  "tokenStore.mjs:102": "splits a SINGLE already-line-split icacls line on "
    + "':(' to separate the principal from its permissions — not a "
    + "line-ending split.",
  "server.mjs:209": "splits the PATH environment string on path.delimiter "
    + "(':' or ';'), not a line ending.",
  "server.mjs:434": "splits a PATH-shaped string on path.delimiter, same as "
    + "server.mjs:209.",
  "server.mjs:509": "splits already-captured process output on \"\\n\" only "
    + "to slice the last N lines for on-screen DISPLAY in an error banner — "
    + "not parsed for keys/values, so a stray \\r renders as a harmless "
    + "trailing space, never a dropped credential.",
  "setupGate.mjs:131": "splits text on a SECRET VALUE to redact it — the "
    + "argument is a token, not a line ending.",
  "updatePolicy.mjs:27": "splits a semver STRING on '-'/'+' then '.', not a "
    + "line ending.",
};

/** Classify every `.split(` call site in *file*'s current source. */
function scanFile(file) {
  const src = fs.readFileSync(path.join(here, file), "utf8");
  const lines = src.split("\n");
  const sites = [];
  lines.forEach((line, i) => {
    if (!line.includes(".split(")) return;
    const lineNo = i + 1;
    const key = `${file}:${lineNo}`;
    let kind;
    if (/\.split\(\s*\/\\r\?\\n\//.test(line)) {
      kind = "fixed";               // split(/\r?\n/) — CRLF-aware
    } else if (/\.replace\(\s*\/\\r\\n\/g[^)]*\)[^;]*\.split\(/.test(line)) {
      kind = "already-safe";        // normalises CRLF onto LF before splitting
    } else if (Object.prototype.hasOwnProperty.call(ALLOWLIST, key)) {
      kind = "allowlisted";
    } else {
      kind = "violation";
    }
    sites.push({ file, line: lineNo, text: line.trim(), kind, reason: ALLOWLIST[key] });
  });
  return sites;
}

const ALL_SITES = PACKAGED_MJS_FILES.flatMap(scanFile);

test("no shipped desktop parser splits file text on a bare \\n", () => {
  const violations = ALL_SITES.filter((s) => s.kind === "violation");
  assert.deepEqual(violations, [],
    "every .split( call in a packaged desktop .mjs must be CRLF-aware "
    + "(split(/\\r?\\n/)), pre-normalised (.replace(/\\r\\n/g,\"\\n\") first), "
    + "or named on crlfParsers.test.mjs's ALLOWLIST with a reason it is not a "
    + "line-ending split over a Windows-writable file. Found unaccounted "
    + `site(s): ${JSON.stringify(violations)}`);

  // Guard the guard: this must have looked at real, non-trivial source — a
  // detector that silently sees zero split( calls anywhere would pass this
  // assertion vacuously and protect nothing.
  assert.ok(ALL_SITES.length >= 10,
    `expected to find at least 10 .split( call sites across the packaged `
    + `desktop .mjs files, found ${ALL_SITES.length} — the scan may be `
    + "broken (wrong directory, empty file list, etc).");
});

test("positive control: the enumeration can see an already-CRLF-safe site", () => {
  // Without this, ALLOWLIST could (accidentally or not) list every real
  // site and the previous test would pass having verified nothing.
  const setAuthModeSite = ALL_SITES.find(
    (s) => s.file === "tokenStore.mjs" && /split\(\/\\r\?\\n\//.test(s.text));
  assert.ok(setAuthModeSite,
    "expected to find a tokenStore.mjs site using split(/\\r?\\n/) — if this "
    + "fails, the scan's regex-detection is broken, not that the site moved");
  assert.equal(setAuthModeSite.kind, "fixed");

  const docRenderSite = ALL_SITES.find((s) => s.file === "docRender.mjs");
  assert.ok(docRenderSite,
    "expected to find docRender.mjs's line-splitting call — if this fails "
    + "the scan is not looking at docRender.mjs at all");
  assert.match(docRenderSite.text, /replace\(\/\\r\\n\/g/,
    "docRender.mjs must normalise CRLF to LF before splitting");
  assert.equal(docRenderSite.kind, "already-safe",
    "the scan must actively RECOGNISE the pre-normalisation, not merely "
    + "fail to flag it for some other reason (e.g. an overbroad allowlist)");
});

test("configuredPort/configuredProfile/configuredAuthMode read a CRLF config.yaml", () => {
  const dir = mkdtempSync(join(tmpdir(), "nh-crlf-cfg-"));
  fs.mkdirSync(join(dir, ".no_human"), { recursive: true });
  const p = join(dir, ".no_human", "config.yaml");
  fs.writeFileSync(p,
    "server:\r\n  port: 9123\r\nllm:\r\n  auth_mode: api_key\r\n"
    + "  auth_profile: personal\r\n");
  assert.equal(configuredPort(p), 9123,
    "a CRLF config.yaml must not strand the port probe on the default 8420");
  assert.equal(configuredProfile(dir), "personal");
  assert.equal(configuredAuthMode(dir), "api_key");
});

test("POSITIVE CONTROL: setAuthMode round-trips a CRLF config.yaml", () => {
  const dir = mkdtempSync(join(tmpdir(), "nh-crlf-setmode-"));
  fs.mkdirSync(join(dir, ".no_human"), { recursive: true });
  const p = join(dir, ".no_human", "config.yaml");
  fs.writeFileSync(p,
    "server:\r\n  port: 9123\r\nllm:\r\n  auth_mode: subscription\r\n"
    + "  auth_profile: personal\r\n");
  setAuthMode("api_key", dir);
  const out = fs.readFileSync(p, "utf8");
  assert.match(out, /auth_mode: api_key/, "the mode must actually change");
  assert.ok(!out.includes("\n") || out.split("\n").every(
    (l, i, a) => i === a.length - 1 || l.endsWith("\r")),
    "a CRLF config.yaml must stay entirely CRLF after the rewrite — no "
    + "mixed line endings");
  assert.match(out, /port: 9123/, "unrelated keys must survive untouched");
  assert.match(out, /auth_profile: personal/, "unrelated keys must survive untouched");
});
