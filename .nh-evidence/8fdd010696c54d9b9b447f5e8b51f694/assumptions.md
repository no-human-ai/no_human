# Assumptions

_Harness-captured record for task `8fdd0106`, commit `22c805c76ceb4b6599140a761b3950b3a76c41e3` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 14 assumptions made on your behalf — verify at review</summary>

- **Q:** Where should we obtain the three sourced legal quotes with source URLs and ISO-formatted fetch dates? Are these in private PR #862 (readable for reference), current external compliance documents we should research, or provided separately? **A:** HUMAN-GATED: not self-answerable
- **Q:** Is CLAUDE.md constraint #6 accessible on the current public main, and what specific section title and structure does it prescribe for the Codex section? **A:** HUMAN-GATED: not self-answerable
- **Q:** Which specific behavior, use case, or compliance rule is 'the withdrawn prohibition' mentioned in the acceptance criteria? What was prohibited and what changed to withdraw it? **A:** The withdrawn prohibition likely refers to a previously-disallowed integration pattern or use case related to Codex backend authentication (specific nature to be determined by cross-referencing issue #8338 or the original private PR discussions), which represents a documented shift from a prohibited status to a permitted/withdrawn-prohibition status. This serves as a compliance narrative element s _(assumption)_
- **Q:** For the authentication modes documentation: should we list all auth modes in the Codex/backend codebase, or only certain categories (e.g., production-approved, excluding experimental/deprecated)? **A:** Document all authentication modes supported in the current codebase without categorical filtering. The acceptance criteria explicitly require listing and describing 'all authentication modes supported in the current codebase,' which means comprehensiveness without excluding experimental, deprecated, or non-production categories unless those categories themselves are absent from the current impleme _(assumption)_
- Legal source quotes will be sourced from issue #8338 discussion, code documentation, or compliance references within the repository; if not found in the codebase, a structured placeholder marked for legal review will be created.
- Each of the three required source quotes will include an ISO 8601-formatted fetch date (YYYY-MM-DD) indicating when the source was reviewed.
- The 'withdrawn prohibition' refers to a previous restriction on Codex usage in CI/CD contexts that has been removed or changed in status; the agent will research git history and issue tracking to identify and document this status change.
- CI/CD limitations section will describe known Codex integration constraints and reference issue #8338 as addressing them partially, not as a complete resolution.
- Supported authentication modes will be determined by examining the current Codex implementation in the codebase; only modes that have code evidence will be documented; no theoretical modes will be assumed.
- Agent will verify through codebase search that ~/.codex/auth.json is never read, referenced, or loaded by Codex; this finding will be stated explicitly in the documentation.
- The closing statement will explicitly note that legal implications remain unsettled and require professional legal review; no definitive legal claims or assertions of settled law will be made.
- RED-first test implementation: test file will be written such that it initially fails on the current main branch (because the Codex section does not yet exist), then documentation will be added to make the test pass.
- Only docs/INTEGRATIONS_LEGAL.md, test files, and RELEASE_MANIFEST will be modified; no functional code changes will be introduced; docs/superpowers/ and private-only export tooling will not be touched.
- Agent will verify all file paths and references exist in the current codebase tree before making any edits; file locations may have changed from the original private branch.

</details>

