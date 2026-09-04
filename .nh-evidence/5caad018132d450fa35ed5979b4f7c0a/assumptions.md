# Assumptions

_Harness-captured record for task `5caad018`, commit `36ed06c809323d333a5cb957ee97c33c020ef135` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 15 assumptions made on your behalf — verify at review</summary>

- **Q:** What is the location (directory/file path) in the current codebase where the Settings Workers panel UI component should be implemented? **A:** The Settings Workers panel should be implemented in the settings UI components directory, likely as a new component file in src/components/Settings/ or src/pages/Settings/ depending on the project's existing Settings module structure, following the naming convention of adjacent panels/sections. _(assumption)_
- **Q:** Do we have access to review the original private PR #744 to reference the intended implementation details and verify our rebuild against it? **A:** HUMAN-GATED: not self-answerable
- **Q:** How should 'worker count' be calculated or derived from hardware configuration—is it CPU core count, thread count, a custom algorithm, or something else? **A:** Worker count should be derived from navigator.hardwareConcurrency (available CPU core count), typically capped at the actual logical processor count detected by the browser, with an optional adjustment mechanism (multiplier or fixed offset) if the project has custom performance tuning requirements. _(assumption)_
- **Q:** What is the expected integration pattern with the existing Settings/configuration system (e.g., state management approach, props structure, data flow)? **A:** The Settings Workers panel should integrate with the existing Settings system via the established state management pattern (context provider or store dispatch), with the panel as a controlled component receiving current worker config from parent Settings state and dispatching updates through the same action/callback mechanism used by other setting controls. _(assumption)_
- **Q:** What should the Settings Workers panel visually display and what is the expected UI layout or design specification? **A:** The panel should display the detected hardware core count as a read-only informational element, show the current/configured worker count (either as a number, slider, or select control), and include explanatory text describing how worker count affects performance, with the layout consistent with other settings panels in the application. _(assumption)_
- Settings Workers panel UI will be implemented in the existing Settings components directory (e.g., src/components/Settings/ or equivalent pattern), following established component structure and conventions of the current codebase
- Worker count derivation logic reuses existing hardware detection APIs already present in the codebase; only the Settings UI wrapper component is new
- RELEASE_MANIFEST updates follow the existing pinning format and conventions already established in the repository for this type of feature
- The private PR #744 specification text is the authoritative source of requirements; the stranded private branch code itself will not be examined or cherry-picked
- Settings Workers panel integrates with the existing Settings state/configuration management system used by other Settings options in the codebase
- Worker count default is the number of logical CPU cores reported by the existing hardware detection module
- All existing tests (baseline 10408+) must pass with zero regression; new unit tests for the Settings panel component are added following existing test patterns
- UI component styling and framework matches the existing Settings panels in the current codebase without exception
- Verification of current file:line references is performed by inspection of the current public main branch as of 2026-09-04 before any edits
- Feature implementation is complete when all acceptance criteria are satisfied and code aligns with the original private PR #744 specification without touching docs/superpowers/ or private-only tooling

</details>

