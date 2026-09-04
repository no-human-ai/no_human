# Verifiers

_Harness-captured record for task `8986ae39`, commit `f78a3fc782343667d9cae8cb8f53e8dbac0c43db` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All added test functions carry at least one assertion; the changes to test_egress_allowlist.py and test_structural_budget.py modify only data dictionaries, adding/modifying no test functions.",
    "evidence": "Every new test function in test_web_dist_staleness.py contains assert statements, e.g. test_fresh_web_dist_equal_mtime_does_not_flap_to_stale ends with `assert v.state == \"fresh\"`; the edits to the other two files only touch module-level dicts, not test functions.",
    "file": "tests/test_web_dist_staleness.py",
    "files_checked": [
      "tests/test_egress_allowlist.py",
      "tests/test_structural_budget.py",
      "tests/test_web_dist_staleness.py"
    ],
    "line": 253,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 691,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change is entirely about detecting/rebuilding a stale web/dist bundle and never touches task status at all, so the statement holds vacuously \u2014 no update_task(validate=False) writes are introduced.",
    "evidence": "The only added file is src/no_human/core/web_build.py, a board-freshness/rebuild utility; it contains no calls to update_task, set_status, or any task-status write (no 'validate', 'update_task', or 'set_status' tokens anywhere).",
    "file": "",
    "files_checked": [
      "src/no_human/core/web_build.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 297,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
