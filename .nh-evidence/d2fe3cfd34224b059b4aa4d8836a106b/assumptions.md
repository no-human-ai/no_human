# Assumptions

_Harness-captured record for task `d2fe3cfd`, commit `a5efb2ceae1e4bd92e2b9e8c2c19c5e6cc63809d` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 1 assumption made on your behalf — verify at review</summary>

- **Q:** In _ahead_reviewed_candidate (~:10533-10539), after the preference for HEAD's sha fails, the code should prefer 'the newest round's sha' from review_history. How is 'newest round' determined (by list index, timestamp, or another field)? And when two stamped candidates remain after that preference—neither being HEAD—what makes them 'unrelated' enough to trigger ReviewedShaMismatch instead of pickin **A:** Newest round is determined by list index: review_history accumulates over the task lifetime, so the last entry in the list is the newest round's sha. Two stamped candidates are unrelated when neither is an ancestor of the other in the git DAG—they diverged at some historical commit, meaning neither can reach the other. When such unrelated candidates both have stamps and neither equals HEAD's sha, _(assumption)_

</details>

