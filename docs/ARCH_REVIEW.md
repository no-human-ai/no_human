# Architecture review — everything measured against the north star (2026-07-14)

> Produced by 5 independent fresh-context review agents (north-star program Task B1), one
> angle each, every finding requiring cited evidence + a concrete failure/cost scenario.
> The one question asked of every mechanism: *does it contribute to — or fight — "a developer
> files a task and walks away; no_human carries it unattended to its human gate at high
> quality and minimal token cost"?* Verify every file:line before acting — the codebase moves.
> Statuses: **OPEN** (B2 backlog, ranked below) · **FIXED** (this program) · **REFUTED**
> (working as intended — do not re-open without new evidence).

## The picture in one paragraph

The pipeline's correctness discipline is strong (never-merge, fail-closed review parsing,
tamper snapshots, per-task orchestrator isolation all verified). The leaks cluster in four
places: (1) **cost containment inside a single attempt** — the deterministic runaway detector
is wired to telemetry only while an LLM supervisor that fails open holds the only abort
authority; (2) **trust-gate seams** — three deterministic gates each inspect a narrower slice
than what actually ships; (3) **cost accounting** — planner/MoA/supervisor/distillation burn
lands in no column, and two docs assert the opposite; (4) **board truthfulness under the
unattended promise** — a dead WS can freeze the board while still showing "Connected".

## B2 backlog (ranked: north-star impact × confidence ÷ fix cost)

| # | Finding | Evidence anchor | Fix | Cost |
|---|---|---|---|---|
| 1 | **FIXED 2026-07-14** — hard detector tier (doom 9×/edit 15×/ping-pong 12) raises StuckAbort from the sink: attempt FAILS with [WIP-PARTIAL] checkpoint + true spend recorded, bounded loop retries fresh; advisory tier stays telemetry — the edit tier counts only edits with NO observed progress since that file's previous edit (a test run whose captured outcome changed, or another file edited in between, resets it); f6e626fd. | bounds.py `hard_stuck_reason`; orchestrator.py StuckAbort handler; tests/test_stuck_abort.py | shipped | M |
| 2 | **FIXED 2026-07-14** — backend yields per-message "usage" events; sink keeps a task-scoped running total (in/out + cache reads, same as db.lifetime_usage) and raises BudgetAbort at the cap → attempt records true spend, task parks behind the same BUDGET_EXHAUSTED blocker | claude_backend.py usage events; orchestrator.py `_begin_attempt_accounting` + BudgetAbort handler; tests/test_stuck_abort.py | shipped | M |
| 3 | **FIXED 2026-07-14** — `_review_base` threaded into `tamper_check_between` for the primary repo; linked repos use `_repro_base_ref` on the same base name (HEAD~1 fallback); guard now inspects exactly what ships. *Known trade-off (review F3): a resumed attempt whose earlier legitimate checkpoint reduced test count now trips the guard — accepted; the guard matches the shipped range by design* | orchestrator.py tamper call sites; test_tamper_guard_sees_the_whole_branch_not_just_the_last_commit | shipped | S |
| 4 | **FIXED 2026-07-14** — invocation error now re-run on the BASE tree (temp worktree at merge-base): base runs clean → attempt FAILS ("the change broke the test runner"); undeterminable → advisory, stated. *Adoption review F1: a bare worktree lacks env_setup, so on setup-dependent projects "reproduces on base" DOWNGRADES to undeterminable (env_dependent guard + test) — clean-base verdicts stay binding; residual: projects needing untracked artifacts (.venv) without env_setup config still read environmental* | orchestrator.py `_invocation_error_reproduces_on_base`; tests/test_base_tree_gate.py | shipped | M |
| 5** | **FIXED (#75) — Planner + MoA proposers + aggregator burn persisted NOWHERE (separate backends; usage only in event text); COST_LEVERS.md:72-73 + northStar.js:30-32 document the false "inside the coder session" claim | orchestrator.py:4642-4666,4732-4746,4798-4813 | plan_* columns + update_attempt + fix the docs | M |
| 6** | **FIXED (#75) — Supervisor/distillation/stuck-hypothesis usage discarded | orchestrator.py:3878-3884,3320-3327,706-716 | utility bucket column | M |
| 7** | **FIXED (#75) — Untracked non-code files (yaml/Dockerfile/requirements/proto) ship missing — commit_paths/favicon incident class still open for non-code | git.py:164-186,204-209,281-287 | completeness guard warns on ANY untracked non-ephemeral file | M |
| 8 | **FIXED 2026-07-14** — held-out suite now runs FIRST in `_run_review` and a failure returns a failing ReviewDecision deterministically, before any reviewer tokens and even in advisory mode | orchestrator.py `_run_review` head; tests/test_holdout_gate.py | shipped | S |
| 9** | **FIXED (#75) — Silent WS death leaves board frozen while "Connected" (two writers per socket; remove-without-close) | app.py:2127-2143 vs broadcast :222-225; App.jsx:485-488,588-590 | one writer per socket + always close | M |
| 10** | **FIXED (#75) — WS change fingerprint misses subtask_progress/pr_url/attempt_count/cancelled → permanently stale cards | app.py:2124-2131; Board.jsx:225-227 | content-hash fingerprint | S |
| 11** | **FIXED (#75) — Review angle passes run after a decided FAIL (can never flip it) — pure Opus cost | reviewer.py:903-931, merge :653-655 | skip angles when not decision.passed | S |
| 12** | **FIXED (#75) — TaskTable prices review fields the list endpoint never sends (undefined→0); drawer/board/stats price coder-only with data available | TaskTable.jsx:127-135; api/models.py:197-199; SlideOver.jsx:235-237 | total_review_* on TaskSummaryOut + sum in UI | S |
| 13** | **FIXED (#75) — Parked tasks depend on a WakeWatcher built inside try/except-pass; parked routes are notify-silent → BLOCKED forever, nobody told | api/app.py:134-148; taxonomy.py:77-81; orchestrator.py:2437-2444 | fail loudly / fallback timeout-notify | S-M |
| 14 | **DEFERRED (low value after batch 1)** — the deterministic detector now has teeth (StuckAbort, B2 #1) so it no longer merely feeds telemetry; wiring its signals INTO the LLM supervisor to cut the ~100 calls/attempt is a marginal cost optimization, not a correctness fix, and the supervisor's own budget (check_every) already bounds it. Left for a dedicated cost pass. | supervisor.py; bounds.py | deferred | M |
| 15** | **FIXED (#75) — Worker-offline not alarmed; web responses tell the human to run CLI; msg.worker sets running unconditionally | app.py:689,1495,2133; App.jsx:482 | board banner off authoritative running | S-M |
| 16** | **FIXED (#75) — _board_tasks unbounded + N+1 attempts query per 2s per socket | app.py:258,261 | join + cap terminals | M-L |
| 17 | **FIXED 2026-07-15 (half) / REFUTED (half)** — dead knobs DELETED (escalate_after field+config keys, Bounds.max_correction_rounds [wake.py's config read stays], StuckDetector.health); the defers-as-OK claim was WRONG: `error` is checked before `deferred`, so a blocking layer with missing creds yields ok=False — verified empirically, no code change | bounds.py; plan_runner.py:38-42 | shipped / refuted | S |
| 18 | **DEFERRED to Phase E follow-up** — multi-client notification dedupe (browser + Electron both 'hidden') is inherently a desktop-window concern; the E1-E5 shell shipped without a notification layer (that was never in E scope), so this lands with a future notification feature, not now. | App.jsx:427-440 | deferred (E) | M |
| 19** | **FIXED (#75) — approved-but-unmerged indistinguishable from un-reviewed (stays in needsYou) | app.py:595; boardLanes.js:23,66-68 | approved_at on summary + sub-state (fold into F) | S |
| 20** | **FIXED (#75) — Angle passes judge a 60k-capped diff with no tools; `tests` angle complex-tier-only while mocked-to-green new tests evade tamper counts | reviewer.py:870-873,933-941; tamper_guard.py:196 | truncation warning + tools; tests-angle all tiers | S |

## FIXED during this program (before the bench ever shipped)

- Bench summed coder-only tokens → ratio rigged in no_human's favor. `northstar.py` now sums
  reviewer buckets, adds a symmetric price-weighted `cost_ratio` (fresh 1.0 / cache-read 0.1 /
  creation 1.25) as the headline, and the gate checks both ratios; guarded by
  `test_score_counts_reviewer_tokens`. Planner/supervisor burn remains under-counted until
  #5/#6 land — the report labels this.
- Analyzer left Claude-Code-mined rules unscoped (project="") → scoped by session cwd (A1).
- `<local-command-stdout>` noise polluted extracted titles/requests (A1).
- `nh history --json-out` stdout wasn't pipeable JSON (A1).

## REFUTED — verified working as intended (do not re-open without new evidence)

MoA fan-out default-off + tier-gated + single-planner fallback (orchestrator.py:4600,4617,
4631-4633) · per-task fresh Orchestrator/backend so no shared-state races (api/app.py:71-94,
scheduler.py:272) · reviewer fail-closed chain incl. no-verdict sentinel and
ReviewerUnavailable (reviewer.py:730-743,1012-1017; orchestrator.py:2770-2777) · angle-pass
token usage correctly merged, no double counting (reviewer.py:635-637; disjoint columns
:1580-1582/:1949-1953) · tamper snapshots cover conftest/skips/tautologies/autouse fixtures
and deleted test files (tamper_guard.py:29-115,179-180) · repro gate error≠fail
(repro_gate.py:95-98,128-135) · board snapshot-replace model + drawer key-remount + 409
double-action guards + SSE Last-Event-ID resume (#53/#55 fixes hold) · prompt↔parser severity
arithmetic consistent (reviewer.py:439-457 ↔ :592-599) · never-merge/_protect_base_branch/
_agent_git_identity verified intact.

## Cross-reference

Phase-C cost levers (the 95.6% cache-read whale) live in the B1 angle-2 decomposition:
in-context tool-result eviction (the `_TOOL_RESULT_CAP` at claude_backend.py:40 is
DISPLAY-ONLY), researcher-subagent enforcement (built + wired but advisory,
orchestrator.py:1426-1447), turn-waste reduction via routing directives
(prompt_blocks.py:183-197). Measured-dead levers (seed diets, compaction rebuild, proposer
retune, context-1m) stay dead — see docs/COST_LEVERS.md.
