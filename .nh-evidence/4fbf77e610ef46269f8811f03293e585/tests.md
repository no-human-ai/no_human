# Tests — the orchestrator's own run

_Harness-captured record for task `4fbf77e6`, commit `9b8c64cddc990f62652e91d98cadf61f0e476040` — not model-authored: no_human wrote this file from the layered test run on the final tree. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
{
  "classified": true,
  "errors": 0,
  "failed": 2,
  "failing_tests": [
    "tests/test_lane_conformance.py::test_the_js_implementation_agrees_on_every_shared_case",
    "tests/test_wake_tick_does_not_stall_scheduler.py::test_the_same_bound_holds_at_a_scaled_down_timeout"
  ],
  "failure_blocks": [
    "FAILED tests/test_lane_conformance.py::test_the_js_implementation_agrees_on_every_shared_case\nFAILED tests/test_wake_tick_does_not_stall_scheduler.py::test_the_same_bound_holds_at_a_scaled_down_timeout",
    "\u2014\u2014\u2014 tests/test_lane_conformance.py::test_the_js_implementation_agrees_on_every_shared_case \u2014\u2014\u2014\n[gw1] darwin -- Python 3.12.13 /Users/eyalgolan/.no_human/worktrees/4fbf77e610ef46269f8811f03293e585.50750.12620f3a/.venv/bin/python3\n\n    def test_the_js_implementation_agrees_on_every_shared_case():\n        \"\"\"Run ``web/src/laneConformance.test.mjs`` and require it to pass.\n    \n        The fixture alone only guarantees agreement when BOTH runners run. Under\n        ``.no_human.yml`` a src/-only change runs the Python suite and nothing else,\n        so without this the Python half could be edited into disagreement with the\n        JS and stay green (demonstrated: moving ``compound_parent`` to the answer\n        lane in ``lanes.py`` + the fixture left pytest at 75 passed while node\n        failed 4).\n    \n        Node absence FAILS rather than skips. A skip would move the same hole to a\n        new place - a machine without node would silently reacquire the one-sided\n        guarantee, and that is exactly the failure mode being fixed here. Failing is\n        safe because node is already a hard requirement of this repo's test story:\n        ``web/package.json`` runs ``node --test src/``, ``.no_human.yml`` shells\n        ``node --test`` for every web-scoped change, and ``web/dist`` is built with\n        it. This particular file also needs NO ``node_modules`` - it imports only\n        ``node:`` builtins and ``./boardLanes.js``, which itself imports nothing -\n        so it runs in a ba\n\u2026 [truncated]",
    "\u2014\u2014\u2014 tests/test_wake_tick_does_not_stall_scheduler.py::test_the_same_bound_holds_at_a_scaled_down_timeout \u2014\u2014\u2014\n[gw1] darwin -- Python 3.12.13 /Users/eyalgolan/.no_human/worktrees/4fbf77e610ef46269f8811f03293e585.50750.12620f3a/.venv/bin/python3\n\nstore = <no_human.core.db.Store object at 0x1131ac080>\nhang_cli = [['gh', 'pr', 'view', '19', '--repo', 'code.example.com/dev/x', ...], ['gh', 'pr', 'view', '18', '--repo', 'code.examp...5', '--repo', 'code.example.com/dev/x', ...], ['gh', 'pr', 'view', '14', '--repo', 'code.example.com/dev/x', ...], ...]\nmonkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x1131ac1d0>\n\n    async def test_the_same_bound_holds_at_a_scaled_down_timeout(store, hang_cli, monkeypatch):\n        \"\"\"Same shape as the `slow` test above (N=20, sequential, one hang per\n        task) but at a millisecond-scale `_CLI_TIMEOUT` so the default (non-slow)\n        test run still exercises the exact mechanism AC3 asks for, fast.\"\"\"\n>       await _run_the_bound_check(\n            store, hang_cli, monkeypatch, n=20, per_call_timeout=0.05, outer_bound=2.0)\n\ntests/test_wake_tick_does_not_stall_scheduler.py:138: \n_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \n\nstore = <no_human.core.db.Store object at 0x1131ac080>\nhang_cli = [['gh', 'pr', 'view', '19', '--repo', 'code.example.com/dev/x', ...], ['gh', 'pr', 'view', '18', '--repo', 'code.examp...5', '--repo', 'code.example.com/dev/x', ...], ['gh', 'pr', 'view', '14', '--repo', 'code.example\n\u2026 [truncated]"
  ],
  "failure_blocks_dropped": 0,
  "ok": false,
  "passed": 13573,
  "pre_existing_failures": [
    "tests/test_lane_conformance.py::test_the_js_implementation_agrees_on_every_shared_case",
    "tests/test_wake_tick_does_not_stall_scheduler.py::test_the_same_bound_holds_at_a_scaled_down_timeout"
  ],
  "ran": true,
  "tamper_flag": false
}
```
