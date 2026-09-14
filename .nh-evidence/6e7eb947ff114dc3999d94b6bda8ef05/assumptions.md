# Assumptions

_Harness-captured record for task `6e7eb947`, commit `c17a77abb33b78247464bdb88276705af902830a` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Should a skipped required angle prevent a task from being marked merge-ready, or should it only flag the task as not-fully-gated while still allowing approval to proceed? **A:** A skipped required angle should prevent a task from being marked merge-ready. This choice prevents the silent-skip problem by making the gate's incompleteness explicit and binding, matching the principle that 'merge-ready' must mean 'fully gated.' A required angle's absence is a genuine unresolved gate condition, not an advisory. This respects the visibility requirement (a human must see the gate _(assumption)_
- **Q:** Is 'required' already modeled as an attribute on angles per task tier in the existing code, or should all angles uniformly treat a no-verdict state as equally problematic regardless of current classification? **A:** 'required' is already modeled as an attribute on angles per task tier. The task description's phrasing ('required angle for its tier') indicates this distinction exists. The fix should apply uniformly to all angles that carry the required flag, treating a no-verdict state as equally problematic for required angles. This allows non-required angles to be advisory while required angles enforce the ga _(assumption)_
- **Q:** Do we have read access to the system that tracks historical attempts and angle results, so we can generate the statistics requested in the acceptance criteria (count of attempts with skipped angles, filtered by date if needed)? **A:** HUMAN-GATED: not self-answerable

</details>

