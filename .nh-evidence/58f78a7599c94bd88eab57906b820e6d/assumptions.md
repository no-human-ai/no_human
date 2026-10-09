# Assumptions

_Harness-captured record for task `58f78a75`, commit `9e55e77c61053ded25abc2e4c5e9e3ac76ed2f3e` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** What numeric value should merge_poll_timeout_seconds fall back to when the configuration is missing, None, non-numeric, or negative? **A:** 30 seconds (int: 30) _(assumption)_
- **Q:** When PR head restore fails after main is successfully advanced, should land_task return ok=True (code is on main; restore is best-effort) or ok=False (operation cannot complete)? **A:** ok=True — the code has successfully landed on the default branch; PR head restore is best-effort, and its failure does not invalidate the primary objective _(assumption)_
- **Q:** When PR head restore fails, what should the warning message text say? (Given the constraint that it must not contain 'restored' or 'retry', what is the intended message or pattern?) **A:** Code landed on main (sha: <sha>) but PR head could not be restored to the reviewed commit. The PR head is out of sync and may need manual intervention. _(assumption)_

</details>

