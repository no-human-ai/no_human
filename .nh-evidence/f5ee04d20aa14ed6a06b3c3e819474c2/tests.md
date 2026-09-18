# Tests — the orchestrator's own run

_Harness-captured record for task `f5ee04d2`, commit `935bf3979ef8af3d2794f04056d5e093a19db4fa` — not model-authored: no_human wrote this file from the layered test run on the final tree. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
{
  "classified": true,
  "errors": 0,
  "failed": 1,
  "failing_tests": [
    "tests/test_gate_oneshot.py::test_check_credential_consults_the_reviewers_role_backend"
  ],
  "failure_blocks": [
    "FAILED tests/test_gate_oneshot.py::test_check_credential_consults_the_reviewers_role_backend",
    "\u2014\u2014\u2014 tests/test_gate_oneshot.py::test_check_credential_consults_the_reviewers_role_backend \u2014\u2014\u2014\n[gw3] darwin -- Python 3.12.13 /Users/eyalgolan/.no_human/worktrees/f5ee04d20aa14ed6a06b3c3e819474c2.6460.92ca8cc9/.venv/bin/python\n\ntmp_path = PosixPath('/private/var/folders/1r/3r0rt1jd4j1456rsg_fh4d380000gn/T/pytest-of-eyalgolan/pytest-4162/popen-gw3/test_check_credential_consults0')\nmonkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x113a62120>\n\n    def test_check_credential_consults_the_reviewers_role_backend(tmp_path, monkeypatch):\n        \"\"\"Regression: `_check_credential` used to hardcode the claude-CLI/\n        subscription check regardless of the actually-configured reviewer\n        backend (\u00a76d). A reviewer pinned to `codex` must be checked via\n        `assert_task_backend_usable(\"codex\", ...)`, never the claude-only path \u2014\n        and must refuse (never silently pass as claude) when codex is\n        unavailable.\"\"\"\n        repo, _bare = _make_repo_with_origin(tmp_path)\n    \n        class _CodexConfig:\n            data = {\n                \"llm\": {\"role_backends\": {\n                    \"reviewer\": {\"backend\": \"codex\", \"model\": \"[REDACTED]-codex\"},\n                }},\n            }\n    \n            def get(self, key, default=None):\n                return self.data.get(key, default)\n    \n        monkeypatch.setattr(oneshot, \"load_config\", lambda **kw: _CodexConfig())\n        # Poison the claude-only path: if `_check_credential` still hardcodes\n        # it, this raises \"the\n\u2026 [truncated]"
  ],
  "failure_blocks_dropped": 0,
  "flaky_excused": [
    "tests/test_gate_oneshot.py::test_check_credential_consults_the_reviewers_role_backend"
  ],
  "ok": false,
  "passed": 13476,
  "ran": true,
  "tamper_flag": false
}
```
