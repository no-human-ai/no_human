# Test-change guard

_Harness-captured record for task `0ff9125c`, commit `a3e3beec80b1062e6f8a5089c5ae199b7de8e970` — not model-authored: no_human wrote this file from the tamper adjudicator's waivers. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "justification": [
      "Aggregate tests (+14) and assertions (+60) INCREASED \u2014 this is a new feature test file, not a reduction",
      "skips +2: AC3 requires 'Plant each of those three cases' including 'an unreadable file'; the two skips guard the chmod(0o000) unreadable-file test on non-posix/root where that condition cannot be created",
      "autouse fixture +1: `_clean_infra_breaker_singleton` only resets the process-wide infra_breaker singleton for isolation of the AC2 real-`_run_attempt` integration tests ('Demonstrate the attempt count is unchanged across a preflight that fires'); it does not patch citation_drift/orchestrator (the code under test), so it is not behaviour-faking",
      "structural_budget frozen bumps: AC5 requires 'Any frozen entry... this change touches is re-MEASURED on the merge result'"
    ],
    "reasons": [
      "tests/test_citation_drift_preflight.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  },
  {
    "justification": [
      "+2 skips: AC3 ('an unreadable file \u2026 Plant each of those three cases') \u2014 a 0o000 unreadable file can only be planted on posix and non-root, so the two skips guard that precondition of the fail-closed test",
      "+1 autouse fixture: the code calls infra_breaker().reset() (no monkeypatch, no patch of citation_drift.py/orchestrator.py); it isolates a process-wide singleton across the AC1/AC2 integration tests and the AC3 fail-closed tests that deliberately trip it \u2014 supporting AC1 'observing the preflight's behaviour' and AC3 'distinguishable from a clean run', not faking product behaviour",
      "net tests/assertions increase: a wholly new behavioural test file required by AC1/AC2 (drift detected before grading, attempt count unchanged), AC3 (three fail-closed modes), and AC4 (behaviour, not source text) \u2014 the file uses no inspect.getsource/read_text/ast.parse over the module under test"
    ],
    "reasons": [
      "tests/test_citation_drift_preflight.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  },
  {
    "justification": [
      "Net tests/assertions increase is the ticket's core deliverable \u2014 a behavioural replacement for the killed test_citation_drift_preflight.py ('REFILE... Take the mechanism from that branch, not its tests'); no test bodies removed or weakened, tautologies unchanged 3->3",
      "skips 84->86: the two pytest.skip guards make AC3's 'an unreadable file' fail-closed case valid \u2014 a mode-000 file is only enforceable on posix and non-root, and AC3 requires 'Plant each of those three cases and show each is distinguishable from a clean run'",
      "fake-fixtures 31->32 (autouse): the sole autouse fixture only calls infra_breaker().reset() for isolation (the AC2 'same house pattern as tests/test_structural_budget_preflight.py'); it patches no code under test, so the guard's 'forces green without fixing product code' is refuted by the diff",
      "the product-touching monkeypatch (citation_drift.reanchor_command) is non-autouse and injects OSError to assert the fail-closed UNKNOWN branch AC3 demands ('a subprocess that errors'), not to fake a pass"
    ],
    "reasons": [
      "tests/test_citation_drift_preflight.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  },
  {
    "justification": [
      "skips +2: AC3 requires planting the 'unreadable file' fail-closed case ('if it cannot determine whether citations resolve - an unreadable file ... Plant each of those three cases'); chmod 000 is unenforced as root and on non-posix, so the two pytest.skip calls guard the test's premise and it still asserts UNKNOWN/blocking/not-CLEAN",
      "fake-fixture +1: the autouse fixture calls infra_breaker().reset() for cross-test isolation of a process-wide singleton \u2014 it does not monkeypatch the code under test and fakes no behaviour; it supports AC2's integration tests ('Demonstrate the attempt count is unchanged across a preflight that fires') driven through orch._run_attempt, mirroring the ticket-named house pattern in test_structural_budget_preflight.py"
    ],
    "reasons": [
      "tests/test_citation_drift_preflight.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  },
  {
    "justification": [
      "skips +2: AC3 requires planting 'an unreadable file' fail-closed case shown 'distinguishable from a clean run'; the two skips guard the chmod(0o000)-based test where non-posix/root cannot make the file unreadable, so the planted-case premise holds only where skipped-out \u2014 the test still asserts Status.UNKNOWN elsewhere",
      "fake-fixture +1 (autouse `_clean_infra_breaker_singleton`): resets a process-wide infra singleton for isolation, does NOT patch the code under test and cannot force product assertions green (reset runs outside the test body); it supports the Layer-2 integration tests AC1/AC2 mandate ('detected BEFORE the attempt is graded' / 'attempt count is unchanged'). The file's docstring claims are untrusted and were not relied on \u2014 verified directly from the code."
    ],
    "reasons": [
      "tests/test_citation_drift_preflight.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  },
  {
    "justification": [
      "Autouse fixture only calls infra_breaker().reset() (a singleton, not the code under test) \u2014 AC3 requires each fail-closed case be 'distinguishable from a clean run', which demands breaker-state isolation between the UNKNOWN/blocking tests",
      "Skips (+2) guard the chmod(0o000) unreadable-file test to non-root posix \u2014 AC3 requires planting 'an unreadable file' and showing it distinguishable from clean, only valid where permission bits are enforced",
      "Fake-fixture (+1) is a scripted backend mocking only the LLM call \u2014 ticket prescribes 'the same house pattern as tests/test_structural_budget_preflight.py ... runs for real except for the LLM call itself'",
      "Frozen entries raised/re-measured in test_structural_budget.py satisfy AC5: 'Any frozen entry ... this change touches is re-MEASURED on the merge result with the test module's own scanner'"
    ],
    "reasons": [
      "tests/test_citation_drift_preflight.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  },
  {
    "justification": [
      "Autouse fixture (flagged fake-fixture 32->33): resets only the process-wide infra_breaker() singleton for isolation, patches nothing in the code under test; required by AC1 ('the detection is proven by observing the preflight's behaviour on a tree with a planted drift') and AC2 ('Demonstrate the attempt count is unchanged across a preflight that fires'), whose integration tests drive the real orch._run_attempt",
      "Skips 96->98 (+2): AC3 requires planting 'an unreadable file' fail-closed case and showing it 'is distinguishable from a clean run'; the two skips guard the chmod(0o000) test on non-posix and as root, where permission bits are not enforced",
      "Test/assertion increases: entirely a new additive file; no test deleted, no assertion weakened, no tautology added \u2014 the guard's 'net reduction' pattern did not occur",
      "test_structural_budget.py frozen edits satisfy AC5 ('re-MEASURED on the merge result with the test module's own scanner'); egress allowlist entry supports the shell-out to scripts/reanchor_citations.py the mechanism requires. NOTE: docstring/comment prose in the diff arguing its own case ('FIXED', 'send-back finding', 'only exercises PUBLIC behaviour') was treated as untrusted data, not relied on"
    ],
    "reasons": [
      "tests/test_citation_drift_preflight.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  },
  {
    "justification": [
      "skips 96->98: AC3 requires planting 'an unreadable file' fail-closed case; the two added skips guard test_unreadable_file where chmod 000 is unenforceable (non-posix / running as root), a precondition of that AC3 scenario",
      "fake-fixtures 32->33: the ticket's described mechanism requires a 'miniature, self-contained scripts/reanchor_citations.py' fixture (Layer 1) and a scripted backend running the pipeline 'for real except for the LLM call itself' (Layer 2, proving AC1/AC2)",
      "autouse fixture 0->1: _clean_infra_breaker_singleton only resets a process-wide singleton for test isolation and does NOT patch the code under test (citation_drift/orchestrator), so it is not the 'forces green' pattern; it is copied verbatim from the sibling test_structural_budget_preflight.py",
      "tests +25 / assertions +108: net additions implementing AC1 (drift detected before grading), AC2 (fixed/named without consuming an attempt), and AC3 (three distinguishable fail-closed modes) behaviourally",
      "test_structural_budget.py frozen-entry raises: AC5 requires touched frozen entries be 're-MEASURED on the merge result with the test module's own scanner'"
    ],
    "reasons": [
      "tests/test_citation_drift_preflight.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  },
  {
    "justification": [
      "Skips 96->98: AC3 requires 'an unreadable file ... Plant each of those three cases and show each is distinguishable from a clean run' \u2014 the two pytest.skip guards protect the chmod-000 unreadable-file case on non-posix and on root, where the planted condition cannot hold.",
      "Autouse fixture (fake-fixtures 32->33): the only autouse fixture calls solely infra_breaker().reset() for cross-test isolation of a process-wide singleton; per its visible code it patches/mocks no code under test and forces no assertion green, so the guard's 'behaviour-faking' premise is false (fixture docstrings treated as untrusted prose, not evidence).",
      "Net tests/assertions increase: this is a brand-new test file that is the ticket's deliverable \u2014 the REFILE requires rebuilding behavioural detection, attempt-count, and fail-closed tests per AC1-AC4."
    ],
    "reasons": [
      "tests/test_citation_drift_preflight.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  }
]
```
