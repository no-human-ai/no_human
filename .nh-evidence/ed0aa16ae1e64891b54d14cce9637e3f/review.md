# Independent review

_Harness-captured record for task `ed0aa16a`, commit `36a382d1d755c87b66205f45ca1b57a19ebc8289` — not model-authored: no_human wrote this file from the fresh-context reviewer's checklist on this commit. It records what the gate produced; it is not a verdict of the model that wrote the code._

<!-- no_human:review-checklist -->
## Independent review — PASSED (4 rounds) on `36a382d`
_A different model, fresh context, commit, push and merge refused at the tool call, told to refute "done". This is the checklist the gate decided on; no_human never merges — a human does._

| Severity | Finding | Where | Note |
|---|---|---|---|
| ✅ | merge_ready_for withholding is advisory-only | `src/no_human/api/models.py:218` | Worth being honest that this branch doesn't actually gate anything — the docstring right above says nobody reads merge_ready to merge, so flipping a stale ready |
| ✅ | hardcoded 'stale' literal instead of delivered_base.STALE | `src/no_human/api/models.py:218` | Same nit as before — compare against delivered_base.STALE instead of the bare string so the vocabulary has a single owner. A rename of that constant would leave |

<details><summary>2 advisory findings (low/nit — never blocking)</summary>

| Severity | Finding | Where | Note |
|---|---|---|---|
| ❌ low | maintainability: stale comment claims single-reader locality | `src/no_human/cli/commands.py:1762` | This comment says pr_base_freshness is read 'only inside that rung itself', but merge_ready_for in api/models.py reads the same key's 'state' field to withhold |
| ❌ low | maintainability: tri-state info sentinel forks the poll contract | `src/no_human/blockers/wake.py:2044` | The three-way meaning of info here (_INFO_UNSET vs None vs dict) is subtle — None specifically means 'shared poll failed, don't re-poll', which is easy for a fu |

</details>
