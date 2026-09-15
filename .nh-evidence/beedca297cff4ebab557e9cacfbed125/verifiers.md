# Verifiers

_Harness-captured record for task `beedca29`, commit `880c91ddd9fc041cb75f6d395929571fc32d0e4b` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All twelve test functions added in test_no_approximate_line_anchors.py contain one or more assert statements; the only edit to test_structural_budget.py changes a frozen dict value, not a test body. No assertion-free test exists.",
    "evidence": "Every added test function contains at least one assert, e.g. test_no_source_comment_carries_an_approximate_line_anchor: `assert offenders == []`, test_the_gate_runs_on_the_pr_lane: `assert names == {\"repoguard\"}`",
    "file": "",
    "files_checked": [
      "tests/test_no_approximate_line_anchors.py",
      "tests/test_structural_budget.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 732,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "",
    "evidence": "no verdict after retry: verifier 'no-unvalidated-status-write' (defined in /Users/eyalgolan/.no_human/verifiers.yaml) never produced a parseable verdict \u2014 no verdict: no VERIFIER_JSON_START marker found",
    "file": "",
    "files_checked": [
      "src/no_human/core/orchestrator.py"
    ],
    "line": 0,
    "no_verdict": true,
    "passed": false,
    "severity": "high",
    "tokens_used": 1188,
    "unavailable": true,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
