# Assumptions

_Harness-captured record for task `f2dea6f3`, commit `6a10ae5cd022295b3303c7440c0628c6ffd69377` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** Does the README contain exactly these four bullet points in this order: 'A plan before any code', 'An adversarial review', 'A tamper guard', 'Proof the fix fixed the bug'? And what heading or section marker identifies the 'install block' that comes after where the new copy should be inserted? **A:** Assume the README contains the four bullet points in the specified order. The install block is likely identified by a heading such as '## Installation', '## Install', or '## Getting Started'. _(assumption)_
- **Q:** What is the exact JSON schema for the snapshot file under scripts/? What should the key names and structure be? (E.g., are the three figures named 'reviewer_rejections', 'tamper_stops', 'refused_proofs', or something else; how should the window and source_tag be nested?) **A:** Assume a JSON schema with keys for the three figures (e.g., 'reviewer_rejections', 'tamper_stops', 'refused_proofs'), the date window (e.g., 'window' with 'start' and 'end', or separate 'window_start'/'window_end' keys), and the source tag (e.g., 'source_tag' with value 'metrics-2026-09'). _(assumption)_
- **Q:** What are the database table and column names? Specifically: (1) which table holds attempts, (2) exact column names for review_passed, task_kind, failure_reason, and repo_path, and (3) the structure of the test_results JSON column—is tamper_flag a direct boolean field within it? **A:** HUMAN-GATED: not self-answerable
- **Q:** Given the test must pass on machines without ~/.no_human directory present, how should test_readme_claims.py validate the recount script? Should it use a fixture database with known test data, mock the database, or take a different approach? **A:** HUMAN-GATED: not self-answerable

</details>

