# Assumptions

_Harness-captured record for task `95757282`, commit `b26009e1c7b44aab9e77b80b1c5039704b01bba7` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

> ⚠️ **Unresolved:** You've hit your session limit · resets 1:40pm (Asia/Jerusalem) ('personal2' subscription)

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** The task specifies storing the pinned base SHA 'on the attempt/task context'—which distinct object should it be stored on: the `attempt` object or the `task` object? **A:** Store the pinned base SHA on the `attempt` object. The task specifies pinning occurs 'at attempt setup (before the coder session starts)' and must prevent the coder from moving the exclusion root mid-attempt. Attempts are the scoped unit: different attempts of the same task may run at different times with different base SHA values, and the exclusion logic must use that specific attempt's pinned va _(assumption)_
- **Q:** The task calls `git ls-remote origin refs/heads/<base>` without noting authentication. Does the bare origin repository require credentials? If yes, which credential set (service account, deployment key, requester's token) should the agent use? **A:** HUMAN-GATED: not self-answerable
- **Q:** The acceptance criteria state 'Full test suites pass unchanged—pytest tests/test_agent_commit_identity_enforced.py tests/test_vcs_git_ls_remote_exact.py'. Does this mean run only those two files, or run the entire pytest suite for the repo? **A:** Run the full pytest suite for the repo. The phrasing 'Full test suites pass unchanged' followed by the specific note on those two files means: the two named files are the primary regression targets and must pass unmodified (zero changes to test code), but all other tests must also pass. This is standard acceptance—the full suite is gated, and the call-out of those two just highlights which ones ar _(assumption)_
- **Q:** For the RELEASE_MANIFEST deliverable, the task mentions 'scanner metric' and 'structural-budget re-freeze' without defining the scanner tool or what constitutes a 're-pin'. What specific metrics should be measured, which scanner reports them, and does 're-pin' include all dependencies or only git-related ones? **A:** Measure ls_remote call latency (target <100ms per call) and gate latency/build time (no regression vs. baseline). The 'scanner' is assumed to be the repo's existing CI gate/merge check system. Update RELEASE_MANIFEST to pin any dependencies that changed (typically lock files, not scope-limited to git-only) and document both the ls_remote latency measurement and scanner output confirming gate laten _(assumption)_

</details>

