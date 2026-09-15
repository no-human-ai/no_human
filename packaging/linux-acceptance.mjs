#!/usr/bin/env node
// Lane A of the Linux acceptance story (docs/LINUX.md §4/§6).
//
// Launch the INSTALLED Linux app (never the dev tree — `--exe` is the binary
// the .deb put at /opt/no_human/no_human, or an extracted AppImage's
// squashfs-root/no_human) under a THROWAWAY HOME, and prove, in order:
//
//   1. first run opens the credential screen (token.html), with nothing of the
//      runner's own ~/.no_human visible — the "new machine opened my board"
//      class the operator hit on 2026-08-16 is impossible here by construction;
//   2. saving a shape-valid DUMMY token writes ~/.no_human/.env at mode 0600
//      into that throwaway HOME (the POSIX branch of tokenStore.mjs);
//   3. the onboarding wizard renders its Welcome step, and is then driven to
//      completion (email + Continue, zero repos/projects, no other gate);
//   4. the board attaches on loopback and the BUNDLED frozen server answers
//      GET /api/tasks with 200 — i.e. bundledNhPath() resolved `nh` inside
//      resources/nh-server and the spawned process is alive — reached PAST
//      the wizard, not the wizard's first step (the bug this fixes: a
//      screenshot named "the board" that was, in fact, step 1 of 7);
//   5. Settings and Stats each open and prove their own on-screen content;
//   6. quitting the app reaps the server: no `no_human` and no `nh` process
//      remains after quitPolicy's grace + escalation.
//
// No task is ever created or run here: the runner's dummy token is
// shape-valid only and cannot authenticate a real API call, so the walk stops
// at "surfaces are reachable post-onboarding" (intake decision) rather than
// asserting a task outcome it cannot honestly produce.
//
// This is docs/WINDOWS.md §5.4 ("Install → launch → board → quit") made
// automatic. Screenshots land in --out so a human can look at what the runner
// saw; the CI job uploads them. Nothing here touches the operator's machine:
// it runs on a GitHub ubuntu runner under Xvfb.
//
// The dummy token is not, and can never be, a credential: it satisfies the
// setup screen's SHAPE check (validateToken: not an sk-ant-api key, no
// whitespace) and nothing else. No task is ever started in this run.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  EMAIL, WIZARD, SURFACES, POST_WIZARD_SURFACES,
  verifySurface, runPostCredentialWalk,
} from "./linuxAcceptanceSurfaces.mjs";

export { EMAIL, SURFACES, POST_WIZARD_SURFACES };

// Assembled rather than one literal so this file — the one that would be
// copied into a `.env` — never contains a full credential-SHAPED line that a
// secret scanner or a human skimming the log could mistake for a real token.
// The prefix alone appears in the unit test's regex and in the docs, and that
// is fine: a prefix is not a credential shape. validateToken() sees the same
// string either way.
export const DUMMY_TOKEN = ["sk-ant", "oat01", "linux-acceptance-dummy-not-a-real-token"].join("-");

export function expectedBoardUrl(port) {
  return `http://127.0.0.1:${port}/`;
}

const MODES = new Set(["setup", "board"]);

/** `--exe <bin> --home <dir> [--out <dir>] [--mode setup|board]`. */
export function parseArgs(argv) {
  const a = { exe: "", home: "", out: "", mode: "setup" };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    const v = argv[i + 1];
    if (k === "--exe") { a.exe = v ?? ""; i++; }
    else if (k === "--home") { a.home = v ?? ""; i++; }
    else if (k === "--out") { a.out = v ?? ""; i++; }
    else if (k === "--mode") { a.mode = v ?? ""; i++; }
    else throw new Error(`unrecognized argument: ${k}`);
  }
  if (!a.exe) throw new Error("--exe <installed no_human binary> is required");
  if (!a.home) throw new Error("--home <throwaway HOME directory> is required");
  if (!MODES.has(a.mode)) throw new Error(`--mode must be one of ${[...MODES].join("|")}, got "${a.mode}"`);
  if (!a.out) a.out = path.join(a.home, "linux-acceptance-out");
  return a;
}

function get(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (r) => {
      let body = "";
      r.on("data", (d) => { body += d; });
      r.on("end", () => resolve({ status: r.statusCode, body }));
    }).on("error", reject);
  });
}

/** PIDs of processes whose comm is exactly `name` (pgrep -x). */
function running(name) {
  try {
    return execFileSync("pgrep", ["-x", name], { encoding: "utf8" })
      .trim().split("\n").filter(Boolean);
  } catch {
    return [];   // pgrep exits 1 when nothing matches
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const a = parseArgs(process.argv.slice(2));
  fs.mkdirSync(a.out, { recursive: true });
  fs.mkdirSync(a.home, { recursive: true });
  if (fs.existsSync(path.join(a.home, ".no_human"))) {
    throw new Error(`${a.home}/.no_human already exists — --home must be a THROWAWAY directory`);
  }
  if (!fs.existsSync(a.exe)) throw new Error(`--exe does not exist: ${a.exe}`);

  const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  // Playwright from the board's dev deps (web/), like desktop/smoke.mjs.
  const { _electron: electron } = await import(
    path.join(ROOT, "web", "node_modules", "playwright", "index.mjs"));

  const app = await electron.launch({
    executablePath: a.exe,
    // Playwright injects --no-sandbox on Linux unless told otherwise
    // (Electron.launch in playwright-core: `if (!options.chromiumSandbox …)
    // unshift("--no-sandbox")`). A user's .deb launch (`.desktop` Exec is the
    // bare binary) runs the Chromium sandbox, so this run must too — otherwise
    // a broken chrome-sandbox/userns setup would crash for the user and still
    // pass here green. For the extracted-AppImage lane the same flag lets
    // AppRun's own userns probe run instead of being short-circuited by an
    // injected --no-sandbox.
    chromiumSandbox: true,
    // HOME is the whole isolation: config, DB, credential file and userData
    // all derive from it. Nothing else of the runner's environment is hidden,
    // deliberately — a .desktop launch on a real machine inherits the same.
    env: { ...process.env, HOME: a.home },
  });
  try {
    const win = await app.firstWindow({ timeout: 30000 });
    await win.waitForLoadState("domcontentloaded");
    const shot = (file) => win.screenshot({ path: path.join(a.out, file) });

    if (a.mode === "setup") {
      if (!/token\.html/.test(win.url())) {
        // error.html carries the server's own diagnosis (backend-cli-missing,
        // spawn-timeout, the captured stderr tail) — put it in the job log,
        // greppable, not only in the screenshot.
        const body = await win.textContent("body").catch(() => "");
        throw new Error(`first run did not open the credential screen; window url: ${win.url()}\n`
          + `page text:\n${(body || "").trim().slice(0, 2000)}`);
      }
      // Prove, then screenshot — never the reverse. SURFACES[0] is the
      // credential screen itself: #token and #save on screen.
      await verifySurface(win, SURFACES[0], { timeout: 10000 });
      await shot(SURFACES[0].file);
      await win.fill("#token", DUMMY_TOKEN);
      await win.click("#save");
    }

    // The board attaches on the configured port (8420 on a fresh HOME).
    await win.waitForURL(/^http:\/\/127\.0\.0\.1:\d+\/?$/, { timeout: 90000 });
    await win.waitForTimeout(2000);

    const port = Number(new URL(win.url()).port);

    // Populated inside betweenWizardAndSurfaces below; read again after
    // runPostCredentialWalk resolves, for the OK line.
    let nhPids = [];

    // The Welcome-step walk, driving the wizard to completion, the
    // non-page checks below (GET /api/tasks, nh-in-shell, live `nh`
    // process, credential file, db file), and the board -> Settings ->
    // Stats walk are ONE composed, unit-tested sequence
    // (runPostCredentialWalk in linuxAcceptanceSurfaces.mjs) rather than
    // separate calls at this call site — so truncating the post-wizard
    // walk, swapping its result, or skipping the Welcome-step walk fails a
    // unit test against a fake page instead of only being visible here,
    // against a real installed build. The function itself fails closed:
    // it throws unless the files it wrote exactly match what
    // SURFACES/POST_WIZARD_SURFACES say this run must write.
    const written = await runPostCredentialWalk(win, {
      mode: a.mode,
      timeout: 15000,
      shot,
      wizard: WIZARD,
      betweenWizardAndSurfaces: async () => {
        const tasks = await get(`${expectedBoardUrl(port)}api/tasks`);
        if (tasks.status !== 200) throw new Error(`GET /api/tasks -> ${tasks.status}`);
        const inShell = await win.evaluate(() =>
          document.documentElement.classList.contains("nh-in-shell"));
        if (!inShell) throw new Error("nh-in-shell class missing — the board is not the packaged bundle, or the preload did not run");

        nhPids = running("nh");
        if (nhPids.length === 0) throw new Error("no bundled `nh` process is running while the board is up");

        const envFile = path.join(a.home, ".no_human", ".env");
        if (!fs.existsSync(envFile)) throw new Error("credential file was not written into the throwaway HOME");
        const mode = fs.statSync(envFile).mode & 0o777;
        if (mode !== 0o600) throw new Error(`credential file mode is ${mode.toString(8)}, expected 600`);
        if (!fs.readFileSync(envFile, "utf8").includes(DUMMY_TOKEN)) {
          throw new Error("credential file does not carry the value that was saved");
        }
        // The frozen server creates its SQLite store on first start
        // (config.py derives it from HOME). Asserted HERE, where the
        // diagnosis is written, rather than discovered three CI steps
        // later as an opaque `test -f`.
        const dbFile = path.join(a.home, ".no_human", "no_human.db");
        if (!fs.existsSync(dbFile)) throw new Error("the bundled server did not create ~/.no_human/no_human.db in the throwaway HOME");
      },
    });

    await app.close();
    // quitPolicy: SIGTERM, a 10 s grace, SIGKILL escalation, and main.mjs
    // holds the quit up to a 20 s hard ceiling — so POLL to a 30 s deadline
    // rather than sleeping a fixed interval that could pass on a slow runner
    // while the escalation is still running (a spurious red, or worse, a
    // green that raced the kill).
    const deadline = Date.now() + 30000;
    let left = { no_human: running("no_human"), nh: running("nh") };
    while ((left.no_human.length || left.nh.length) && Date.now() < deadline) {
      await sleep(500);
      left = { no_human: running("no_human"), nh: running("nh") };
    }
    if (left.no_human.length || left.nh.length) {
      throw new Error(`processes left 30 s after quit: ${JSON.stringify(left)}`);
    }
    // Built from what this run actually verified and wrote — `written` (plus
    // the credential-screen shot, in setup mode) — not a fixed success
    // string, so a run that wrote fewer/other files than it claims cannot
    // print a full-success line: runPostCredentialWalk above already throws
    // before this point if `written` disagrees with what SURFACES /
    // POST_WIZARD_SURFACES say the run must have written.
    const surfaceByFile = new Map(SURFACES.map((s) => [s.file, s]));
    const shotFiles = [...(a.mode === "setup" ? [SURFACES[0].file] : []), ...written];
    const walked = shotFiles.map((f) => surfaceByFile.get(f)?.key ?? f);
    console.log(`OK: ${walked.join(" -> ")} -> quit (port ${port}); `
      + `${nhPids.length} nh process reaped; credential 0600 in throwaway HOME; `
      + `screenshots ${shotFiles.join(", ")} in ${a.out}`);
  } catch (e) {
    try { await app.close(); } catch { /* already gone */ }
    throw e;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((e) => {
    console.error(`FAIL: ${e && e.message ? e.message : e}`);
    process.exit(1);
  });
}
