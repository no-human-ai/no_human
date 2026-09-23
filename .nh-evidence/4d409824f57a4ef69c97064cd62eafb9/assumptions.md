# Assumptions

_Harness-captured record for task `4d409824`, commit `ee58870d05aaa94fd061cd5694c34a8ea9abfd3b` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** When acceptance_criteria is null or empty in a grill "done" response, should it result in: (A) an empty list [], or (B) synthetic criteria like ["Implement: {title}"] (current force-finish behavior)? **A:** (A) an empty list []. The coercion in parse_grill_response is correct. The problem is not the empty list itself, but that it happens silently. A warning must make the silent failure visible, as issue #511 established for the URL intake path. _(assumption)_
- **Q:** Should both the normal-path code (parsing "done" responses) and force-finish-path code (parsing "question" responses) be modified to emit the warning and achieve identical behavior, or should modifications target only one path? **A:** Both paths should be modified. The done-path and force-finish-path code must emit identical warnings and produce identical acceptance_criteria structure to prevent drift. The acceptance criteria requirement that 'a test pins both in one place so they cannot drift apart again' mandates a unified approach across both branches. _(assumption)_

</details>

