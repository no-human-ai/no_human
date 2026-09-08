# Assumptions

_Harness-captured record for task `01e986fd`, commit `eae5777291c9ba3831491161df7f3059c37c38d5` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** For MAJOR-3 (the TESTING hop that writes a task_phases row when no tests run), should we add a new legal state machine edge IMPLEMENTING → AWAITING_APPROVAL with its own test, or close the phase row immediately with an outcome note saying no tests ran? **A:** Close the phase row immediately with an outcome note saying no tests ran, rather than add a new state machine edge. The task principle states 'do not record a test phase that did not occur'; closing the row immediately maintains data integrity and avoids creating false TESTING phase records in the database. Adding a state machine edge would increase complexity while still allowing the violation. T _(assumption)_
- **Q:** For the MINOR issue with the unreachable guard at ~:4531-4532 (AWAITING_APPROVAL reachability check): should we drop this guard code entirely, or add test cases to cover it? **A:** Drop the guard code entirely rather than add test cases. The task states the guard is 'unreachable in practice (IMPLEMENTING->TESTING is always legal)' and 'unobserved by tests'. Maintaining unreachable/dead code increases technical debt; testing code that cannot be reached in practice provides false coverage assurance. Removal is the cleaner engineering choice. _(assumption)_

</details>

