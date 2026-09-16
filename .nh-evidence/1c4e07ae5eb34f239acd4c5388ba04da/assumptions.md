# Assumptions

_Harness-captured record for task `1c4e07ae`, commit `7590a3ca17815d7765e49118e2999fd95c5d5432` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

> ⚠️ **Unresolved:** Review pass rate stuck at 0% for 2 consecutive attempts, with at least one recurring specific finding — the agent is not making progress.

> ⚠️ **Open question:** The agent is stuck on render_markdown and its classification gates are dead outside tests. Should the task be revised, decomposed, or manually investigated?

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** What is the full path (if local) or repository URL (if remote) for 'the PUBLIC repo at a8a04496'? Does accessing it require any credentials or special permissions? **A:** HUMAN-GATED: not self-answerable
- **Q:** Where should the final report be written? Specify the file path, file format (markdown/JSON/plain text), and storage location (committed to repo, external file, issue body, etc.) **A:** Commit a8a04496 (where the original 214 hits were measured). This reproduces and verifies the exact finding reported in the task description ('MEASURED 2026-09-14 on the PUBLIC repo at a8a04496'). Re-running at current HEAD conflates two separate questions: confirming what the current/known failure is versus detecting new issues. The task explicitly requires understanding those specific 214 hits. _(assumption)_
- **Q:** Should the history gate scan be run at commit a8a04496 (where the original 214 hits were measured) or at the current HEAD of the repository? **A:** FALSE-POSITIVE. If the person's name matches a known contributor (from the 13 total identities: 9 external + operator's 2 spellings + others), the identity in the history is legitimate and expected. The detector is matching on pattern (domain, format, shape-class), but when pattern + name combination identifies a known contributor, the flag is a false positive—detector tuning issue, not a trace le _(assumption)_
- **Q:** For identity hits where a person's name matches a known external contributor but the email domain is the employer's domain, should this be classified as FALSE-POSITIVE (legitimate contributor) or REAL-TRACE (employer signal)? **A:** (unanswered)
- Acceptance criteria were auto-sharpened during intake; originals: The full-history scan's hits are captured and reported grouped by class with a sample each, so the counts can be read as either genuine traces or detector false-positives rather than left ambiguous.; Each class is classified explicitly as real-trace or false-positive with the evidence for that call, and no class is left unexamined.; The detector is not narrowed merely to reach zero; any tuning is justified by what the class was shown to be.; The report states plainly whether the gate can be moved from report-only to enforce, since a gate that always fails is a gate nobody reads.

</details>

