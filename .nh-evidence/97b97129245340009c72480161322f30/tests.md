# Tests — the orchestrator's own run

_Harness-captured record for task `97b97129`, commit `ba97158c7aef0e0706443f81db54c2daa28026c3` — not model-authored: no_human wrote this file from the layered test run on the final tree. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
{
  "classified": true,
  "errors": 0,
  "failed": 2,
  "failing_tests": [
    "tests/test_gate_oneshot.py::test_check_credential_consults_the_reviewers_role_backend",
    "tests/test_wake_tick_does_not_stall_scheduler.py::test_twenty_parked_tasks_each_hanging_do_not_stall_the_tick_beyond_n_times_t"
  ],
  "failure_blocks": [
    "FAILED tests/test_gate_oneshot.py::test_check_credential_consults_the_reviewers_role_backend\nFAILED tests/test_wake_tick_does_not_stall_scheduler.py::test_twenty_parked_tasks_each_hanging_do_not_stall_the_tick_beyond_n_times_t",
    "\u2014\u2014\u2014 tests/test_gate_oneshot.py::test_check_credential_consults_the_reviewers_role_backend \u2014\u2014\u2014\n[gw3] darwin -- Python 3.12.13 /Users/eyalgolan/.no_human/worktrees/97b97129245340009c72480161322f30.6460.54291798/.venv/bin/python\n\ntmp_path = PosixPath('/private/var/folders/1r/3r0rt1jd4j1456rsg_fh4d380000gn/T/pytest-of-eyalgolan/pytest-2280/popen-gw3/test_check_credential_consults0')\nmonkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x116b0a660>\n\n    def test_check_credential_consults_the_reviewers_role_backend(tmp_path, monkeypatch):\n        \"\"\"Regression: `_check_credential` used to hardcode the claude-CLI/\n        subscription check regardless of the actually-configured reviewer\n        backend (\u00a76d). A reviewer pinned to `codex` must be checked via\n        `assert_task_backend_usable(\"codex\", ...)`, never the claude-only path \u2014\n        and must refuse (never silently pass as claude) when codex is\n        unavailable.\"\"\"\n        repo, _bare = _make_repo_with_origin(tmp_path)\n    \n        class _CodexConfig:\n            data = {\n                \"llm\": {\"role_backends\": {\n                    \"reviewer\": {\"backend\": \"codex\", \"model\": \"[REDACTED]-codex\"},\n                }},\n            }\n    \n            def get(self, key, default=None):\n                return self.data.get(key, default)\n    \n        monkeypatch.setattr(oneshot, \"load_config\", lambda **kw: _CodexConfig())\n        # Poison the claude-only path: if `_check_credential` still hardcodes\n        # it, this raises \"the\n\u2026 [truncated]"
  ],
  "failure_blocks_dropped": 0,
  "flaky_excused": [
    "tests/test_gate_oneshot.py::test_check_credential_consults_the_reviewers_role_backend",
    "tests/test_wake_tick_does_not_stall_scheduler.py::test_twenty_parked_tasks_each_hanging_do_not_stall_the_tick_beyond_n_times_t"
  ],
  "ok": false,
  "passed": 13464,
  "ran": true,
  "tamper_flag": false
}
```
