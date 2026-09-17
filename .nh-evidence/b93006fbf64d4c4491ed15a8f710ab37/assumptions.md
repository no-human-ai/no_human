# Assumptions

_Harness-captured record for task `b93006fb`, commit `ea2a8f75395008b08905bc9584c0d7c10f0aba9b` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

> ⚠️ **Unresolved:** mechanical derived-artefact conflict resolution failed: https://github.com/no-human-ai/no_human/pull/463 step=regenerate

> ⚠️ **Open question:** PR https://github.com/no-human-ai/no_human/pull/463 conflicts only in derived artefact(s) (RELEASE_MANIFEST.txt) but mechanical resolution failed at step 'regenerate'; detail: check_release_manifest --write failed (1): Traceback (most recent call last): File "/private/var/folders/1r/3r0rt1jd4j1456rsg_fh4d380000gn/T/nh-derived-2a2k7sso/scripts/check_release_manifest.py", line 410, in <module> raise

<details><summary>⚠️ 11 assumptions made on your behalf — verify at review</summary>

- **Q:** Should the mitigation apply to Windows release builds only, or to all platform builds (Windows, macOS, Linux)? **A:** All platform builds (Windows, macOS, Linux). electron-builder's dependency-fetching behavior is cross-platform, and addressing only Windows leaves the same vulnerability present during macOS and Linux release cuts. Platform-specific binaries (nsis for Windows, code-signing tools for macOS, etc.) will each have the same unpinned-download risk. Fixing systematically prevents the failure mode from su _(assumption)_
- **Q:** Which mitigation is preferred: (a) cache in CI (reduces but doesn't eliminate CDN risk), (b) vendor binaries (removes CDN dependency, adds repo size and refresh overhead), or (c) accept and document the dependency in the release runbook? **A:** (a) Cache in CI, as the starting mitigation. One observed occurrence is not enough to justify vendoring overhead. A warm cache reduces CDN exposure without committing to binary vendoring or repo weight. This position is reversible: once historical release run data is gathered (measure: how often the specific nsis/resources/7zip/electron downloads timeout or 504 across previous builds), escalate to _(assumption)_
- **Q:** For the baseline measurement required by acceptance criteria—how far back in release history should we search (e.g., last 10 releases, last 6 months, all of 2026)—and do you have read access to CI logs and build history to retrieve this data? **A:** HUMAN-GATED: not self-answerable
- **Q:** If option (b) vendor is selected, should binaries be committed to the repo or hosted externally, and if external, what infrastructure is available and who owns updates when electron-builder versions change? **A:** HUMAN-GATED: not self-answerable
- The agent will enumerate binary dependencies by inspecting electron-builder's configuration and cache directory structure during a local test build, since these are not explicitly listed in the codebase.
- For frequency measurement, the agent will query available CI workflow runs (GitHub Actions history) for the last 50 release builds or 6 months of history, whichever is available, looking for download failures on the known binary labels (nsis-*.7z, 7zip-win-x64.tar.gz, electron zip).
- If only 1-2 download failures are found historically, the agent will implement option (c) + (a): document the dependency list and add CI caching via actions/cache keyed on the electron-builder version hash, with residual exposure that a cold cache remains CDN-dependent.
- If 3+ download failures are found, the agent will implement option (b): vendor binaries into a dedicated .electron-builder-cache directory or external artifact store, set ELECTRON_BUILDER_BINARIES_MIRROR to point at them, with explicit note that this adds repo maintenance burden when electron-builder updates.
- The dependency list and chosen mitigation will be documented in docs/RELEASE.md as a structured table showing each binary, its source URL, pinned version, and the mitigation applied.
- The agent will assume the existing two-retry rule for infra failures is a documented CI policy and will verify it is enforced at the workflow level (not added as a loop inside the build step itself).
- No retry mechanism will be added inside the build workflow; the agent will treat build step failure as a signal to be escalated, not looped on.

</details>

