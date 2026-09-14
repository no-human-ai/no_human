# Independent review

_Harness-captured record for task `9c59b93c`, commit `263ef53302306b7fd97d9080ef5bc2f91232bc69` — not model-authored: no_human wrote this file from the fresh-context reviewer's checklist on this commit. It records what the gate produced; it is not a verdict of the model that wrote the code._

<!-- no_human:review-checklist -->
## Independent review — PASSED (8 rounds) on `263ef53`
_A different model, fresh context, commit, push and merge refused at the tool call, told to refute "done". This is the checklist the gate decided on; no_human never merges — a human does._

| Severity | Finding | Where | Note |
|---|---|---|---|
| ✅ | unbounded fold-probe cache | `src/no_human/agent/exec_names.py:263` | Switching from maxsize=1 to maxsize=None means every distinct cwd we ever classify sticks around for the life of the process. It's bounded by how many worktrees |
| ✅ | forge/raw-text folding keys off process cwd, not command cwd | `src/no_human/agent/exec_names.py:124` | The forge and raw-text half folds against the process's own cwd/PATH rather than the command's cwd, so a mixed-volume host (process on a case-sensitive mount, c |

<details><summary>1 advisory finding (low/nit — never blocking)</summary>

| Severity | Finding | Where | Note |
|---|---|---|---|
| ❌ low | maintainability: fold decision now has two authorities | `src/no_human/agent/venv_install_guard.py:1209` | These bare `.lower()` comparisons here and in _uses_active_env fold case unconditionally, while the classifier they're supposed to mirror (_is_installer_name) f |

</details>
