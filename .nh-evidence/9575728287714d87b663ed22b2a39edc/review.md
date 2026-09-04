# Independent review

_Harness-captured record for task `95757282`, commit `6ad824741950a0b77cfe72b56e87c19ec1e77aa7` — not model-authored: no_human wrote this file from the fresh-context reviewer's checklist on this commit. It records what the gate produced; it is not a verdict of the model that wrote the code._

<!-- no_human:review-checklist -->
## Independent review — PASSED (1 round) on `6ad8247`
_A different model, fresh context, commit, push and merge refused at the tool call, told to refute "done". This is the checklist the gate decided on; no_human never merges — a human does._

| Severity | Finding | Where | Note |
|---|---|---|---|
| ✅ | base_pin re-read every attempt, never read back from the stored row | `src/no_human/core/orchestrator.py:4786` | One thing worth being explicit about: base_pin is re-resolved from the remote at the top of every _run_attempt, and the base_pin_sha column you persist is never |
| ✅ | unfetched pin falls open to checking base's own history | `src/no_human/core/orchestrator.py:3200` | The cat-file -e fail-closed branch here treats a pin whose object isn't local as "exclude nothing." That's the safe direction for missing an exclusion, but note |
