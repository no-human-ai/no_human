# Assumptions

_Harness-captured record for task `2d9a7369`, commit `af78f7e61ff35608aec14157b028daf34de06177` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** What timeout duration (in seconds) should constrain the subprocess call to scripts/check_release_manifest.py --write? The task says "like the existing approve path" — does an existing timeout value in the code exist to reference, and if not, what should we use? **A:** Without access to the code in this final turn, assume 60 seconds as a reasonable bounded timeout for a subprocess call to regenerate RELEASE_MANIFEST.txt, consistent with manifest-level operations. The task references 'like the existing approve path' but does not cite the specific duration; if the approve path's timeout exists in src/no_human/vcs/manifest_repair.py (likely as a module-level consta _(assumption)_
- **Q:** What is the exact signature of the on_repair callback function that is already passed to commit_with_manifest_repair? The task shows on_repair(pinned, note) but the parameter types (e.g., is pinned a list, set, or string?) and the source/existing code for this function must be clarified. **A:** From the task description, on_repair is called as on_repair(pinned, note). The most reasonable inference from context is: pinned is a list of file path strings (List[str]) representing the files that were pinned and required repair (matching the offending paths parsed from the pre-commit gate's refusal output), and note is a string message describing the repair action taken (e.g., 'regenerated via _(assumption)_

</details>

