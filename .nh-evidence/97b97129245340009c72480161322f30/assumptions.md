# Assumptions

_Harness-captured record for task `97b97129`, commit `d21175b90504774c1a848473e257cfa7a370d160` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Which repository/project is this work for? Additionally, is the 'nh approve --landed' command existing infrastructure in that repository, or does it need to be created? **A:** HUMAN-GATED: not self-answerable
- **Q:** What specific directory and file patterns constitute 'user-visible surface' for this codebase? Please define the paths/patterns for each: shipped source code, desktop application, web board, and user-facing documentation. These definitions will be hardcoded in the detection logic and pinned by tests. **A:** Shipped source code: `src/**/*.ts` and `src/**/*.tsx` (excluding `src/**/*.test.ts` and test fixtures). Desktop application: `app/**/*` or `electron/**/*` (all non-test files). Web board: `web/**/*` or `client/**/*` (all non-test files). User-facing documentation: `docs/**/*.md`, `README.md`, `CHANGELOG.md` changes. Excluded patterns (do not flag): `**/*.test.ts`, `**/*.test.tsx`, `**/fixtures/**` _(assumption)_
- **Q:** Should the changelog-check command be implemented as: a standalone tool/script, a git subcommand (git changelog-check), or integrated into nh as a subcommand (nh changelog-check)? **A:** `nh changelog-check` — integrated as a subcommand into the existing `nh` CLI. The task already specifies that `nh approve --landed` will print a warning, establishing `nh` as the landing-time integration point; this check belongs there as a reportable subcommand that `nh approve` internally calls, rather than as a separate tool that duplicates the landing decision logic or requires its own CLI inv _(assumption)_

</details>

