# Assumptions

_Harness-captured record for task `3517050d`, commit `58a1894f754a1ad4775a6bca87040e36a22d600e` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** Where should the corrective round measure the frozen-entry value from at commit time — re-measure the file(s) immediately before the actual commit/write step, or diff against the final working tree state right before finalize? Is there an existing 'commit' step/hook in the orchestrator that this should hook into, or does one need to be added? **A:** Measure at commit time: hook the re-freeze measurement into the existing commit/finalize step so the frozen value is computed from the final working tree right before the commit is made (i.e., as the last action before finalize writes/commits), rather than mid-round. If the orchestrator's finalize step doesn't currently expose a hook point for this, add one immediately preceding the commit rather _(assumption)_
- **Q:** What is the intended bound on corrective rounds for this cause (e.g., a fixed max, or reuse of an existing round-limit constant), and what exact 'cause' string/field should be reported when the bound is hit (e.g., a new failure reason enum value, or reuse of structural_budget_grown's kind)? **A:** Reuse the existing round-limit constant/mechanism rather than introducing a new bound specific to this cause, and report the cause via the existing structural_budget_grown kind/event (adding a distinct terminal state like 'budget_preflight_exhausted' only if the existing kind can't carry a 'bound reached' signal) rather than inventing a wholly new enum; the reported cause field should be asserted _(assumption)_

</details>

