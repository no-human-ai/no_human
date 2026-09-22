# Assumptions

_Harness-captured record for task `65b30513`, commit `1949165c0907449fccd58c75d7dbdfef37ba510c` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Where should we search for existing git-path decoders in the repository: only `core.enc`, the entire `core` package, or the whole codebase? **A:** Search the entire core package first. Encoding/decoding utilities for paths would naturally belong in core infrastructure; starting with only core.enc risks missing related utilities elsewhere in that module, while expanding to the whole codebase wastes search scope if the decoder exists in core. A senior engineer would prioritize the core package as the most likely home for canonical git-path han _(assumption)_
- **Q:** What test infrastructure is available: Can we create temporary files with non-ASCII names for testing, and what approach should be taken for InspectionTracker integration testing (automated test code vs. manual validation)? **A:** HUMAN-GATED: not self-answerable
- **Q:** Should the fix handle correction of already-cached mangled paths from reviews processed before this change, or only apply prospectively to new reviews? **A:** HUMAN-GATED: not self-answerable

</details>

