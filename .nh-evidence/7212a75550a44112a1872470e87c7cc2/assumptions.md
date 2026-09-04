# Assumptions

_Harness-captured record for task `7212a755`, commit `d23384dfc8e712e45a8b96cd30077431a3e0a695` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 14 assumptions made on your behalf — verify at review</summary>

- **Q:** Which repository contains the Models Settings pane code? The task references 'public main' branch but does not name the repository or provide a URL. **A:** HUMAN-GATED: not self-answerable
- **Q:** The task directs me to 'verify each file:line reference against the current tree,' but does not provide these original references. Since the codebase has moved, how should I identify current file paths? Should I access the original private PR #788 to extract references, search the current repository, or will you provide a file mapping? **A:** HUMAN-GATED: not self-answerable
- **Q:** What version and dependency changes should RELEASE_MANIFEST include? (Semantic version bump rule, which dependencies to pin, specific target versions) **A:** Semantic version bump: patch increment (Z in X.Y.Z) for bug-fix release. Pin the current versions of all direct dependencies used by Models Settings pane UI (typically React, state management, and UI component libraries in use). Target versions should match what is currently passing the baseline test suite (minimum 10449 tests). If RELEASE_MANIFEST uses lock-file references, update to current comm _(assumption)_
- **Q:** How should I run the baseline test suite to verify 10449+ tests pass? (Which test command, test framework, environment setup, or CI integration?) **A:** HUMAN-GATED: not self-answerable
- The Models Settings pane component is located in the current public main branch codebase (agent will search for it rather than assume a specific path from the private PR).
- The 'row-note' feature is an editable text input field associated with model rows that should persist to the app's state management system or local storage, with changes reflecting immediately in the UI without full app restart.
- The 'Reset/re-pick' button restores model configurations to default state and updates the UI immediately by dispatching state changes through the existing state management pattern (Redux, hooks context, or equivalent).
- The root cause of 'silently inert' behavior is that state mutations are not properly triggering component re-renders; the fix requires ensuring immutable state updates and proper event handler wiring.
- Persistence of user changes means data survives across component re-mounts within the current session and maintains consistency with the app's authoritative state store.
- RELEASE_MANIFEST updates should follow semantic versioning (patch bump) with dependency version pins consistent with the rest of the codebase.
- The baseline test suite can be run via the standard project test command; 'minimum 10449 tests' is the pass threshold, not a target count.
- Implementation should adopt existing code patterns for state management, component structure, and testing present in the current public main branch.
- File paths and component references must be validated by searching the current codebase; no paths from the private PR should be assumed to be current.
- No file modifications in `docs/superpowers/` or any private-only tooling directories; implementation is strictly confined to the Models Settings feature and RELEASE_MANIFEST.

</details>

