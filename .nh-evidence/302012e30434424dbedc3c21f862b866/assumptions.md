# Assumptions

_Harness-captured record for task `302012e3`, commit `903253f41cecc392b9e1d9f548941880d7e8114a` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 1 assumption made on your behalf — verify at review</summary>

- **Q:** The acceptance criteria specify applying the fixture to 'all six nh start tests,' but the task description mentions '(and any other CliRunner invocation of start).' Should the cleanup fixture be applied only to those six explicitly named tests, or should it also be applied to any other tests in the codebase that invoke the start command via CliRunner? **A:** Apply the cleanup fixture to all tests in the codebase that invoke the start command via CliRunner, not only the six explicitly named tests. The task description's parenthetical '(and any other CliRunner invocation of start)' indicates the root cause—shared app state mutation—affects any such invocation, not just those six. A senior engineer would apply the fixture to all instances of the pattern _(assumption)_
- Acceptance criteria were auto-sharpened during intake; originals: tests/test_cli_commands.py's nh start tests restore the module-level app's state (setup_mode, setup_reason, _worker_opts) after each invocation, and the API lifespan's shutdown deletes setup_mode/setup_reason; _require_credentials keeps its opt-in semantics; app.py within its frozen budget or re-measured downward; Red-first: tests/test_cli_commands.py::test_start_runs_jira_poller_when_enabled followed by tests/test_local_model_preflight.py in one process fails on the pre-fix tree with the 503 setup-mode text and passes after; a lifespan-cleanup test is red on the pre-fix tree; test_local_model_preflight.py itself unchanged; uv run pytest -q -n 4 passes with the whole summary line in the PR body, plus the before/after serial command outputs; scripts/check_release_manifest.py --strict OK

</details>

