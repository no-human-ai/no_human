# Assumptions

_Harness-captured record for task `beedca29`, commit `dc264a199aa8a85cea2bc9c6ff5ba69afcf2af11` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

> ⚠️ **Unresolved:** You've hit your weekly limit · resets 10am (Asia/Jerusalem) ('personal' subscription)

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Should the solution (whether a gate validating ~NNNN anchors or a convention forbidding them) apply only to Python source files in src/, or should it also address approximate line anchors in tests/, docs/, scripts/, examples/, or other directories? **A:** src/ only, specifically shipped Python source files. The problem is concentrated and most critical there (17 of ~20 anchors are in src/no_human/core/orchestrator.py alone), this is where churn and impact are highest, and it is the code shipped to users. Tests, docs, and scripts can follow separate policies if needed; focusing the solution on shipped source first ensures tight enforcement where it _(assumption)_
- **Q:** If approach (a) — a validating gate — is chosen, what tolerance window defines an anchor as acceptably correct? For example: ±5 lines (like the citation checker, but converted to fail instead of warn), ±3 lines, ±1 line, or exact match (±0)? **A:** ±5 lines. This follows the existing citation checker precedent mentioned in the task (which already tolerates ±5), is conservative enough to catch the 46–300 line errors observed in PR #313, and allows for minor edits near the referenced location without false positives. Tighter tolerance (±1 or ±0) would be fragile; wider tolerance defeats the purpose. _(assumption)_
- **Q:** If approach (b) — a convention forbidding new approximate line anchors — is chosen, what automatic mechanism enforces this: a pre-commit hook, a linter rule integrated into CI, a dedicated CI check, or another enforcement point? **A:** A dedicated pytest-based CI test that scans src/ files for the `~NNNN` pattern (excluding quantities with units), fails if any are found, and is run on every PR before merge. This follows the existing pattern of tests/test_readme_claims.py, is language-consistent, integrates naturally into CI, and prevents new violations from entering the repo automatically rather than relying on review attention. _(assumption)_

</details>

