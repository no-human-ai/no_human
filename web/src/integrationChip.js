// Pure mapping helpers for the Integrations settings panel — status → chip and
// integration → labels/brand color. Kept out of the JSX so the state machine is
// unit-testable with `node --test`.

// The status chip an IntegrationStatus renders. Five states:
//   unconfigured (neutral) · ambient — no stored config, but the operator's
//   own CLI (gh/git) is already authenticated for it (SCRUM-81, `status`
//   field from GET /api/integrations) (ambient) · configured-but-untested
//   (ok) · connected after a passing test (ok) · error after a failing test
//   (error). Ambient is checked before the configured/unconfigured split:
//   the backend never marks a stored/configured integration "ambient" (see
//   list_integrations_with_ambient), so this can't shadow a real config.
export function statusChip(it) {
  if (!it) return { label: "Unconfigured", tone: "neutral" };
  if (it.status === "ambient") return { label: "Active via CLI auth", tone: "ambient" };
  if (!it.configured) return { label: "Unconfigured", tone: "neutral" };
  // Configured, but its own switch (`integrations.<name>.enabled`, or Slack's
  // `intake`) is off — so `nh serve` starts no poller and no worker for it.
  // This panel said "Configured" for exactly that state, which is how a fully
  // filled-in Linear that polls nothing reads as working. `enabled` is null
  // for github/gitlab/jenkins, which have no switch of their own, so they can
  // never land here. Checked before healthy: a passing test on a switched-off
  // integration proves the credential works, not that anything is running.
  if (it.enabled === false) return { label: "Configured, off", tone: "neutral" };
  if (it.healthy === false) return { label: "Error", tone: "error" };
  if (it.healthy === true) return { label: "Connected", tone: "ok" };
  return { label: "Configured", tone: "ok" };
}

// The board's "Failing" badge — separate from statusChip's five states so an
// integration that has never been tested by hand (chip stays "Configured")
// still surfaces the scheduled/boot-time health probe's verdict (integrations
// /health.py). Only for integrations the operator has actually switched on:
// `enabled === false` is a deliberate off state, not a failure to report.
export function healthBadge(it) {
  if (!it || it.enabled === false || it.healthy !== false) return null;
  return { label: "Failing", tone: "error", detail: it.detail || "", checkedAt: it.checked_at || "" };
}

export const NAME_LABEL = {
  jira: "Jira", linear: "Linear", github: "GitHub", gitlab: "GitLab",
  jenkins: "Jenkins", circleci: "CircleCI", slack: "Slack",
  teams: "Microsoft Teams",
  // Lowercase on purpose — the vendor styles its own name that way, and the
  // generated fallback ("Monday") would be a different word.
  monday: "monday.com",
};

// Where each integration is configured (shown in the expand, so the operator
// knows which file/section to edit — there is one source of truth per setting).
export const CONFIG_HINT = {
  jira: "config.yaml → integrations.jira",
  linear: "config.yaml → integrations.linear",
  monday: "config.yaml → integrations.monday (board_id + status_column)",
  circleci: "config.yaml → ci (backend: circleci)",
  github: "config.yaml → ci (backend: github_actions)",
  gitlab: "config.yaml → ci (backend: gitlab)",
  jenkins: "config.yaml → ci (backend: jenkins)",
  slack: "config.yaml → notifications.slack_webhook_url",
  teams: "config.yaml → notifications.teams_webhook_url",
};

export const KIND_LABEL = {
  issue_tracker: "Issue tracker",
  vcs: "Version control",
  ci: "CI / CD",
  notifications: "Notifications",
};

// Each vendor's own published brand colour, kept as a record.
//
// NOTHING RENDERS WITH THIS ANY MORE, and that is deliberate rather than an
// oversight. It used to fill the integration glyphs in integrationIcons.jsx; a
// generic glyph painted in a vendor's registered colour, right beside that vendor's
// name, is what turns a neutral shape into a claim about a specific company. The
// glyphs now paint in one neutral app accent instead. Do not wire this back into
// anything that renders a vendor's mark or a stand-in for one — see TRADEMARK.md.
export const BRAND_COLOR = {
  jira: "#2684FF",
  linear: "#5E6AD2",
  github: "#6E7681",
  gitlab: "#FC6D26",
  jenkins: "#D33833",
  circleci: "#00CF69",
  slack: "#E01E5A",
  teams: "#6264A7",
};

// Which ~/.no_human/.env key backs each integration's secret (shown, never the
// value), so the config-expand can tell the operator exactly what to set.
export const SECRET_ENV_KEY = {
  jira: "JIRA_API_TOKEN",
  linear: "LINEAR_API_KEY",
  monday: "MONDAY_API_TOKEN",
  circleci: "CIRCLECI_TOKEN",
  // Both notify-out webhook URLs live in config.yaml, not .env — that is where
  // notify/build_notifier reads them from. They are still secrets (never
  // echoed back, scrubbed from /api/config).
  slack: "(webhook in config)",
  teams: "(webhook in config)",
};
