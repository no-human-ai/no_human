# Assumptions

_Harness-captured record for task `e83b0b6d`, commit `86c25f59d6975ee86f913aebaa0fb124a08d595b` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Should the divergence advisory (naming both SHAs and stating that delivery will refuse) be output as a log/event line, a structured field in the result object, or both? **A:** both — emit an advisory as an event/log line naming both SHAs and the delivery refusal, AND record diverged: true as a structured field in the base_staleness context payload; this surfaces the issue immediately to operators while enabling programmatic detection _(assumption)_
- **Q:** Should the protected-branch test for merge_base_into_branch be added to test_base_staleness_pushed_branch.py (colocated with the divergence test), or to a separate test file/module? **A:** test_base_staleness_pushed_branch.py — the task states that file 'has the harness' for real bare remote testing, and colocating the merge_base_into_branch protection test there keeps related branch-protection tests together in the same module that exercises the base staleness logic _(assumption)_
- **Q:** When the remote branch doesn't exist or fetch_remote_branch_sha returns no tip, should _refresh_stale_base skip the divergence check silently, or emit a different advisory/error? **A:** skip the divergence check silently — divergence is only meaningful when a remote tip exists; if fetch_remote_branch_sha returns no tip, there is no divergence to detect, so the check should not emit an advisory or error _(assumption)_

</details>

