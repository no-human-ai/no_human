# Assumptions

_Harness-captured record for task `0ab78498`, commit `55916ece77c69f7d058ede2c4d9a7acce3c50bf0` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 1 assumption made on your behalf — verify at review</summary>

- **Q:** Where should the unit tests for the before_send filter be created? What is the project's test file discovery pattern for web/ unit tests (e.g., web/test/, web/__tests__/, web/src/__tests__/)? **A:** Most likely web/__tests__/ for project-level unit tests (following Jest convention), or co-located web/src/__tests__/ if the project uses source-directory test discovery. Without reading package.json or jest.config.js, assume the more common Jest pattern: files matching *.test.js or *.spec.js in a web/__tests__/ directory at the project root, or __tests__/ subdirectories adjacent to source files. _(assumption)_

</details>

