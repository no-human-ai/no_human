# Verifiers

_Harness-captured record for task `0b9a029c`, commit `af5cbadaa8b40beee5a05d59fda84e43238fcb41` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "",
    "evidence": "Every added/modified test carries assertions; e.g. new test_plan_gate_task_with_no_attempts_reports_its_planner_spend has 'assert out.cost_usd is not None', 'assert out.attempt_count == 0', and modified test_metrics_cost_usd_total_equals_the_sum_of_task_costs has 'assert m[\"cost_usd_total\"] == pytest.approx(...)'.",
    "file": "tests/test_task_cost_includes_ledger.py",
    "files_checked": [
      "tests/test_api.py",
      "tests/test_cli_commands.py",
      "tests/test_readme_claims.py",
      "tests/test_structural_budget.py",
      "tests/test_task_cost_includes_ledger.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1084,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "No new or modified code performs any task-status write; the changes are purely SELECT-based cost aggregation and reshaping, so there is no update_task(validate=False) status write and nothing bypasses set_status.",
    "evidence": "The diff only adds/modifies cost/ledger read paths: cost.ledger_rows_as_attempts, db.usage_ledger_rows_by_task / task_usage_ledger_rows / OWNED_LEDGER_SQL / unattributed_usage_totals(owned=), and metrics.compute_metrics \u2014 none call update_task or write a task status at all.",
    "file": "",
    "files_checked": [
      "src/no_human/core/cost.py",
      "src/no_human/core/db.py",
      "src/no_human/core/metrics.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 470,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
