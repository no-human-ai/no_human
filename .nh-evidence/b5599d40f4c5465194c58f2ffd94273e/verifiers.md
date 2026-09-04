# Verifiers

_Harness-captured record for task `b5599d40`, commit `9e1923867f2b61b1afee8023bc7d891a372394a0` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Every added test function in the new file contains at least one assert statement or pytest.raises block; the test_structural_budget.py change only edits frozen-value dict entries, adding/modifying no test function bodies.",
    "evidence": "test_split_drafts_is_refused_in_setup_mode: `assert r.status_code == 503` and `assert \"CLAUDE_CODE_OAUTH_TOKEN\" in r.json()[\"detail\"]`; test_scrub_still_runs_on_the_missing_credential_path uses `with pytest.raises(MissingCredentialError, ...)`",
    "file": "tests/test_setup_mode_boot.py",
    "files_checked": [
      "tests/test_setup_mode_boot.py",
      "tests/test_structural_budget.py"
    ],
    "line": 179,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 713,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The new code only adds a per-tick auth check that idles dispatch (returns []) and emits events \u2014 it never writes a task status, let alone via update_task(validate=False), so the statement is trivially satisfied by this change.",
    "evidence": "The diff's only additions are an auth-probe gate (self._auth_check()) that on AuthError logs a warning, emits a 'setup_required' event, and `return []` to idle; there are no update_task(...) calls and no task-status writes anywhere in the added or modified lines.",
    "file": "src/no_human/core/scheduler.py",
    "files_checked": [
      "src/no_human/core/scheduler.py"
    ],
    "line": 2093,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 355,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
