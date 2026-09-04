# Test-change guard

_Harness-captured record for task `1f762d5b`, commit `1908563da78c005252a3a67084928c3b1c6278dd` — not model-authored: no_human wrote this file from the tamper adjudicator's waivers. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "justification": [
      "Flagged fake-fixture is the autouse `_clear_health_results`, which only clears the new module's `_RESULTS` cache for test isolation and patches no product code; HTTP is faked at the `reg._http_get` seam as AC 'Unit tests (mock HTTP, no network)' requires. The whole net-additive file is required by AC 'New module holds all probe logic' and 'Status endpoint returns the new fields' (tests write directly to `_RESULTS`, so the reset is needed for order-independence). No assertions removed, no skips/tautologies added."
    ],
    "reasons": [
      "tests/test_integrations_health.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  },
  {
    "justification": [
      "The flagged autouse fixture `_clear_health_results` only resets the in-memory `h._RESULTS` persistence cache between tests; it patches no product code and forces no assertion green. AC 'Status endpoint returns the new fields' and 'Boot + interval scheduling tested via the seam' require this isolation because their tests seed/read `h._RESULTS` directly (e.g. `test_status_endpoint_exposes_health_fields` sets `h._RESULTS[\"jira\"]`), so cross-test state must be cleared. The diff is net-additive (new test file + web badge tests) with real healthy/unhealthy/credential-scrub/disabled-not-probed assertions matching the ACs; no test or assertion was removed or weakened."
    ],
    "reasons": [
      "tests/test_integrations_health.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  },
  {
    "justification": [
      "Flagged autouse/monkeypatch fixture: the autouse fixture `_clear_health_results` only does `h._RESULTS.clear()` (test-state isolation, patches no code under test), while the counted mock swaps `reg._http_get`/`_http_post` at the network seam, which AC 'Unit tests (mock HTTP, no network): healthy on 200, unhealthy with status-code detail on 401/404/DNS error, timeout handled; credentials never appear in detail (assert)' directly requires; tests/assertions increased and skips/tautologies are unchanged. In-file comments arguing the fixture's own legitimacy were treated as untrusted data, not evidence."
    ],
    "reasons": [
      "tests/test_integrations_health.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  },
  {
    "justification": [
      "The lone autouse fixture `_clear_health_results` monkeypatches no product code \u2014 it resets the in-memory `h._RESULTS` cache between tests, standard isolation required because tests seed `_RESULTS` directly to satisfy AC \"Status endpoint returns the new fields\" and AC \"Boot + interval scheduling tested via the seam\"",
      "The per-test `monkeypatch.setattr(reg, \"_http_get\", ...)` calls are mandated by AC \"Unit tests (mock HTTP, no network): healthy on 200, unhealthy with status-code detail on 401/404/DNS error, timeout handled\"",
      "All changes are additive (tests +17, assertions +51, skips 73->73, tautologies 3->3); no assertion is deleted, skipped, or turned tautological \u2014 the guard's \"forces green\" label misreads a state-reset fixture as a behaviour-fake",
      "Diff comments/docstring arguing the change's own case were treated as untrusted data and not used as evidence"
    ],
    "reasons": [
      "tests/test_integrations_health.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  }
]
```
