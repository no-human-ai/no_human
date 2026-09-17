// Human-readable labels for the event kinds the board renders.
//
// This lived inside SlideOver.jsx until 2026-08-22. It was moved out so the
// mapping is a real importable module: `node --test` can load it and assert
// that a kind actually resolves to a label. The guard it replaces read this
// file as TEXT and asserted a substring, which passed even when the mapping
// was commented out and the board had silently fallen back to the raw kind.

export const EVENT_LABELS = {
  kind: "Task type",
  state: "Status",
  context_gather: "Context",
  context: "Context",
  profile: "Profile",
  ci_backend: "CI",
  attempt_start: "Attempt",
  attempt_failed: "Attempt failed",
  supervisor: "Supervisor",
  commit: "Commit",
  lint: "Lint",
  tests: "Tests",
  tamper: "Tamper check",
  review_start: "Review",
  review: "Review result",
  review_error: "Review error",
  agent_error: "Agent error",
  stuck: "Stuck",
  failed: "Failed",
  bounds: "Bounds",
  tool_use: "Tool",
  agent_text: "Agent",
  thinking: "Reasoning",
  supervisor_decision: "Supervisor",
  // env setup/teardown (Item 2)
  env_setup: "Env setup",
  env_setup_failed: "Env setup failed",
  env_teardown: "Env teardown",
  // the learning loop, applied (S3-B2)
  knowledge_accessed: "Knowledge applied",
  // skills & subagents (Items 3, 4)
  skills_materialized: "Skills loaded",
  skills_loaded: "Skills",
  // investigation report (Item 1)
  investigation_report: "Investigation report",
  // checkpoint & resume (existing, unlabelled)
  checkpoint: "Checkpoint",
  resume_wip: "Resume WIP",
  resume_checkpoint_lost: "Checkpoint lost",
  // compound tasks / lead agent (Item 5)
  decompose: "Decompose",
  compound_done: "Compound done",
  quality_gate: "Quality gate",
  quality_gate_failed: "Quality gate failed",
  unblock: "Unblock",
  retry: "Retry",
  escalate: "Escalate",
  // subagent events
  subagent_start: "Subagent",
  subagent_progress: "Subagent",
  subagent_done: "Subagent done",
  review_posted: "Review posted to PR",
  // post-PR watcher ladder (blockers/wake.py) — merged/comments/CI/Enterprise CI
  merged: "PR merged",
  pr_closed: "PR closed",
  pr_feedback: "PR feedback",
  pr_feedback_skipped: "Bot comments ignored",
  pr_feedback_deferred: "PR feedback deferred",
  commit_refused: "Commit refused (attempt failed honestly)",
  manifest_repaired: "Manifest pins re-approved by the pipeline",
  pr_ci_red: "PR CI red",
  pr_ci_infra: "PR CI red (infra — not acted on)",
  pr_ci_advisory: "PR CI red (advisory mode)",
  escalated_ci: "CI escalated",
  escalated_revisions: "Revisions escalated",
  escalated_timeout: "Park timeout",
  resumed: "Resumed",
  wake_tick: "Watcher heartbeat",
  state_repaired: "State repaired",
  // Enterprise CI integration gate (M6)
  ci_gate_trigger: "Enterprise CI triggered",
  ci_gate_poll: "Enterprise CI running",
  ci_gate_pass: "Enterprise CI passed",
  ci_gate_fail: "Enterprise CI failed",
  ci_gate_blocked: "Enterprise CI blocked",
  ci_gate_refused: "Enterprise CI refused",
  // the board's approve button, refused (task e24cee25/PR #643: a refusal
  // used to reach the operator nowhere at all)
  approve_refused: "Approve refused",
  // stale-but-mergeable PR re-measurement (task 22c4ddf6 finding #3):
  // trunk moved under a still-MERGEABLE PR
  pr_base_remeasured: "PR base re-measured",
  pr_base_undetermined: "PR base freshness undetermined",
};

export function eventLabel(kind) {
  return EVENT_LABELS[kind] || kind;
}
