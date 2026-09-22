# Verifiers

_Harness-captured record for task `65b30513`, commit `1949165c0907449fccd58c75d7dbdfef37ba510c` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "Each test function added or modified includes at least one assert statement or pytest.raises block; the non-test helpers (_chunk, _passing_block, _commit, _tool_use) are not tests.",
    "evidence": "Every added/modified test contains assertions or pytest.raises, e.g. test_octal_escaped_non_ascii_header_path_decodes_to_the_real_path has `assert _unquote_path(f'\"a/{escaped}\"') == \"docs/\u00e9val.md\"` and test_uninspected_cut_file_routes_through_reviewer_unavailable uses `with pytest.raises(ReviewerUnavailable)` plus `assert backend.calls == 2`.",
    "file": "tests/test_diff_coverage.py",
    "files_checked": [
      "tests/test_diff_coverage.py"
    ],
    "line": 70,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 568,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
