// Unit tests for the pure parts of packaging/linux-acceptance.mjs — the Lane-A
// driver that launches the INSTALLED Linux app under a throwaway HOME on the CI
// runner (docs/LINUX.md §4/§6).
// The driver itself only runs on Linux with an installed package; these pin the
// argument contract and the dummy-credential shape so a CI edit cannot silently
// point it at the wrong binary or hand the setup screen a value it rejects.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { DUMMY_TOKEN, EMAIL, SURFACES, expectedBoardUrl, parseArgs }
  from "../packaging/linux-acceptance.mjs";
import { validateToken } from "./tokenStore.mjs";

const DRIVER_PATH = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "packaging", "linux-acceptance.mjs");
const SURFACES_PATH = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "packaging", "linuxAcceptanceSurfaces.mjs");
const driverSource = fs.readFileSync(DRIVER_PATH, "utf8");
const surfacesSource = fs.readFileSync(SURFACES_PATH, "utf8");

test("parseArgs requires --exe and --home, defaults --mode to setup", () => {
  assert.throws(() => parseArgs([]), /--exe/);
  assert.throws(() => parseArgs(["--exe", "/opt/no_human/no_human"]), /--home/);
  const a = parseArgs(["--exe", "/opt/no_human/no_human", "--home", "/tmp/h", "--out", "/tmp/o"]);
  assert.deepEqual(a, { exe: "/opt/no_human/no_human", home: "/tmp/h", out: "/tmp/o", mode: "setup" });
  assert.equal(parseArgs(["--exe", "x", "--home", "y", "--mode", "board"]).mode, "board");
  assert.throws(() => parseArgs(["--exe", "x", "--home", "y", "--mode", "nope"]), /--mode/);
  assert.throws(() => parseArgs(["--exe", "x", "--home", "y", "--bogus"]), /unrecognized/);
});

test("the dummy token passes the setup screen's own validator and can never be a real credential", () => {
  // The same function token.html calls before saving — if its rules move, this
  // fails here instead of on a runner 20 minutes into a build.
  assert.equal(validateToken(DUMMY_TOKEN), "");
  assert.match(DUMMY_TOKEN, /^sk-ant-oat01-/);
  assert.match(DUMMY_TOKEN, /dummy-not-a-real-token$/);
  assert.doesNotMatch(DUMMY_TOKEN, /\s/);
});

test("the board URL is loopback on the configured port", () => {
  assert.equal(expectedBoardUrl(8420), "http://127.0.0.1:8420/");
});

// ── screenshot inventory (AC-1/AC-3): the driver's own filenames, pinned ────
// The bug this fixes: the driver wrote "02-board.png" right after the
// credential save — a screenshot of onboarding step 1 of 7 (Welcome), never
// the board — and every downstream consumer treated it as board evidence.
// These pin the honest replacement: five filenames, each declared once in
// packaging/linuxAcceptanceSurfaces.mjs's SURFACES table (re-exported here so
// a future edit to the driver cannot silently reintroduce an unpinned,
// unproven screenshot).

test("the driver writes exactly the five SURFACES screenshots, in order", () => {
  assert.deepEqual(SURFACES.map((s) => s.file), [
    "01-credential-screen.png",
    "02-onboarding-welcome.png",
    "03-board-first-run.png",
    "04-settings.png",
    "05-stats.png",
  ]);
});

test("no screenshot filename says \"board\" for the wizard's Welcome step, and the board's own filename does say board", () => {
  const welcome = SURFACES.find((s) => s.key === "onboarding-welcome");
  const board = SURFACES.find((s) => s.key === "board-first-run");
  assert.ok(welcome && board);
  assert.doesNotMatch(welcome.file, /\bboard\b/i);
  assert.match(board.file, /\bboard\b/i);
});

test("the driver contains exactly one win.screenshot call site — every filename is data-driven through SURFACES, not an ad-hoc literal", () => {
  const hits = driverSource.match(/\.screenshot\(/g) || [];
  assert.equal(hits.length, 1,
    `expected exactly one .screenshot( call (the shared "shot" helper); found ${hits.length}. `
    + "Every screenshot the driver writes must go through walkSurfaces()/SURFACES so a filename can never outrun its DOM proof.");
});

// The gap this closes: the walker and the SURFACES table are both pinned
// above, but nothing previously pinned what the DRIVER actually hands the
// walker — `walkSurfaces(win, SURFACES.slice(2), …)` is the one call site
// that decides whether Settings and Stats get captured at all. A regression
// that narrowed it (e.g. `SURFACES.slice(2, 3)`, dropping Settings and
// Stats, or `SURFACES.slice(2, 4)`, dropping just Stats) would leave every
// other test in this file and in linuxAcceptanceSurfaces.test.mjs green,
// because those only exercise the walker/table in isolation. This test
// extracts the literal second argument of each `walkSurfaces(win, …)` call
// straight from the driver's source and evaluates it against the REAL
// SURFACES array, so it fails the moment the driver stops feeding the
// walker the full board→Settings→Stats tail.
test("the driver's walkSurfaces calls together cover every SURFACES entry exactly once, in order", () => {
  // Lazy match up to the shared third-argument marker (`{ shot`), not the
  // first comma — the surfaces expression itself can contain a comma (e.g. a
  // mutated `SURFACES.slice(2, 3)`), and stopping at the first comma would
  // truncate that expression instead of evaluating it.
  const calls = [...driverSource.matchAll(/walkSurfaces\(\s*win\s*,\s*([\s\S]*?),\s*\{\s*shot/g)].map((m) => m[1].trim());
  assert.equal(calls.length, 3, `expected exactly 3 walkSurfaces(win, …) call sites in the driver; found ${calls.length}`);
  const resolveArg = (expr) => new Function("SURFACES", `return (${expr});`)(SURFACES);
  const combined = calls.map(resolveArg).flat();
  assert.deepEqual(combined, SURFACES,
    `the driver's walkSurfaces() calls must together cover every SURFACES entry exactly once, in order; `
    + `got file list ${JSON.stringify(combined.map((s) => s.file))}`);
});

// ── AC-4: every pre-existing assertion is still present and enforced ───────
// Executable guard, not prose: this goes RED if a future edit deletes one of
// the seven load-bearing assertions carried over from before this fix, or
// loosens the isolation/sandbox constraints that must not move.

test("the driver still carries every pre-existing assertion and isolation constraint", () => {
  const mustContain = [
    "token\\.html",                 // credential screen detection (note: real source has an escaped dot, not a literal ".")
    "GET /api/tasks ->",            // board API 200 check
    "nh-in-shell",                  // packaged-bundle class check
    "running(\"nh\")",              // live bundled nh process check
    "0o600",                        // credential file mode check
    "no_human.db",                  // SQLite store creation check
    "Date.now() + 30000",           // polled 30s process-reaping deadline
    "chromiumSandbox: true",        // Chromium sandbox must stay on
    ".no_human already exists",     // throwaway-HOME pre-flight refusal
  ];
  for (const fragment of mustContain) {
    assert.ok(driverSource.includes(fragment), `driver source is missing required fragment: ${JSON.stringify(fragment)}`);
  }
});

// ── AC-5: dummy credential shape only, no real secret is ever read ─────────

test("the acceptance email is a reserved-TLD (.invalid) address, and no real credential env var is read anywhere in Lane A", () => {
  assert.match(EMAIL, /\.invalid$/);
  const forbidden = [/ANTHROPIC_API_KEY/, /CLAUDE_CODE_OAUTH_TOKEN/, /process\.env\.\w*TOKEN\w*/, /process\.env\.\w*KEY\w*/];
  for (const source of [driverSource, surfacesSource]) {
    for (const re of forbidden) {
      assert.doesNotMatch(source, re);
    }
  }
});
