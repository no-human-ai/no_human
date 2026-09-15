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
import { DUMMY_TOKEN, expectedBoardUrl, parseArgs, EMAIL, SURFACES, POST_WIZARD_SURFACES }
  from "../packaging/linux-acceptance.mjs";
import { validateToken } from "./tokenStore.mjs";

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

// ── Bugfix pins: "Linux acceptance stops at onboarding step 1, not the board
// it names" (docs/LINUX.md §6). The driver used to screenshot "02-board.png"
// which was, in fact, the wizard's Welcome step. These pin the fix's shape
// from the driver's own re-exports (packaging/linuxAcceptanceSurfaces.mjs has
// its own, larger unit suite in linuxAcceptanceSurfaces.test.mjs). ──────────

test("every screenshot filename names what it actually shows — a table, not a claim", () => {
  // The exact mapping a reviewer (or a future edit) could silently break:
  // pins filename -> label together, per the "the fix must fix this specific
  // sentence" requirement, not merely "the file exists".
  const mapping = SURFACES.map((s) => [s.file, s.label]);
  assert.deepEqual(mapping, [
    ["01-credential-screen.png", "the credential screen (token.html) on first run"],
    ["02-onboarding-welcome.png", "the onboarding wizard's Welcome step (step 1 of 7) — NOT the board"],
    ["03-board-first-run.png", "the task board reached after onboarding completes (zero tasks, first-run empty state)"],
    ["04-settings.png", "the Settings overlay"],
    ["05-stats.png", "the Stats page"],
  ]);
  // The specific defect: no surface is honestly named "board" before step 3,
  // and the one that used to be mislabeled says, in its own label, that it is
  // NOT the board.
  assert.match(SURFACES[1].label, /NOT the board/);
});

test("POST_WIZARD_SURFACES (what the driver walks after the wizard) is board, settings, stats — in order", () => {
  // Re-checked from the driver's own import/re-export, not just the surfaces
  // module's test file — a regression at the driver's import site (e.g.
  // importing the wrong array) would be invisible to linuxAcceptanceSurfaces
  // .test.mjs alone, since that file imports the same module directly.
  assert.deepEqual(POST_WIZARD_SURFACES.map((s) => s.key), ["board-first-run", "settings", "stats"]);
  assert.equal(POST_WIZARD_SURFACES.length, SURFACES.length - 2);
});

test("the reserved-TLD dummy email is used, never a real address, and the wizard email is not a credential", () => {
  // RFC 2606: .invalid never resolves and is safe to hardcode in a file whose
  // output lands in CI logs.
  assert.match(EMAIL, /\.invalid$/);
});

const DRIVER_SRC = fs.readFileSync(
  path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "packaging", "linux-acceptance.mjs"),
  "utf8",
);

test("every pre-existing load-bearing assertion in the driver is still present and enforced", () => {
  // A source-grep, not a behavioral run (the driver only runs against an
  // installed Linux build) — but it fails the moment any one of these
  // fragments is edited away, which is exactly what "still present and still
  // enforced" needs pinned for a change that touches this file's structure.
  const mustContain = [
    // 1. the credential screen on first run
    "token.html",
    // 2. GET /api/tasks 200
    "GET /api/tasks ->",
    // 3. the nh-in-shell class
    "nh-in-shell",
    // 4. a live bundled `nh` process (pgrep -x nh via running())
    '"pgrep"',
    'running("nh")',
    // 5. the credential file at mode 0600 carrying the saved value
    "0o600",
    "DUMMY_TOKEN",
    // 6. ~/.no_human/no_human.db created
    "no_human.db",
    // the polled 30s process-reaping deadline after quit
    "Date.now() + 30000",
    // chromiumSandbox: true and the throwaway-HOME pre-flight refusal
    "chromiumSandbox: true",
    ".no_human already exists",
  ];
  for (const fragment of mustContain) {
    assert.ok(DRIVER_SRC.includes(fragment), `expected the driver to still contain: ${fragment}`);
  }
});

test("the driver never reads, prints, or logs a real credential — only the dummy token and its own env passthrough", () => {
  // The real credential env var (desktop/tokenStore.mjs's API_KEY_VAR) must
  // never be named in the driver; the only env handling is `...process.env,
  // HOME: a.home` (passthrough for the throwaway launch, not a credential read).
  assert.doesNotMatch(DRIVER_SRC, /ANTHROPIC_API_KEY/);
  // The dummy token is written to disk (by the app, under the throwaway HOME)
  // and compared against file contents — never handed to console.log/console.error.
  const loggingLines = DRIVER_SRC.split("\n").filter((l) => /console\.(log|error)/.test(l));
  for (const line of loggingLines) {
    assert.doesNotMatch(line, /DUMMY_TOKEN/, `a console line must not print DUMMY_TOKEN: ${line}`);
  }
});
