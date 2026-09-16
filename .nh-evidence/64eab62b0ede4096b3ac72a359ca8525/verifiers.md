# Verifiers

_Harness-captured record for task `64eab62b`, commit `92997203b003ed20f6bded7a3f29b53aa2509a06` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All new and modified test functions across the three files carry at least one assert, pytest.raises block, or assertion-bearing helper; the modified fake_run in test_doctor_editable_install.py is a helper closure inside a test that itself asserts msg is None.",
    "evidence": "Every added/modified test function contains assertions, e.g. test_install_walks_pins_utf8_and_replace_on_every_runner_call has `assert ok is True` and per-kwarg asserts; test_the_subject_is_undecodable_as_cp1255 uses `with pytest.raises(UnicodeDecodeError)`.",
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
    "tokens_used": 967,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  },
  {
    "comment": "The change is purely about subprocess text-decoding; no new or modified code touches task status transitions, so nothing calls update_task with validate=False. The statement holds vacuously for this diff.",
    "evidence": "Every hunk in the diff only adds encoding=\"utf-8\", errors=\"replace\" (and in worktree.py a PYTHONIOENCODING env pin) to existing subprocess.run(...) calls; none of the added/modified lines call update_task or write a task status at all.",
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
    "tokens_used": 420,
    "unavailable": false,
    "verifier_id": "no-unvalidated-status-write"
  }
]
```
