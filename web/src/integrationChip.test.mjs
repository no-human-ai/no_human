import test from "node:test";
import assert from "node:assert/strict";
import {
  statusChip, healthBadge, KIND_LABEL, BRAND_COLOR, NAME_LABEL, CONFIG_HINT,
} from "./integrationChip.js";

// Must match integrations/__init__.py `_ORDER`.
const ALL = ["jira", "linear", "monday", "github", "gitlab", "jenkins",
             "circleci", "slack", "teams"];

test("statusChip maps the four non-ambient states", () => {
  assert.deepEqual(statusChip({ configured: false }), { label: "Unconfigured", tone: "neutral" });
  assert.deepEqual(statusChip({ configured: true, healthy: null }), { label: "Configured", tone: "ok" });
  assert.deepEqual(statusChip({ configured: true, healthy: true }), { label: "Connected", tone: "ok" });
  assert.deepEqual(statusChip({ configured: true, healthy: false }), { label: "Error", tone: "error" });
  // null/undefined input is safe
  assert.equal(statusChip(null).tone, "neutral");
});

// SCRUM-81 shipped the payload's `status` field as 'configured' | 'ambient' |
// 'unconfigured'. This is the panel's third, distinct chip state: an
// unconfigured provider (no stored token) whose CLI (gh/git) is itself
// already authenticated.
test("statusChip renders a distinct third state for status: 'ambient'", () => {
  assert.deepEqual(
    statusChip({ configured: false, healthy: null, status: "ambient" }),
    { label: "Active via CLI auth", tone: "ambient" },
  );
});

// `nh serve` starts the Jira/Linear pollers only when
// `integrations.<name>.enabled` is true (cli/commands.py). A fully configured
// integration with that switch off runs NOTHING, and this panel used to call
// it "Configured" — which is the "I don't have Linear" report.
test("statusChip does not call a switched-off integration Configured", () => {
  assert.deepEqual(
    statusChip({ configured: true, healthy: null, enabled: false }),
    { label: "Configured, off", tone: "neutral" },
  );
  // Even a PASSING connection test doesn't make it running — that proves the
  // credential works, not that a poller was started.
  assert.equal(statusChip({ configured: true, healthy: true, enabled: false }).label,
               "Configured, off");
});

// A mute switch that ships on (teams: enabled defaults true) must not read
// as "on" on a fresh, unconfigured install — `!configured` is checked before
// `enabled` is ever looked at, so this can never render an "on" tone.
test("statusChip shows an unconfigured mute switch as Unconfigured, not on", () => {
  assert.deepEqual(
    statusChip({ configured: false, enabled: true, healthy: null }),
    { label: "Unconfigured", tone: "neutral" },
  );
});

test("statusChip is unchanged for integrations that have no switch", () => {
  // github/gitlab/jenkins are views over ci.*; the API sends enabled: null.
  assert.deepEqual(statusChip({ configured: true, healthy: null, enabled: null }),
                   { label: "Configured", tone: "ok" });
  assert.deepEqual(statusChip({ configured: true, healthy: true, enabled: true }),
                   { label: "Connected", tone: "ok" });
  // A payload from before the field existed must behave exactly as it did.
  assert.deepEqual(statusChip({ configured: true, healthy: true }),
                   { label: "Connected", tone: "ok" });
});

test("statusChip renders all three states distinctly from a fixture payload list", () => {
  const fixture = [
    { name: "jira", configured: true, healthy: null, status: "configured" },
    { name: "github", configured: false, healthy: null, status: "ambient" },
    { name: "gitlab", configured: false, healthy: null, status: "unconfigured" },
  ];
  const chips = fixture.map(statusChip);
  assert.deepEqual(chips, [
    { label: "Configured", tone: "ok" },
    { label: "Active via CLI auth", tone: "ambient" },
    { label: "Unconfigured", tone: "neutral" },
  ]);
  // Every label and tone in the fixture is unique — no two states collapse.
  assert.equal(new Set(chips.map((c) => c.label)).size, 3);
  assert.equal(new Set(chips.map((c) => c.tone)).size, 3);
});

// A stored/configured integration is never relabelled "ambient" even if its
// CLI also happens to be authenticated — the backend guarantees this
// (list_integrations_with_ambient), and the panel must not second-guess it
// by checking `configured` before `status`.
test("statusChip never treats a configured integration as ambient", () => {
  assert.deepEqual(
    statusChip({ configured: true, healthy: true, status: "configured" }),
    { label: "Connected", tone: "ok" },
  );
});

test("KIND_LABEL covers every integration kind", () => {
  for (const k of ["issue_tracker", "vcs", "ci", "notifications"]) {
    assert.ok(KIND_LABEL[k], `missing kind label: ${k}`);
  }
});

test("NAME_LABEL / CONFIG_HINT cover every integration", () => {
  for (const name of ALL) {
    assert.ok(NAME_LABEL[name], `name label: ${name}`);
    assert.ok(CONFIG_HINT[name], `config hint: ${name}`);
  }
  assert.equal(NAME_LABEL.circleci, "CircleCI");   // proper casing, not "Circleci"
  assert.equal(NAME_LABEL.teams, "Microsoft Teams");
  // Lowercase, because that is how the vendor writes it — and because the
  // `NAME_LABEL[name] || name` fallback would render "monday" anyway, so an
  // entry that merely capitalised it would be a change, not a label.
  assert.equal(NAME_LABEL.monday, "monday.com");
});

// BRAND_COLOR is deliberately NOT required to cover ALL. It renders nothing —
// integrationIcons.jsx paints every glyph in one neutral app accent, precisely
// so a generic shape in a vendor's registered colour beside that vendor's name
// stops being a claim about that company (TRADEMARK.md). Growing it with each
// new integration would re-establish the habit the removal was meant to end.
// What still has to hold is that every entry it DOES carry is a well-formed
// hex, so the dead record cannot rot into something that renders wrong if it
// is ever revived.
test("every BRAND_COLOR entry is a well-formed hex", () => {
  const entries = Object.entries(BRAND_COLOR);
  assert.ok(entries.length > 0);
  for (const [name, hex] of entries) {
    assert.match(hex, /^#[0-9A-Fa-f]{6}$/, `brand color: ${name}`);
  }
});

// healthBadge (integrations/health.py's boot-time + scheduled probe, surfaced
// on the board) is deliberately separate from statusChip: a manual test can
// pass (chip stays "Configured"/"Connected") while the background probe is
// currently failing, and vice versa right after a fix until the next probe.
test("healthBadge returns null unless enabled and currently failing", () => {
  assert.equal(healthBadge(null), null);
  assert.equal(healthBadge(undefined), null);
  // healthy: true -> nothing to flag.
  assert.equal(healthBadge({ enabled: true, healthy: true, detail: "authenticated" }), null);
  // never probed -> nothing to flag (not the same as failing).
  assert.equal(healthBadge({ enabled: true, healthy: null, detail: "" }), null);
  // the operator turned it off -> not a failure to report.
  assert.equal(healthBadge({ enabled: false, healthy: false, detail: "HTTP 404" }), null);
});

test("healthBadge flags an enabled-but-failing integration", () => {
  assert.deepEqual(
    healthBadge({ enabled: true, healthy: false, detail: "HTTP 404 from acme.atlassian.net" }),
    { label: "Failing", tone: "error", detail: "HTTP 404 from acme.atlassian.net", checkedAt: "" },
  );
});
