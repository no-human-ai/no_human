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
//   npm run e2e            # everything (needs a server on :8420 for live-flows)
//   node e2e/board.mjs     # one suite
//
// live-flows.mjs drives the REAL server read-only: every non-GET request is hard-blocked, so it
// can never approve/cancel/retry anything on the operator's board.

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));

// live-flows needs the running server; the rest are self-contained (they serve dist themselves).
const SUITES = [
  ["board", "board.mjs"],
  ["drawer", "drawer.mjs"],
  ["inspector scroll drift (G-2)", "inspector-scroll-drift.mjs"],
  ["merge progress", "merge-progress.mjs"],
  ["composer", "composer.mjs"],
  ["backlog queue", "backlog-queue.mjs"],
  ["form order", "form-order.mjs"],
  ["outcomes (3-lane board)", "outcomes.mjs"],
  ["mobile nav", "mobile-nav.mjs"],
  ["onboarding a11y", "onboarding-a11y.mjs"],
  ["onboarding consent step", "onboarding-consent-step.mjs"],
  ["onboarding summary counts", "onboarding-summary-counts.mjs"],
  ["onboarding minimal path", "onboarding-minimal-path.mjs"],
  ["onboarding step nav", "onboarding-step-nav.mjs"],
  ["onboarding recent card layout", "onboarding-recent-card-layout.mjs"],
  ["integrations help + validate", "integrations-help-validate.mjs"],
  ["settings a11y", "settings-a11y.mjs"],
  ["settings account", "settings-account.mjs"],
  ["models pane", "models-pane.mjs"],
  ["rules archive", "rules-archive.mjs"],
  ["failure reason", "failure-reason.mjs"],
  ["grill a11y", "grill-a11y.mjs"],
  ["dead click race", "dead-click-race.mjs"],
  ["live flows (needs :8420)", "live-flows.mjs"],
  ["electron shell (needs :8420 + desktop install)", "electron-smoke.mjs"],
];

const run = (file) =>
  new Promise((resolve) => {
    const p = spawn(process.execPath, [join(HERE, file)], { stdio: ["ignore", "pipe", "pipe"] });
    let out = "";
    p.stdout.on("data", (d) => (out += d));
    p.stderr.on("data", (d) => (out += d));
    p.on("close", (code) => resolve({ code, out }));
  });

let failed = 0;
for (const [name, file] of SUITES) {
  const { code, out } = await run(file);
  const last = out.trim().split("\n").filter(Boolean).pop() || "(no output)";
  if (code === 0) {
    console.log(`PASS  ${name.padEnd(26)} ${last}`);
  } else {
    failed += 1;
    console.log(`FAIL  ${name.padEnd(26)} ${last}`);
    console.log(out.split("\n").filter((l) => /^FAIL|PAGEERROR|Error/.test(l)).slice(0, 6).join("\n"));
  }
}

console.log(failed ? `\n${failed} SUITE(S) FAILED` : "\nUI GATE GREEN");
process.exit(failed ? 1 : 0);
