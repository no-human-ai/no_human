#!/usr/bin/env node
// Materialise node_modules/electron/dist before packaging.
//
// Electron 42 removed the package's `postinstall` hook. The binary and the
// `dist/` directory beside it — which is where Electron's MIT notice and
// Chromium's BSD notice live — are downloaded on the FIRST EXECUTION of the
// electron binary instead. A bare `npm ci` therefore left that directory
// absent until desktop/package.json's own postinstall started fetching it
// (this script stays as the packaging-time backstop), and electron-builder
// only WARNS when an `extraResources` source is
// missing, so a clean CI build would package an app with no third-party
// notices in it and exit 0.
//
// Running the binary once is the documented way to trigger the fetch. This
// script does that, then asserts the two notices exist, so the failure is a
// named error here rather than a silent omission in a shipped artefact.
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const desktop = join(here, "..", "desktop");
const require_ = createRequire(join(desktop, "package.json"));

const NOTICES = ["LICENSE", "LICENSES.chromium.html"];
const distDir = join(desktop, "node_modules", "electron", "dist");
const present = () => NOTICES.every((n) => existsSync(join(distDir, n)));

if (present()) {
  console.log("ensure-electron: notices already present in", distDir);
  process.exit(0);
}

let binary;
try {
  binary = require_("electron");
} catch (err) {
  console.error("ensure-electron: electron is not installed in desktop/ —", err.message);
  process.exit(1);
}

console.log("ensure-electron: fetching the Electron binary (42+ downloads on first run)…");
try {
  execFileSync(binary, ["--version"], { stdio: "ignore", timeout: 15 * 60 * 1000 });
} catch (err) {
  // A failure to RUN is not fatal on a headless box; what matters is whether
  // the download landed. Fall through to the check and report on that.
  console.error("ensure-electron: running the binary failed:", err.message);
}

if (!present()) {
  console.error(
    "ensure-electron: node_modules/electron/dist is still missing " +
    NOTICES.filter((n) => !existsSync(join(distDir, n))).join(", ") + ".\n" +
    "Packaging would ship an app without Electron's and Chromium's licence " +
    "notices, which their terms require. Fix the download before building.");
  process.exit(1);
}
console.log("ensure-electron: ok —", distDir);
