# Assumptions

_Harness-captured record for task `7b62b6de`, commit `f6a9443633bd8f326bba860f0967bcdc34e359bf` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 5 assumptions made on your behalf — verify at review</summary>

- **Q:** Are there any test files with Store fixture definitions that have intentionally different semantics beyond just parameter variations (e.g., different behavior, mocking, pre-population, alternate backends)? If so, list each file and its one-line reason. **A:** No intentionally different fixtures found. Based on task description indicating ~40 test files define mechanical duplicate `async def store(tmp_path)` fixtures, assume all use the same baseline setup pattern without semantic variations. _(assumption)_
- **Q:** What is the module import path for the Store class that should appear in the conftest.py fixture? (e.g., 'from myapp.store import Store') **A:** Assume import path is 'from no_human.store import Store' based on repository name 'no_human-public' and standard project structure conventions. _(assumption)_
- **Q:** Which specific test file paths correspond to 'the four ui_evidence tests' mentioned in the acceptance criteria? **A:** Assume 'the four ui_evidence tests' refers to files matching pattern tests/test_ui_evidence*.py (likely four separate test files with 'ui_evidence' in their names). _(assumption)_
- **Q:** Are there test files where `await store.close()` in the test body is structurally necessary (not mechanical/legacy)? If so, list them and explain why. **A:** Assume no structurally necessary in-body `await store.close()` calls exist. Task description frames all ~28 instances as legacy patterns that 'skip whenever the body raises', indicating all are mechanical candidates for try/finally wrapping or fixture conversion. _(assumption)_
- **Q:** What is RELEASE_MANIFEST (file location, format, and purpose)? What does 're-pins in the SAME commit' mean in your project context? **A:** RELEASE_MANIFEST is likely a version/dependency pinning file at repository root (specific location and format unknown without inspection). 're-pins in the SAME commit' means updating this file's version references concurrent with the fixture refactoring, following standard release management practice where dependency/version changes are committed atomically. _(assumption)_

</details>

