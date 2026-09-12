# Tests — the orchestrator's own run

_Harness-captured record for task `19dd94b3`, commit `71c7204ad1dbcd59b1700b8f3e528a5b4705c716` — not model-authored: no_human wrote this file from the layered test run on the final tree. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
{
  "classified": true,
  "errors": 0,
  "failed": 1,
  "failing_tests": [
    "tests/test_vcs_git_ls_remote_exact.py::test_latency_against_a_local_bare_origin_is_under_100ms"
  ],
  "failure_blocks": [
    "FAILED tests/test_vcs_git_ls_remote_exact.py::test_latency_against_a_local_bare_origin_is_under_100ms",
    "\u2014\u2014\u2014 tests/test_vcs_git_ls_remote_exact.py::test_latency_against_a_local_bare_origin_is_under_100ms \u2014\u2014\u2014\n[gw2] darwin -- Python 3.12.13 /Users/eyalgolan/.no_human/worktrees/19dd94b38fa2488383da432909fee8a2.52752.4b0294c6/.venv/bin/python3\n\nbare_origin = {'bare': PosixPath('/private/var/folders/1r/3r0rt1jd4j1456rsg_fh4d380000gn/T/pytest-of-eyalgolan/pytest-55545/popen-gw...test-55545/popen-gw2/test_latency_against_a_local_b0/work'), 'develop_sha': 'a0ca9ae95fd3f3aba464027bfd45cdc6df7dae2e'}\n\n    def test_latency_against_a_local_bare_origin_is_under_100ms(bare_origin):\n        \"\"\"Not a hard perf gate (CI variance), but pins the design claim that a\n        single exact-ref `ls-remote` against a local remote is cheap enough to\n        run once per attempt without becoming the bottleneck \u2014 measured, not\n        assumed. See PR body for the measured number this test asserts against.\"\"\"\n        repo = GitRepo(bare_origin[\"work\"])\n        start = time.monotonic()\n        sha = repo.ls_remote_exact(\"refs/heads/develop\")\n        elapsed_ms = (time.monotonic() - start) * 1000\n        assert sha == bare_origin[\"develop_sha\"]\n>       assert elapsed_ms < 100, f\"ls_remote_exact took {elapsed_ms:.1f}ms locally\"\nE       AssertionError: ls_remote_exact took 242.7ms locally\nE       assert 242.69854230806231 < 100\n\ntests/test_vcs_git_ls_remote_exact.py:171: AssertionError"
  ],
  "failure_blocks_dropped": 0,
  "flaky_excused": [
    "tests/test_vcs_git_ls_remote_exact.py::test_latency_against_a_local_bare_origin_is_under_100ms"
  ],
  "ok": false,
  "passed": 12362,
  "ran": true,
  "tamper_flag": false
}
```
