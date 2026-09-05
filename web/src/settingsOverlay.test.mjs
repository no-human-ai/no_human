import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Settings moved from a routed page (page === "settings") to an overlay dialog
// with a left section list, modeled on the Claude macOS desktop app — and the
// Config tab/panel was removed entirely (the backend bug it exposed is fixed
// separately in tests/test_api_config_scrub.py). Like themeVars.test.mjs and
// tokenBridge.test.mjs, this is static source analysis: no jsdom/React
// renderer is wired into this project's `node --test` harness, so these
// assertions read the .jsx source rather than mounting components.

const here = fileURLToPath(new URL(".", import.meta.url));
const settingsJsx = readFileSync(here + "Settings.jsx", "utf8");
const appJsx = readFileSync(here + "App.jsx", "utf8");
const stylesCss = readFileSync(here + "styles.css", "utf8");

test("the Config section is gone: no ConfigPanel, no config entry in the section list", () => {
  assert.doesNotMatch(settingsJsx, /ConfigPanel/, "ConfigPanel must be removed");
  // fetchConfig itself came BACK (D3.2, 2026-09-01) for an unrelated reason —
  // the Second-brain pane reads `config.learning.auto_manage` to decide which
  // of its two renderings to show — so it is no longer a valid proxy for "the
  // removed Config panel is gone". What actually matters, still asserted
  // directly: no ConfigPanel component and no "config" section/label.
  assert.doesNotMatch(settingsJsx, /key:\s*["']config["']/);
  assert.doesNotMatch(settingsJsx, /label:\s*["']Config["']/);
});

test("the section list covers Projects, Rules, Skills, Second brain, Integrations, Account, Updates", () => {
  // The name used to claim "exactly" while only checking presence, so adding a
  // section quietly made the title a lie. Both directions are asserted now:
  // every expected label is there, and the list holds nothing else.
  // "Usage insights" was REMOVED from the nav (operator, 2026-08-26): telemetry
  // is on by default and no longer surfaced in the UI, so the pane is gone.
  // "Models" is the model-picker pane (part 3 of 3): one row per role, fed by
  // GET /api/models — see modelsPanelView.test.mjs.
  // "Second brain" (D3.2, 2026-09-01 hotfix): the learnings pane was renamed
  // and is now the ONLY surface for it — see sidebarNav.test.mjs for the
  // sidebar-row removal this pairs with.
  // "Workers" (task 05a9cee0, re-home): re-homed out of ModelsPanel's old
  // WorkersRow into its own section — see workersPanelView.test.mjs.
  const expected = ["Projects", "Rules", "Skills", "Second brain", "Integrations",
                    "Models", "Workers", "Account", "Updates"];
  for (const label of expected) {
    assert.match(settingsJsx, new RegExp(`label:\\s*["']${label}["']`), `missing section: ${label}`);
  }
  const sectionsBlock = settingsJsx.match(/const SECTIONS = \[[\s\S]*?\];/)?.[0] ?? "";
  const found = [...sectionsBlock.matchAll(/label:\s*["']([^"']+)["']/g)].map((m) => m[1]);
  assert.deepEqual(found, expected,
    "SECTIONS holds a label this test does not know about");
});

test("every section key has a panel wired into the dispatch", () => {
  // A section in the list with no matching render line is a menu entry that
  // opens a blank pane — it looks fine until it is clicked.
  const sectionsBlock = settingsJsx.match(/const SECTIONS = \[[\s\S]*?\];/)?.[0] ?? "";
  const keys = [...sectionsBlock.matchAll(/key:\s*["']([^"']+)["']/g)].map((m) => m[1]);
  assert.ok(keys.length >= 7, `expected the full section list, got ${keys}`);
  for (const key of keys) {
    assert.match(settingsJsx, new RegExp(`section === ["']${key}["']\\s*&&`),
      `section "${key}" is listed but never rendered`);
  }
});

test("Settings renders as an overlay (backdrop + dialog), not a bare page", () => {
  assert.match(settingsJsx, /role="dialog"/);
  assert.match(settingsJsx, /aria-modal="true"/);
  // A backdrop element that closes the overlay on click.
  assert.match(settingsJsx, /backdrop/i);
});

test("the overlay closes on Escape", () => {
  assert.match(settingsJsx, /["']Escape["']/);
});

test("App.jsx no longer routes Settings as a page — it drives a settingsOpen overlay state", () => {
  assert.doesNotMatch(appJsx, /page === "settings"/);
  assert.match(appJsx, /settingsOpen/);
});

// Review fix #3: the trigger that opened the overlay gets focus back on close
// (Escape, ×, backdrop all tear down the component the same way, so a single
// mount/unmount effect covers all three paths).
test("the overlay captures the trigger element on open and restores focus to it on unmount", () => {
  assert.match(settingsJsx, /triggerRef\.current\s*=\s*document\.activeElement/,
    "must capture document.activeElement (the trigger) when the overlay opens");
  // The restore must run from a cleanup function (mount/unmount effect), not
  // from the Escape/close handlers directly — that's what makes it fire for
  // Escape, the × button, and the backdrop alike.
  assert.match(settingsJsx, /return\s*\(\)\s*=>\s*\{[^}]*triggerRef\.current[^}]*\.focus\(\)/s,
    "must restore focus to the captured trigger in an effect cleanup");
  // Guard against the trigger having been unmounted before the overlay closes.
  assert.match(settingsJsx, /document\.contains\(trigger\)/,
    "must guard against the trigger no longer being in the DOM");
});

// Review fix #4: section nav buttons expose aria-current when active.
test("active section nav buttons get aria-current=\"true\"", () => {
  assert.match(settingsJsx, /aria-current=\{section === s\.key \? "true" : undefined\}/,
    "nav buttons must set aria-current=\"true\" only for the active section");
});

// Review fix #1: the overlay header (Settings.jsx) is the single title; each
// panel's own in-body heading text is scope-hidden under the overlay
// container rather than deleted, so panels stay intact if reused elsewhere.
// SCRUM-14: api_key-mode rendering. The metered-key alarm must no longer be
// unconditional — it has to be gated by the authPanelView view-model so the
// api_key path (where the key IS the chosen billing path) can suppress it.
test("metered alarm is gated by the view-model, not unconditional", () => {
  assert.match(settingsJsx, /view\.showMeteredAlarm/,
    "the metered alarm must be gated by view.showMeteredAlarm, not status.metered_key_present directly");
  assert.doesNotMatch(settingsJsx, /\{status\.metered_key_present\s*&&/,
    "the metered alarm must not read status.metered_key_present unconditionally anymore");
  // The OAuth form must still exist in source for the subscription path.
  assert.match(settingsJsx, /<form className="auth-form"/);
});

// The api_key mode billing-path panel: driven by auth_mode via the view-model,
// naming the fixed key source (~/.no_human/.env) per the intake answer.
test("api_key billing-path panel is rendered", () => {
  assert.match(settingsJsx, /authPanelView/, "Settings.jsx must import/use the authPanelView view-model");
  assert.match(settingsJsx, /view\.mode === "api_key"/);
  assert.match(settingsJsx, /~\/\.no_human\/\.env/, "must name the fixed .env key source");
  assert.match(settingsJsx, /view\.apiKeyPresent/, "must surface the api_key_present state via the view-model");
});

test("panel section titles are scoped-hidden inside the overlay, not duplicated", () => {
  const titleTextOccurrences = (settingsJsx.match(/panel-title-text/g) || []).length;
  // ProjectsPanel, MemoryList (rules+skills share one component), LearningsPanel.
  assert.ok(titleTextOccurrences >= 3,
    `expected the panel-title-text marker on at least 3 panel headings, found ${titleTextOccurrences}`);
  assert.match(stylesCss, /\.settings-overlay-body\s+\.panel-title-text\s*\{[^}]*display:\s*none/,
    "styles.css must scope-hide .panel-title-text under .settings-overlay-body");
  // The count/toggle siblings (e.g. .memory-count) must stay untouched — only
  // the redundant text label is hidden, not the whole heading.
  assert.doesNotMatch(stylesCss, /\.settings-overlay-body\s+\.memory-title\s*\{[^}]*display:\s*none/,
    "must not hide the whole heading (that would also hide the count badge)");
});
