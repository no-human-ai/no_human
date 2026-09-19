# Tests — the orchestrator's own run

_Harness-captured record for task `d41812aa`, commit `f2697a233055781bd25542aa755eb413fd2f7075` — not model-authored: no_human wrote this file from the layered test run on the final tree. It records what the gate produced; it is not a verdict of the model that wrote the code._

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
    "\u2014\u2014\u2014 tests/test_vcs_git_ls_remote_exact.py::test_latency_against_a_local_bare_origin_is_under_100ms \u2014\u2014\u2014\n[gw2] darwin -- Python 3.12.13 /Users/eyalgolan/.no_human/worktrees/d41812aa1c0e4929b9f3017884dea7a9.6460.e4921561/.venv/bin/python\n\nbare_origin = {'bare': PosixPath('/private/var/folders/1r/3r0rt1jd4j1456rsg_fh4d380000gn/T/pytest-of-eyalgolan/pytest-5951/popen-gw2...ytest-5951/popen-gw2/test_latency_against_a_local_b0/work'), 'develop_sha': '9532668413b617bbd3332b51227dd8e23d69d2b7'}\n\n    def test_latency_against_a_local_bare_origin_is_under_100ms(bare_origin):\n        \"\"\"Not a hard perf gate (CI variance), but pins the design claim that a\n        single exact-ref `ls-remote` against a local remote is cheap enough to\n        run once per attempt without becoming the bottleneck \u2014 measured, not\n        assumed. See PR body for the measured number this test asserts against.\"\"\"\n        repo = GitRepo(bare_origin[\"work\"])\n        start = time.monotonic()\n        sha = repo.ls_remote_exact(\"refs/heads/develop\")\n        elapsed_ms = (time.monotonic() - start) * 1000\n        assert sha == bare_origin[\"develop_sha\"]\n>       assert elapsed_ms < 100, f\"ls_remote_exact took {elapsed_ms:.1f}ms locally\"\nE       AssertionError: ls_remote_exact took 129.4ms locally\nE       assert 129.40541699936148 < 100\n\ntests/test_vcs_git_ls_remote_exact.py:171: AssertionError"
  ],
  "failure_blocks_dropped": 0,
  "flaky_excused": [
    "tests/test_vcs_git_ls_remote_exact.py::test_latency_against_a_local_bare_origin_is_under_100ms"
  ],
  "ok": false,
  "passed": 13503,
  "ran": true,
  "tamper_flag": false
}
```
