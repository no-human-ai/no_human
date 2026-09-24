// The UI gate. `npm run build` first — these drive the BUILT bundle (web/dist), never the dev
// server, because that is the only thing that catches what the other gates cannot:
//
//   * A ReferenceError in a .jsx renders a BLANK PAGE. `node --test` never mounts a component and
//     `vite build` bundles an undeclared identifier happily — a blank Stats page once shipped past
//     197/197 green unit tests.
//   * Tailwind's Preflight is OFF, so `border` paints NOTHING without `border-solid` and controls
//     keep native chrome unless they state a background. Neither is visible in source review.
//   * Light-theme regressions (a var read but never defined falls back to a hardcoded DARK literal).
//
// Each suite exits non-zero on failure, so this does too.
//
//   npm run e2e             # everything in manifest.mjs (needs a server on :8420 for
//                            #   the "manual"-lane walks that want one — see manifest.mjs)
//   npm run e2e:ci          # only the "ci"-lane walks (what the web_e2e GitHub Actions job runs)
//   node e2e/run-all.mjs --lane=manual   # only the "manual"-lane walks
//   node e2e/board.mjs      # one suite
//
// Which walk runs in which lane, and why, lives in one place: e2e/manifest.mjs.

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { WALKS } from "./manifest.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));

const laneArg = process.argv.find((a) => a.startsWith("--lane="));
const lane = laneArg ? laneArg.slice("--lane=".length) : "all";
if (!["all", "ci", "manual"].includes(lane)) {
  console.error(`Unknown --lane=${lane} (expected: all, ci, manual)`);
  process.exit(2);
}
const SUITES = WALKS.filter((w) => lane === "all" || w.lane === lane);

const run = (file) =>
  new Promise((resolve) => {
    const started = process.hrtime.bigint();
    const p = spawn(process.execPath, [join(HERE, file)], { stdio: ["ignore", "pipe", "pipe"] });
    let out = "";
    p.stdout.on("data", (d) => (out += d));
    p.stderr.on("data", (d) => (out += d));
    p.on("close", (code) => {
      const elapsedS = Number(process.hrtime.bigint() - started) / 1e9;
      resolve({ code, out, elapsedS });
    });
  });

const suiteStart = process.hrtime.bigint();
let failed = 0;
for (const { name, file } of SUITES) {
  const { code, out, elapsedS } = await run(file);
  const last = out.trim().split("\n").filter(Boolean).pop() || "(no output)";
  const timing = `(${elapsedS.toFixed(1)}s)`;
  if (code === 0) {
    console.log(`PASS  ${name.padEnd(26)} ${timing.padEnd(8)} ${last}`);
  } else {
    failed += 1;
    console.log(`FAIL  ${name.padEnd(26)} ${timing.padEnd(8)} ${last}`);
    console.log(out.split("\n").filter((l) => /^FAIL|PAGEERROR|Error/.test(l)).slice(0, 6).join("\n"));
  }
}
const totalS = Number(process.hrtime.bigint() - suiteStart) / 1e9;

console.log(
  failed
    ? `\n${failed} SUITE(S) FAILED — ${SUITES.length} walks, ${failed} failed, ${totalS.toFixed(1)}s`
    : `\nUI GATE GREEN — ${SUITES.length} walks, 0 failed, ${totalS.toFixed(1)}s`
);
process.exit(failed ? 1 : 0);
