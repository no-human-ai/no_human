# Assumptions

_Harness-captured record for task `29ada5ea`, commit `800b87426efefad3b649513b365b8abe59b4012e` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** What is the git repository URL or path, and which branch should the agent work on? The working directory is a temporary directory (not a git repo); should the agent clone/fetch the repo, or is it checked out elsewhere? **A:** HUMAN-GATED: not self-answerable
- **Q:** Should this fix be incorporated into the existing PR #475 (which is currently blocked) or developed as a separate feature branch and PR? **A:** HUMAN-GATED: not self-answerable
- **Q:** The task says the ours-blob fallback in load_scanner 'may stay' for legacy merges. Should the agent definitely retain this fallback (possibly with a comment explaining backwards-compatibility), or remove it to enforce the new design? **A:** Retain the fallback. The task description explicitly states the ours-blob fallback 'may stay for a merge whose ours side predates the move', indicating backwards compatibility is acceptable. The acceptance criteria require the production module to be returned for a normal case and pytest not to be required on that path, but do not mandate fallback removal. A senior engineer would retain it with a _(assumption)_

</details>

