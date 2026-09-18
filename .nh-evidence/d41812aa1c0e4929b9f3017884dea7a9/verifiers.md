# Verifiers

_Harness-captured record for task `d41812aa`, commit `bdb5a1d4e1aaa13ad03166a16c3e026d7be92ff2` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Every added/modified test function either has a direct assert (e.g. the guard quadratic test's `assert ratio < 3.0`, the slow scheduler test's `assert elapsed <= 22.0`) or calls an assertion helper (`_run_the_bound_check`, or the imported target test functions), so none is assertion-free.",
    "evidence": "test_the_same_bound_holds_at_a_scaled_down_timeout calls the assertion helper `_run_the_bound_check(...)` which contains `assert started == []`, `assert proxy.waits == ...`; test_pr497 tests call `_target_scheduler_test`/`_target_guard_test()` which assert; guard test has `assert ratio < 3.0`",
    "file": "tests/test_wake_tick_does_not_stall_scheduler.py",
    "files_checked": [
      "tests/test_guard.py",
      "tests/test_pr497_timing_gates_are_load_independent.py",
      "tests/test_wake_tick_does_not_stall_scheduler.py"
    ],
    "line": 156,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 840,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
