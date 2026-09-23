# Tests — the orchestrator's own run

_Harness-captured record for task `d347526b`, commit `efb13bc1f6a201017491332f94a03f11773c05e3` — not model-authored: no_human wrote this file from the layered test run on the final tree. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
{
  "classified": true,
  "errors": 0,
  "failed": 3,
  "failing_tests": [
    "tests/test_release_binary_deps.py::test_both_release_jobs_pin_electron_cache_env_vars",
    "tests/test_release_binary_deps.py::test_the_pinned_cache_dirs_are_not_a_bare_tilde",
    "tests/test_release_binary_deps.py::test_the_cache_path_matches_the_pinned_env_vars_exactly"
  ],
  "failure_blocks": [
    "FAILED tests/test_release_binary_deps.py::test_both_release_jobs_pin_electron_cache_env_vars\nFAILED tests/test_release_binary_deps.py::test_the_pinned_cache_dirs_are_not_a_bare_tilde\nFAILED tests/test_release_binary_deps.py::test_the_cache_path_matches_the_pinned_env_vars_exactly",
    "\u2014\u2014\u2014 tests/test_release_binary_deps.py::test_both_release_jobs_pin_electron_cache_env_vars \u2014\u2014\u2014\n[gw3] darwin -- Python 3.12.13 /Users/eyalgolan/.no_human/worktrees/d347526bbaa448019507a2069967c31d.56167.5c9f41fe/.venv/bin/python3\n\n    def test_both_release_jobs_pin_electron_cache_env_vars():\n        workflow = _load_workflow()\n        for job_name in (\"windows\", \"linux\"):\n            job = _job(workflow, job_name)\n            env = job.get(\"env\", {})\n>           assert \"ELECTRON_CACHE\" in env\nE           AssertionError: assert 'ELECTRON_CACHE' in {}\n\ntests/test_release_binary_deps.py:496: AssertionError",
    "\u2014\u2014\u2014 tests/test_release_binary_deps.py::test_the_pinned_cache_dirs_are_not_a_bare_tilde \u2014\u2014\u2014\n[gw3] darwin -- Python 3.12.13 /Users/eyalgolan/.no_human/worktrees/d347526bbaa448019507a2069967c31d.56167.5c9f41fe/.venv/bin/python3\n\n    def test_the_pinned_cache_dirs_are_not_a_bare_tilde():\n        \"\"\"electron-builder resolves ELECTRON_BUILDER_CACHE/ELECTRON_CACHE with\n        Node's path.resolve(), which does NOT expand a leading '~' -- it becomes\n        a literal '~' directory under the step's cwd. A value like\n        '~\\\\AppData\\\\Local\\\\electron-builder\\\\Cache' therefore never matches\n        where actions/cache restores/saves ('~' expanded to the runner's home),\n        so the cache can never hit. The env value must be an absolute path with\n        no leading tilde (e.g. built from the `runner.temp`/`runner.workspace`\n        contexts, which are already absolute).\n        \"\"\"\n        workflow = _load_workflow()\n        for job_name in (\"windows\", \"linux\"):\n            job = _job(workflow, job_name)\n>           env = job[\"env\"]\n                  ^^^^^^^^^^\nE           KeyError: 'env'\n\ntests/test_release_binary_deps.py:513: KeyError",
    "\u2014\u2014\u2014 tests/test_release_binary_deps.py::test_the_cache_path_matches_the_pinned_env_vars_exactly \u2014\u2014\u2014\n[gw3] darwin -- Python 3.12.13 /Users/eyalgolan/.no_human/worktrees/d347526bbaa448019507a2069967c31d.56167.5c9f41fe/.venv/bin/python3\n\n    def test_the_cache_path_matches_the_pinned_env_vars_exactly():\n        \"\"\"The cache step's `path:` entries must be the SAME strings as the\n        pinned ELECTRON_BUILDER_CACHE/ELECTRON_CACHE env vars, so the directory\n        actions/cache restores into is provably the directory electron-builder\n        reads from -- not merely two paths that happen to resolve the same way\n        under two different expansion rules.\n        \"\"\"\n        workflow = _load_workflow()\n        for job_name in (\"windows\", \"linux\"):\n            job = _job(workflow, job_name)\n>           env = job[\"env\"]\n                  ^^^^^^^^^^\nE           KeyError: 'env'\n\ntests/test_release_binary_deps.py:533: KeyError"
  ],
  "failure_blocks_dropped": 0,
  "ok": false,
  "passed": 13552,
  "pre_existing_failures": [
    "tests/test_release_binary_deps.py::test_both_release_jobs_pin_electron_cache_env_vars",
    "tests/test_release_binary_deps.py::test_the_pinned_cache_dirs_are_not_a_bare_tilde",
    "tests/test_release_binary_deps.py::test_the_cache_path_matches_the_pinned_env_vars_exactly"
  ],
  "ran": true,
  "tamper_flag": false
}
```
