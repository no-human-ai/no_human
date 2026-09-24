# Independent review

_Harness-captured record for task `ab8f8358`, commit `5269b68f3438797e013a4e852b0e1bd57f5dc3ef` — not model-authored: no_human wrote this file from the fresh-context reviewer's checklist on this commit. It records what the gate produced; it is not a verdict of the model that wrote the code._

<!-- no_human:review-checklist -->
## Independent review — PASSED (2 rounds) on `5269b68`
_A different model, fresh context, commit, push and merge refused at the tool call, told to refute "done". This is the checklist the gate decided on; no_human never merges — a human does._

| Severity | Finding | Where | Note |
|---|---|---|---|
| ✅ | partial_success loses the auto-reconcile-to-DONE the crash previously had as FAILED | `src/no_human/core/task.py:278` | Worth being explicit that a salvaged task is now strictly less self-healing than the plain FAILED it replaces: the periodic sweep only reconciles FAILED rows, s |
| ✅ | salvage doesn't check pr_url, so its 'before a PR was opened' reason can be false | `src/no_human/core/scheduler.py:1399` | The docstring says partial_success exists only for a crash before any PR opened, but nothing here checks the attempt's pr_url — you're inferring pre-PR purely f |
| ✅ | PR-body criteria (3 status rationale, 4 visibility statement, 6 suite summary) have no artifact | — | Flagging for the human that the PR-body requirements in criteria 3, 4 and 6 have nowhere to live since the PR never opened — that's an environment gap, not some |
| ✅ | maintainability angle did not run (reached no verdict) | — | advisory — the extra angle pass was skipped; the main review still gates |
| ✅ | silent-failure angle did not run (reached no verdict) | — | advisory — the extra angle pass was skipped; the main review still gates |
