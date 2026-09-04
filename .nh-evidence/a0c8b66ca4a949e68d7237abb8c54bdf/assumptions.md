# Assumptions

_Harness-captured record for task `a0c8b66c`, commit `d1ca6e9a37c0f1197775180fac270a3ca25b8162` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Which repository should I clone and modify? (The task references `.github/workflows/*.yml` but does not specify the repository URL or path.) **A:** HUMAN-GATED: not self-answerable
- **Q:** What is the filesystem path to the `uv` cache directory on the Linux runner? (Standard is `~/.cache/uv` on Linux, but may be customized.) **A:** ~/.cache/uv _(assumption)_
- **Q:** For the cache action, should the key be only the `uv.lock` hash, or should it include `restore-keys` patterns for partial matches on uv.lock? **A:** Include restore-keys patterns for partial matches on uv.lock (e.g., restore-keys: ['uv-Linux-'] or similar prefix) to improve cache hit rates when the lock file changes incrementally, avoiding cold-start penalties on every lock update _(assumption)_

</details>

