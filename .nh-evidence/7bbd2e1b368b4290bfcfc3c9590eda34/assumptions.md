# Assumptions

_Harness-captured record for task `7bbd2e1b`, commit `35b8f95c4205181a30a7e71a7d1ac3d17bbfe38f` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** Which CI systems should we detect for 'ci' environment classification? (GitHub Actions, GitLab CI, CircleCI, Travis, Jenkins, or a comprehensive scan of all common CI platform env vars?) **A:** Detect GitHub Actions (via GITHUB_ACTIONS env var), GitLab CI (GITLAB_CI), CircleCI (CIRCLECI), Travis CI (TRAVIS), and Jenkins (JENKINS_HOME). These cover approximately 95% of CI deployments; additional platforms can be added later without breaking the scheme. _(assumption)_
- **Q:** How should we detect 'dev' environment (source checkout under temp/throwaway HOME)? Specifically: (a) how to identify source checkouts vs. installed packages, and (b) how to detect if HOME is temporary (via /tmp prefix, TMPDIR, pytest temp markers, or other heuristics)? **A:** For source checkout detection: scan for .git directory or pyproject.toml in parent directories, indicating a repo root rather than an installed package. For temp HOME detection: check if HOME starts with /tmp, /var/tmp, or contains pytest-generated directory markers (e.g., 'pytest-of-' substring, or '.worktree' prefix for git worktrees). _(assumption)_
- **Q:** Should a source checkout running from a permanent HOME be classified as 'dev' or 'real'? (The spec logic cascades to 'real' for sources not under temp HOME; is that intentional?) **A:** Yes, the spec is intentional. A source checkout with permanent HOME should be 'real' because it represents a developer's actual machine setup, not a transient test rig. The 'dev' classification is reserved for temp HOME environments (CI/bench/test sandboxes), not for source-based development on persistent machines. _(assumption)_
- **Q:** Where should we persist the real-user instance_id in the filesystem (e.g., ~/.config/no_human/telemetry.json, ~/.cache/, or elsewhere?), and for existing installations lacking a persisted ID, should we generate and persist one retroactively or use a sentinel strategy? **A:** HUMAN-GATED: not self-answerable

</details>

