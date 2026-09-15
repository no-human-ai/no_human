# Assumptions

_Harness-captured record for task `f40b0c0f`, commit `55ca5765b2bbd6cb4db7a1c4e5eb2743fe3101cd` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Do both cli/commands.py (nh approve) and api/app.py (board Approve handler) already have direct access to the repo's profile object at the point where they invoke land_task, or must we locate where the profile is resolved and modify these callers to receive and pass it? **A:** cli/commands.py and api/app.py likely do NOT have direct access to the profile object at present; the profile is resolved in a central location (the orchestrator or a project_profiles resolution layer), and these callers must be modified to receive and pass the test_cmd to land_task. The fix will require extending land_task's signature to accept a test_cmd parameter and updating both callers to re _(assumption)_
- **Q:** What are the exact criteria to determine if a profile test command is 'pytest-based'? Should we check for the substring 'pytest' in the command, or does the profile object have a structured type/runner field that explicitly indicates the test runner? **A:** The profile object likely exposes test_cmd as a simple string field (e.g., 'pytest -q', 'npm test', 'uv run pytest -q -n 4') without a structured type/runner indicator. Pytest-basedness should be determined by checking if the string contains 'pytest' as a substring or if the command starts with 'python' followed by '-m' and 'pytest'; this is simpler and more maintainable than a structured field. _(assumption)_
- **Q:** Are profile test commands stored and represented as shell strings (e.g., 'npm test', 'python -m pytest -q -n 4') requiring shell parsing, or as command lists (e.g., ['npm', 'test']) for direct subprocess.run() invocation? **A:** Profile test commands are stored as shell command strings (e.g., 'npm test', 'uv run pytest -q -n 4'), not as pre-parsed lists. Before subprocess.run() invocation, they must be split into lists; the existing code pattern (referenced in argv[:3] checks) processes them into list form by the time they reach the subprocess layer. _(assumption)_

</details>

