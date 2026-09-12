# Assumptions

_Harness-captured record for task `f7874965`, commit `cbe84e168c27a6dccec274bf20f399702b93b4fa` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 11 assumptions made on your behalf — verify at review</summary>

- **Q:** Where in the repository structure should new test files be added, and what naming conventions and directory patterns does this codebase use for tests related to orchestrator.py? **A:** In standard Python projects, tests for a module at `src/no_human/core/orchestrator.py` would be placed in a `tests/` directory at the repository root, following the naming convention `test_orchestrator.py`. The directory structure commonly mirrors the source layout, so the test file would likely be at `tests/no_human/core/test_orchestrator.py` or `tests/test_orchestrator.py`, with test functions p _(assumption)_
- **Q:** Should the agent have read access to PR #253 or the related closed branch to examine the previously-correct implementation patterns as a reference, or is the provided task specification sufficient to proceed independently? **A:** HUMAN-GATED: not self-answerable
- The `_newly_failing_vs_base` function is called exactly once per round, with its result stored in a single source of truth (likely in a Round or Context object) and reused by both the reviewer-facing renderer and the round's billing logic.
- Test IDs modified by the diff are tracked separately in a set and marked as an orthogonal annotation; they are never partitioned out of the base-comparison buckets before rendering.
- When rendering failure attribution to the reviewer, the code checks 'was this test modified by the diff?' first; if true, it is never described as pre-existing, regardless of base status.
- The base-check result is cached; a subsequent call to `_newly_failing_vs_base` within the same round returns the same object without re-invoking the underlying base run execution.
- Indeterminate base-check outcomes (error, timeout, different collection, empty result, non-pytest command, worktree-add failure, renamed test) render explicitly as UNKNOWN/failure, not as pre-existing or as a partition of NEW vs pre-existing.
- The existing test asserting the helper is invoked twice per round is updated to assert it is invoked exactly once, and new tests verify the reviewer-facing prompt string contains the correct split.
- Test files defining the reviewed-failure rendering have observable assertions on the actual prompt text sent to the reviewer, not just on the `review()` kwargs.
- No existing test assertion is weakened from blocking (e.g., `assert_raises`, `assert_true`) to non-blocking (e.g., warning-only); the test and assertion counts per file do not decrease.
- Owned/modified test IDs remain in their true base-comparison bucket and are tagged inline in the rendered output; no second partition is created for them.

</details>

