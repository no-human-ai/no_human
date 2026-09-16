# Verifiers

_Harness-captured record for task `64eab62b`, commit `9e386c38daba4db72105c0a94fdb84eff6530901` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new tests in test_subprocess_decodes_utf8.py, the new test in test_doctor_walks_provision.py, and the modified test_spec_resolving_to_a_sibling_worktree_of_the_same_repo_is_silent (which retains `assert msg is None`) contain at least one assertion or pytest.raises block.",
    "evidence": "Every added/modified test contains assertions, e.g. test_install_walks_pins_utf8_and_replace_on_every_runner_call has `assert ok is True` and multiple `assert kw.get(...)`, and test_the_subject_is_undecodable_as_cp1255 uses `with pytest.raises(UnicodeDecodeError)`.",
    "file": "",
    "files_checked": [
      "tests/test_doctor_editable_install.py",
      "tests/test_doctor_walks_provision.py",
      "tests/test_subprocess_decodes_utf8.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 1008,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change set is purely about subprocess text-encoding hardening and contains no new or modified task-status write, so it cannot introduce an update_task(validate=False) status transition.",
    "evidence": "Every hunk in the diff only adds encoding=\"utf-8\", errors=\"replace\" (and one PYTHONIOENCODING env pin) to existing subprocess.run() calls in build_info.py, orchestrator.py, review_routing.py, reviewer_worktree.py, scheduler.py, and worktree.py; no line adds or modifies any update_task(...) or status-writing call.",
    "file": "",
    "files_checked": [
      "src/no_human/core/build_info.py",
      "src/no_human/core/orchestrator.py",
      "src/no_human/core/review_routing.py",
      "src/no_human/core/reviewer_worktree.py",
      "src/no_human/core/scheduler.py",
      "src/no_human/core/worktree.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "high",
    "tokens_used": 473,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
