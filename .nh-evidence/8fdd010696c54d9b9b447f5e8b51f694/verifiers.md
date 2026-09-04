# Verifiers

_Harness-captured record for task `8fdd0106`, commit `22c805c76ceb4b6599140a761b3950b3a76c41e3` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "All seven added test functions (test_integrations_legal_doc_exists_with_codex_section, test_codex_section_carries_three_sourced_quotes, test_codex_section_names_source_urls, test_fetch_dates_are_iso_formatted, test_unfavourable_half_and_8338_named_as_partial, test_withdrawn_prohibition_named_as_withdrawn, test_auth_modes_match_the_code, test_codex_auth_json_is_never_read_is_stated, test_nothing_is_stated_as_settled_law) contain at least one assert; the non-test helpers use pytest.fail but the statement only governs test functions.",
    "evidence": "Each test_* function contains assert statements, e.g. test_codex_auth_json_is_never_read_is_stated: assert \"~/.codex/auth.json\" in section",
    "file": "tests/test_integrations_legal_codex.py",
    "files_checked": [
      "tests/test_integrations_legal_codex.py"
    ],
    "line": 0,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 437,
    "unavailable": false,
    "verifier_id": "tests-assert-something"
  }
]
```
