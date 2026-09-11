# Verifiers

_Harness-captured record for task `5c49b2b7`, commit `1c4c00297cafd6e283635aef209fc03ba296d3b8` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All 21 newly added test_* functions in test_venv_install_guard.py include at least one assert statement; the changes to test_structural_budget.py and test_readme_claims.py only edited data tables, not test functions. Non-test helpers like _RootCapturingBackend.run are not test functions and are exempt from the statement.",
    "evidence": "Every added test function contains asserts, e.g. test_orchestrator_threads_its_own_worktree_root_as_session_root: assert backend.session_roots, \"the fake backend's run() was never called\"",
    "file": "tests/test_venv_install_guard.py",
    "files_checked": [
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_venv_install_guard.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1196,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "None of the added or modified lines write a task status at all \u2014 they thread a `session_root` argument into backend.run calls \u2014 so there is no `update_task(..., validate=False)` status write to violate the statement.",
    "evidence": "The entire diff only adds `session_root=self._session_root_for(repo)` to existing `self.backend.run(...)` calls and introduces the new `_session_root_for` method, which returns `str(repo.path) if self._worktree_isolation_enabled() else None`. No hunk touches task status, `update_task`, or `set_status`.",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 452,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
