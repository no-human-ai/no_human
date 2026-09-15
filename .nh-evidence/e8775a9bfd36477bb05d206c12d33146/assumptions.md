# Assumptions

_Harness-captured record for task `e8775a9b`, commit `4c06c3c2caa4b692cf5b943340586d39bf9a6a07` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

> ⚠️ **Unresolved:** You've hit your session limit · resets 1:10am (Asia/Jerusalem) ('personal' subscription)

<details><summary>⚠️ 1 assumption made on your behalf — verify at review</summary>

- **Q:** For write-side operations (`gh api -X POST/PATCH`, `glab api --method PUT/POST` for PR comments), how should the subprocess timeout in `_run_cli` be handled? Should the timeout apply uniformly to all calls with stated rationale, or should write-side operations be exempted or handled differently? Please explain your choice, particularly regarding the risk of partial state when requests succeed serv **A:** Apply the timeout uniformly to all _run_cli calls, including write-side operations (gh api -X POST/PATCH, glab api --method PUT/POST). Rationale: (1) Preventing scheduler stalls takes absolute precedence—a stalled scheduler stops ALL work, including retries, making it worse than a single timed-out write; (2) tasks that timeout remain parked in the queue and are retried on the next tick, providing _(assumption)_
- Acceptance criteria were auto-sharpened during intake; originals: No subprocess the wake watcher runs can block without a bound, demonstrated by a test that hangs the runner and fails before the fix.; The scheduler's dispatch loop cannot be stalled by the wake watcher regardless of how many tasks are parked: a test with N parked tasks each hitting the slow path shows tick duration does not grow without bound in N.; Both properties are pinned by tests that go RED when the bound is removed, shown by running them against the unfixed code rather than asserted in prose.; The decision taken for the write-side callers (gh api POST/PATCH, glab api PUT/POST) is stated with its reason, since a client timeout there abandons the response and not the server-side effect.; No comment or docstring claims a bound the code does not enforce; if a per-call bound is added, nothing may describe it as bounding the tick.

</details>

