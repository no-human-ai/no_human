# Assumptions

_Harness-captured record for task `19dd94b3`, commit `71c7204ad1dbcd59b1700b8f3e528a5b4705c716` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

> ⚠️ **Unresolved:** max_attempts (3) reached without a passing, untampered change. The attempt trail is in this blocker's evidence and 'what I tried'.

> ⚠️ **Open question:** The agent could not complete this within bounds. Refine the task, split it, or advise an approach.

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** What does the wizard currently do when an onboarding endpoint fails (network error, server error, timeout)—does it block further progress, show an error with retry, allow offline mode, or behave another way? **A:** This is an explicit open question in the task marked as the decision point for whether the feature is shippable. Without code exploration, a reasonable assumption: the wizard shows an error message and offers retry when any onboarding endpoint fails, since other required steps must also complete. However, the task explicitly warns against guessing this—the new email registration step must match wh _(assumption)_
- **Q:** Where should the registered email address be persisted—client-side only (localStorage), server-side in a database, or both? **A:** HUMAN-GATED: not self-answerable
- **Q:** How strict should the email validation be—just non-empty with an '@' symbol, a regex pattern, or RFC 5322 validation—and what error message should a rejected email show? **A:** Use practical email validation: non-empty, must contain '@', must have content before and after the '@'. Show error message 'Please enter a valid email address' for rejection. This avoids the UX friction of strict RFC 5322 validation while filtering obviously malformed addresses that the send path would reject anyway. _(assumption)_
- **Q:** Which email template should the send path call—one of the platform-specific templates (macOS download, Linux download, Windows waitlist) or a generic template? Should the app auto-detect the OS or ask the user? **A:** Auto-detect the user's operating system and use the corresponding template (macOS download, Linux download, or Windows waitlist). OS detection is appropriate since the app distributes platform-specific installers. If detection fails, default to macOS as the primary target. The template selection should live in the send module behind the seam, not in the onboarding UI. _(assumption)_

</details>

