# Tests — the orchestrator's own run

_Harness-captured record for task `2dcc6f80`, commit `ec151501f54abb618c0206444736652aa9118a0c` — not model-authored: no_human wrote this file from the layered test run on the final tree. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
{
  "classified": true,
  "errors": 0,
  "failed": 2,
  "failing_tests": [
    "tests/test_reviewer_worktree.py::test_config_reserialization_excused_but_key_change_and_source_edit_caught",
    "tests/test_reviewer_worktree.py::test_a_non_bookkeeping_config_key_and_a_tracked_edit_still_discard"
  ],
  "failure_blocks": [
    "FAILED tests/test_reviewer_worktree.py::test_config_reserialization_excused_but_key_change_and_source_edit_caught\nFAILED tests/test_reviewer_worktree.py::test_a_non_bookkeeping_config_key_and_a_tracked_edit_still_discard",
    "\u2014\u2014\u2014 tests/test_reviewer_worktree.py::test_a_non_bookkeeping_config_key_and_a_tracked_edit_still_discard \u2014\u2014\u2014\n[gw2] darwin -- Python 3.12.13 /Users/eyalgolan/.no_human/worktrees/2dcc6f8072c34c00bd17589f0a6504f5.82890.36bfca6e/.venv/bin/python3\n\nworktree_env = {'remote': PosixPath('/private/var/folders/1r/3r0rt1jd4j1456rsg_fh4d380000gn/T/pytest-of-eyalgolan/pytest-50012/popen-.../pytest-of-eyalgolan/pytest-50012/popen-gw2/test_a_non_bookkeeping_config_0/upstream/.git/worktrees/reviewer-wt'), ...}\n\n    def test_a_non_bookkeeping_config_key_and_a_tracked_edit_still_discard(\n        worktree_env,\n    ):\n        \"\"\"Pin: the benign-key allowlist must not swallow a real violation.\n    \n        Same baseline as the bookkeeping-key test above, but this time the\n        changed config key (`include.path`) is NOT on the allowlist, and a\n        tracked source file is ALSO edited. Both must still land in `modified`\n        and the delta must stay non-empty \u2014 this is the exact protection the\n        module exists for, and the allowlist added by this change must not widen\n        to cover it.\n        \"\"\"\n        wt = worktree_env[\"wt\"]\n        common_dir = worktree_env[\"common_dir\"]\n        cfg = common_dir / \"config\"\n    \n        before = rw.snapshot(wt, timeout=_TIMEOUT)\n    \n        _git(wt, \"config\", \"--file\", str(cfg), \"include.path\", \"/tmp/evil\")\n        (wt / \"src\" / \"main.py\").write_text(\"v2 -- reviewer edit\\n\")\n    \n>       delta = rw.compare(wt, before, timeout=_TIMEOUT)\n          \n\u2026 [truncated]"
  ],
  "failure_blocks_dropped": 0,
  "ok": false,
  "passed": 12026,
  "pre_existing_failures": [
    "tests/test_reviewer_worktree.py::test_config_reserialization_excused_but_key_change_and_source_edit_caught",
    "tests/test_reviewer_worktree.py::test_a_non_bookkeeping_config_key_and_a_tracked_edit_still_discard"
  ],
  "ran": true,
  "tamper_flag": false
}
```
