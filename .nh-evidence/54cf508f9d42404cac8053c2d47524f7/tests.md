# Tests — the orchestrator's own run

_Harness-captured record for task `54cf508f`, commit `3fa913f051995aba3223cfa0f30947f96e5b8c86` — not model-authored: no_human wrote this file from the layered test run on the final tree. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
{
  "classified": true,
  "errors": 0,
  "failed": 1,
  "failing_tests": [
    "tests/test_wake_tick_does_not_stall_scheduler.py::test_the_same_bound_holds_at_a_scaled_down_timeout"
  ],
  "failure_blocks": [
    "FAILED tests/test_wake_tick_does_not_stall_scheduler.py::test_the_same_bound_holds_at_a_scaled_down_timeout",
    "\u2014\u2014\u2014 tests/test_wake_tick_does_not_stall_scheduler.py::test_the_same_bound_holds_at_a_scaled_down_timeout \u2014\u2014\u2014\n[gw3] darwin -- Python 3.12.13 /Users/eyalgolan/.no_human/worktrees/54cf508f9d42404cac8053c2d47524f7.56167.d9c3edd7/.venv/bin/python3\n\nstore = <no_human.core.db.Store object at 0x113a3f0e0>\nhang_cli = [['gh', 'pr', 'view', '19', '--repo', 'code.example.com/dev/x', ...], ['gh', 'pr', 'view', '18', '--repo', 'code.examp...5', '--repo', 'code.example.com/dev/x', ...], ['gh', 'pr', 'view', '14', '--repo', 'code.example.com/dev/x', ...], ...]\nmonkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x113a3e9c0>\n\n    async def test_the_same_bound_holds_at_a_scaled_down_timeout(store, hang_cli, monkeypatch):\n        \"\"\"Same shape as the `slow` test above (N=20, sequential, one hang per\n        task) but at a millisecond-scale `_CLI_TIMEOUT` so the default (non-slow)\n        test run still exercises the exact mechanism AC3 asks for, fast.\"\"\"\n>       await _run_the_bound_check(\n            store, hang_cli, monkeypatch, n=20, per_call_timeout=0.05, outer_bound=2.0)\n\ntests/test_wake_tick_does_not_stall_scheduler.py:138: \n_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \n\nstore = <no_human.core.db.Store object at 0x113a3f0e0>\nhang_cli = [['gh', 'pr', 'view', '19', '--repo', 'code.example.com/dev/x', ...], ['gh', 'pr', 'view', '18', '--repo', 'code.examp...5', '--repo', 'code.example.com/dev/x', ...], ['gh', 'pr', 'view', '14', '--repo', 'code.example\n\u2026 [truncated]"
  ],
  "failure_blocks_dropped": 0,
  "ok": false,
  "passed": 13551,
  "pre_existing_failures": [
    "tests/test_wake_tick_does_not_stall_scheduler.py::test_the_same_bound_holds_at_a_scaled_down_timeout"
  ],
  "ran": true,
  "tamper_flag": false
}
```
