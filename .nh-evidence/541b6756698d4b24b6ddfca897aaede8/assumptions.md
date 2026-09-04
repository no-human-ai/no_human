# Assumptions

_Harness-captured record for task `541b6756`, commit `2cd6bd5345e8df18e7e884aa825cfbe9f0299d67` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 13 assumptions made on your behalf — verify at review</summary>

- **Q:** Can we access the original private PR #811 specification or implementation to understand complete requirements for the verifiers CLI feature, especially what 'propose from review findings' means? **A:** HUMAN-GATED: not self-answerable
- **Q:** Where in the current public repository should the verifiers CLI be implemented (which directory/module)? **A:** In a CLI or tools module directory, most likely under a `cli/` or `tools/` structure as a `verifiers` subcommand or module, following the repo's existing CLI organization patterns for developer tooling _(assumption)_
- **Q:** Which of the 12 currently-failing tests must pass for this feature to be considered complete? **A:** All 12 tests that directly verify the core verifiers CLI operations (list, add, check verifiers, and propose from review findings), as these are essential to the feature's functionality and were part of the original implementation _(assumption)_
- **Q:** Should this verifiers CLI be publicly exposed as a user-facing command, or is it internal developer tooling? **A:** Public developer-facing command/API, since the feature is being re-homed to the public repository main branch rather than remaining private, indicating it is intended for public consumption _(assumption)_
- Natural-language verifiers are configuration-driven rules (stored as JSON/YAML) that validate code against textual criteria; the CLI will provide 'list', 'add', and 'check' subcommands plus a 'propose' capability.
- Target implementation is against the latest public main branch as of 2026-09-04; the private no_human-private branch will not be consulted due to conflict risk—implementation is from first principles.
- All file:line references from the private PR must be validated against the current public tree before any edits; references will be considered stale until verified.
- RELEASE_MANIFEST is a single manifest file tracking feature/version pins that must be re-pinned in the same commit as feature implementation.
- The 12 failing tests in the private PR are blockers; the agent must resolve all of them to achieve merge readiness (tests_ran_and_passed rule).
- Propose-from-review-findings means the agent should extract verifier suggestions from code review comments and surface them as new rule candidates in the CLI output.
- The agent will not modify anything under docs/superpowers/ or any private-only export utilities; updates are only to public-facing CLI, core logic, and tests.
- Implementation assumes prior art or test specifications exist in the current tree defining the CLI interface and verifier schema; the agent will infer structure from tests if needed.
- Empty acceptance criteria means success is defined by: (1) passing the 2 existing verifiers, (2) all test suite passing, (3) merge policy satisfied.

</details>

