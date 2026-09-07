# Assumptions

_Harness-captured record for task `2b8fe5b0`, commit `da93f817b8b058b2d79efc9a25f97c9f8a99862c` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Which repository (provide the GitHub org/repo or other reference) should I work on? **A:** HUMAN-GATED: not self-answerable
- **Q:** What branch should I base these changes on, and what should be the target branch for the PR (e.g., main, develop, or a release branch)? **A:** The 'strict manifest gate' is a CI verification that ensures RELEASE_MANIFEST.txt contains current hashes/checksums for all release artifacts. It requires re-pinning when new files (latest.yml, latest-linux.yml) are added to a release—'re-pinning' means updating the manifest's checksums/file listings to match the actual release artifacts. The gate likely fails if the manifest entries do not match _(assumption)_
- **Q:** What is the 'strict manifest gate' (which CI job or verification tool?), under what condition does it require re-pinning RELEASE_MANIFEST.txt, and what does 're-pin' mean (e.g., update a hash, version, or timestamp)? **A:** (unanswered)

</details>

