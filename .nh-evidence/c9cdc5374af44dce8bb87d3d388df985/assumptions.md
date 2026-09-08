# Assumptions

_Harness-captured record for task `c9cdc537`, commit `70fcfcca6e006e9747f4342ad23594c1e55a1a4b` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** Where in the codebase is the supervisor_decision emitter and its prompt assembly function that needs to be modified to include send_back_feedback? **A:** The supervisor_decision emitter and its prompt assembly function are most likely located in a supervisor module within src/no_human/ (e.g., src/no_human/supervisor/ or src/no_human/supervisor.py), or within src/no_human/api/ if the supervisor logic is part of the API layer. The prompt assembly function would construct the context passed to the supervisor decision model and is where send_back_feedb _(assumption)_
- **Q:** Should the new unit test (supervisor prompt/context builder with feedback) and decision-level test (supervisor decision with fake model) be added to existing supervisor test files, or placed in new test files? Please provide the expected test file paths. **A:** New tests should be added to an existing supervisor test file (e.g., tests/test_supervisor.py or tests/test_supervisor_prompt.py if one exists), to keep supervisor-related tests colocated. If no dedicated supervisor test file exists, create tests/test_supervisor_context.py for the unit test of the prompt/context builder and tests/test_supervisor_decision.py for the decision-level test with the fak _(assumption)_

</details>

