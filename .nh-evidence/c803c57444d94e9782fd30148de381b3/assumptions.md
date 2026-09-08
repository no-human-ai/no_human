# Assumptions

_Harness-captured record for task `c803c574`, commit `fbed9f1a76d86ba25852bbee953a45441c119513` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 1 assumption made on your behalf — verify at review</summary>

- **Q:** For node --test test runs, does `test_result.failing_tests` contain individual test case ids in the format `path/to/file.test.mjs::test_name::nested_name`, and we extract the file path as the prefix before the first `::`? Or is the id format different, and if so, how should we extract the file paths for the serial rerun? **A:** Node.js test runner (node --test) formats failing test IDs as `path/to/file.test.mjs::test_name` for top-level tests and `path/to/file.test.mjs::test_name::nested_name` for nested tests, with `::` as the separator. Extract file paths as the prefix before the first `::` to identify unique failing test files for the serial rerun command. _(assumption)_

</details>

