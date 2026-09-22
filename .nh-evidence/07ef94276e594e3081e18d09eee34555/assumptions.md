# Assumptions

_Harness-captured record for task `07ef9427`, commit `71eed0828a6d190f52fae30df2d08c9d0d14923b` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Which of the three proposed approaches should be implemented? A) Bound override diffs with budget_diff and emit a DISCLOSURE ledger (truncation warning to reviewer, no inspection requirement) so the verdict proceeds but the reviewer is informed; B) Provide repository access to the override path where it exists (oneshot clones the PR) and apply the full mechanism from PR #437 including the inspecti **A:** A) Bound override diffs with budget_diff and emit a DISCLOSURE-only ledger (truncation warning to reviewer, no inspection requirement). The description establishes that the override path lacks repository access in cases like _fast_review (single-turn, no tools), making the inspection-requirement guard from PR #437 unenforceable there. A DISCLOSURE ledger informs the reviewer that truncation occurr _(assumption)_
- **Q:** If approach B is chosen: Do all three diff_override call sites (review/oneshot.py and the two in core/orchestrator.py) have reliable access to a repository checkout for reading files? **A:** No. The description indicates oneshot.py clones the PR checkout (supporting approach B there), but core/orchestrator.py's two diff_override call sites are not similarly documented as having reliable repository access. A differentiated implementation would be needed: approach B where repos are provably available (oneshot.py), approach A elsewhere (orchestrator.py call sites). _(assumption)_
- **Q:** If approach C is chosen: What measurement scope and evidence criteria would constitute sufficient proof that override diffs cannot exceed _DIFF_CAP in practice? (e.g., how many recent PRs to analyze, what time period, what statistical threshold like '100% of observed diffs stay under cap') **A:** HUMAN-GATED: not self-answerable

</details>

