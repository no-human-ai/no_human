# Assumptions

_Harness-captured record for task `0c7cc4b2`, commit `1e2c83a261453936fb991015bf25878813c7032f` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

> ⚠️ **Unresolved:** max_attempts (3) reached without a passing, untampered change. The attempt trail is in this blocker's evidence and 'what I tried'.

> ⚠️ **Open question:** The agent could not complete this within bounds. Refine the task, split it, or advise an approach.

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Does the scope include the desktop application code (0.2.2 on macOS), or only the web UI in web/src/? **A:** Scope is primarily the web UI (web/src/), since the confirmed vulnerability chain traces through web/src/api.js (fetching /api/profiles), web/src/telemetry.js (PostHog configuration), and web/src/replayScrub.js (exclusion mechanism). Desktop app should be included only if it also sends telemetry to PostHog with the same body-capture configuration; verify whether it uses the same analytics backend _(assumption)_
- **Q:** Do you have access to (a) a running instance of this application with PostHog session recording connected, and (b) credentials to the PostHog account to inspect both live and historical recorded sessions? **A:** HUMAN-GATED: not self-answerable
- **Q:** For the default-deny requirement, should this be achieved through a PostHog configuration change (immediate but potentially breaking for all endpoints), or through comprehensive code-level API enumeration followed by per-endpoint exclusion rules? **A:** Comprehensive code-level API enumeration with allowlist-based masking rules. The task description explicitly rejects the per-path denylist approach (which missed this endpoint) and advocates for fail-closed posture. Enumerate every endpoint the UI calls, classify each for sensitive data, and explicitly allow bodies only for known-safe endpoints. Whether the allowlist is enforced at PostHog configu _(assumption)_

</details>

