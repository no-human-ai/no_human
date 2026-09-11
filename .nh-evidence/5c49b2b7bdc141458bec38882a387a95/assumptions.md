# Assumptions

_Harness-captured record for task `5c49b2b7`, commit `0e1470d97231e5fa24454df8bc6d0677d13ecbc0` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 2 assumptions made on your behalf — verify at review</summary>

- **Q:** What is the exact function signature / call-site change expected for threading the session root into the guard (e.g., new parameter name on `venv_install_guard.evaluate` or the hook builder), and should both coder backends be updated in this same task or is one the reference implementation? **A:** Add an explicit `session_root` (or `worktree_root`) parameter to `venv_install_guard.evaluate` (and the hook-builder function that constructs the guard callback), defaulting to None to preserve a discovery fallback for unknown callers. Both coder backends should be updated in this task since the description states both already capture `cwd` once per session when building their guard hook and the s _(assumption)_
- **Q:** Should the discovery fallback (for a caller supplying no root) be implemented in this task, or is the fix limited to the explicit-root path with discovery removed/left as a documented TODO? **A:** Discovery should remain in this task as a fallback (not removed), but hardened per the description's requirements: refuse config.worktree_root(config) and all of its ancestors (not a hard-coded literal), and probe the .git marker with os.lstat so a dangling symlink counts as present. The task explicitly says 'IF DISCOVERY MUST REMAIN as a fallback... it must refuse... AND EVERY ANCESTOR... and it _(assumption)_

</details>

