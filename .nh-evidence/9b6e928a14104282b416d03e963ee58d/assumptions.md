# Assumptions

_Harness-captured record for task `9b6e928a`, commit `5f5d623b8cf5fb704023961f912efd08325e5b57` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** The task notes that PRs become 'stale' when trunk moves beyond them, but doesn't specify the algorithm. Is a PR considered stale when: (a) trunk has advanced at least one commit since delivery? (b) a specific duration has elapsed (e.g., N seconds, related to your median landing cadence of ~1507s)? (c) a specific number of commits have landed on trunk since delivery? (d) something else? **A:** (a) trunk has advanced at least one commit since delivery _(assumption)_
- **Q:** When should stale-but-mergeable PRs be re-measured? Should this happen via: (a) a background poller running on a schedule? (b) an event/hook triggered when trunk advances (if so, what event source)? (c) on-demand checks when a coder inspects a task? (d) something else? **A:** (b) an event/hook triggered when trunk advances (e.g., via commit notifications or landing events in the CI system) _(assumption)_
- **Q:** The acceptance criteria require 'landing something on trunk and observing the previously-mergeable task's recorded state change.' For this test/demo, do we need: (a) a real repository with real CI (requiring credential access)? (b) a mock/local test environment? (c) both (mocks for unit tests, real system for acceptance tests)? **A:** HUMAN-GATED: not self-answerable
- **Q:** When a stale-but-mergeable PR is re-measured, which state should be updated: (a) only base_sha (to the current trunk commit)? (b) base_sha plus re-verified mergeability? (c) base_sha, mergeability, and other task state? (d) something else? **A:** (b) base_sha plus re-verified mergeability _(assumption)_

</details>

