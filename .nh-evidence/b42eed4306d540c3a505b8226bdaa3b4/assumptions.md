# Assumptions

_Harness-captured record for task `b42eed43`, commit `4c2bdd7c047c53ec1dcd5fb64246ed1f7fbe1bc6` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

> ⚠️ **Unresolved:** You've hit your monthly spend limit · raise it at claude.ai/settings/usage?from=cc_cli_limit_message ('default' subscription)

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** For the acceptance test case, what should 'planting a scanned term in a new commit' consist of—adding specific byte content to a file, creating a file with a particular name, adding a commit message pattern, or something else? **A:** Adding specific byte content to a file, since the scanner detects hits in file blobs and a planted term must be artifact content that the detector recognizes. _(assumption)_
- **Q:** Should the fix also apply the range vs pre-existing split to the 'extra file(s)' count (currently reporting 195 files), as indicated in the design notes, or is that out of scope for this change? **A:** Yes, apply the range vs pre-existing split to the 'extra file(s)' count as well, for consistency across all reported metrics and to match the design notes guidance. _(assumption)_
- **Q:** Does this fix require modifications to the `.nh-local/nh-guard` pre-push hook script itself, or only to `verify_public_history.py`? **A:** Only `verify_public_history.py` needs modification; the `.nh-local/nh-guard` hook script should not require changes if it simply invokes the Python script and relays output. _(assumption)_
- **Q:** If `verify_public_history.py`'s output format must change to show separate range and pre-existing counts, will this break existing tools or scripts that parse its current output? If so, should we maintain backward compatibility through flags/versioning, or accept breaking changes? **A:** The output format must change to report separate range and pre-existing counts; no backward compatibility requirement is stated in the task, so accept the breaking change and communicate it clearly in release notes. _(assumption)_

</details>

