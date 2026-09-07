# Assumptions

_Harness-captured record for task `2d30b000`, commit `8ecb120e007867377ff571f417458ced5b05c98a` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Is the `prefix` field already present in the current GET /api/fs/suggest API responses, or is it still coming from the sibling task? If absent, is defensive handling preferred, or can we assume it will be available before this code ships? **A:** The prefix field is being added by a sibling task and should be treated as optional; defensive handling is preferred to support both prefix-present and prefix-absent scenarios, with the logic defaulting to old behavior when absent. _(assumption)_
- **Q:** In web/src/discoveredRepos.js, does searchEmptyMessage currently exist as an exported function, and if so, what is its exact signature and current logic? **A:** searchEmptyMessage exists as a function in web/src/discoveredRepos.js and currently reads refusals but ignores roots_missing; it should be modified to add the new case for missing paths. _(assumption)_
- **Q:** The five repo-path call sites listed (Onboarding.jsx :106, discoveredRepos.js :149, TaskComposer.jsx :647, Settings.jsx :1982, learningGroups.js :15) — is this list exhaustive, or might there be other .split('/').pop() or .split('\\').pop() calls on repository paths that also need the pathBasename fix? **A:** The task description explicitly scopes in these five sites and explicitly excludes others (summaries.js and SlideOver.jsx), suggesting this list is exhaustive for the intended fix scope; verification would require full codebase search. _(assumption)_

</details>

