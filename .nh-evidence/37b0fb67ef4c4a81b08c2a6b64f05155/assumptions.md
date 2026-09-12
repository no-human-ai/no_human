# Assumptions

_Harness-captured record for task `37b0fb67`, commit `b4cddeb0cc2345fb6540bb3979de8d992da1120b` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

> ⚠️ **Unresolved:** PR feedback revised 3 time(s), exceeding max_revision_rounds=2; escalating so a human can decide rather than revising indefinitely.

<details><summary>⚠️ 1 assumption made on your behalf — verify at review</summary>

- **Q:** Which repository/module path contains guard.evaluate, venv_install_guard._venv_root_of, and guard._protected_venvs (e.g. is this the same repo as PR #236)? **A:** Same repo, single module tree: src/no_human/agent/venv_install_guard.py (contains _venv_root_of and denial_reason) and src/no_human/agent/guard.py (contains evaluate and _protected_venvs), with corresponding tests in tests/test_venv_install_guard.py and tests/test_guard.py. _(assumption)_

</details>

