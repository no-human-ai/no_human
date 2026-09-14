# Assumptions

_Harness-captured record for task `0ff9125c`, commit `a561bc92b6afc275c345ea8802a8694d821b7a1a` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 6 assumptions made on your behalf — verify at review</summary>

- **Q:** When the preflight detects a drifted citation, should it automatically reanchor it by invoking scripts/reanchor_citations.py, or should it report the drift and return the round to the coder without consuming an attempt? **A:** Automatically reanchor drifted citations by invoking scripts/reanchor_citations.py, then allow the attempt to proceed. This prevents the loss of attempt budget while ensuring citations remain accurate. _(assumption)_
- **Q:** How should we acquire the preflight mechanism from PR #213 given that it conflicts with main in three files—should we manually extract and port the implementation to current main, or reimplement the mechanism fresh? **A:** Manually extract the implementation from PR #213, fix the fail-open exception handling and subprocess error checking (replace check=False), and port to current main. Do not carry over the tests from that branch. _(assumption)_
- **Q:** At what specific step in the attempt workflow should the preflight execute—as part of pre-submission validation, integrated into the test suite, or as a separate pre-grading hook? **A:** Execute as a separate pre-grading hook after reviews pass and tests complete, but before the attempt is marked as complete and graded. _(assumption)_
- **Q:** Which documentation files should the preflight scan for citations—only README, all .md files, or a specific designated set? **A:** Scan all .md files in the repository root and docs/, with README as the primary target and baseline. _(assumption)_
- **Q:** How is the 8-attempt budget tracked and stored in the attempt workflow, and where should we observe or log it to demonstrate that it remains unchanged when a preflight catches and handles drift? **A:** The 8-attempt budget is tracked and incremented by the attempt controller (external to this task). Observe the attempt counter/record before and after the preflight runs to verify it does not increment when drift is detected and handled by the preflight. _(assumption)_
- **Q:** Should the test cases that plant the three failure modes (unreadable file, subprocess error, citation without unique occurrence) be permanent fixtures committed to the repo, or created and torn down dynamically during test execution? **A:** Create test fixtures dynamically during test execution using pytest fixtures and temporary directories (tmpdir, monkeypatch), then tear them down after each test. Do not commit permanent planted-drift trees to the repo. _(assumption)_

</details>

