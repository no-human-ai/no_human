# Assumptions

_Harness-captured record for task `9416809a`, commit `b428359e7888987685390f6f8de24e073b6c611c` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Should the fix completely remove job name matching from _ci_failure_unrelated and only extract test identifiers from the error message, or should it refine the job name matching to only apply when the job name looks like a valid test identifier (e.g., pytest node syntax, JUnit-style format, or Test class names)? **A:** Refine job name matching to only apply when the job name looks like a valid test identifier (pytest node syntax, JUnit format, or Test class names). This is safer than completely removing job name matching, preserves legitimate use cases where job names are test identifiers, and targets the root cause of false positives like generic names ('Python'). _(assumption)_
- **Q:** For the runner-acquisition error message, should the regex match only the exact message 'The job was not started because it repeatedly failed to be acquired', or also match variations and related infrastructure-level runner failures? **A:** Match the exact message 'The job was not started because it repeatedly failed to be acquired' and similar variations for the same root cause (runner acquisition timeouts). Being strictly matched to known infrastructure failure patterns avoids over-generalization that could misclassify real test failures as infrastructure issues. _(assumption)_
- **Q:** In which test file or module should the acceptance tests be added? (The criteria reference 'the same test module as the first criterion.') **A:** Add acceptance tests to the test module for core/orchestrator.py (following standard Python convention: tests/core/test_orchestrator.py or equivalent), since the first criterion tests _ci_failure_unrelated in that module, and the criteria specify co-location in the same test module. _(assumption)_

</details>

