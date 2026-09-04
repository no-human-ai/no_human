# Assumptions

_Harness-captured record for task `8ddc0d1a`, commit `c73e30c1ad92ce479590df0d393b70bb895c564a` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** The example message format is shown as '.git/common/config (non-benign keys: <k1>, <k2>)' — should this be the exact output format with that punctuation and wording, or are variations acceptable? **A:** The exact output format with that punctuation and wording should be used for consistency and testability — the test assertions require a deterministic format, and the spec example is the contract. _(assumption)_
- **Q:** When the non-benign key list exceeds _MAX_PATHS_PER_BUCKET, what exact format should the truncated message use? Should it follow the same pattern as the existing path-list truncation in the codebase? **A:** Follow the existing path-list truncation pattern in the codebase using _MAX_PATHS_PER_BUCKET; the truncated message should reuse the same logic (e.g., list up to the cap, then append '(+N more)' or similar) to maintain consistency with how other bounded lists are surfaced in verdict text. _(assumption)_
- **Q:** Is there exactly one compare() function in src/no_human/core/reviewer_worktree.py, or are there multiple variants/overloads that each need updating? **A:** There is one primary compare() function in the reviewer_worktree module that finalized the config check and needs updating; the 'or wherever' phrasing in the spec reflects uncertainty about exact location, not multiple compare() variants. _(assumption)_
- **Q:** Can you confirm the target repository (URL or path) and whether the implementation team has write and commit access for this codebase? **A:** HUMAN-GATED: not self-answerable

</details>

