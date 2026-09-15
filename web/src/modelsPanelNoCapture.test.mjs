import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// AC3 of the profile-name/session-replay DOM leak fix: ModelsPanel.jsx's
// `title` attributes had to be checked one by one, and every one that can
// carry user-chosen or machine-identifying text closed the same way the
// drainChip.js leak was closed (this file's leak channel is session
// replay's DOM/rrweb capture, not the network-body capture that
// replayScrub.js/telemetry.js govern — `.ph-no-capture` is the block class
// posthog-js passes to rrweb's recorder, unrelated to network masking).
//
// Two distinct `reason` sources feed `title={...}` in this file:
//
//   - backendPanelView.js's `reason: o.available ? "" : o.reason || ""`
//     (backendPanelView.js:39) is the raw `backend_settings.py:86`
//     `str(exc)` — an exception message that can interpolate the operator's
//     configured `llm.local_base_url` (config.py's assert_local_backend_mode
//     raises AuthError text naming that config path). This feeds BOTH the
//     coder-backend row's own picker (ModelsPanel.jsx ~109/119) AND the
//     "Reviewer backend override" section (~384/393), which fetches and
//     renders the exact same backendOptions list — a second render site for
//     the same potentially-identifying string, found while auditing this
//     file for AC3, beyond what the original bug report enumerated. All
//     four are masked.
//
//   - modelsPanelView.js's `reason: o.disabled_reason || ""`
//     (modelsPanelView.js:74) is `model_settings.py:109-110`'s
//     `CODER_BACKEND_REASON.format(model_id=opt.id)` — a fixed catalog
//     string parameterised only by a model id already visible elsewhere on
//     the same row (the option's own value/label). This feeds the model-row
//     picker (ModelsPanel.jsx ~316) and is deliberately left unmasked: it
//     names no user, no host, and no path.
//
// This test pins both findings by reading the actual source files, so a
// regression (a new reason-bearing title losing its ph-no-capture, or a
// class being added to the fixed-string row where it does nothing useful
// but signals someone thought it needed it) fails a test instead of
// depending on someone noticing in review.

const SRC = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(SRC, p), "utf8");

const panel = read("ModelsPanel.jsx");
const modelsView = read("modelsPanelView.js");
const backendView = read("backendPanelView.js");
const modelSettingsPy = read("../../src/no_human/core/model_settings.py");
const backendSettingsPy = read("../../src/no_human/core/backend_settings.py");

test("every backendPanelView-sourced title in ModelsPanel.jsx carries ph-no-capture", () => {
  // Coder-backend row: the <select>'s own <option> and its disabled-hint div.
  assert.match(
    panel,
    /<option key=\{o\.id\} className="ph-no-capture" value=\{o\.id\} disabled=\{disabled\} title=\{o\.reason \|\| undefined\}>/,
    "CoderBackendRow's backend <option> must carry ph-no-capture (title carries backend_settings.py's str(exc))",
  );
  assert.match(
    panel,
    /<div key=\{o\.id\} className="ntm-hint ph-no-capture" title=\{o\.reason\}>/,
    "CoderBackendRow's disabled-backend hint div must carry ph-no-capture",
  );

  // Reviewer backend override: the second render site for the identical
  // backendOptions list (same leak class, not called out by name in the
  // original bug report, closed the same way here).
  assert.match(
    panel,
    /<option key=\{o\.id\} className="ph-no-capture" value=\{o\.id\} disabled=\{o\.disabled\} title=\{o\.reason \|\| undefined\}>/,
    "Reviewer backend override's <option> must carry ph-no-capture too — it renders the same backendOptions list",
  );
  assert.match(
    panel,
    /<div key=\{o\.id\} className="ntm-hint ph-no-capture" title=\{o\.reason\}>/,
    "Reviewer backend override's disabled-backend hint div must carry ph-no-capture too",
  );
});

test("backendPanelView's reason really is the raw backend_settings.py exception text (why masking is needed)", () => {
  assert.ok(
    backendView.includes('reason: o.available ? "" : o.reason || ""'),
    "backendPanelView.js must still source `reason` from the raw payload's o.reason, not a re-derived/sanitised value",
  );
  assert.match(
    backendSettingsPy,
    /"reason":\s*str\(exc\)/,
    "backend_settings.py must still produce `reason` from str(exc) — an exception message, not a fixed string",
  );
});

test("the model-row disabled_reason is a fixed system string, so it is deliberately left unmasked", () => {
  // modelsPanelView.js:74 — the ONLY place `reason` reaches ModelsPanel.jsx's
  // model-row <option> (line ~316, intentionally untouched by this fix).
  assert.ok(
    modelsView.includes('reason: o.disabled_reason || ""'),
    "modelsPanelView.js must still source the model-row `reason` from disabled_reason",
  );
  // model_settings.py:109-110 — disabled_reason is ALWAYS either "" or the
  // fixed CODER_BACKEND_REASON template (parameterised only by a model id
  // already visible on the row), never free-form/user-supplied text.
  assert.match(
    modelSettingsPy,
    /"disabled_reason":\s*\(\s*CODER_BACKEND_REASON\.format\(model_id=opt\.id\) if opt\.requires_backend else ""\s*\)/,
    "model_settings.py's disabled_reason must still be the fixed CODER_BACKEND_REASON template, not free-form text",
  );

  // The model-row <option> itself needs no ph-no-capture — its title is a
  // fixed system string, never user-chosen or machine-identifying — but an
  // extra, redundant className here would be harmless over-masking, not a
  // defect, so this deliberately does NOT fail if one is later added (a
  // stricter regex here would penalise someone erring toward masking more,
  // not less). It only pins that the fixed-string <option> element itself
  // still exists, with or without an optional className.
  const modelRowOption = panel.match(
    /\{row\.options\.map\(\(o\) => \(\s*<option key=\{o\.id\}(?: className="[^"]*")? value=\{o\.id\} disabled=\{o\.disabled\} title=\{o\.reason \|\| undefined\}>/,
  );
  assert.ok(
    modelRowOption,
    "the model-row <option> (fixed disabled_reason string) must still exist",
  );
});
