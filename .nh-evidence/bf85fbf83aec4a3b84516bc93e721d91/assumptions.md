# Assumptions

_Harness-captured record for task `bf85fbf8`, commit `6f9723d6882d461e9d79da7fc68744dcfebd0ced` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** Do we have write access to the no_human repository at https://github.com/no-human-ai/no_human, or is the repository already locally cloned with git credentials configured? **A:** HUMAN-GATED: not self-answerable
- **Q:** Which of these documentation files currently exist in the repository: docs/WINDOWS.md, docs/LINUX.md, and docs/INSTALLER.md? **A:** Each documentation file should list only its platform-specific release asset file: latest.yml for docs/WINDOWS.md, latest-linux.yml for docs/LINUX.md, and docs/INSTALLER.md should list both files since it serves as the platform-neutral installer guide covering all platforms. This pattern follows the existing documentation structure where each platform-specific file (WINDOWS.md and LINUX.md) owns i _(repo-evidence)_
- **Q:** For the documentation updates describing release assets: should each doc list only its platform-specific file (latest.yml for WINDOWS.md, latest-linux.yml for LINUX.md, and both for INSTALLER.md), or should all three documents list both latest.yml and latest-linux.yml? **A:** RELEASE_MANIFEST.txt exists and is located at the repository root. Format: one row per file with the pattern `<sha256>  <path>` (SHA256 hash, two spaces, then file path). The file includes a header comment explaining: 'One row per file: `<sha256>  <path>`. CI verifies a checkout against this file with scripts/check_release_manifest.py, so every content change is visible here, like a lockfile. Rows _(repo-evidence)_
- **Q:** Does RELEASE_MANIFEST.txt exist in the repository, and if so, what is its current format (e.g., newline-separated filenames, CSV, YAML)? **A:** (unanswered)
- Acceptance criteria were auto-sharpened during intake; originals: the windows-x64 CI release artefact contains latest.yml and the linux one latest-linux.yml, both emitted by electron-builder (not hand-written); ci.yml comments and the WINDOWS/LINUX/INSTALLER docs state the real contract: yml ships so the in-app check can report a newer version; nhCanAutoUpdate stays false and no desktop code changes

</details>

