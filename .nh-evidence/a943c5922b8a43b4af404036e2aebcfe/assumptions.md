# Assumptions

_Harness-captured record for task `a943c592`, commit `0e06b38d585bc8d6bd3f9e02b6dc6415e5f76613` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** Does the acceptance criterion requiring verify_public_history.py not run synchronously during the land's push mandate the out-of-band approach (direction 1), or should implementation direction be chosen based on measurement of all three options? **A:** No, the acceptance criterion does not mandate direction 1. It specifies the observable outcome (no synchronous execution in report mode) but remains agnostic to implementation path. All three directions—out-of-band, scoped on-push, or synchronous with visibility—could satisfy the criterion. Implementation direction should be chosen based on measurement of trade-offs among all three, not predetermi _(assumption)_
- **Q:** For out-of-band scanning, should it execute immediately after the push completes, on a periodic schedule, or support both? **A:** Should be determined by operational requirements and measurement. Immediate post-push execution is the reasonable default, providing timely user feedback for a ~4-minute scan. Periodic scheduling should be evaluated as a secondary option if operational overhead or resource contention makes immediate execution untenable. Hybrid support can be future-extensible. _(assumption)_
- **Q:** Should violations detected by out-of-band scanning block future operations (e.g., prevent the next land) or only be logged for review? **A:** Should respect NH_GUARD_MODE setting: violations detected in report mode (current default) are logged for review without blocking. In enforce mode (T2 direction), violations should block subsequent lands to maintain the gate's intended enforcement function. The mode controls whether blocking is active, not whether scanning occurs. _(assumption)_
- **Q:** Are there constraints on external systems or dependencies for out-of-band scanning (e.g., no external job queues, no new persistence layers)? **A:** HUMAN-GATED: not self-answerable

</details>

