# Assumptions

_Harness-captured record for task `6b89bb60`, commit `896e81ecacef957576f925320c1b24e8280a67d2` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 1 assumption made on your behalf — verify at review</summary>

- **Q:** Does hash_path contain its own subprocess.check_output() calls that read file paths from git, or does it only operate on filenames provided as arguments? **A:** hash_path only operates on filenames provided as arguments and does not contain its own subprocess.check_output() calls that read file paths from git. Fixing tracked_files() alone resolves both the --write and check paths. _(assumption)_

</details>

