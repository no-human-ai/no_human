# Verifiers

_Harness-captured record for task `c5ec4908`, commit `da9ea01cc0fb3dc85907ea4812ac7d783179146c` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All twelve added test functions contain at least one assert statement; the non-test helpers (_agent_message, _write_fake_cli, etc.) are not test functions and are exempt.",
    "evidence": "Every def test_* in the new file contains assert statements, e.g. test_the_event_cap_is_max_turns_times_the_per_turn_factor has 'assert cx._event_cap(0) == cx._MAX_STREAM_EVENTS'",
    "file": "tests/test_codex_stream_flood_bound.py",
    "files_checked": [
      "tests/test_codex_stream_flood_bound.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 596,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
