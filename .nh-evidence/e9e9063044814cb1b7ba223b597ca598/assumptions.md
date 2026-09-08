# Assumptions

_Harness-captured record for task `e9e90630`, commit `6938b03744c2f870a1db95374c9ec07ae2003d6d` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** When the implement prompt narrates a rebase failure caused by overlapping files, what exact wording or format should be used to name the files to the coder? (e.g., 'Overlapping files preventing rebase: calc.py', 'Conflict in: calc.py', or other phrasing) **A:** Rebase could not complete due to overlapping file changes: [filenames], where [filenames] is a comma-separated list of the files from overlapping_files. Pattern: 'Rebase could not complete due to overlapping changes in: calc.py, schema.py' if multiple files. _(assumption)_
- **Q:** Should the `overlapping_files` field in base_staleness events and task records be a required field (always present, possibly as an empty list when no overlap) or an optional field (only included when overlap is detected)? **A:** Optional field—only included in base_staleness events and task records when overlap is detected (non-empty list). Absent fields default to empty list via .get('overlapping_files') or [] pattern, reducing event payload noise for the common case of no overlap. _(assumption)_
- **Q:** Where should the new real-git test for the 1-commit overlapping gap with rebase conflict be added—to tests/test_base_staleness_overlap.py (alongside the extended AST test), to tests/test_retry_base_staleness.py, or a new file? **A:** tests/test_retry_base_staleness.py, since the task specifies the new real-git test uses 'same fixtures as tests/test_retry_base_staleness.py' and retry-related tests should be co-located with existing fixtures; tests/test_base_staleness_overlap.py receives only the extended AST wiring test. _(assumption)_

</details>

