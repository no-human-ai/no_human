# Verifiers

_Harness-captured record for task `0aa52103`, commit `fbf486b8e16419edb95cf16084979732f66d1b26` — not model-authored: no_human wrote this file from the deterministic verifier rules selected for this commit's files. It records what the gate produced; it is not a verdict of the model that wrote the code._

```json
[
  {
    "comment": "The three new .ob-offline-* rules use only existing CSS vars (--state-failed, --surface-1, --text-hi, --text-muted), all defined in both :root (dark) and [data-theme=\"light\"]; no new hex/rgb/hsl literal is introduced (color-mix and `transparent` are not literals), and the JSX changes add only class names, no inline color styles.",
    "evidence": ".ob-offline-banner { background: color-mix(in oklab, var(--state-failed) 12%, var(--surface-1)); border: 1px solid var(--state-failed); color: var(--text-hi); } .ob-offline-hint { color: var(--text-muted); } .ob-offline-retry { border: 1px solid var(--state-failed); background: transparent; color: var(--state-failed); }",
    "file": "web/src/styles.css",
    "files_checked": [
      "web/src/Onboarding.jsx",
      "web/src/api.js",
      "web/src/offlineRetry.js",
      "web/src/onboardingOffline.test.mjs",
      "web/src/styles.css"
    ],
    "line": 6046,
    "no_verdict": false,
    "passed": true,
    "severity": "medium",
    "tokens_used": 803,
    "unavailable": false,
    "verifier_id": "board-uses-theme-tokens"
  }
]
```
