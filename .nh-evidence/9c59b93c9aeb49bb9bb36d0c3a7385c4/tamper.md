# Test-change guard

_Harness-captured record for task `9c59b93c`, commit `e07006c96db544a35fac52f0f981cb87f61468c8` — not model-authored: no_human wrote this file from the tamper adjudicator's waivers. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "justification": [
      "test_case_fold_sweep.py autouse fixture only calls host_folds_case.cache_clear() (not a product-code patch) and supports AC4's 'before/after sweep through the real guard entry point'; the probe is now memoized so the cache must be cleared between rows",
      "test_exec_names.py +2 skips are environment-preconditions on NEW real-filesystem tests (host actually folds / root can read 0o000), guarding AC3's real unmeasurable-probe checks, not neutering any existing test",
      "test_exec_names.py autouse fixture clears the newly lru_cached probe between real-volume/pinned tests \u2014 required by the fix's memoization, consistent with AC2/AC3",
      "test_venv_install_guard.py autouse fixture likewise only clears the memoized probe cache, supporting AC1's capitalised-installer tests"
    ],
    "reasons": [
      "tests/test_case_fold_sweep.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)",
      "tests/test_exec_names.py: skip/xfail markers 0->2 (test neutered)",
      "tests/test_exec_names.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)",
      "tests/test_venv_install_guard.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  },
  {
    "justification": [
      "test_exec_names.py skips (+2): AC1 'a capitalised spelling ... is refused wherever the lowercase spelling is refused' and AC3 are covered by NEW tests whose two skips are environmental guards (volume actually folds / process can read 0o000 dirs), not neutered assertions",
      "test_exec_names.py autouse fixture (+1): the ticket's own logic change memoizes the probe ('must do its filesystem work ONCE per process') so an lru_cache-clearing fixture is required for test isolation; it clears cache, patches no product code",
      "test_case_fold_sweep.py autouse fixture (+1): same cache_clear fixture, required by AC4 'a before/after sweep through the real guard entry point shows no command moving from denied to allowed' which replays the memoized probe",
      "test_venv_install_guard.py autouse fixture (+1): same cache_clear fixture, supporting AC1's real/pinned fold-verdict tests",
      "Removed permissive-fallback test is mandated by AC3 'no test asserts the permissive fallback as correct' \u2014 ticket quotes that exact 'is (os.name == \"nt\")' assertion as certifying the wrong behaviour"
    ],
    "reasons": [
      "tests/test_case_fold_sweep.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)",
      "tests/test_exec_names.py: skip/xfail markers 0->2 (test neutered)",
      "tests/test_exec_names.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)",
      "tests/test_venv_install_guard.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  },
  {
    "justification": [
      "test_exec_names.py removed assertion `_folds_case(missing) is (os.name == \"nt\")`: AC3 requires 'a probe that cannot complete results in folding, never in the more permissive answer, and no test asserts the permissive fallback as correct' \u2014 the ticket names this exact assertion as certifying the wrong behaviour",
      "3 autouse fixtures clear only `host_folds_case.cache_clear()`: AC2 ('fold decision derived from a path\u2026 not only when a source checkout runs', with cwd threaded per ticket) makes the probe cwd-keyed and lru_cached, and AC4's real-`evaluate` sweep requires fresh cache state \u2014 the fixtures reset cache, they do not patch code under test",
      "2 skip markers are environmental preconditions in NEW tests distinguishing folding vs case-sensitive volumes, required to exercise AC1 ('on a case-insensitive filesystem a capitalised spelling is refused wherever the lowercase spelling is refused') \u2014 a folding volume cannot hold two distinct-cased files, and root cannot test a 0o000 dir; net coverage rose +19 tests/+35 assertions"
    ],
    "reasons": [
      "tests/test_case_fold_sweep.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)",
      "tests/test_exec_names.py: skip/xfail markers 0->2 (test neutered)",
      "tests/test_exec_names.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)",
      "tests/test_venv_install_guard.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  },
  {
    "justification": [
      "test_case_fold_sweep.py autouse fixture is a cache_clear-only isolator for AC4: 'A before/after sweep through the real guard entry point shows no command moving from denied to allowed' \u2014 it cannot force a test green",
      "test_exec_names.py skip #1 (skip when 'this volume folds case') guards a NEW case-sensitive test supporting AC1's no-false-DENY requirement; skip #2 (skip when running as root) guards a NEW test for AC3 'a probe that cannot complete results in folding, never in the more permissive answer' \u2014 neither neuters existing coverage",
      "test_exec_names.py and test_venv_install_guard.py autouse fixtures are cache_clear-only, required by the logic change making host_folds_case an lru_cache'd probe keyed on (cwd, path_env) \u2014 visible in the signature change to 'lambda *a, **k' and .cache_clear() calls \u2014 for test isolation, not behavior faking"
    ],
    "reasons": [
      "tests/test_case_fold_sweep.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)",
      "tests/test_exec_names.py: skip/xfail markers 0->2 (test neutered)",
      "tests/test_exec_names.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)",
      "tests/test_venv_install_guard.py: autouse monkeypatch fixture 0->1 (forces green without fixing product code)"
    ],
    "verdict": "LEGITIMATE",
    "where": ""
  }
]
```
