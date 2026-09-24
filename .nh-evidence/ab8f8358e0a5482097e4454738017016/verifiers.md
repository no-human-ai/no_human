# Verifiers

_Harness-captured record for task `ab8f8358`, commit `5269b68f3438797e013a4e852b0e1bd57f5dc3ef` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All six test functions added in this change (all in the new test_scheduler_crash_salvage.py) contain multiple assert statements; test_structural_budget.py only altered frozen data dicts, adding/modifying no test functions.",
    "evidence": "Every new test function contains assert statements, e.g. test_a_crash_after_the_commit_records_the_branch_and_sha: `assert rec, ...`, `assert branch.startswith(\"no-human/\")`, `assert out.salvaged_branch == branch`",
    "file": "tests/test_scheduler_crash_salvage.py",
    "files_checked": [
      "tests/test_scheduler_crash_salvage.py",
      "tests/test_structural_budget.py"
    ],
    "line": 168,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 545,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "All new status writes (FAILED/PARTIAL_SUCCESS from the pool-crash handler) go through set_status, and no modified code writes status via update_task; the statement is about update_task specifically, which is not used here.",
    "evidence": "The only new status writes in the crash handler use set_status: `await self.store.set_status(task, _TS.PARTIAL_SUCCESS, validate=False)` and `await self.store.set_status(task, _TS.FAILED, validate=False)`; no new/modified code calls update_task to write a status.",
    "file": "src/no_human/core/scheduler.py",
    "files_checked": [
      "src/no_human/blockers/landed_override.py",
      "src/no_human/core/lanes.py",
      "src/no_human/core/scheduler.py",
      "src/no_human/core/task.py"
    ],
    "line": 2624,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 675,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  },
  {
    "comment": "The change is pure routing/predicate logic; no new or modified color values are introduced, so every color still comes from existing CSS variables and light/dark rendering is unaffected.",
    "evidence": "The only color-bearing lines in the diff reuse existing tokens, e.g. failed lane keeps accent: \"var(--c-escalated)\" while merely adding \"partial_success\" to its statuses; no hex/rgb/hsl literal is added anywhere in the diff.",
    "file": "web/src/boardLanes.js",
    "files_checked": [
      "web/src/boardGroups.js",
      "web/src/boardGroups.test.mjs",
      "web/src/boardLanes.js",
      "web/src/boardLanes.test.mjs",
      "web/src/slideOverSummary.js",
      "web/src/slideOverSummary.test.mjs"
    ],
    "line": 33,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 424,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
