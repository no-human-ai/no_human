# Assumptions

_Harness-captured record for task `7ee2d939`, commit `3ca689e203ca76d6c9b3e354b8be8f5d05e11e92` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

> ⚠️ **Unresolved:** max_attempts (3) reached without a passing, untampered change. The attempt trail is in this blocker's evidence and 'what I tried'.

> ⚠️ **Open question:** The agent could not complete this within bounds. Refine the task, split it, or advise an approach.

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Does the desktop client on first launch (with no credential configured) boot a server at all? The task flags this as an open question—if not, the onboarding funnel's first step is structurally invisible for that segment server-side. **A:** Likely yes—standard desktop application architecture boots a local server on startup—but the task explicitly flags this as an open question requiring empirical verification before claiming the blind spot is fully closed. Cannot proceed with server-side instrumentation claims for that segment until verified. _(assumption)_
- **Q:** For `repo_selected` events: should they fire once per onboarded repository (cardinality = count of repos per install), or at most once per onboarding session (cardinality = 1 per install)? The task describes this as either/or, with opposite implications for the wire. **A:** At most once per onboarding session/install. The previous attempt fired once per onboarded repository, which created a cardinality leak (repo count derivable from event count); emitting at most once per install prevents this while preserving the funnel signal for abandonment. The acceptance criteria confirm this design. _(assumption)_
- **Q:** Should the onboarding flow attempt live validation or probing of the user's credential (for UX feedback), or should all credential operations be deferred until task creation? The prior branch probed and violated the quota rule; clarify whether that is a use case here. **A:** HUMAN-GATED: not self-answerable

</details>

