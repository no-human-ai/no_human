// The onboarding wizard's step list. Pulled out of Onboarding.jsx so it has
// one real source of truth an e2e walk can import (e.g.
// e2e/onboarding-consent-step.mjs), instead of a hardcoded rail-length
// constant going stale every time a step is added or removed here.
export const BASE_STEPS = [
  { key: "welcome",  title: "Welcome" },
  // Required (operator decision, 2026-09-12): every install registers an
  // email address before continuing — no "skip"/"remind me later". It is the
  // address the existing welcome email (src/no_human/email/base.py, frozen
  // copy of no_human-cloud's template) goes to. Not skippable, so it is its
  // own step rather than a field bolted onto "welcome" or "summary".
  { key: "email",    title: "Email" },
  // The "You"/team step left the free-tier wizard on the operator's 2026-08-09
  // decision: the value it collected was write-only in the local product
  // (persisted at complete, read by nothing). It belongs to the future
  // free→team / free→cloud UPGRADE onboarding, where team scoping is real.
  { key: "repos",    title: "Repositories" },
  { key: "projects", title: "Projects" },
  // "Repo docs & wiki" left the wizard (operator, 2026-09-04): it was a step
  // that asked nothing decision-worthy of the user. The wiki is now enqueued
  // automatically, in the background, when Launch completes onboarding — see
  // kickoffWikiGeneration() in Onboarding.jsx's finish().
  { key: "integrations", title: "Integrations" },
  // "AI history" + "Rules review" left the wizard (operator, 2026-08-30): the
  // AI-learnings walk made onboarding long, and the work already lives in
  // Settings. The Settings "!" badge nudges the user to complete it there;
  // second-brain rules/learnings are viewed and added in the Settings panes.
  // "Community" joined the wizard on the operator's 2026-09-12 request so every
  // first-run user is offered the Discord once, in the place they are already
  // looking, rather than only in a README they may never open. Kept to one
  // decision and one action, for the same reason the team/docs/AI-history/
  // telemetry steps above were removed: it offers, it never gates.
  { key: "discord",  title: "Community" },
  { key: "summary",  title: "Launch" },
];
