# Independent review

_Harness-captured record for task `9c59b93c`, commit `e07006c96db544a35fac52f0f981cb87f61468c8` — not model-authored: no_human wrote this file from the fresh-context reviewer's checklist on this commit. It records what the gate produced; it is not a verdict of the model that wrote the code._

<!-- no_human:review-checklist -->
## Independent review — PASSED (4 rounds) on `e07006c`
_A different model, fresh context, commit, push and merge refused at the tool call, told to refute "done". This is the checklist the gate decided on; no_human never merges — a human does._

| Severity | Finding | Where | Note |
|---|---|---|---|
| ✅ | fold probe ignores the command's own env PATH | `src/no_human/agent/venv_install_guard.py:943` | We already receive the command's env in denial_reason but only thread cwd into host_folds_case, so the PATH half of the union is the orchestrator's own PATH rat |
| ✅ | unbounded lru_cache keyed on cwd | `src/no_human/agent/exec_names.py:264` | maxsize=None here means one cached bool per distinct (cwd, path_env) forever. It's tiny so I wouldn't lose sleep, but a bounded maxsize would be tidier given cw |

<details><summary>2 advisory findings (low/nit — never blocking)</summary>

| Severity | Finding | Where | Note |
|---|---|---|---|
| ❌ low | maintainability: second fold authority in _effective_prefixes | `src/no_human/agent/venv_install_guard.py:1188` | This `.lower()` quietly becomes a second place that decides how installer-name spellings compare, separate from `_is_installer_name`, which folds only where the |
| ❌ low | silent-failure: os.path.exists swallows errors into a determinate permissive False | `src/no_human/agent/exec_names.py:171` | os.path.exists eats OSError and returns False, so this line can't tell a genuinely-absent swapped spelling apart from one it couldn't stat (permission-denied an |

</details>
