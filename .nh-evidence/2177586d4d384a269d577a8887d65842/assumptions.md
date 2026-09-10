# Assumptions

_Harness-captured record for task `2177586d`, commit `3a95dacba74077018328a92f87497b57223d5ec3` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Should the pre-review path call the exact same `_newly_failing_vs_base` helper used post-review (around line 7296), reusing its base-tree re-run logic, rather than a separate implementation? **A:** Yes — refactor so the pre-review path calls the same `_newly_failing_vs_base` helper used at core/orchestrator.py:7296 rather than reimplementing base-tree comparison logic, so both paths ask the identical question and cannot diverge in behavior. _(assumption)_
- **Q:** What exact field/shape should carry the per-id attribution shown to the reviewer (e.g., a `pre_existing_ids`/`new_ids` list plus an `attribution: attributed|unknown` status), and does this replace or augment the existing `classified` field at line 13964? **A:** Augment rather than replace: keep `classified` as-is for backward compatibility but add explicit fields `pre_existing_ids` (list), `new_ids` (list), and `attribution: "attributed"|"unknown"` at the site currently hardcoding `classified=False` (core/orchestrator.py:13964), mirroring the shape already produced by the post-review `pre_existing` logic at core/orchestrator.py:7296 so downstream reviewe _(assumption)_
- **Q:** When the base-tree re-run itself fails to execute (e.g., base checkout error, timeout), should the attempt still proceed to review with an 'unknown' attribution, or should it block/retry the base comparison first? **A:** Fire-and-forget unknown-tagging: if the base-tree re-run fails to execute (checkout error, timeout), do not block or retry the attempt's progression to review — mark attribution as "unknown" (with empty/absent pre_existing_ids/new_ids) and proceed, since the ticket explicitly requires an incomplete attribution be reported as unknown rather than blocking flow or excusing the attempt. _(assumption)_

</details>

