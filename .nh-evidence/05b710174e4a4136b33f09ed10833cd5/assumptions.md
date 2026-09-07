# Assumptions

_Harness-captured record for task `05b71017`, commit `cfb0b004a5cee0ac3353657088551932db248d15` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** The specification refers to running `check_release_manifest.py --write` 'with the existing bounded timeout', but does not specify the timeout value. What timeout duration (in seconds) should be used, or which variable/constant in the existing codebase defines it? **A:** Identify and reuse the existing timeout constant already applied to check_release_manifest.py --write in the codebase (likely from PR #121's reactive implementation); if no constant is formalized, apply 30 seconds as a reasonable default for manifest operations _(assumption)_
- **Q:** If `git add -- <paths>` fails during the proactive step (permission error, invalid path, etc.), should it: (a) prevent the commit attempt entirely, or (b) be logged and allow the commit to proceed (matching the documented --write failure handling)? **A:** Option (b): log the git add error and allow the commit to proceed, consistent with the documented --write failure handling doctrine that does not fail commits _(assumption)_
- **Q:** When paths is None, should the proactive step: (a) skip entirely and rely on commit_all's existing behavior, (b) run --write but skip the `git add` staging step, or (c) run the full proactive step including `git add` on all unstaged changes? **A:** Option (a): skip the proactive step entirely when paths is None and rely on commit_all's existing sweep behavior with the reactive route as fallback _(assumption)_
- **Q:** For the public vs. private shape detection (checking file presence/absence: EXPORT_CLASSIFICATION.txt, check_release_manifest.py, etc.), should these checks be implemented as new file-existence logic inside the proactive step function, or should I reuse existing shape-detection logic already present elsewhere in the codebase? **A:** Option (b): reuse existing shape-detection logic elsewhere in the codebase if already present; otherwise implement file-existence checks (EXPORT_CLASSIFICATION.txt, check_release_manifest.py, RELEASE_MANIFEST.txt presence) as a local helper function within or alongside the proactive step _(assumption)_

</details>

