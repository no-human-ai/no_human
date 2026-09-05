# Changelog

All notable changes to no_human. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- **The MCP bridge runs on the MCP SDK 2.x API, and the requirement is now
  `mcp>=2,<3`.** `intake/mcp_bridge.py` imports `mcp.server.mcpserver.MCPServer`
  instead of the `mcp.server.fastmcp.FastMCP` the SDK removed in 2.0.0, which
  lifts the `<2` cap 0.1.4 added as a hotfix. The cap stays one major up for
  the same reason: a fresh `pip install no-human` is the only lane that does
  not resolve through `uv.lock`, and `tests/test_mcp_dependency_bound.py` fails
  if the declared bound admits a major the bridge has not been ported to.
  Relocking moves `claude-agent-sdk` from 0.2.121 to 0.2.152, because 0.2.121
  itself pinned `mcp<2.0.0`. If you followed 0.1.4's workaround
  (`uvx --with "mcp<2" no-human mcp-serve`), drop the `--with`: it now
  conflicts with the declared bound. First contribution by @Siddh2024
  (public issue #16).

## [0.2.0] — 2026-09-05

First release developed entirely in the open on the public repository, including
the first external-contributor PRs.

### Security
- The board API enforces a loopback boundary (`api/local_boundary.py`): a
  request whose `Host` is not loopback is refused (400, DNS-rebinding defence),
  a cross-origin browser write to any state-changing route is refused (403),
  the CORS grant is exact-host loopback only, and the `/ws` handshake applies
  the same `Host`/`Origin` gate. Allowed hosts are loopback plus the
  configured `server.host`, which the `nh` CLI addresses the board by. At
  v0.1.9 only 11 of the 50 mutating routes had an origin check and CORS
  allowed every origin. The boundary is one middleware dispatch, on the order
  of 100 µs per request.
- `land_task` refuses in-process when the calling process carries the
  agent-session mark, so a marked agent cannot land a PR by driving the
  landing module directly.
- The Jenkins console-log excerpt sends its SSO credentials only to the
  configured `ci_gate.jenkins_controller`, over `https` with TLS verified
  (`ci_gate.jenkins_ca_bundle` or the system store); it previously sent them
  to whatever host a failing check's `targetUrl` named, with verification off.
- Both coder backends strip the launcher's credential-shaped environment
  variables from the child process (`agent/child_env.py`); see
  docs/security.md §7 for what is removed and what that means inside the
  child.

### Added
- `ci_gate.jenkins_ca_bundle`: PEM path the CI-log fetch verifies the Jenkins
  TLS chain against (empty = system trust store).
- `nh verifiers list/add/check/propose`: inspect, additively author, and
  dry-run-select the repo's natural-language verifiers from the CLI, and turn
  a task's persisted review findings into candidate verifier YAML
  (`nh verifiers propose TASK_ID [--apply]`). None of the four make a model
  or network call.
- Subscription users see token usage (not dollar estimates) as their usage
  display on the board.
- A Settings ▸ Workers panel showing the worker count derived from hardware
  (`cpu_count`/`hardware_ceiling`) and where the limit comes from.
- Evidence ledger: every claim in a PR body links to its proof, and the body
  leads with the verdict and a per-kind "How I verified this".
- Telemetry (defaults unchanged) tags every event with its environment and a
  stable install id, and `task_failed` carries a privacy-safe CLOSED
  failure-category enum (`reason_category`) so failure patterns are visible
  without leaking content.
- A default UI walk runs at finalize when the coder writes no manifest;
  UI-evidence walks build the web UI from the worktree and run against a
  hermetic backend (throwaway HOME + isolated server), never the live config.
- Integration health probes (boot-time + scheduled) surface healthy/detail on
  the board; task cards show elapsed running time, amber past 2h.

### Changed
- Development now happens directly in this repository: changes land here as
  commits and pull requests rather than arriving through a periodic export.
- Approve runs the FULL test gate when the squash tree diverges from the
  attempt's tested tree, so a conflict-round tree cannot ship untested.
- Onboarding no longer has a DOCS step — wiki generation runs fully async after
  Launch and asks nothing of the user.
- The board serves a strict Content-Security-Policy; board Markdown is hardened
  against XSS with a dependency-regression guard.
- First external-contributor PRs landed: MCP bridge honoring server.host/port,
  board CSP form-action, regression tests, a cross-platform LF release
  manifest, docs index + integration-probe accuracy, and onboarding
  failure-output.
- The Linux-bundle CI job's dependency-install timeout was raised (5→15 min)
  with uv caching so the packaging gate stops timing out.

### Fixed
- Integration health: an enabled-but-unconfigured target (e.g. Microsoft Teams
  on a fresh install) reports neutral, not a red "Failing" chip; a configured
  target that genuinely fails still reports the failure.
- `nh start` no longer refuses to boot without a credential — a new user can
  reach the onboarding that sets the credential up; on a source checkout it
  rebuilds (or warns on) a stale `web/dist` so the current UI is served.
- The onboarding wizard survives a dead/restarting backend with a reconnect
  banner and retry instead of a bare "Failed to fetch".
- Repo-folder Search scans a user-typed root wherever it resolves (not only
  under home) and never misreports a refusal as "no git repositories there".
- The "Complete AI configuration" nudge no longer renders off-screen on phones.
- Models settings: the dead row-note and silently-inert Reset/re-pick are
  fixed, and a one-line reason shows under the backend picker instead of a raw
  AuthError paragraph.
- `GET /api/worker/status` no longer intermittently blocks 5–14 s.
- A verifier walled by quota/overload parks on quota-refresh instead of
  escalating to a human as an unknown failure.
- The "approved — merge pending" chip no longer sticks after a task leaves
  awaiting-approval.
- A board bound to a non-loopback interface (`nh start --host 0.0.0.0`) answers
  `400 bad_host` to a browser that addresses it by a LAN name or IP other than
  the configured `server.host`; set `server.host` to that name, or use an SSH
  tunnel or loopback port-forward.
- The CI-log excerpt logs a warning when SSO credentials are set but
  `ci_gate.jenkins_controller` is empty or not an https URL, instead of
  silently returning nothing; the fetch is scoped to the controller's host,
  port and path, not its host alone.

## [0.1.9] — 2026-09-01

The evidence release. PR bodies shrink to final results with the full receipts
in a per-attempt artifact; UI-touching tasks attach real screenshots and a walk
video; the second brain manages itself — screened learnings activate on a daily
cap with an audit trail, one Settings surface, and a kill switch; and the board
gets faster on large fleets.

### Added

- **Visual proof on UI PRs.** After tests pass on a task that touched `web/` or
  `desktop/`, the harness runs the browser walk and attaches screenshots (up to
  six) and a walk video via an evidence side branch that never reaches main or
  the export (`nh-evidence/<task-id>`, deleted when the task branch is cleaned).
- **Auto-managed learnings.** Harvested learnings that pass the existing
  screens (dedupe, PII, provenance, terms) activate automatically — capped per
  day (`learning.auto_activate_daily_cap`, default 10), audited in
  `learning_events` (including which trigger tags fired), auto-retired after 90
  days unused (auto-activated rows only), with Pause/Delete/Restore in the UI
  and a kill switch (`learning.auto_manage: false`) that restores the confirm
  queue exactly.
- **Second brain, one surface.** The pane lives in Settings only, opens with a
  plain explainer, shows each learning with its origin task and usage count,
  and keeps the operator's manual rules untouched. The sidebar "!" badge is its
  own click target that deep-links there and clears only once the pane has
  actually been seen.
- **Create-time feasibility.** The pre-flight hint now appears the moment a
  task is created (a status toast from the create response), not only while the
  task is still pending.
- **Board pagination.** `GET /api/tasks` accepts opt-in `?limit`/`?offset`
  (SQL-side, deterministic ordering); the no-params response is byte-identical
  to before.
- **Convergence early-abort.** An attempt that stops making progress (no file
  edit or real test run within a bounded window) is aborted early with an
  honest blocker and checkpoint instead of burning to the turn cap
  (`worker.abort_non_converging`, default on; corpus-anchored thresholds).

### Changed

- **PR bodies carry final results only.** One line per test layer from the
  final run, a pointer to the full verification log (attempt-scoped artifact,
  surfaced by `nh logs`), a 6,000-character budget enforced in code, and
  failure excerpts that show the first failing blocking layer's traceback.
- **Learning triggers match whole words.** The injection trigger no longer
  fires on substrings ("fact" inside "artefact"), and every injection audit
  row records the tags that fired.
- **Task detail payloads are lean.** The three heavy per-attempt fields
  (review checklist, verifier results, test results) moved behind a lazy
  details endpoint with client-side caching keyed on terminal attempt status.
- **Doc citations tolerate drift.** Line-anchored citations resolve by content
  within a window, with a mechanical re-anchor helper
  (`scripts/reanchor_citations.py`).

### Fixed

- **Inspector scroll drift.** The running-task inspector no longer drifts while
  live events append (observer armed once per drawer, compensation only for
  growth fully above the viewport, native scroll anchoring disabled on that one
  container) — pinned by a three-scenario end-to-end test.
- **Orphaned work that actually landed now reconciles** to a delivered state
  through a validated transition instead of being marked failed.
- **Human holds survive blocker updates,** and conflict/quota watchers respect
  them — a held task stays held until a human resumes it.
- **Reviewer verdict parsing takes the last well-formed verdict block,** so
  quoted untrusted content earlier in a transcript cannot preempt the real
  verdict.
- **The verifier module's docs match its wiring,** its retry window is pinned
  by tests, and stale distribution/backend docs were rewritten to the shipped
  reality.

## [0.1.8] — 2026-08-30

The pre-flight release. no_human now reads a new task before you start it: when
it looks large or spread across many surfaces, the board says so honestly — with
the band of how often similar tasks finished in one pass, never a promise — and
offers a one-click split into scoped sub-tasks you edit before creating. The
coder backend is selectable from the board, and the codex backend stops leaking
a file descriptor per attempt.

### Added

- **Pre-flight feasibility hint + 1-click split.** A new task that looks large or
  multi-surface gets an advisory card ("this looks large") showing the free
  signal — the share of similar tasks that finished in one pass — and a
  "Split into sub-tasks" button that drafts 2–8 scoped children you can edit
  before creating them. Purely advisory: nothing is gated, and the honest copy
  never claims the task will fail.
- **Coder-backend selector in the board.** The coder backend (Claude / Codex /
  local) is now selectable from Settings, not only from the CLI and API — a user
  who installed a local model can finally reach it from the UI.
- **Configurable worker count** in Settings, and a **Settings/onboarding pass**:
  clearer first-run/model panes and a redesigned running-task digest on top of
  the 0.1.7 running-task screen.

### Changed — cost accounting

- **Codex attempts are priced server-side** at OpenAI rates instead of being
  billed at Anthropic's flat rate, so per-task cost is honest across backends.
- **The tamper base is `origin/<base>...HEAD`**, so a sanctioned merge is no
  longer charged with main's already-landed edits.

### Fixed — reliability

- **The deployed-0.1.7 update stall is fixed** — installing a new build while a
  task is in flight no longer wedges the scheduler behind a dead sibling.
- **The codex backend no longer leaks a file descriptor per attempt** — the
  subprocess transport is closed on teardown.
- **Reviewer-worktree integrity guard** now catches `git update-index
  --assume-unchanged` on a tracked source file (the one thing it exists to
  catch), and no longer discards a verdict on a benign `.git/common/config`
  reserialization.
- **The safety guard peels `timeout`/`xargs`/`nice`/`stdbuf` wrappers**, so a
  wrapped destructive command in an already-denied compound classifies
  DESTRUCTIVE, not HYGIENE.
- **A quota wall in a verifier/supervisor call is classified QUOTA and parked**,
  not misreported as an infra failure; the tamper adjudicator gets one bounded
  retry on a mechanical failure instead of returning CANNOT_DECIDE; a failed
  `head_sha()` no longer clobbers a real "behind HEAD" verdict.
- **The Jenkins CI adapter reports an infra/access error as UNKNOWN, not
  FAILED.**

### Changed — privacy & telemetry

- **Dead-click and heatmap capture are pinned off** in the analytics init; the
  never-pre-tick proposals privacy guard now lives in Settings.

### Fixed — the review loop and PR body

- **A review-passed PR whose local merge is clean no longer escalates to a
  human just because GitHub's cached `mergeable` says CONFLICTING.** Every
  landing rewrites `RELEASE_MANIFEST.txt`/`EXPORT_CLASSIFICATION.txt`, which
  flips the forge's asynchronous `mergeable` to CONFLICTING for every other
  open PR even though a fresh local three-way merge against refs fetched the
  same tick finds no conflicting path at all. That contradiction used to
  defer and then escalate once it persisted past a bound (measured live: 4/4
  review-passed deliveries in one day needed a human takeover) — a *definite*
  empty local conflict-path set is now trusted outright, the round bookkeeping
  resets, and the task falls through to the approve path exactly as if the
  forge had reported MERGEABLE. A genuine source-code conflict is unaffected:
  a non-empty or unresolvable local result still opens a coder round or
  escalates at the bound, unchanged, and generated-file-only conflicts still
  route through the existing derived-artefact resolver from commit
  `26dc16248`.

### Fixed — tests & docs

- Handoff-family mutation-test gaps are covered (assertions made to bite), three
  order-dependent tests were made hermetic, and doc citations were re-anchored to
  symbols rather than line numbers.
- The Claude-credential paragraph was dropped from the README Install section.

## [0.1.7] — 2026-08-30

Server/fleet reliability + cost-observability work landed after the 0.1.6 cut
(068db272). Each change landed behind an independent fresh-context review.

### Fixed — the review loop and PR body

- **A finished PR that conflicts only in the derived export artefacts
  (`RELEASE_MANIFEST.txt` / `EXPORT_CLASSIFICATION.txt`) no longer escalates
  to a human.** The mechanical resolver now always runs `export_guard.py
  approve --prune` (even when the branch's own diff pins nothing new), so a
  stale pin for a path that stopped shipping on either side of the merge is
  dropped before `verify` runs instead of failing it with "pinned but not
  shipped". The `verify`-time count-reconcile backstop is no longer gated on
  the classification file itself having been part of the conflict, so a
  manifest-only conflict with a clean-merge count drift is reconciled the
  same way a classification-file conflict already was. A conflict touching
  any non-derived source file still escalates exactly as before.
- **The reviewer-worktree integrity guard no longer discards a verdict on a
  benign `.git/common/config` reserialization.** Concurrent writers of the
  shared common dir rewrite that file with the same *effective* keys (different
  whitespace/order); the guard byte-hashed it and threw away the review, forcing
  a wasted re-attempt — the dominant cause of discarded verdicts. It now
  adjudicates config by its effective key set (`git config --list --file`, no
  `--includes`, no hook/filter execution), still catching every executable-key
  change.
- **A merge-policy compute failure no longer drops the review checklist from the
  PR body** (evidence is gathered once, before the try; a NOT-COMPUTED row is
  disclosed).
- **A `|` in a value no longer truncates the PR body's two-column tables** — the
  CI/Verifiers/Merge-policy/Tests rows render through the pipe-safe cell helper.
- The `max-height:1080px` sidebar block no longer leaks into mobile.

### Added

- **The task-detail API surfaces the lifetime budget** (used/cap/remaining,
  cost-weighted — the exact metric BUDGET_EXHAUSTED kills on), so a human can see
  how close a task is to being killed, sourced from the gate's own helpers.

### Fixed — safety guard

- **A `find … -delete`/`-exec` wrapped in `timeout`/`xargs`/`nice`/`stdbuf` (and
  siblings) inside an already-denied compound now classifies DESTRUCTIVE, not
  HYGIENE**, on the codex post-hoc backend (e.g. `grep -rn X /Users && timeout 5
  find /Users -delete`). The scan-severity check's wrapper-stripping missed
  these because they take a non-flag operand or flag+value pair, not the bare
  flag `_strip_wrappers`'s recovery scan expects — the compound was already
  denied via the `grep` half, so only the terminating/non-terminating label was
  wrong. Fixed with a local `_peel_scan_wrappers` helper scoped to that one
  check; the shared `_WRAPPERS` set and its five other consumers are unchanged.

## [0.1.6] — 2026-08-28

Release range `8a55a92d3..c3cd200bf1`, each change landed behind an independent
fresh-context review. Themed highlights below; the git range is the full record.

### Changed — onboarding, rebuilt around real-user feedback

- **The setup wizard was reworked screen by screen** from a real-user walk plus
  an impeccable design pass. The welcome headline is the product's binding
  slogan verbatim; the repositories step lists recently-worked-on repos as
  quick-add cards, names exactly what it scanned, and — the headline fix —
  **those cards now show their profile result and the Prove-test-command panel
  in-flow**, so the core "prove a test command" trust step is reachable for the
  repos a user actually adds (previously only 30-days-untouched list rows had
  it). Continue on the repos step now profiles and registers the selected repos,
  so Launch no longer reports "Repos 0" for a repo you just added.
- **Projects are added one at a time** with their own repo picker; the step
  warns before discarding a typed-but-unadded project. The rules step groups
  proposals by project and states the scan's scope. The AI-history step no
  longer names a third-party IDE a Claude Code user never installed, and its
  privacy/scope copy meets the AA-contrast bar.
- **Usage insights (telemetry) is on by default and no longer asked about or
  shown** in onboarding or Settings (operator decision); `config.yaml`
  `telemetry.enabled: false` is the one opt-out. It stays inert until an
  ingestion endpoint is configured, which ships empty.
- Minimal one-repo path + clickable/jumpable steps + validate-at-the-step; a
  compact "Finish setup" sidebar entry that deep-links to real Settings panes
  and disappears when done.

### Fixed — Windows, reliability, and the review loop

- **Windows: no more empty console windows.** The app-spawned server ran with
  `detached`, which made Windows ignore `CREATE_NO_WINDOW`, so every
  `claude.exe`/`git.exe` grandchild opened its own visible empty console. Not
  detached on win32, plus a shared console-suppression helper at nh's own spawn
  sites (git, codex, test runner).
- **Desktop first-launch self-heals**: a slow first launch that had already
  started a healthy server no longer latches a credential-error page — it
  re-probes and recovers.
- **A `create_wiki_job`/`update_wiki_job` SQLite write-lock race** (the wiki
  feature committed without the shared write lock, unsafe under the concurrent
  workers) is fixed.
- Repo discovery no longer triggers a macOS access prompt during setup, treats
  `$HOME` itself as a depth-1 root, and dropped a second unbounded scanner.

### Added

- Wiki generation as a persisted background job with structured output.
- `verify-history --since <ref>` for an incremental public-export history scan.
- Per-phase task timestamps + a "ran vs wall" breakdown in the task drawer.
- **A local-model selector in Settings → Models**: the `local` coder backend
  and its model + server-URL are now configurable from the board (previously the
  backend could only be reached from the CLI/API, so an installed local server
  was unselectable in the UI). Coder role only — the reviewer, planner,
  supervisor and utility tiers stay on Claude per the pinned-roles constraint;
  the server's key never enters `config.yaml` (only the mode/URL), staying in
  `~/.no_human/.env`. The local base-URL validator was hardened to loopback or
  literal RFC1918 only, closing a metadata-endpoint (cloud IMDS) SSRF surface
  that the new write path would otherwise have exposed.
- **The first-run setup screen can also connect the OpenAI (codex) coder** — an
  optional, skippable section below the required Claude credential. ChatGPT
  subscription is instructions-only (you run `codex login`; no_human stores no
  OpenAI credential, per the codex auth constraint); the API-key path writes
  `OPENAI_API_KEY` to `~/.no_human/.env` only (never config.yaml). Claude stays
  required — it still pays for the pinned review/plan/supervisor roles.

## [0.1.5] — 2026-08-27

Release range `70b880cf5..8a55a92d3` (134 commits since 0.1.4, each landed
behind an independent fresh-context review). The themed entries below cover
the major changes; the full per-commit record is the git range.

### Fixed — reliability of the review loop and the coder backend

- **The reviewer-worktree integrity guard no longer discards a completed
  review verdict on a byte-identical rewrite of a shared `.git` file.** Its
  file identity was `st_mtime_ns`-based, so ordinary bookkeeping in the shared
  common dir read as "the reviewer wrote to the worktree it was judging" and
  threw away the verdict — measured discarding 14+ verdicts across a day.
  Identity is now content-hash based, and `common/HEAD` is adjudicated by
  content shape (an ordinary branch switch is clean; a detach or garbage is a
  violation).
- **A denied read-only filesystem scan (`find`, `grep -r`) no longer
  terminates the coder attempt.** Every `GuardDecision` denial site now
  classifies its severity explicitly; a scan that only reads is hygiene
  (non-terminating), while one that mutates or exfiltrates — a `find … -delete`,
  a pipe into a writer, a redirect to a file — stays destructive.
- **The Codex teardown is bounded on every await of its exit path**, so a
  stranded reap can no longer hold a worker slot forever.
- **`nh status` distinguishes a health probe that timed out from a server that
  is down** — a brief stall no longer renders as a definitive "server not
  running", while connection-refused stays definitive.

- **Electron moves to 43.4.1**, clearing all 18 open Electron advisories (4
  high) against the desktop bundle: the app now ships Chromium 150 instead of
  the 38-line's Chromium. The jump goes to the current supported line rather
  than to 39.8.10, the version the advisories name — Electron patches only its
  three newest majors, so 39 is already unsupported and would have to be
  redone. `extract-zip` (a high with no patch available) leaves the tree
  entirely: Electron 43 no longer depends on it. `tar` and `brace-expansion`
  are patched in the same lockfile pass, taking `npm audit` to zero.

### Security

- **The human gate is now also enforced at the ACT, one layer behind the
  lexical guard below — and this too is a cost-raising layer, not a closed
  door.** `agent/session_mark.py` stamps every subprocess the two coding
  backends launch (`ClaudeBackend._options()`, `CodexBackend._child_env()`)
  with an env-var mark (`NO_HUMAN_AGENT_SESSION`, plus a `_KIND` for
  diagnostics) that every descendant of that session inherits. The
  gate-ending acts then refuse a caller carrying it: `nh approve` and
  `nh merge-stack run` refuse before `_bootstrap` or the `Store` is touched,
  and an HTTP middleware refuses `POST /api/tasks/<id>/approve`,
  `/approve-landed`, `/finish-review` and `/shipped` when the request carries
  the `X-No-Human-Agent-Session` header (a marked CLI client sends it) or the
  server process is itself marked. Fail-closed: an unrecognized mark value
  counts as marked.

  **What shipped is a DROPPABLE mark that gate-ending acts refuse — not the
  positive capability an earlier draft of this entry promised.** That draft
  said the routes would "require something an agent session structurally
  lacks"; the opposite polarity shipped, and stating it the old way would
  claim a structural impossibility the code does not establish. What the
  placement actually buys is (1) ONE choke point at the act instead of
  chasing twelve spellings on the way to it, and (2) the only enforcement
  that works at all on the Codex backend, where `codex exec` offers no
  PreToolUse veto and the lexical rules can only run after the fact.
  Two residuals, disclosed rather than implied away: an environment variable
  can be DROPPED, so a session that both dodges the argv layer (e.g. a script
  written by one tool call and run by the next) and unsets the var in the
  same step is past both layers; and the HTTP check is honour-system, since
  the local server cannot see a caller's environment — a marked caller that
  omits the header is not refused by it. Four commit subjects on the branch
  that shipped this say "so the spelling of the invocation stops mattering",
  which is backwards — spelling decides whether the mark survives to the act.
  Landed subjects cannot be corrected without rewriting history, so the
  correction is recorded here and in `docs/security.md`.

- **The obvious routes an agent session had to ending the human gate now cost
  it a denial — and this is a cost-raising layer, not a closed door.**
  The known gaps are listed below — deliberately without a count, because the
  count has gone stale twice: the review that would make it accurate always
  happens after the commit that states it. The check at the act itself —
  shipped in the entry above — sits behind this one and is a better-placed
  layer of the same kind, not the thing that closes the door.
  `nh approve` performs
  a real `git merge --squash` and pushes to the default branch, and
  `POST /api/tasks/<id>/approve` (plus `/approve-landed`, `/shipped` and
  `/finish-review`) is the same act over the local server. None of them was
  denied by the agent guard, while every forge spelling was. Twelve reachable
  spellings in both coder and read-only mode, including `no-human approve` —
  the second console script for the same entry point, and the name the install
  docs teach. Found by fact-checking the claim "the merge commands are denied
  to the agent's sessions" against the source; there is no evidence any agent
  ever ran one.

  **KNOWN GAPS, stated here rather than 45 lines down.** `case x in x) nh
  approve <id>;; esac` and `cat <(nh approve <id>)` are not denied, and neither
  is a script tool's `system()` payload once an option separates the binary
  from the verb — `awk 'BEGIN{system("nh --repo . approve <id>")}'` gets
  through where `system("nh approve <id>")` does not. That last one is round
  four's own bug, still live one layer down, and it was found only once a
  reviewer varied the option axis their previous sweep had held fixed — a
  corpus that never varies the axis a rule keys on cannot measure that rule,
  it can only report that it did not fire. They are
  disclosed rather than chased: eight rounds of adding the next shape produced
  a rule that still loses to shell grammar, which is the signal to stop. The
  better-placed check is at the ACT — `nh approve`, `nh merge-stack run` and
  the four gate-ending routes refusing a caller that carries the agent-session
  mark, shipped in the entry above — which collapses the spellings into one
  choke point and also covers the Codex backend, where no PreToolUse rule can
  act at all. It refuses a DROPPABLE mark rather than requiring a capability
  an agent session structurally lacks, so it raises the cost of these gaps
  instead of making them unreachable.
  Two smaller asymmetries, same category: a python payload that merely PRINTS
  the route is denied where a node one is not, and an `awk`/`perl` one-liner
  containing `$` plus a word like `shipped` or `serve` is refused as
  undecidable.

  The rule reads **argv**, not the command line, and refuses input it cannot
  resolve rather than allowing it. Four earlier rounds were lexical and each was
  wrong in a direction nobody predicted — a flag
  (`python -u -c`), a global option (`nh --repo . approve`), a redirection
  (`nh 2>/dev/null approve`), a quoted task id, a `..` segment curl normalises
  away, a percent-escaped byte, a `git` substring in a path reaching across a
  newline. Those are what a shell resolves and a regex cannot, so the guard now
  splits the line into commands, peels wrappers, and asks what each command is.

  Naming the act stays allowed, which took three attempts to get right: a
  reviewer can read, grep and `git log` the landing code, grep the route in the
  file that defines it, run its tests, and write a commit message or PR title
  that mentions the command. The exemption is a property of the program being
  run rather than of a message-option grammar, so it no longer depends on which
  option came first or on which separators a scoping regex happened to list.

  Every fix round so far has been wrong somewhere new, and each miss was found
  by an independent review rather than by me. The lexical rounds lost to shell
  grammar the text never modelled; the reviews executed the spellings in a real
  shell rather than reasoning about them, which is why the list kept growing.
  The last round added: backslash-line-continuation, a redirection glued onto
  the verb, `$'\x61pprove'` (decoded rather than refused — the escapes are
  deterministic), two-level shell nesting, the route reached through
  `node`/`bun`/`perl`/`ruby`/`gh api`, `osascript`'s `do shell script`, and the
  undecidable refusal reading the command that will actually run rather than
  `argv[0]` as typed.

  Six defects the fix ITSELF introduced were found and removed along the way,
  and they are listed rather than counted because a count is what went stale
  here twice:
  a quadratic in the mask lookup (14.6 s inside a PreToolUse hook);
  an exponential in the import-list pattern (9.9 s on 24 aliases);
  a second quadratic in the gate-mention scan (3.4 s on an 800-line script);
  a decoder that let `$'\x27'` inject a quote and hide an arbitrary command;
  and two over-denials — one blocking `pytest -k approve`, i.e. running the
  tests for the code this rule protects, and one blocking a commit message
  that mentioned the command.

  `docs/security.md` no longer publishes a closed list of exceptions. It says
  what this is: a layer that raises the cost of the obvious spellings, in front
  of a second, better-placed cost-raising layer at the act itself. Seven rounds
  and six reviews produced that sentence, and it is the honest one.

- **The forge-merge argv check (`_forge_invocations` in `agent/guard.py`) read
  `gh`/`glab` off the bare command and never recursed into a shell wrapper —
  its sibling git check already did. `bash -c "gh pr merge 7"`, `sh -c`,
  `timeout`, `xargs`, `$(gh pr merge 7)`, `{ gh pr merge 7; }` and
  `if...then...fi` all reached `gh pr merge`/`glab mr merge` past the guard in
  both coder and read-only mode. Fixed the same way the earlier PreToolUse
  round fixed the equivalent gap for `nh approve`: bounded two-level recursion
  into shell runners and grouping heads, mirroring `_git_invocations`. A
  second, smaller defect in the same function: `--hostname`/`-H`/`--host` were
  listed as value-taking global options, which they are not (`gh --help`,
  `glab --help`, executed, list only `-R`/`--repo`) — a boolean flag in that
  set swallows the next token, which was the verb, so `gh -H pr merge 7` read
  as `("merge", "7")` and passed. Narrowed the set to `{-R, --repo}` and moved
  subcommand detection from positional-word reading to a scan for the `pr`/`mr`
  noun, which is what makes the narrower set safe against an unrecognized
  flag. The recursion is bounded at `_depth < 2`, same as the git check, so a
  1,000-level nested wrapper still evaluates in well under a second.
  Independent review then caught that the noun scan, alone, read the token
  immediately after `pr`/`mr` as the verb unconditionally — a value-taking
  global landing AFTER the noun (`gh pr -R o/r merge 7`, `gh pr --repo o/r
  merge 7`, and the `glab mr` equivalents) shifted the verb into the flag's
  slot the same way a global BEFORE the noun used to. Fixed by skipping the
  same modelled globals (and their `-R=`/`--repo=` single-token form) a
  second time, after the noun, before reading the verb — an unmodelled flag
  there is still read as the verb on purpose, so it cannot swallow the next
  token and over-deny an unrelated command.

  **2026-08-23 round: `glab mr accept` is a Cobra alias for `glab mr merge`**
  (confirmed by running `glab mr accept --help`, which prints the same USAGE
  line and pairs `glab mr merge 235`/`glab mr accept 235` under EXAMPLES) and
  was reaching both session modes undenied; `gh` has no equivalent alias
  (`gh pr accept --help` falls back to generic help, and `gh alias list` lists
  only `co: pr checkout`), so only the `glab` side changed. A second gap in
  the same round: `x=$(gh pr merge 7)` — an assignment-prefixed command
  substitution — was allowed while the equivalent backtick form,
  `` out=`gh pr merge 7` ``, was already denied, because `_SUBST_HEAD`'s
  lookbehind requires start-of-string or whitespace before `$(`/backtick and
  an immediately-preceding `=` defeats it; `export`/`local`/`readonly`/
  `declare`/`typeset` prefixes on the same substitution had their own gap,
  since those declarator words are not shell keywords `_strip_shell_keywords`
  models and are not wrappers `_strip_wrappers` models, so they were read as
  argv[0] and rejected outright rather than peeled. `setsid gh pr merge 7`
  was a third gap: `setsid` was already a modelled trailing-argv runner
  elsewhere in the guard, but `_forge_invocations`'s own recursion only
  consulted the narrower shell-runner set. All three are fixed additively —
  new sibling regexes/constants, no existing rule narrowed — and a 1,000-deep
  nested-assignment input (~50k characters) still evaluates in well under a
  second in both modes.
  **Known gaps, named rather than left silent, same as above:** `ssh host
  "gh pr merge 7"` executes the mention on a remote host under credentials
  this process cannot account for; `find . -exec gh pr merge 7 \;` has its
  own `\;`-vs-`+` batching grammar that earns its own parser; and
  `case x in x) gh pr merge 7;; esac` reaches the same shell-grammar blind
  spot the `nh approve` check's own `case...esac` gap (above) already
  illustrates, but for this different check. None of the three is a
  narrowing — all were already unreached before this round.

## [0.1.4] — 2026-08-21

Fixes `nh mcp-serve` on every install that resolves dependencies from PyPI —
which is every install that is not a git checkout.

- **The MCP SDK requirement is capped below 2.0.0.** `intake/mcp_bridge.py`
  imports `mcp.server.fastmcp`; the SDK removed that path in 2.0.0, and the
  requirement (`mcp>=1.28.0`) had no upper bound. So `uvx no-human mcp-serve`,
  `nh mcp-serve` after `uv tool install no-human`, and the Claude Code plugin's
  command all died with `ModuleNotFoundError` on 0.1.1, 0.1.2 and 0.1.3, while
  CI, the MCP container, the desktop bundles and every dev checkout stayed
  green on the locked 1.29.0. Workaround on an older version:
  `uvx --with "mcp<2" no-human mcp-serve`.
- **The gate that missed it now exists.** CI's wheel job installs with
  `uv tool install`, which resolves from PyPI rather than `uv.lock` — the only
  lane that sees what a user sees — and now imports the bridge in that env.
  `tests/test_mcp_dependency_bound.py` fails if the declared bound ever admits
  an SDK without the module the bridge imports, 2.x pre-releases included.
- **What actually changed underneath.** 2.0.0 shipping was not the trigger:
  the locked `claude-agent-sdk` 0.2.121 requires `mcp<2.0.0` and its latest
  0.2.143 relaxed that to `mcp<3.0.0`, so a transitive cap had been holding
  this package up by accident. Porting the bridge to the 2.x API
  (`mcp.server.MCPServer`) is tracked as its own issue.

- The PyPI project page links back to the site, source, docs, changelog,
  issues and release notes (`[project.urls]`); the package had no project
  links before.

- `nh task add --backend` (and `backend` on `POST /api/tasks`) now routes
  THAT task's coder to the named backend — `claude`, `codex` or `local` —
  instead of only labelling it while `worker.backend` decided. Reviewer,
  planner, supervisor and utility stay on Claude either way: the factory
  ignores an override for any non-coder role. An unknown name is refused at
  intake (CLI choice / HTTP 422); a per-task codex/local run gets the same
  credential and CLI preflight the global setting gets, at orchestrator
  construction, before any model call. (public issue #5)

## [0.1.3] — 2026-08-20

Registry release: the package now carries what the official MCP Registry
needs, plus the loop fixes that had accumulated since 0.1.2.

- `server.json` at the repository root describes the MCP bridge (`nh
  mcp-serve`, two tools, stdio) as the PyPI package `no-human`, and the README
  carries the registry's `mcp-name: io.github.no-human-ai/no_human` ownership
  marker. A manual `publish-mcp-registry.yml` workflow publishes it with OIDC
  — no token in the repository — and refuses unless pyproject, server.json and
  PyPI agree on the version.
- A second console script, `no-human`, is the same entry point as `nh`, so
  `uvx no-human mcp-serve` runs the bridge the way registry clients invoke it.
- The wheel-build refusal when the board is absent now also says the short
  way out: `uv tool install no-human` installs the published wheel, board
  included (public issue #4).

- The wake watcher's PR-conflict rung no longer falls through to an
  expensive coder round when it can't tell what's conflicting: a failed
  conflicting-path enumeration — whether `conflicting_paths()` raises, or
  simply returns no result for an unresolvable ref, the more common case —
  now retries once after a best-effort `git fetch` of the base and branch
  refs, and if it's still unresolved afterward, escalates `NOVEL_UNKNOWN`
  instead of guessing. The failure reason now reaches both `task.context`
  and the persisted event's new `error` field, not only the log.
- The review gate no longer takes a blocking finding's word for it: on the
  gate path, a FAIL with non-critical blocking findings now gets one bounded,
  single-turn refute pass (read-only, ~180s) before it's charged to the
  coder. A finding demotes to advisory only when the refute pass cites its
  own counter-evidence at a file:line that itself passes the existing
  citation-existence check; a goal veto, a `spec_compliance:false` verdict,
  and critical-severity findings can never be demoted. A refute pass that
  times out, errors, or reaches no verdict changes nothing — the FAIL stands
  byte-identical, its tokens folded into the decision like every other
  discarded round.

## [0.1.2] — 2026-08-20

Security and release-infrastructure release: same product, patched runtime,
and a release lane for every platform.

- Dependency security: Electron moves to 38.8.6 (the last 38.x), clearing
  every advisory patched within the current major; js-yaml 4.3.1, fast-uri
  3.1.5, undici 6.28.0, postcss 8.5.26, mcp 1.29.0 and cryptography 50.0.0
  likewise. 21 of the repository's 40 open Dependabot alerts closed by
  measurement, not estimate; 19 remain — 18 gated on the Electron 39 major
  (deliberately deferred to a scheduled release) and one, extract-zip
  (GHSA alert #45, high), with no patched version in existence to move to.
- The Windows job gains the same on-demand release lane the Linux job has
  (`workflow_dispatch` + `windows_release`): build, verify against the tree
  that built it, checksum, 7-day artefact. Ordinary CI runs are untouched.
- Windows bundles now carry the same BUILD_STAMP provenance
  (`commit=/dirty=/board_sha256=`) POSIX builds have had since the stale-DMG
  incident — an absent stamp fails verification rather than passing quietly.

## [0.1.1] — 2026-08-20

Also in this release — reliability, honesty, and cost, measured not asserted
(full suite 8,864/0; funnel 5/5 with every holdout green; reviewer recall
17/19, up from 15/19):

- The eval judge's verdict now survives mid-run emission, a truncated end
  marker, and marker drift — six bench tasks per run were being scored as
  failures because a verdict could not be parsed, not because work was wrong.
- Git lock contention (another process briefly holding `index.lock`) is
  retried with two short backoffs instead of crashing the task; every other
  git failure still fails fast and loud.
- Fix pairs: when a task fails on an error this machine has overcome before,
  the retry is handed what worked — as evidence, never as an instruction.
- A retry that ends byte-identical to its predecessor (same failure, same
  diff) stops the loop and escalates honestly instead of buying the most
  expensive third attempt.
- Judgment-call blockers (ambiguity, novel-unknown, impossible) get exactly
  one supervisor-checked challenge before parking; external blockers are
  honored untouched, and a park is never converted into a fake "done".
- The reviewer carries a maintainability-trajectory lens: does this change
  make the NEXT change harder? Concrete findings only, capped below blocking
  severity.
- `nh bench harvest`: escalated, parked, and failed tasks become bench-spec
  candidates for curation.
- The intake grill's answering pass pays for what the task needs: probe
  budget scales with the question count; prose-only tasks skip filesystem
  probes (assumption-grade answers, clearly marked).
- Onboarding: two checkouts of the same repository are tellable apart —
  colliding names show their full path. (Authored end-to-end by no_human
  from its own board, review PASS, 8,847/0.)
- The stale-data banner no longer eats clicks while disconnected.
- docs: an operator profile for reviewing untrusted external PRs in a
  credential-isolated container.
- This release restores auto-update for installed apps: it ships the ZIP and
  `latest-mac.yml` that `electron-updater` requires (0.1.0's release lacked
  both).

### Added
- CI builds the board-carrying wheel on every run and proves it installs:
  `uv tool install <wheel>` yields an `nh` that finds its board and the Agent
  SDK's bundled `claude` — no Node, no separate CLI install. A release build
  (`workflow_dispatch` with `wheel_release`) keeps the wheel as an artefact.
- A Claude Code plugin at `plugins/no-human/` exposing the MCP bridge's two
  tools (`task_add`, `task_status`).
- A `Publish to PyPI` workflow (`workflow_dispatch` only, typed confirmation)
  that builds the board-carrying wheel and uploads it with PyPI Trusted
  Publishing — no API token anywhere in the repository.
- Version is 0.1.1 across `pyproject.toml`, `desktop/package.json` and
  `web/package.json` (and those lockfiles' root entries), so a built wheel is
  no longer labelled with the released 0.1.0's version.
- `CHANGELOG.md` (this file) and `glama.json`.

### Changed
- README: download buttons, the site's hero loop under the title, install
  leads with the desktop app and names each build's architecture; `nh approve`
  is documented as what it does — it squash-lands the PR as the configured
  operator identity (`git.approve_identity`).
- `CONTRIBUTING.md`, `docs/adapters.md` and the `nh task add --backend` help no
  longer say "a single Claude backend": the coder runs on the Claude Agent SDK
  by default with OpenAI Codex as the sanctioned second backend
  (`worker.backend`); reviewer, planner, supervisor, utility and intake tiers stay on
  Claude.

### Fixed
- The shipped harvest test no longer asserts that the (unshipped) scored corpus
  directory exists, so the public repository's CI runs green.

## [0.1.0] — 2026-08-16

First packaged release. no_human takes a software task end to end — plan,
code, test, adversarial review by a second model, and a pull request with the
evidence it works. A human approves and merges.

### Added
- **macOS** — `no_human-0.1.0.dmg`, signed and notarized (Apple silicon). The
  app bundles the server and the board and runs on your own Claude
  subscription. A `.sha256` ships alongside.
- **Windows** — `no_human-0.1.0-UNSIGNED.exe` (x64 installer, per-user, no
  administrator prompt) and `no_human-0.1.0-UNSIGNED.zip` (portable build of
  the same payload). Unsigned: SmartScreen warns until code signing lands.
  `SHA256SUMS-windows.txt` ships alongside.
- **Linux** — `no_human-0.1.0-linux-amd64.deb` and
  `no_human-0.1.0-linux-x86_64.AppImage` (x64), added to the same release on
  2026-08-18, built by the public repository's CI. `SHA256SUMS-linux.txt`
  ships alongside.

[Unreleased]: https://github.com/no-human-ai/no_human/compare/v0.1.4...HEAD
[0.1.4]: https://github.com/no-human-ai/no_human/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/no-human-ai/no_human/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/no-human-ai/no_human/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/no-human-ai/no_human/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/no-human-ai/no_human/releases/tag/v0.1.0
