# Assumptions

_Harness-captured record for task `f2cd85f8`, commit `61d22ca4a9cca56c8affe7aff3f799800bc8c9f7` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** How should synchronous git subprocesses (ls-remote, fetch) be scheduled within the PostToolUse hook to avoid blocking the event loop? **A:** Use asyncio.to_thread() (Python 3.9+) or loop.run_in_executor() with ThreadPoolExecutor to schedule synchronous git subprocesses. If the PostToolUse hook is async, wrap the sync subprocess call in to_thread() and await it; this moves execution to a thread pool while keeping the event loop free. Error handling should catch exceptions in the thread and propagate them through the returned Task so fai _(assumption)_
- **Q:** When checking whether the claimed commit exists on sibling branches, should the guard fetch fresh remote refs, rely on cached local tracking branches, or use another approach? **A:** HUMAN-GATED: not self-answerable
- **Q:** Is `Orchestrator._run_attempt` running in an async context, or is it synchronous? **A:** Orchestrator._run_attempt is most likely synchronous or contains a mix of sync and async code paths. The extracted decision function should be synchronous (blocking I/O for git operations is standard), but delivery's call site may need wrapping with asyncio.run() or to_thread() if _run_attempt itself is called from an async context. The mid-attempt guard's call can be direct synchronous if the Pos _(assumption)_
- **Q:** Is the `no-human/4135165f-*` branch head 4e597d53 still available in the repo? **A:** HUMAN-GATED: not self-answerable

</details>

