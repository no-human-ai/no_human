# Verifiers

_Harness-captured record for task `c1a0416d`, commit `5779f4d12cbc4f3d7b385ecf4adae2b5bc347dbb` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All seven added test functions (and the parametrized one) each contain at least one assert; the diff to test_structural_budget.py only touches module-level dict values/comments, adding no assertion-free test functions.",
    "evidence": "Every added test function contains asserts, e.g. `assert reviewer.calls`, `assert not eligible, (by, why)`, `assert eligible is expect_eligible`",
    "file": "tests/test_wip_checkpoint_routed_to_review.py",
    "files_checked": [
      "tests/test_structural_budget.py",
      "tests/test_wip_checkpoint_routed_to_review.py"
    ],
    "line": 107,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 658,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code performs a task status write at all, so there is no `update_task(validate=False)` bypassing `set_status`; the statement holds vacuously for this change.",
    "evidence": "The diff modifies only review-routing logic (`_route_unjudged_head`, `_head_is_blocked_checkpoint`, `_already_satisfied_eligible`, and the hoisted call in `_run_attempt`); it calls `repo.head_commit(base)` and `self._emit_review(...)` but contains no `update_task(...)` call and no `validate=False` argument anywhere.",
    "file": "",
    "files_checked": [
      "src/no_human/blockers/taxonomy.py",
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 656,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
