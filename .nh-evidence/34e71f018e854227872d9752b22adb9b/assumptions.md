# Assumptions

_Harness-captured record for task `34e71f01`, commit `5144eff4905c7177884ca85fa9fc3b50e129b75c` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Has extract_acceptance_criteria been tested/verified to correctly handle Monday's acceptance criteria in both formats: (a) markdown-style headers (## Acceptance Criteria) with plain bullets, and (b) checkbox-only items? **A:** No, extract_acceptance_criteria has not yet been verified to correctly handle Monday's markdown-style header format (## Acceptance Criteria with plain bullets). The task description indicates this is what the PR will implement and test; checkbox-only format handling status is unclear without inspection. The task explicitly states a test will verify both formats work after the change. _(assumption)_
- **Q:** Is _checklist_items referenced anywhere in the codebase besides monday.py:248 (definition) and monday.py:617 (call)? **A:** _checklist_items is likely only referenced in monday.py at the two locations specified (definition at 248, call at 617). The task structure and scope (fixing Monday's private copy) suggests it is Monday-specific with no other callers, but a codebase grep would be needed to confirm. _(assumption)_
- **Q:** If extract_acceptance_criteria doesn't correctly handle Monday's markdown header format, should the solution extend extract_acceptance_criteria in criteria.py, or should monday.py add Monday-specific preprocessing/postprocessing? **A:** The solution should extend extract_acceptance_criteria in criteria.py to handle both Monday's formats (markdown headers and checkboxes), then import and use this shared function in monday.py with no private helper. The acceptance criteria explicitly requires monday.py to import extract_acceptance_criteria and have no private checklist helper, indicating the shared function is the intended approach _(assumption)_

</details>

