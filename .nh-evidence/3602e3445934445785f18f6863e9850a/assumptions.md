# Assumptions

_Harness-captured record for task `3602e344`, commit `315253b294249ad746f668f0e8b9891269c19256` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** For worktree._builder_python's special requirement to try the displaced venv first: should the shared interpreter resolver have an optional `displaced_venv` parameter (tried first in the fallback chain), or should worktree._builder_python remain a wrapper that tries displaced before calling the helper without passing this parameter? **A:** Optional `displaced_venv` parameter in the shared resolver function, allowing worktree._builder_python to pass the displaced venv path to be tried first in the fallback chain, rather than keeping a wrapper wrapper with duplicate fallback logic. This centralizes the fallback ordering logic and keeps the parameter contract explicit. _(assumption)_
- **Q:** After analyzing the import graph for cycles, if multiple existing modules could host the shared resolver without introducing cycles, should we prefer one of them (e.g., vcs/ since approve_merge is there), or create a new module? **A:** Create a new small module (e.g., `src/no_human/interpreters.py` or `src/no_human/python_resolver.py`) to host the shared resolver. This is the safest choice when the import graph analysis hasn't been completed, avoiding any risk of circular imports between vcs/, core/, and testing/ consumers. _(assumption)_
- **Q:** When no interpreter resolves and a manifest_repair site fails closed, should the error message follow a generic pattern (e.g., 'No Python interpreter found') or include site-specific context to identify which operation failed (e.g., 'Cannot approve pending pins: No Python interpreter found')? **A:** Include site-specific context identifying which operation failed (e.g., 'Cannot approve pending pins: No Python interpreter found' rather than generic 'No Python interpreter found'). This matches standard CLI error messaging practice and helps users identify exactly which of the five manifest_repair operations failed when no interpreter resolves. _(assumption)_

</details>

