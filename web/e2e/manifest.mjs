// Single source of truth for which e2e walks run where. Nothing ran these in
// CI before this file existed (`npm test` is unit tests only; `npm run e2e`
// was never wired into any workflow) — see the PR body for the full story.
//
// lane: "ci"     — runs in the `web_e2e` GitHub Actions job on every push/PR
//                   touching this repo. Must be green; a red "ci"-lane walk
//                   fails the build.
// lane: "manual" — excluded from the `web_e2e` job. Every "manual" entry
//                   carries a `reason` explaining why, so exclusion is a
//                   documented, reviewable decision rather than a silent skip.
//                   Run by hand: `node e2e/<file>` (or, if it needs a live
//                   server, per its own header comment).
//
// e2eManifest.test.mjs enforces: every walk file in this directory (other
// than run-all.mjs and this file) appears here exactly once, every "manual"
// reason is substantive (>=40 chars), and every listed file exists on disk.
export const WALKS = [
  // ── ci lane — green, run on every push/PR ────────────────────────────────
  { file: "board.mjs", name: "board", lane: "ci" },
  { file: "drawer.mjs", name: "drawer", lane: "manual", reason:
    "Quarantined: '[M4+] the drawer body scrolls vertically on a phone' fails " +
    "with overflowY:visible on a narrow viewport — the secondary-inspector " +
    "accordion has no vertical scroll container on mobile, so Diff/Attempts " +
    "are unreachable below the fold. Real CSS/layout defect in the app (not " +
    "this walk); fixing it is application-code work, out of scope here. See " +
    "the QUARANTINED comment in e2e/drawer.mjs for full detail." },
  { file: "inspector-scroll-drift.mjs", name: "inspector scroll drift (G-2)", lane: "ci" },
  { file: "merge-progress.mjs", name: "merge progress", lane: "manual", reason:
    "Quarantined: pre-existing failure, not caused by this change. The first " +
    "check crashes uncaught — 'page.evaluate: Execution context was destroyed, " +
    "most likely because of a navigation' at the .btn-approve latency probe. " +
    "No <form> wraps the button and no app-code navigation call was found near " +
    "handleApprove; whether this is a walk race or a genuine unwanted " +
    "navigation is undiagnosed. Left red rather than guessed at — see the " +
    "Budget rule in .no_human/PLAN.md." },
  { file: "composer.mjs", name: "composer", lane: "ci" },
  { file: "backlog-queue.mjs", name: "backlog queue", lane: "ci" },
  { file: "form-order.mjs", name: "form order", lane: "ci" },
  { file: "outcomes.mjs", name: "outcomes (3-lane board)", lane: "ci" },
  { file: "mobile-nav.mjs", name: "mobile nav", lane: "manual", reason:
    "Quarantined: 4 of ~100 checks fail — 'no nav label is clipped' at the two " +
    "narrowest viewports (small, mobile-short; both themes), always the same " +
    "label, always the same 2px. Real CSS regression: src/styles.css:379 gives " +
    ".nh-navrow-label 'flex: 1; min-width: 0' (shrink + ellipsize), but the " +
    "narrow-viewport override at src/styles.css:627 sets 'flex: 0 0 auto; " +
    "max-width: 100%', whose flex-basis:auto sizes the label to its own " +
    "content instead of the track, so it overflows before ellipsis can fire. " +
    "Application CSS change, out of scope here." },
  { file: "onboarding-a11y.mjs", name: "onboarding a11y", lane: "ci" },
  { file: "onboarding-consent-step.mjs", name: "onboarding consent step", lane: "ci" },
  { file: "onboarding-summary-counts.mjs", name: "onboarding summary counts", lane: "manual", reason:
    "Quarantined: this walk's own bug (a stale fixed hop count) is fixed and " +
    "it no longer times out. Fixing it exposed a separate, real, pre-existing " +
    "product bug: launchReadiness() in src/onboardingProjects.js hardcodes " +
    "jumpTo:1/jumpTo:2 step indices that predate the 'email' step landing at " +
    "index 1, so both 'Fix ->' buttons jump one step early. Fixing that is an " +
    "application-code change, out of scope here — see the QUARANTINED comment " +
    "in e2e/onboarding-summary-counts.mjs." },
  { file: "onboarding-email-reload.mjs", name: "onboarding email reload", lane: "ci" },
  { file: "onboarding-minimal-path.mjs", name: "onboarding minimal path", lane: "ci" },
  { file: "onboarding-step-nav.mjs", name: "onboarding step nav", lane: "ci" },
  { file: "onboarding-discord-step.mjs", name: "onboarding discord step", lane: "ci" },
  { file: "onboarding-recent-card-layout.mjs", name: "onboarding recent card layout", lane: "ci" },
  { file: "integrations-help-validate.mjs", name: "integrations help + validate", lane: "ci" },
  { file: "settings-a11y.mjs", name: "settings a11y", lane: "ci" },
  { file: "settings-account.mjs", name: "settings account", lane: "ci" },
  { file: "models-pane.mjs", name: "models pane", lane: "ci" },
  { file: "rules-archive.mjs", name: "rules archive", lane: "ci" },
  { file: "failure-reason.mjs", name: "failure reason", lane: "ci" },
  { file: "grill-a11y.mjs", name: "grill a11y", lane: "ci" },
  { file: "dead-click-race.mjs", name: "dead click race", lane: "ci" },
  { file: "replay-body-leak.mjs", name: "replay body leak", lane: "ci" },
  { file: "replay-dom-leak.mjs", name: "replay DOM leak", lane: "ci" },
  // Deterministic, server-free layout regressions (render fixed markup + the
  // real styles.css in headless Chromium, no board/server involved at all).
  // PLAN.md described these as "Playwright-runner .spec files, not standalone
  // node scripts" — that turned out to be inaccurate: both run cleanly as
  // plain `node e2e/<file>` (verified: exit 0, PASS line printed). Ci lane.
  { file: "fx-tree-layout.spec.mjs", name: "fx tree layout", lane: "ci" },
  { file: "test-plan-header.spec.mjs", name: "test plan header", lane: "ci" },

  // ── manual lane — need infrastructure this job intentionally does not stand up ──
  { file: "live-flows.mjs", name: "live flows (needs :8420)", lane: "manual", reason:
    "Needs a running board server on :8420 (drives the REAL server read-only, " +
    "hard-blocking every non-GET request). The web_e2e job serves web/dist " +
    "directly and never boots the Python backend, so this walk has no server " +
    "to point at in CI." },
  { file: "electron-smoke.mjs", name: "electron shell (needs :8420 + desktop install)", lane: "manual", reason:
    "Needs :8420 AND an installed desktop app shell to smoke-test, neither of " +
    "which the web_e2e job builds or installs. That is the desktop job's " +
    "concern, not this one; wiring it up here would duplicate a much heavier " +
    "build this job explicitly does not want." },
  { file: "sweep.mjs", name: "sweep (full visual sweep, needs :8420)", lane: "manual", reason:
    "Drives a live board on :8420 (BASE defaults there) and writes a full " +
    "screenshot report to e2e/sweep-shots/ for human review — it is a manual " +
    "visual-sweep tool, not a pass/fail gate, and has no server to run against " +
    "in this job." },
  { file: "persona-file-task.mjs", name: "persona file task (needs :8420)", lane: "manual", reason:
    "Files a task through the REAL board GUI at :8420 and takes a JSON path " +
    "argument describing the task to file — it is an interactive/CLI tool for " +
    "a human to invoke with specific content, not a self-contained pass/fail " +
    "suite this job can run unattended." },
];
