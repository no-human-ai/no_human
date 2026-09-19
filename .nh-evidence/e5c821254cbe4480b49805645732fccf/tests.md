# Tests — the orchestrator's own run

_Harness-captured record for task `e5c82125`, commit `0bb0d934b7110c45f41709131f9e5a2273a62810` — not model-authored: no_human wrote this file from the layered test run on the final tree. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
{
  "classified": true,
  "errors": 0,
  "failed": 1,
  "failing_tests": [
    "tests/test_wake_tick_does_not_stall_scheduler.py::test_twenty_parked_tasks_each_hanging_do_not_stall_the_tick_beyond_n_times_t"
  ],
  "failure_blocks": [
    "FAILED tests/test_wake_tick_does_not_stall_scheduler.py::test_twenty_parked_tasks_each_hanging_do_not_stall_the_tick_beyond_n_times_t"
  ],
  "failure_blocks_dropped": 0,
  "flaky_excused": [
    "tests/test_wake_tick_does_not_stall_scheduler.py::test_twenty_parked_tasks_each_hanging_do_not_stall_the_tick_beyond_n_times_t"
  ],
  "ok": false,
  "passed": 13534,
  "ran": true,
  "tamper_flag": false
}
```
