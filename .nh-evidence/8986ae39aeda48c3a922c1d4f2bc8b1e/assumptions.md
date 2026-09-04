# Assumptions

_Harness-captured record for task `8986ae39`, commit `f78a3fc782343667d9cae8cb8f53e8dbac0c43db` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** If npm ci or npm run build fails with an error (syntax error, missing dependency, etc.), should nh start fail completely and refuse to start, or should it warn loudly and attempt to serve the existing web/dist (accepting stale/broken state as degraded)? **A:** npm ci or npm run build failures should warn loudly but not prevent startup; attempt to serve the existing web/dist (even if stale) as a degraded state. This maintains developer workflow continuity and parallels the existing missing-dist warning behavior (~:5952), while making the problem visible. A build error is painful but less harmful than a hard crash that blocks all development. _(assumption)_
- **Q:** When web/dist is completely missing (deleted, never built) AND NH_NO_AUTO_BUILD=1 is set: should we force a rebuild anyway since there's nothing to serve, or should we respect the env var and attempt startup (serving an error)? **A:** Respect the NH_NO_AUTO_BUILD=1 env var and attempt startup with the loud warning fallback, even when dist is completely missing. The env var is explicitly set for fast restarts; overriding it defeats the user's intent. The loud warning will direct the operator to manually rebuild, making the missing state obvious without forcing a rebuild they opted out of. _(assumption)_
- **Q:** For the staleness check, should we monitor all files recursively under web/src, or only code/config files (.ts, .tsx, .js, .jsx, .json, vite.config.*, package.json)? Should package-lock.json or pnpm-lock.yaml changes also trigger rebuild? **A:** Monitor selectively: source files (.ts, .tsx, .js, .jsx), web/package.json, web/package-lock.json (or pnpm-lock.yaml), and vite.config.* files. Lock file changes DO trigger rebuild since they represent dependency version changes. Skip node_modules, .git, dist, and other non-source files. Use mtime of the newest file in this filtered set under web/src, compared against web/dist/index.html mtime. Th _(assumption)_

</details>

