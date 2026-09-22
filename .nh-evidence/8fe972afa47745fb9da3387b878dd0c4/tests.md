# Tests — the orchestrator's own run

_Harness-captured record for task `8fe972af`, commit `ddb91594053f28fa6aef162631355d1dcb2ef03c` — not model-authored: no_human wrote this file from the layered test run on the final tree. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
{
  "classified": true,
  "errors": 0,
  "failed": 1,
  "failing_tests": [
    "tests/test_funnel_eval.py::test_a_wedged_holdout_is_killed_by_process_group"
  ],
  "failure_blocks": [
    "FAILED tests/test_funnel_eval.py::test_a_wedged_holdout_is_killed_by_process_group",
    "\u2014\u2014\u2014 tests/test_funnel_eval.py::test_a_wedged_holdout_is_killed_by_process_group \u2014\u2014\u2014\n[gw3] darwin -- Python 3.12.13 /Users/eyalgolan/.no_human/worktrees/8fe972afa47745fb9da3387b878dd0c4.56167.f2c74ac7/.venv/bin/python3\n\ntmp_path = PosixPath('/private/var/folders/1r/3r0rt1jd4j1456rsg_fh4d380000gn/T/pytest-of-eyalgolan/pytest-7624/popen-gw3/test_a_wedged_holdout_is_kille0')\nmonkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x111f88c20>\n\n    def test_a_wedged_holdout_is_killed_by_process_group(tmp_path, monkeypatch):\n        \"\"\"`_holdout_ok`'s timeout path had no test. A plain `proc.kill()` reaps\n        the shell and leaves the real work running; the marker file below is\n        written by a GRANDCHILD, so it only stays absent if the whole group died.\"\"\"\n        import dataclasses\n        import time\n    \n        from no_human.eval import funnel_eval as fe\n    \n        marker = tmp_path / \"grandchild-survived\"\n        # The grandchild touches the marker at +3s and the kill lands at +1s, so\n        # checking at ~+5s is a REAL test of the group kill: a surviving grandchild\n        # has written the file by then. The first version slept 30s before\n        # touching, which made `not marker.exists()` true whether the group died or\n        # not \u2014 the timing bound below was doing all the work and the marker was\n        # decoration. Both are asserted now, and they catch different failures: the\n        # bound catches \"never killed at all\", the marker catches \"only the direct\n\n\u2026 [truncated]"
  ],
  "failure_blocks_dropped": 0,
  "flaky_excused": [
    "tests/test_funnel_eval.py::test_a_wedged_holdout_is_killed_by_process_group"
  ],
  "ok": false,
  "passed": 13555,
  "ran": true,
  "tamper_flag": false
}
```
