# Assumptions

_Harness-captured record for task `05a1fa28`, commit `522703d96728f91723afdfed6d0cafd5c413207b` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Does 'dated weekly reset' refer specifically to reset messages that explicitly mention weekly recurrence (e.g., 'every Monday at 10am'), or does it include any reset message with an explicit date/time, regardless of whether recurrence is specified? **A:** Includes any reset message with an explicit date/time, regardless of whether recurrence is explicitly mentioned. The term 'weekly reset' distinguishes the reset type (quota resets) from session-limit resets, not a message-format requirement. The problem example 'resets Sep 8 at 10am (Europe/London)' demonstrates a dated reset without explicit recurrence language. _(assumption)_
- **Q:** When the acceptance criteria require 'exactly ONE task probes the quota', should this apply only to the first probe after the reset time is reached, or should all probes for that reset going forward send a single task? **A:** Should apply to all probes for that reset going forward (persistent behavior per probe cycle). Each probe after the reset time is reached sends exactly one task to check quota status for the active profile, rather than dispatching max_workers tasks. This persists for all retry attempts, not just the first probe. _(assumption)_
- **Q:** How should the system identify the 'active profile' for isolation — from a configuration setting, from request context, from auth state, or another mechanism? **A:** HUMAN-GATED: not self-answerable

</details>

