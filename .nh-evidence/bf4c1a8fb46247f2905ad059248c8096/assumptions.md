# Assumptions

_Harness-captured record for task `bf4c1a8f`, commit `92c525839875c845bc8d8a0dba6fc76c3a24da0d` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** Which sqlite3 exceptions should trigger a retry of the lease write—only 'database is locked' errors or also other transient OperationalError messages? The task warns against over-widening the classifier. **A:** Only 'database is locked' errors should trigger retry. The task explicitly warns against over-widening the classifier ('Do not widen the transient-error classifier to catch every OperationalError') and identifies the specific error that causes the wedge. Other transient OperationalError messages should fail closed immediately, as the retry budget is designed specifically for contention during the _(assumption)_
- **Q:** Are there code surfaces consuming paused_reason beyond the CLI and two web files mentioned (drainChip.js, App.jsx)—for example, API endpoints, board components, or other services? The task marks this as an OPEN QUESTION and requires complete enumeration before introducing the new value. **A:** The task marks this as OPEN QUESTION requiring enumeration before introducing the new paused_reason value. Without tool access, reasonable candidates beyond CLI and web (drainChip.js, App.jsx) would include: API endpoints that serialize QueueHealth to clients, test fixtures that validate paused_reason values, monitoring or logging systems that filter on paused_reason, and possibly native agent com _(assumption)_

</details>

