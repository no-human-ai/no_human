# Independent review

_Harness-captured record for task `7212a755`, commit `d23384dfc8e712e45a8b96cd30077431a3e0a695` — not model-authored: no_human wrote this file from the fresh-context reviewer's checklist on this commit. It records what the gate produced; it is not a verdict of the model that wrote the code._

<!-- no_human:review-checklist -->
## Independent review — PASSED (1 round) on `d23384d`
_A different model, fresh context, commit, push and merge refused at the tool call, told to refute "done". This is the checklist the gate decided on; no_human never merges — a human does._

| Severity | Finding | Where | Note |
|---|---|---|---|
| ✅ | Redundant restart indicators | `web/src/ModelsPanel.jsx:448` | We now show both the global restart banner and a per-row 'Saved: … until restart' hint for the same underlying state. Not wrong, but it's a lot of repeated mess |
| ✅ | Select value keyed off fresh disk read | `web/src/ModelsPanel.jsx:352` | Now that the dropdown value comes from row.saved (a raw disk read) rather than the running config, an out-of-catalog id sitting in config.yaml will leave the se |
