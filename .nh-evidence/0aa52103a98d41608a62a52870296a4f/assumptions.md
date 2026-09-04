# Assumptions

_Harness-captured record for task `0aa52103`, commit `fbf486b8e16419edb95cf16084979732f66d1b26` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** When the server probe succeeds after network failures, should we re-invoke ALL fetches in the current step, or only the specific fetch(es) that originally failed? **A:** Re-invoke ALL fetches in the current step when the probe succeeds. This ensures the wizard has current data after reconnection, avoids tracking which individual fetches failed, and aligns with standard reconnection behavior. _(assumption)_
- **Q:** Should the 3-second probe interval remain fixed, or escalate (e.g., exponential backoff: 3s, 6s, 12s…)? The spec says 'backoff' but also 'every 3s', creating ambiguity. **A:** Use a fixed 3-second probe interval. The task spec explicitly states 'every 3s'; the term 'backoff' here refers to the delay before starting retries rather than escalating intervals. The endpoint is cheap, and a fixed cadence is simpler to test and reason about. _(assumption)_
- **Q:** Should probing stop after N failed attempts, or retry indefinitely (with the chosen interval) until the server responds? **A:** Retry indefinitely (or until the user navigates away) with no hard stop limit. The user is already stuck in the wizard; the most helpful behavior is persistent reconnection attempts until the server recovers. User frustration is minimized by never giving up without explicit user dismissal. _(assumption)_
- **Q:** The spec references 'like /api/version' as the health check endpoint — is /api/version the actual endpoint, or should I verify the correct health endpoint exists and is performant on your backend? **A:** HUMAN-GATED: not self-answerable

</details>

