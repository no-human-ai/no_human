# Assumptions

_Harness-captured record for task `4fbf77e6`, commit `9b8c64cddc990f62652e91d98cadf61f0e476040` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

> ⚠️ **Unresolved:** You've hit your weekly limit · resets 5am (Asia/Jerusalem) ('personal' subscription)

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** Where is the source repository located (Git URL or local path)? Are credentials needed to access it? **A:** HUMAN-GATED: not self-answerable
- **Q:** What is the data structure of the `prompt` object passed to `_review_once()`? Should coverage rejection feedback be injected into the system message, user message, as a separate field, or elsewhere? **A:** Yes, `InspectionTracker.rejection()` **already returns a string containing the specific unreferenced file paths** (lines 275-281 in diff_coverage.py). The method calls `unreferenced()` to get the set of missing paths (line 277), and if any exist, returns a formatted string that includes `{', '.join(missing)}` — the exact paths as a comma-separated list (line 280-281). The rejection string can be u _(assumption)_
- **Q:** Does the current InspectionTracker.rejection() method already return a string containing the specific unreferenced file paths, or only a generic message that would require parsing to extract them? **A:** The current code structure provides no mechanism for accumulating feedback across multiple rejection rounds — the prompt is passed identically each round (line 3056-3060), and there is no multi-round state carried forward. The most reasonable design is **only the most recent round's unreferenced paths should be included in the feedback to round N+2**. This avoids false positives (claiming a path i _(assumption)_
- **Q:** If round N+1 also results in a coverage rejection (different unreferenced paths) after receiving feedback from round N, should round N+2 receive feedback about all prior unreferenced paths or only the most recent failure's paths? **A:** (unanswered)

</details>

