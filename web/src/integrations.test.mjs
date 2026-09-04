import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// The Integrations settings panel now renders an editable Configure form per
// card, generated from the `fields` spec GET /api/integrations returns (PR
// #153's write path). Like themeVars.test.mjs / settingsOverlay.test.mjs, this
// is static source analysis: no jsdom/React renderer is wired into this
// project's `node --test` harness, so these assertions read the .jsx source
// rather than mounting components.

const here = fileURLToPath(new URL(".", import.meta.url));
const src = readFileSync(here + "Integrations.jsx", "utf8");

// Regression guard for the Launch-summary mislabeling bug (2026-09-01): that
// bug was NOT here — this panel already rendered every integration
// GET /api/integrations returns, unfiltered. But the Onboarding fix narrows
// the LAUNCH step's summary row to a 5-integration subset, so this panel
// pins the other half of the claim: Settings → Integrations still shows all
// nine, unfiltered, so no one "fixes" the mislabel by narrowing this panel to
// match.
test("Settings -> Integrations renders every integration the server returns, unfiltered", () => {
  assert.match(src, /\{items\.map\(\(it\)\s*=>/,
    "the panel must render one card per fetched integration");
  assert.doesNotMatch(src, /items\.filter\(/,
    "the card list must not be narrowed by a client-side filter");
  const chipSrc = readFileSync(here + "integrationChip.js", "utf8");
  const nameLabelMatch = chipSrc.match(/export const NAME_LABEL = \{([\s\S]*?)\n\};/);
  assert.ok(nameLabelMatch, "could not find NAME_LABEL in integrationChip.js");
  const nameLabelBody = nameLabelMatch[1].replace(/\/\/.*$/gm, ""); // strip line comments
  const jsNames = new Set([...nameLabelBody.matchAll(/\b([a-z]+):\s*"/g)].map((m) => m[1]));
  const orderMatch = pySrcForOrder().match(/_ORDER = \[([\s\S]*?)\]/);
  assert.ok(orderMatch, "could not find _ORDER in integrations/__init__.py");
  const pyNames = new Set([...orderMatch[1].matchAll(/"([a-z]+)"/g)].map((m) => m[1]));
  assert.equal(pyNames.size, 9, `_ORDER should list all nine integrations, found ${pyNames.size}`);
  assert.deepEqual([...jsNames].sort(), [...pyNames].sort(),
    "NAME_LABEL must cover exactly the nine integrations _ORDER discovers, " +
    "so the panel never silently drops one");
});

function pySrcForOrder() {
  return readFileSync(here + "../../src/no_human/integrations/__init__.py", "utf8");
}

test("the Configure form is generated from the integration's fields spec", () => {
  assert.match(src, /it\.fields\s*\|\|\s*\[\]/, "must read fields off the card, defaulting to []");
  assert.match(src, /fields\.map\(\(f\)\s*=>/, "must render one row per field in the spec");
  // Each row shows the field's label (never placeholder-only).
  assert.match(src, /<label className="ntm-label"[^>]*>\s*\{f\.label\}/s);
});

test("secret fields render as password inputs with the required placeholder", () => {
  assert.match(src, /type=\{f\.secret \? "password" : "text"\}/);
  assert.match(src, /placeholder=\{f\.secret \? fieldSecretLabel\(f\) : ""\}/);
  assert.match(src, /from "\.\/integrationSecret\.js"/);
});

test("the Secret summary line and the form badge derive from the same source, not it.configured", () => {
  // The reported defect: the card's Secret line used to render `it.configured`
  // (an integration-wide predicate that never looks at the secret itself) —
  // see integrationSecret.test.mjs for the behavioural coverage of the fix.
  assert.doesNotMatch(src, /it\.configured \? "●●● set"/);
  assert.doesNotMatch(src, /integration-secret\$\{it\.configured/);
});

test("no field is ever prefilled from server data — every field starts blank", () => {
  // toggleConfigure seeds the whole form to "" regardless of `set`, for both
  // secret and non-secret fields (the GET response never exposes a value for
  // either kind, only `set: bool`).
  assert.match(src, /initial\[f\.name\]\s*=\s*""/,
    "the configure-open handler must seed every field to an empty string");
  // The controlled `value` must come only from local form state, never from
  // the field spec (`f.set` / `f.name` used as a value source would leak or
  // fake a prefill).
  assert.match(src, /value=\{values\[f\.name\]\s*\?\?\s*""\}/,
    "input value must bind only to local formValues state");
  assert.doesNotMatch(src, /value=\{f\.set/, "must never derive the input value from f.set");
});

test("save submits only dirty fields, and an empty secret submit keeps the current value", () => {
  assert.match(src, /function dirtyPayload/);
  assert.match(src, /if\s*\(!dirty\.has\(f\.name\)\)\s*continue/, "untouched fields must be skipped");
  assert.match(src, /if\s*\(f\.secret\s*&&\s*val\s*===\s*""\)\s*continue/,
    "an empty secret submit must be omitted (keep current), never sent as a blank overwrite");
});

test("client-side no-newline validation mirrors the server rule", () => {
  assert.match(src, /handleFieldBlur/);
  assert.match(src, /\/\[\\n\\r\]\//, "must check for newline/carriage-return characters");
});

// The note this panel renders is a CLAIM about what the backend does:
// "Saving here makes X your active CI backend and turns CI on for this
// workspace." The only thing that makes it true is `_CI_BACKEND_BY_NAME` in
// no_human/integrations/__init__.py, which is what `save_integration_config`
// consults before pinning ci.backend + ci.enabled.
//
// This test used to assert `CI_AUTOPIN` contained exactly
// ["github","gitlab","jenkins","circleci"] — a hardcoded second copy of the
// truth. It was green for months while the Python map omitted circleci, so it
// guarded the sentence and not the behaviour: an operator who configured
// CircleCI was told the gate was on, and no `ci:` block was ever written.
// Read the real map instead, so the two cannot drift again.
const pySrc = readFileSync(here + "../../src/no_human/integrations/__init__.py", "utf8");

function pythonCiAutopinNames() {
  const m = pySrc.match(/_CI_BACKEND_BY_NAME\s*=\s*\{([\s\S]*?)\}/);
  assert.ok(m, "could not find _CI_BACKEND_BY_NAME in integrations/__init__.py");
  const names = [...m[1].matchAll(/"([a-z_]+)"\s*:/g)].map((x) => x[1]);
  assert.ok(names.length >= 3, `parsed only ${names.length} names — the scan broke`);
  return new Set(names);
}

function jsxCiAutopinNames() {
  const m = src.match(/CI_AUTOPIN\s*=\s*new Set\(\[([^\]]*)\]\)/);
  assert.ok(m, "could not find CI_AUTOPIN in Integrations.jsx");
  const names = [...m[1].matchAll(/"([a-z_]+)"/g)].map((x) => x[1]);
  assert.ok(names.length >= 3, `parsed only ${names.length} names — the scan broke`);
  return new Set(names);
}

test("the CI auto-pin note is shown for exactly the integrations the backend really auto-pins", () => {
  const py = pythonCiAutopinNames();
  const jsx = jsxCiAutopinNames();
  assert.deepEqual(
    [...jsx].sort(), [...py].sort(),
    "the panel promises 'active CI backend' for a set that differs from " +
    "_CI_BACKEND_BY_NAME — one of the two is lying to the operator",
  );
  assert.ok(py.has("circleci"),
    "circleci must be auto-pinned: its form writes ci.project, so without a " +
    "map entry it saves settings and selects no backend at all");
});

test("every auto-pinned integration's form actually writes into ci.*", () => {
  // The other half of the same claim: a name in the map whose FIELD_SPECS
  // write somewhere else pins a backend with no pipeline target.
  const py = pythonCiAutopinNames();
  const specs = pySrc.match(/FIELD_SPECS[\s\S]*?\n\}\n/);
  assert.ok(specs, "could not find FIELD_SPECS");
  for (const name of py) {
    const block = specs[0].match(
      new RegExp(`"${name}":\\s*\\[([\\s\\S]*?)\\]`),
    );
    assert.ok(block, `no FIELD_SPECS entry for auto-pinned '${name}'`);
    assert.match(block[1], /config_path="ci\./,
      `'${name}' is auto-pinned but no field writes into ci.*`);
  }
});

test("the CI auto-pin note uses plain, active-voice copy", () => {
  assert.match(src, /active CI\s*\n?\s*.*backend/, "must state it becomes the active CI backend");
  assert.match(src, /turns CI on/i, "must state CI gets enabled, in plain language");
  // No raw config-key jargon in the visible copy string itself.
  assert.doesNotMatch(src, /Saving here makes[^<]*ci\.(backend|enabled)/,
    "the visible note text must not contain raw config keys like ci.backend/ci.enabled");
});

test("field help is the shared FieldHint driven by the server's help catalogue, not a hardcoded map", () => {
  // The old inline FIELD_HELP is gone; help text/URL now come from f.help /
  // f.help_url (integrations/help.py) and render through one FieldHint.
  assert.doesNotMatch(src, /const FIELD_HELP =/);
  assert.match(src, /import FieldHint from "\.\/FieldHint\.jsx"/);
  assert.match(src, /<FieldHint id=\{hintId\} text=\{f\.help\} url=\{f\.help_url\} \/>/);
});

test("Save is disabled while saving, on validation errors, or with nothing dirty to send", () => {
  assert.match(src, /disabled=\{saving \|\| hasErrors \|\| !hasChanges\}/);
});

test("Test connection is disabled during its own check AND while a save is in flight", () => {
  assert.match(src, /disabled=\{testing === it\.name \|\| saving === it\.name\}/);
});

test("fetchIntegrations in load() has a catch guard (no unhandled-rejection / stuck spinner)", () => {
  assert.match(src, /fetchIntegrations\(\)/);
  assert.match(src, /\.then\(\(r\) => setItems\(r\.integrations \|\| \[\]\)\)\s*\n\s*\.catch\(\(\) => setItems\(\[\]\)\)/,
    "load() must chain a .catch right after fetchIntegrations().then(...)");
});

test("a successful save merges the refreshed status+fields back into the card list", () => {
  assert.match(src, /saveIntegrationConfig\(it\.name,\s*payload\)/);
  assert.match(src, /setItems\(\(prev\)\s*=>\s*prev\.map\(\(x\)\s*=>\s*\(x\.name === it\.name \? \{ \.\.\.x, \.\.\.refreshed \} : x\)\)\)/);
});

// ── Review-fix pins (dual-review Minor findings on the Configure form) ─────

test("required-field validation fires only for non-secret fields the user dirtied and then emptied", () => {
  assert.match(src, /function handleFieldBlur\(name,\s*value,\s*secret\)/,
    "blur handler must receive the field's secret flag alongside name/value");
  assert.match(src, /!secret\s*&&\s*dirty\.has\(name\)\s*&&\s*value\s*===\s*""/,
    "required rule: non-secret, previously-dirtied, now-empty — never on first open, never for secrets");
  assert.match(src, /This field is required\./, "must show a visible required-field message");
  // Still wired to the field's secret flag at the call site, not hardcoded.
  assert.match(src, /onBlur\(f\.name,\s*e\.target\.value,\s*f\.secret\)/);
});

test("inputs wire aria-invalid and aria-describedby to their helper/error element ids", () => {
  assert.match(src, /aria-invalid=\{fieldErrors\[f\.name\]\s*\?\s*"true"\s*:\s*undefined\}/);
  assert.match(src, /aria-describedby=\{describedBy\}/);
  // The ids it points at are actually rendered on the hint/error elements.
  assert.match(src, /const hintId = f\.help \? fieldHintId\(integration\.name, f\.name\) : null/);
  assert.match(src, /const errorId = fieldErrors\[f\.name\]\s*\?\s*`\$\{inputId\}-error`\s*:\s*null/);
  assert.match(src, /id=\{hintId\}/);
  assert.match(src, /id=\{errorId\}/);
});

test("the save-error message is an aria-live=polite region and a focus target", () => {
  assert.match(src, /className="new-task-error"\s+aria-live="polite"/,
    "the save error must announce to assistive tech as it appears");
  assert.match(src, /ref=\{errorRef\}/, "the error region must be focusable as the no-field-error fallback");
});

test("after a failed save, focus moves to the first invalid field, else the error region", () => {
  assert.match(src, /const firstInvalid = fields\.find\(\(f\)\s*=>\s*fieldErrors\[f\.name\]\)/);
  assert.match(src, /\(target \|\| errorRef\.current\)\?\.focus\(\)/);
});

test("Escape closes the expanded card AND, if a Configure form is open, closes it and wipes formValues", () => {
  assert.match(src, /const closeOnEscape = useCallback\(\(\) => \{/,
    "escape handler must be a single function so it can reset every configure-form field");
  assert.match(src, /setExpanded\(null\);\s*\n\s*setConfiguring\(null\);\s*\n\s*setFormValues\(\{\}\);/,
    "escape must close the configure form (setConfiguring(null)) and wipe typed values (setFormValues({}))");
  assert.match(src, /useEscapeKey\(closeOnEscape,\s*expanded !== null\)/);
});

// Walk bug (2026-08-12): the result region used to gate on it.healthy ===
// true/false, which silently rendered nothing for the ambient-auth payload
// (healthy: null). It must route through testResultView / testResults state
// instead, and every runTest exit path (success and failure) must record a
// result so a click can never end in silence.
test("the test-connection result is never gated on healthy === true/false", () => {
  assert.doesNotMatch(src, /it\.healthy === true/,
    "the result region must not gate rendering on it.healthy === true");
  assert.doesNotMatch(src, /it\.healthy === false/,
    "the result region must not gate rendering on it.healthy === false");
  assert.match(src, /testResultView\(/, "must route the result through testResultView");
  assert.match(src, /testResults\[it\.name\]/, "must render from per-integration testResults state");
  // Both the try and catch arms of runTest must write a result, or an
  // exception path could still end in silence.
  const runTestMatch = src.match(/async function runTest\(name\) \{[\s\S]*?\n  \}\n/);
  assert.ok(runTestMatch, "could not find runTest");
  const runTestBody = runTestMatch[0];
  const tryIdx = runTestBody.indexOf("try {");
  const catchIdx = runTestBody.indexOf("} catch");
  const finallyIdx = runTestBody.indexOf("} finally");
  assert.ok(tryIdx >= 0 && catchIdx > tryIdx && finallyIdx > catchIdx, "runTest must have try/catch/finally");
  const tryBody = runTestBody.slice(tryIdx, catchIdx);
  const catchBody = runTestBody.slice(catchIdx, finallyIdx);
  assert.match(tryBody, /setTestResults\(/, "the try arm must call setTestResults");
  assert.match(catchBody, /setTestResults\(/, "the catch arm must call setTestResults");
});

// Health probes (integrations/health.py, boot-time + scheduled) are surfaced
// on the board as a red "Failing" badge + detail for an enabled-but-failing
// integration — separate from the manual Test-connection result above.
test("the card list renders healthBadge's Failing badge and detail", () => {
  assert.match(src, /import\s*\{[^}]*\bhealthBadge\b[^}]*\}\s*from\s*"\.\/integrationChip\.js"/,
    "must import healthBadge from integrationChip.js");
  assert.match(src, /healthBadge\(it\)/, "must call healthBadge per integration row");
  assert.match(src, /badge\.label/, "must render the badge's label (\"Failing\")");
  assert.match(src, /badge\.detail/, "must render the badge's detail alongside the label");
});
