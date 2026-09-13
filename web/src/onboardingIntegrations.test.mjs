import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// The wizard's "Connect your tools" step. Static source analysis, the same way
// integrations.test.mjs / onboardingRoster.test.mjs read their JSX — no
// jsdom/React renderer is wired into this project's `node --test` harness.
//
// The step used to render a read-only status list: grepping it for
// linear/teams/slack/jira returned only `<linearGradient>`, an SVG tag. These
// assertions hold the two properties that fixed that, and the one that must
// never regress: the card family contains NO credential input.

const here = fileURLToPath(new URL(".", import.meta.url));
const src = readFileSync(here + "Onboarding.jsx", "utf8");

// The card component, isolated so assertions can't accidentally match JSX from
// another step (the repos step has text inputs of its own).
const CARD = (() => {
  const start = src.indexOf("function IntegrationSetupCard");
  assert.ok(start > 0, "IntegrationSetupCard must exist");
  const end = src.indexOf("\nfunction Stagger", start);
  assert.ok(end > start, "could not bound IntegrationSetupCard");
  return src.slice(start, end);
})();

const STEP = (() => {
  const start = src.indexOf('{step.key === "integrations" &&');
  assert.ok(start > 0, "the integrations step must exist");
  // The Community step joined the wizard (2026-09-12) directly after this one,
  // so the end marker moved to it — bounding on "summary" would now swallow
  // the Discord step's markup into this step's assertions.
  const end = src.indexOf('{step.key === "discord" &&', start);
  assert.ok(end > start, "could not bound the integrations step");
  return src.slice(start, end);
})();

test("the step is generated from the server's discovered specs", () => {
  // Fetched from the setup endpoint (which walks DEFAULT_CONFIG), not the
  // status-only endpoint the read-only version used.
  assert.match(src, /fetchIntegrationSetup\(\)/);
  assert.match(STEP, /integrations\.map\(\(spec\)/);
  assert.match(STEP, /<IntegrationSetupCard/);
});

test("no integration is named in the UI — a new one renders with no edit here", () => {
  // The bug report was "grep finds only <linearGradient>". The fix must not be
  // "now grep finds four hardcoded names".
  for (const name of ["jira", "linear", "circleci", "slack", "teams"]) {
    assert.ok(
      !new RegExp(`["'\`]${name}["'\`]`, "i").test(STEP + CARD),
      `${name} must not be hardcoded in the integrations step`,
    );
  }
});

test("the on/off switch is whatever the spec's enable_field names", () => {
  assert.match(CARD, /const enableField = spec\.enable_field;/);
  // The checkbox reflects EFFECTIVE state, not the raw stored value: a mute
  // switch that ships on (e.g. teams) must not render as checked on a fresh,
  // unconfigured install — see effectiveEnabled() in integrationSetup.js.
  assert.match(CARD, /checked=\{isOn\}/);
  assert.match(CARD, /effectiveEnabled\(spec, values\)/);
});

test("clicking a card opens its setup even when the switch can't turn on (F9)", () => {
  // teams is a mute switch that reads OFF-but-unconfigured and cannot be toggled
  // ON in the UI. Before F9 its body was gated on isOn alone, so clicking teams
  // did nothing and its webhook-setup note was unreachable. The body now opens
  // on isOn OR an explicit expand, and flipping the switch flips that expand —
  // so every card, teams included, reveals its setup on click.
  assert.match(CARD, /const showBody = isOn \|\| expanded;/,
    "the body must open on an explicit expand, not on isOn alone");
  assert.match(CARD, /\{showBody && \(/,
    "the card body must be gated on showBody, not isOn");
  assert.match(CARD, /onToggleOpen\?\.\(\)/,
    "toggling the switch must also toggle the card open, so a mute switch reveals its setup");
});

test("every other control comes from the spec's fields, typed by `kind`", () => {
  assert.match(CARD, /settings\.map\(\(f\)/);
  assert.match(CARD, /f\.kind === "bool"/);
  assert.match(CARD, /f\.kind === "list"/);
  assert.match(CARD, /\{f\.label\}/);
});

test("THE CREDENTIAL RULE: the card renders no secret input of any kind", () => {
  // A password field, a `secret` branch, or a literal token/key input in this
  // family would mean the wizard could carry a credential into config.yaml.
  assert.ok(!/type="password"/.test(CARD), "no password input");
  assert.ok(!/type=\{[^}]*password/.test(CARD), "no conditional password input");
  assert.ok(!/f\.secret/.test(CARD), "the setup spec has no `secret` field to branch on");
  assert.ok(!/api_key|api_token|webhook_url|\bsecret_value\b/i.test(CARD),
            "no credential field name may appear in the card");
  // What it does instead: name the env var.
  assert.match(CARD, /secretHint\(spec\)/);
});

test("the step tells the user where credentials go, in the step copy itself", () => {
  assert.match(STEP, /~\/\.no_human\/\.env/);
  assert.match(STEP, /never taken here/i);
});

test("the chip's on/off follows the draft, but 'Ready' still needs the server", () => {
  // M3: the chip must AGREE with the just-ticked Enable checkbox — passing the
  // draft `values` flips it "Off" → "On — needs settings" immediately instead
  // of lagging until Save. The honesty guard is preserved because the draft
  // only reaches effectiveEnabled (the on/off gate); "Ready" still requires
  // `verified` (a passing live test / persisted last_verified_at), which is NOT
  // in the draft — so a typed-but-unsaved value can never reach "Ready", the
  // exact "Settings disagrees with reality" defect this test guards against.
  assert.match(CARD, /const ready = readiness\(spec, \{ verified, values \}\);/);
  assert.match(CARD, /integration-chip tone-\$\{ready\.tone\}/);
  // The verified flag is still a passing test / persisted verification, never
  // "a value was typed".
  assert.match(CARD, /test\.healthy === true/);
});

test("the Launch summary counts integrations from the server specs", () => {
  const summary = src.slice(src.indexOf('{step.key === "summary" &&'));
  assert.match(summary, /setupSummary\(integrations\)/);
  assert.match(summary, /Tracker &amp; notification integrations on/);
});

// Bug report (2026-09-01): the product ships NINE integrations (github/gitlab/
// jira/monday/linear/slack/teams/jenkins/circleci — all nine render in
// Settings → Integrations, see integrations.test.mjs), but GET
// /api/integrations/setup only discovers the FIVE config-block ones
// (jira/linear/monday/slack/teams — the issue_tracker + notifications kinds).
// The forge/CI kinds (github/gitlab/jenkins/circleci) are configured per-repo
// under `ci.*` and are deliberately absent from that endpoint. The row used to
// say bare "Integrations on 0 of 5", which reads as the product's full
// integration count. The label must say what it counts instead of silently
// scoping the word "Integrations" to a five-item subset.
test("the Launch summary's integrations row names its own scope, not the product total", () => {
  const summary = src.slice(src.indexOf('{step.key === "summary" &&'),
                             src.indexOf("{readiness && !readiness.error && readiness.usable === 0"));
  // The row must not present a bare "Integrations on" — that reads as "all of
  // them" when the product ships nine and this endpoint only ever returns
  // five (config-block: jira/linear/monday/slack/teams).
  assert.doesNotMatch(summary, /<span>Integrations on<\/span>/,
    "a bare 'Integrations on' label misrepresents a 5-of-9 subset as the total");
  // The replacement label states its scope in the copy itself.
  assert.match(summary, /<span>Tracker &amp; notification integrations on<\/span>/);
});

test("saving sends only changed values and re-seeds from the response", () => {
  const save = src.slice(src.indexOf("async function saveIntegration"),
                         src.indexOf("function toggleRepo"));
  assert.match(save, /changedValues\(spec, intDraft\)/);
  assert.match(save, /saveIntegrationSetup\(spec\.name, values\)/);
  // The refreshed spec replaces the local one, so the card reflects what
  // landed on disk rather than what was typed.
  assert.match(save, /draftFrom\(\[refreshed\]\)\[spec\.name\]/);
});

test("every class the card uses has a rule in styles.css", () => {
  const css = readFileSync(here + "styles.css", "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const used = new Set();
  for (const m of CARD.matchAll(/className="([^"{]+)"/g)) {
    for (const c of m[1].split(/\s+/)) if (c.startsWith("ob-integration")) used.add(c);
  }
  assert.ok(used.size >= 8, `expected the card to use its own classes, saw ${used.size}`);
  for (const c of used) {
    assert.ok(new RegExp(`\\.${c}[\\s,.:{]`).test(css), `.${c} has no rule in styles.css`);
  }
});

test("default_repo renders as a select over registered repos, not free text (C3)", () => {
  // "Run tasks in repo" must be a dropdown over the operator's registered
  // profiles — a pulled-in ticket can only ever name a repo no_human knows.
  assert.match(CARD, /f\.kind === "repo_select"/);
  assert.match(CARD, /\(f\.options \|\| \[\]\)\.map\(\(opt\)/);
  assert.match(CARD, /<select/);
});

test("the Integrations step explains that GitHub/GitLab/CI come from the repo profile (C3)", () => {
  assert.match(STEP, /configured per repository from its profile/);
  assert.match(STEP, /GitHub, GitLab and CI/);
});
