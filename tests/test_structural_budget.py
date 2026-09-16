"""A structural-size ratchet over `src/no_human`, in the repo's own guard idiom.

WHY THIS EXISTS. `src/no_human` is 106k LOC and growing; nothing looks at
function size, complexity, or file size, so a function can silently become
2,000+ lines / cc ~250 with no signal anywhere. This is not a lint config and
does not refactor anything — it freezes TODAY's offenders by name and value
and fails on any of three things: (1) a NEW offender that appears outside the
freeze, (2) an EXISTING frozen entry that has grown, (3) a frozen entry that
has shrunk below threshold or whose symbol vanished (renamed/deleted) without
its allow-list entry being deleted. Case (3) is the ratchet: the budget can
only move down.

THE FORMULA, as code-level truth over `ast.walk(fn)` for a function node:

    cc = 1
        + 1 for each If | For | AsyncFor | While | ExceptHandler | With
              | IfExp | match_case
        + (len(values) - 1) for each BoolOp
        + len(ifs) for each comprehension

`AsyncWith` is deliberately **not** counted — an intake-resolved asymmetry,
pinned by `test_cc_formula_on_a_hand_counted_snippet` below so a future edit
to this formula fails loudly instead of silently deflating every frozen
value. This is arbitrary-but-stable and NOT radon-comparable; its only job
is monotonicity. Nested function bodies count toward the enclosing function
too (`ast.walk` descends into them), so growth anywhere inside a function —
including a closure defined inside it — fails that function's budget. This
is intentional, not a bug.

WHAT THIS DOES NOT COVER. A size ratchet, not a design review: splitting a
2,000-line function into ten 200-line ones sharing mutable state passes
clean. `lambda` bodies are uncounted. Module-level code (outside any
function) is invisible except through the whole-file line-count rule.

SCOPE — a deliberate deviation from an intake answer, recorded so it reads as
a choice, not an oversight. Intake said to exclude `test_*.py` under `src/`.
The only match is `src/no_human/testing/test_layers.py`, which is production
code (the tamper guard's own layer classifier) despite its filename.
Excluding it would be exactly the "exclude a path to hide an offender" move
this ticket exists to forbid. It is not an offender today, so scanning it
changes nothing measurable, and the rule stays simple: every `.py` under
`src/no_human`, no exclusions.

`ast.parse` errors are never swallowed: a `SyntaxError` propagates with its
`filename` attribute set to the offending path (via the `filename=` kwarg to
`ast.parse`), rather than being caught and skipped. A file this scanner
cannot parse must fail loudly, not vanish from coverage.
"""

from __future__ import annotations

import ast
import re
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "no_human"

MAX_FUNCTION_LINES = 300
MAX_FUNCTION_CC = 60
MAX_FILE_LINES = 2500

# Frozen at HEAD 2b2370f582f95465ba408c12224a511c6c74f692, 2026-08-26.
# Measured with the scanner below; see the PR body for the full table.
# 16 functions > 300 lines.
#
# LEDGER CONVENTION: each entry below records what was measured AT THAT
# POINT IN TIME ("actual 3019", "measured on this tree"). Never phrase an
# entry as matching/equalling the frozen value the row lands on -- later
# entries are expected to move that same value further, which turns a true
# sentence false without anyone editing it. `test_no_ledger_entry_claims_
# equality_with_a_frozen_value` below enforces this.
FROZEN_FUNCTION_LINES = {
    # 2099 -> 2108 (+9): PR #877 widens the tamper base to three-dot
    # origin/<base>...HEAD so a sanctioned merge isn't charged with main's own
    # landed test edits (attempt-authored gutting still fires). Re-anchored on merge.
    # 2108 -> 2118 (+10): the two `_consume_human_gate` call sites (revision
    # and fresh branch paths) that rearm-once the human resume_from gate.
    # Measured directly against the rebased tree with the scanner below.
    # 2118 -> 2130 (+12): D1.1 (short PR bodies) fix round, review finding #6 —
    # the shared first-blocking-failure excerpt feeding both stuck detection
    # and failure_reason. Measured on the D1.1 squash-merge result.
    # 2130 -> 2137 (+7): P2 (turn-cap convergence early-abort) — the
    # per-attempt `ConvergenceTracker` reset (alongside `_stuck`'s) and the
    # `StuckAbort`/`ConvergenceAbort` abort handler merged into one `except`
    # clause with a one-line `kind = ... if ... else ...` dispatch, replacing
    # what was a `StuckAbort`-only branch. Measured on this tree with the
    # scanner below.
    # 2137 -> 2156 (+19): P2 review-fix round 2 — the per-attempt
    # `ConvergenceTracker` reset now carries a task-id-scoped tuple (plus its
    # anchored comment) and passes `cap=self.bounds.max_turns_per_attempt`,
    # and `_abort_kind(exc)` replaces the inline ternary in the merged
    # abort handler. Measured on this tree with the scanner below.
    # 2156 -> 2167 (+11): the cancel-reason discriminator is recorded on the
    # direct-unwind path too (record_cancel_reason guarded by
    # SERVER_STOP_REASON, with its reasoning comment), so the property holds
    # for callers with no HTTP handler in the loop. Measured on this tree
    # with the scanner below.
    # 2167 -> 2186 (+19): the structural-budget preflight call site — a
    # try/except around `_structural_budget_preflight` (mirroring the
    # repro-gate call site immediately above it) plus its explanatory
    # comment, so a frozen-file growth buys its corrective round in the
    # SAME attempt instead of costing a whole extra one. Measured on this
    # tree with the scanner below.
    # 2186 -> 2210 (+24): attribution-guard hardening — `ls_remote_exact` is
    # called ONCE here, right after `_protect_base_branch`, to pin the base
    # sha BEFORE the coder session starts (plus the fail-closed
    # advisory/log branch when the pin doesn't resolve, and threading
    # `base_pin` through to the `_foreign_authored_commits` call site).
    # Deliberately kept inline rather than split out, since the whole point
    # is that the pin is captured in `_run_attempt`'s own frame, before the
    # coder can influence it — a helper callable from elsewhere would weaken
    # that guarantee. Measured on this tree with the scanner below.
    # 2210 -> 2253 (+43): an earlier, review-failed version of the send-back
    # "no changes needed" fix kept the landing logic INLINE in the
    # `if resumed_commit is None:` block, plus used `set_status(...,
    # validate=False)` — a project-rule violation caught in review.
    # 2253 -> 2214 (-39): the review fix delegates the landing decision to a
    # new sibling method, `_land_no_changes_needed` (next to
    # `_mechanical_round`), so the zero-diff call site shrinks to a ~4-line
    # `landed = await self._land_no_changes_needed(...); if landed is not
    # None: return landed`. The delegation costs lines in the new helper
    # (see the file-total entry below) but nets `_run_attempt` itself
    # smaller than even the pre-incident-fix baseline.
    # 2214 -> 2164: re-frozen at the measured value on this tree after the
    # red-run failure-blocks fix (task 3bccb499). The old 2214 carried 63
    # lines of slack over main's measured 2151; the fix itself is +13 lines
    # here (2151 -> 2164) — the CAP shrank, the function did not. Measured
    # on this tree with the scanner below.
    # 2164 -> 2192 (+28): the background-run report nudge's call site in
    # `_run_attempt`'s committed-diff path (task 7a7713e3, landed on top of
    # 3bccb499's 2164). Measured on this tree with the scanner below.
    # 2192 -> 2212 (+20): the `_node_serial_rerun` call site + `serial_passed`
    # guard in the plain-red branch — a cold `node --test` run gets one
    # serial re-run of only its failing files before being blamed on the
    # change (tasks ba602e95 / f8af7f46, 2026-09-08). Measured on this tree
    # with the scanner below.
    # 2212 -> 2220 (+8): 99fa5ba5 (bounded failing_tests and joins at
    # every write site; the carried dropped count reaches the coder prompt)
    # landed on top of cf6f6c19. Measured on this tree by the scanner's own metric.
    # Landing of 4e0299ad (hand-finished after the independent review of
    # 429b471f): 2220 -> 2223 (+3): the excused-path comment now says the
    # pre-review block may already have written its own copy of the artifact
    # instead of claiming a single write. Measured on this tree with the
    # scanner below.
    # 2220 -> 2231 (+11): terminal telemetry for real task-end states
    # (MAJOR-1(a)) — a `self.emit("cancelled_hard", ...)` call right in the
    # `CancelledError` except-block, plus its explanatory comment
    # distinguishing this hard-cancel end state from `_honor_cancel`'s
    # cooperative-pause `kind="cancelled"` (not an end, not counted here).
    # Kept inline rather than split out, matching the base-pin precedent
    # above: this IS the hard-cancel unwind's own frame, and firing the
    # once-only terminal event from anywhere else would mean re-deriving
    # "was this attempt actually torn down" from state instead of just
    # being on the code path that tore it down. Measured on this tree with
    # the scanner below.
    # 2223 -> 2243 (+20) (task 30a97d8a): the single-write fix itself — the
    # pre-review block now records its rendered red detail keyed on the
    # `test_result` object's identity plus the `classified: False` marker on
    # its `test_results` write, and TESTING's plain branch reuse check
    # (`reused_pre_review = bool(pre) and pre[0] is test_result`) that skips
    # its own `_red_test_detail` call and red `tests` emit when it is
    # classifying that SAME cached object. The excused-path comment this
    # entry's previous +3 landed is also corrected here to state the
    # single-write invariant accurately instead of the old "may already have
    # written its own copy" hedge. Measured on this tree with the scanner
    # below.
    # 2254 -> 2261 (+7): #114 phase 2 — the `_build_type_hook` call site
    # and its anchored comment, `type_hook` reaching
    # `_compose_post_tool_hooks`, and the widened `backend_degraded`
    # condition. Re-measured on the merge result.
    # 2261 -> 2280 (+19): citation-drift preflight call site — the
    # `_citation_drift_preflight` invocation immediately after the
    # structural-budget preflight block, wrapped in the same
    # `try/except CancelRequested` / `except (BudgetAbort, StuckAbort,
    # ConvergenceAbort)` pattern the two sibling preflight call sites
    # already use. The preflight body itself lives in its own method, not
    # here. Re-measured on the MERGED tree with the scanner's own metric.
    "core/orchestrator.py:Orchestrator._run_attempt": 2280,
    # 760 -> 778 (+18): dispatch-time intake-eval hoisted path — the `elif
    # ctx.get("eval_result")` branch that acts on a grill/wizard-stored
    # verdict (idempotency marker, cost/residual-gap comments) added inside
    # `_drive`. Measured on this tree with the scanner below.
    # Landing of 4e0299ad (hand-finished after the independent review of
    # 429b471f): 778 -> 783 (+5): `_failing_labels` skips the harness-generated
    # `_PRE_REVIEW_RED_LABEL` row (one condition, four comment lines) so a
    # red test the coder never touched cannot count as a recurring reviewer
    # finding. Measured on this tree with the scanner below.
    # 783 -> 789 (+6): second landing pass — the row is dropped ONCE, in
    # `_reviewer_items`, before BOTH D6 halves (the pass rate had still
    # counted it: a one-round red run equalised 2/3 and 1/2 into 2/4 and
    # 1/2). Measured on this tree with the scanner below.
    "core/orchestrator.py:Orchestrator._drive": 789,
    # 449 -> 457 (+8): D3.1 (2026-08-31, auto-activation pipeline) adds the
    # one call (plus its explanatory comment) that hands `paused`/
    # `activated_at`/`learning_events` schema work to a new sibling method,
    # `_ensure_d3_learning_columns` — split out specifically so the three new
    # columns' worth of ALTERs did NOT land inline here. Re-anchored on merge.
    # 457 -> 467 (+10): `base_pin_sha` added to the generic `wanted`/`existing`
    # additive-column dict (attribution-guard hardening) — same one-line-per-
    # column shape as every other entry here, plus its explanatory comment.
    # Measured on this tree with the scanner below.
    "core/db.py:Store._ensure_task_columns": 467,
    # 429 -> 434 (+5): pre-existing red on main at 03b262d23 — e922e9b4's
    # (#935 derived-artefact keystone) landing grew the conflict watcher
    # without re-freezing; nh approve runs change-scoped tests only, so the
    # ratchet never fired at land time. Repaired (measured) on this merge.
    # 434 -> 441 (+7): 2026-09-01 trust-the-local-merge fix (forge-vs-local
    # mergeability flip-flop) — the empty-conflict-set branch now zeroes the
    # stale-flags counter, records the observability-only
    # `pr_conflict_local_clean_checks` counter, and emits a named
    # `pr_conflict_local_clean` event instead of deferring/escalating at a
    # bound; the docstring also gained a paragraph explaining why the local
    # merge is authoritative. Measured on this merge.
    # 441 -> 453 (+12): mechanical resolution extended to cover
    # `tests/test_structural_budget.py` FROZEN_* numeric-only conflicts
    # (alongside the pre-existing RELEASE_MANIFEST.txt/
    # EXPORT_CLASSIFICATION.txt paths) -- the `BUDGET_TEST_PATH` import, the
    # widened `eligible` fallback set, and the `pr_conflict_resolved` event's
    # budget note. Measured on this tree with the scanner below.
    # 453 -> 458 (+5): the failed-DerivedResolution escalation now surfaces
    # `result.detail` (capped, whitespace-collapsed) in the event text and
    # `question`, and prefixes `step=` onto the stored `evidence` -- step
    # alone was not root-causeable. Measured on this tree.
    # 458 -> 464 (+6): pushed-tip rewrite fix (2026-09-13) — the pr_conflict
    # send-back message now calls `base_merge_conflict_instruction` instead
    # of a hardcoded rebase string, so the coder is told to merge the pinned
    # base and never rewrite the already-pushed branch. Measured on this
    # tree with the scanner below.
    "blockers/wake.py:WakeWatcher._check_pr_conflict": 464,
    # 418 -> 424 (+6): D1.1 fix round — attempt-scoped verification-artifact
    # write wired into `_finalize` (review findings #1/#7). Measured on the
    # D1.1 squash-merge result.
    # 424 -> 437 (+13): D1.2 (2026-08-31, "visual proof") adds the one
    # best-effort call that runs the UI-evidence browser walk after tests
    # pass and threads its rendered media section into `_pr_body`.
    # Re-anchored on merge.
    # 437 -> 441 (+4): pushed-tip rewrite fix (2026-09-13) — the PR-open
    # retry's comment explaining `force_with_lease`'s scope was expanded to
    # describe `agent/pushed_tip_guard.py` now denying the rebase that used
    # to make this retry's non-fast-forward path the expected outcome
    # (comment-only; `forced = _is_non_fast_forward(exc)` itself is
    # unchanged). Measured on this tree with the scanner below.
    "core/orchestrator.py:Orchestrator._finalize": 441,
    # Pre-existing on main (measured red at d3d7d3a82a, this session's start):
    # an earlier fleet land grew stream() +6 without re-freezing it on its
    # merge result — the same "landed without measuring the ratchet" failure
    # the FROZEN_FILE_LINES note below records. Re-anchored to the current
    # baseline so the full suite is green again; growth from here fails.
    "agent/claude_backend.py:ClaudeBackend.stream": 407,
    # 386 -> 394 (+8): verifier-wall-park bugfix (tasks 279c03c5/c5b24230/
    # 7da7c7ce) — `_raise_if_verifier_wall` (renamed from
    # `_raise_if_verifier_quota_wall`) now also classifies a returned
    # errored `AgentResult` via `_quota_signal`/`_infra_sdk_failure`, so
    # `_run_review` gained the `result.is_error` check and passes `result`
    # through on both `_judge_call` branches. Measured on this tree.
    # 394 -> 425 (+31): task 90097 (round 4 of 0847f2c2) removed the round-3
    # unjudged-checkpoint-head instance flag — an out-of-brief, undisclosed
    # instance flag with a lifetime hole (armed by `_route_unjudged_head`,
    # only ever consumed by the NEXT `_run_review` call, so it stayed armed
    # across every non-review exit in between). Deleting the flag with no
    # replacement (measured 394, matching the pre-flag value) turned out to
    # be UNSAFE: `_run_review`'s no-reviewer/`allow_advisory` branch would
    # then unconditionally rubber-stamp a routed diff, which silently
    # re-broke two pre-existing honesty-gate tests
    # (`tests/test_resume_wiring.py::
    # test_inherited_work_is_not_credited_as_a_do_nothing_attempts_own` and
    # `tests/test_blocker_challenge.py::
    # test_an_empty_attempt_after_a_challenge_is_not_credited`) that rely on
    # exactly this call fail-closing an unjudged own-partial checkpoint.
    # Restoring the safety property without a flag or a new parameter: the
    # advisory branch now asks `self._is_own_partial(repo, task.context or
    # {}, repo.head_sha())` directly — the SAME stateless rule
    # `_run_attempt` already uses to decide `branched_from_own_partial`,
    # re-derived fresh from `repo`/`task.context` on every call (no
    # `self` flag, no threaded parameter). `_route_unjudged_head` only ever
    # routes a head for which that call is true, and nothing commits
    # between the routing and this call, so HEAD is still that checkpoint —
    # asking again here tells an unreviewed own-partial apart from a
    # human-gated resume (which reports False, so it keeps the advisory
    # freebie) without threading anything between the two calls. Measured
    # on this tree.
    # 425 -> 512 (+87): fix for "a red pre-review test run reaches the coder
    # even when the review fails" — a red pre-review run now emits its own
    # `tests` event/`test_results` write (via the existing
    # `_red_test_detail`) unconditional of the review verdict, hands the
    # reviewer a bounded `failing_test_ids`/`failing_test_ids_dropped` kwarg
    # pair, and the nested `_pre_review_red_checklist_item()` helper (whose
    # body counts toward this function per the scanner's own nested-function
    # rule) attaches a synthetic `ChecklistItem` to a decision that already
    # FAILED, at all three `ReviewDecision`-returning exits (the LLM-reviewer
    # path, the `genuinely_failed` verifier early-return, and the
    # reviewer-crash `except Exception` handler), so the failing ids ride
    # into `_record_review_feedback` on every FAIL exit; a PASSing decision
    # gets no item. Measured on this tree with the scanner below.
    # 512 -> 522 (+10) then 522 -> 537 (+15): send-back on the above
    # (MAJOR-1) — the intervening revision's `_pre_review_red_is_blocking`
    # call (a separate, non-nested method, so its own body did not count
    # here) force-failed `decision.passed` off a partial reclassification of
    # the red run that never reached `_flaky_on_rerun`, the tiebreaker the
    # post-review TESTING step's real plain-red path uses, and fail-closed
    # every red run on an `env_setup` project with no re-run evidence. That
    # call and the unconditional forcing block are both removed; the
    # net-larger body here is two explanatory comment blocks (in place of
    # the removed call and the removed forcing line) documenting exactly why
    # this function must NOT reclassify or force the verdict — the
    # unchanged post-review TESTING step is the sole gate now, so a PASS
    # verdict over a red run fails the round there instead of here. One
    # runtime branch (`if not decision.passed:`, the CC entry below) replaces
    # the forcing block; the rest of the growth is the explanatory comment,
    # which the scanner counts the same as code. Measured on this tree with the
    # scanner below.
    # Landing of 4e0299ad (hand-finished after the independent review of
    # 429b471f): 537 -> 543 (+6): the block comment states what the row IS
    # (a blocking item on an already-failed decision, skipped by D6) instead
    # of "visibility only", and the row's label is the shared constant.
    # Measured on this tree with the scanner below.
    # 543 -> 544 (+1): the block comment names `_reviewer_items` and both
    # D6 halves. Measured on this tree with the scanner below.
    # 544 -> 580 (+36) (task 30a97d8a): the pre-review block now caches its
    # rendered red detail — `self._pre_review_red_render = (test_result,
    # text, blocks, blocks_dropped, artifact_path)`, keyed on the result
    # object's own identity — right after the existing `_red_test_detail`
    # call, and its `test_results` write gains the explicit `classified:
    # False` marker (plus the comment explaining `update_attempt`'s REPLACE
    # semantics and why TESTING overwrites it to `True`), so a review FAIL
    # that never reaches TESTING still leaves an explicit "never classified"
    # row instead of an implicit, indistinguishable-from-failed one.
    # Measured on this tree with the scanner below.
    "core/orchestrator.py:Orchestrator._run_review": 580,
    # 377 -> 398 (+21): quota-saturation mid-run halt. `bench_run` now builds
    # a `QuotaHaltDetector`, threads `halt.observe(score)`/`halt.scored(...)`
    # through the per-spec checkpoint save inside `_run_spec`, and prints the
    # halt banner + `--resume` re-invocation command (`resume_command`) and
    # `sys.exit(1)` once `halt.stopped`. Estimated cc cost was thought to be
    # smaller before this landed; measured cc is 58, still under the 60
    # function-cc ceiling so no new `FROZEN_FUNCTION_CC` entry is needed.
    # Measured on this tree with the scanner below.
    # 398 -> 400 (+2): pin-rederivation follow-up (no-human/89acc73f-2) adds
    # one `console.print(escape(pin_rederivation_note(card)))` call after the
    # publish summary, surfacing the recorded-branch/HEAD-fallback disclosure
    # in the terminal, not just the markdown report. Already present on this
    # branch before the quota-halt bump above; never previously reflected in
    # the ratchet. Measured on the merge result with the scanner below.
    # 400 -> 402 (+2): #227 (PR #308) routes this function's `backend_factory`
    # through `make_backend`, which needs `backend="claude"` and
    # `config=config.data` where the direct `ClaudeBackend(...)` needed neither.
    # Both load-bearing: without `config` the factory cannot read
    # `llm.permission_mode`, the key that PR exists to honour.
    # Measured on the merge result with the scanner below.
    "cli/commands.py:bench_run": 402,
    # Grew to 304 (> 300) when D3.1 (2026-08-31, auto-activation pipeline)
    # threaded `learning.auto_manage`/`learning.auto_activate_daily_cap`
    # through `nh serve`'s `HarvestJob` construction — the kill switch's own
    # config must reach the CLI-driven scheduling path, not just the API
    # server's embedded one. Reviewed on its merits; frozen here.
    "cli/commands.py:serve": 304,
    # Grew to 371 when the UI-evidence prompt block landed (task 389210fa):
    # an inline enable+glob gate + the ui_evidence_block call. Reviewed on
    # its merits (the block is inert until a profile opts in); frozen here.
    # 371 -> 381 (+10): follow-up to ce4d4a73 (#151) -- the staleness
    # narration now decides with `should_rebase(...)` instead of the bare
    # `commits_behind >= threshold`, and names the overlapping files when a
    # below-threshold overlap-triggered rebase failed to complete. Measured
    # with the scanner below.
    # 381 -> 394 (+13): a rebased task branch could never be delivered (a
    # rebase of an already-pushed branch makes the old remote tip mutually
    # unreachable with the new head, so delivery's ancestor check refused
    # it forever). `base_staleness` now merges instead of rebasing a pushed
    # branch, and this preamble gained an `elif stale.get("merged")` branch
    # narrating that case to the coder. Measured with the scanner below.
    # 394 -> 423 (+29): pushed-tip rewrite fix (2026-09-13) — a new branch
    # for the `merge_conflict` case (base-refresh skipped a merge on a
    # conflict) tells the coder to run `git merge <base_pin>` themselves via
    # `base_merge_conflict_instruction`, instead of the old rebase-oriented
    # preamble text. Measured on this tree with the scanner below.
    # 423 -> 431 (+8): B2 fix (pushed-tip guard review round) — the
    # `merge_conflict` branch's comment now explains why the preamble can't
    # claim "CONFLICT" specifically (the flag also covers any other
    # exception `_refresh_stale_base` caught), and the preamble text itself
    # changed to the provable-in-every-case "did not finish — no partial
    # merge was left in progress." Measured on this tree with the scanner
    # below.
    "core/orchestrator.py:Orchestrator._build_implement_prompt": 431,
    # 332 -> 333 (+1): pin-rederivation follow-up adds one
    # `pin_rederivation_note(card),` line to the markdown body list so the
    # published report carries the same recorded-branch/HEAD-fallback
    # disclosure as the terminal (see the `bench_run` note above). Measured
    # on the merge result with the scanner below.
    "eval/northstar_card.py:render_northstar_md": 333,
    "core/orchestrator.py:Orchestrator._reformat_summary_markdown": 327,
    "core/orchestrator.py:Orchestrator._generate_plan": 322,
    "core/orchestrator.py:Orchestrator._scan_leaf_blocks": 319,
    "core/orchestrator.py:Orchestrator._escalate_reviewer_unavailable": 317,
    # First frozen at 314 (> 300): `_citation_drift_preflight` is new to
    # this task (the whole "citation drift gets a preflight round" feature),
    # never previously in this table. Its length is not padding — two
    # send-back findings from one review round both
    # landed inside this one method: (1) the docstring's closing paragraph
    # was rewritten from a one-sentence "changes nothing about the BAR"
    # claim into an explicit, honest disclosure that this preflight's
    # notion of "a citation" is narrower than TESTING's own checker run
    # (`docs/WINDOWS.md` citations are outside `CITATION_TABLE` and so
    # outside this preflight's view — a correctness claim that was simply
    # false before the fix), and (2) both `_revert_worktree_writes` call
    # sites in this method gained a `reason=` argument so the advisory they
    # emit stops blaming this preflight for "writing despite being told not
    # to" when writing mechanically is its entire job. Reviewed on its
    # merits; frozen here as its landing baseline, measured on this tree
    # with the scanner below.
    # 314 -> 322 (+8): the send-back round that corrected the docstring's
    # "Honest limit" paragraph (closed-set `_CITATION_DOC_PATHS` list spelled
    # out, "strictly wider" replaced with the true "incomparable" claim about
    # TESTING's scope) and the leading "never costs a whole attempt" summary
    # sentence gained its own qualifying clause. Re-measured directly with
    # the scanner below on this tree, not carried over as a stale delta.
    "core/orchestrator.py:Orchestrator._citation_drift_preflight": 322,
    # Grew to 314 (> 300) when the done_no_evidence repair shape landed
    # (task bf413cc6): two new refusal guards + the DONE branch. The growth
    # was reviewed on its merits; frozen here as its landing baseline.
    # Grew 315 -> 322 (+7) when the escalated_hand_landed shape landed
    # (commit ee792bfa): a new terminal state for an escalated task a human
    # landed by hand, gated like failed_pre_pr/pending_never_ran. Reviewed
    # on its merits; re-anchored here as its landing baseline.
    "blockers/landed_override.py:approve_landed_override": 322,
    # +43: folds the whole unattributed_usage ledger (owned + ownerless) into
    # cost_usd_total/tokens_total and exposes ledger_owned_tokens/
    # ledger_ownerless_tokens, so per-task pre-attempt spend agrees with
    # `nh status` and the board (see TEST PLAN AC1).
    "core/metrics.py:compute_metrics": 389,
    # NEW (324, > 300): mechanical resolution extended to cover
    # `tests/test_structural_budget.py` FROZEN_* numeric-only conflicts --
    # the new budget-hunk branch in the worktree merge-step loop, the
    # ship-classified-paths extension, the names-to-add extension, the
    # re-anchor proof-test call, and the extended final-return detail
    # string; re-homed onto the tree where the inventory-backend dispatch
    # (97a9fd79b) also lives in this function. Measured on this merged tree
    # with the scanner below.
    "vcs/derived_conflict.py:_resolve_in_worktree": 324,
    # NEW (322, > 300): fix for "a red pre-review test run reaches the coder
    # even when the review fails" — `_build_review_prompt` gains the
    # `failing_test_ids`/`failing_test_ids_dropped` keyword-only params and
    # the fixed, deterministic `failing_ids_section` block (independent of
    # the 4,000-char `_OUTPUT_CAP`'d `test_output` excerpt, applied by the
    # caller, not this function) that lists the harness's own pre-review
    # failing test ids and tells the reviewer a PASS verdict over them will
    # be rejected regardless. Measured on this tree with the scanner below.
    # 322 -> 326 (+4): send-back on the above (MAJOR-1) — reworded the
    # comment above `failing_ids_section` (see the file-line entry above for
    # the full rationale: `Orchestrator._run_review` no longer forces
    # `decision.passed = False` off this section alone; the post-review
    # TESTING step is the sole gate now). The rendered `failing_ids_section`
    # text is byte-identical to before — asserted verbatim by
    # `test_build_review_prompt_carries_fixed_failing_ids_section`. Measured
    # on this tree with the scanner below.
    # Landing of 4e0299ad (hand-finished after the independent review of
    # 429b471f): 326 -> 327 (+1): the failing-ids section tells the reviewer a
    # PASS cannot excuse the red run — on any round the review passes, the
    # harness's testing step classifies it (the earlier text promised a
    # rejection that an excused red never gets). Measured on this tree with
    # the scanner below.
    # 327 -> 342 (+15): follow-up to 4e0299ad — the section gave the reviewer
    # NO indication the ids were unattributed at review time, so a critical
    # verdict was ordered unconditionally. 86b5bf3d rounds 3 and 4 were both
    # failed on `tests/test_guard.py`'s redness, for tests neither coder
    # touched. Reworded the
    # comment above `failing_ids_section` and its text to say the harness
    # has NOT yet attributed the ids, a base-tree-red id is not this
    # change's defect, the post-review TESTING step decides, and "critical
    # severity" applies only when the diff plausibly explains the failure.
    # No classification, base checkout, or extra test run added at review
    # time.
    # 341 -> 347 (+6): comment only. The wording above pointed at a task that
    # is `failed` and called the `tests/test_guard.py` redness environmental.
    # It was neither: those tests inherited the runner's PATH instead of
    # building the environment they meant to exercise, and were fixed by
    # pinning `env=` (8b85472d). The frozen 342 was itself stale — the base
    # measures 341, and a frozen value ABOVE the measurement passes silently,
    # which is why it went unnoticed. Measured on this tree with the scanner
    # below.
    "review/reviewer.py:_build_review_prompt": 347,
}

# 5 functions with estimated cyclomatic complexity > 60.
FROZEN_FUNCTION_CC = {
    # 250 -> 251 (+1): P2's `kind = "stuck" if ... else "non-converging"`
    # dispatch (an IfExp) in the merged StuckAbort/ConvergenceAbort handler.
    # 251 -> 252 (+1): the `if reason != SERVER_STOP_REASON` guard on the
    # direct-unwind cancel-reason write. Measured on this tree.
    # 252 -> 255 (+3): the structural-budget preflight call site's two
    # `except` clauses plus the `if budget_outcome is not None` guard —
    # same try/except shape as the repro-gate call site above it. Measured
    # on this tree with the scanner below.
    # 255 -> 257 (+2): attribution-guard hardening — the `if base:` guard
    # around the `ls_remote_exact` pin capture, plus the fail-closed
    # `if base_pin is None:` advisory branch when the pin doesn't resolve.
    # Measured on this tree with the scanner below.
    # 257 -> 264 (+7): an earlier, review-failed version of the send-back
    # "no changes needed" fix kept the guard pair (`_send_back_resume_round`
    # / `pr.url`) inline in `_run_attempt` itself.
    # 264 -> 258 (-6): the review fix delegates both guards, plus the
    # validated status-transition branching, into `_land_no_changes_needed`
    # — `_run_attempt`'s own zero-diff site is now just the delegating call
    # plus its `if landed is not None:` check.
    # 258 -> 235: re-frozen at the measured value on this tree after the
    # red-run failure-blocks fix (task 3bccb499). Main measures 232 and the
    # fix is +3 CC (the old 258 was 26 CC of slack). Measured on this tree
    # with the scanner below.
    # 235 -> 240 (+5): the report-nudge call site's try/except and its
    # once-guard in `_run_attempt` (task 7a7713e3, landed on top of 3bccb499's
    # 235). Measured on this tree with the scanner below.
    # 240 -> 243 (+3): the `_node_serial_rerun` call site's `if serial_passed:`
    # branch in the plain-red branch (tasks ba602e95 / f8af7f46, 2026-09-08).
    # Measured on this tree with the scanner below.
    # 243 -> 247 (+4): 99fa5ba5 (bounded failing_tests and joins at
    # every write site; the carried dropped count reaches the coder prompt)
    # landed on top of cf6f6c19. Measured on this tree by the scanner's own metric.
    # 247 -> 250 (+3) (task 30a97d8a): the single-write fix's TESTING-side
    # reuse check in the plain branch — `reused_pre_review = bool(pre) and
    # pre[0] is test_result`, then `if reused_pre_review: ... else:` around
    # the `_red_test_detail` call, plus a second `if not reused_pre_review:`
    # guarding TESTING's own red `tests` emit so it only fires when this
    # step is NOT reusing the pre-review block's render. Measured on this
    # tree with the scanner below.
    # 250 -> 251 (+1): #114 phase 2 adds `or type_hook is not None` to the
    # `backend_degraded` condition, so that BoolOp carries one more value.
    # Re-measured on the merge result.
    # 251 -> 254 (+3): citation-drift preflight call site — the
    # `try/except CancelRequested` / `except (BudgetAbort, StuckAbort,
    # ConvergenceAbort)` wrapper around the `_citation_drift_preflight`
    # call adds one `try` handler branch each. Re-measured on the merge
    # result with the scanner below.
    "core/orchestrator.py:Orchestrator._run_attempt": 254,
    # Landing of 4e0299ad: unchanged at 115 — the harness row is dropped by
    # the comprehension filter inside `_reviewer_items`, which the scanner
    # counts the same as the `if` it replaced (the first landing pass had a
    # three-clause condition in `_failing_labels`, measured 116; the second
    # pass moved the check into `_reviewer_items`, measured 115). Measured on
    # this tree with the scanner below.
    "core/orchestrator.py:Orchestrator._drive": 115,
    "agent/guard.py:_approve_denial": 81,
    # 73 -> 74 (+1): same cause as the LINES entry above — e922e9b4's landing
    # grew the conflict watcher; pre-existing red on main at 03b262d23,
    # repaired (measured) on this merge.
    "blockers/wake.py:WakeWatcher._check_pr_conflict": 74,
    # 73 -> 74 (+1): same verifier-wall-park cause as the LINES entry above
    # — the added `result.is_error` branch is one more `If`. Measured on
    # this tree.
    # 74 -> 77 (+3): task 90097 (round 4), same cause as the LINES entry
    # above — the flag's `if unjudged_checkpoint_head:` branch is replaced
    # by a `try/except Exception` around `self._is_own_partial(...)` plus
    # its own `if own_unjudged_partial:` branch (one extra `If` for the
    # `except`, one for the guard). Measured on this tree.
    # 77 -> 88 (+11): fix for "a red pre-review test run reaches the coder
    # even when the review fails" - same cause as the LINES entry above: the
    # pre-review-red detection if/emission, the nested
    # `_pre_review_red_checklist_item()` helper's own `if pre_review_red` /
    # `if pre_review_failing_ids` / `else` branches (counted toward the
    # enclosing function per the scanner's nested-function rule), and the
    # three `if pre_review_item is not None:` guards at each
    # `ReviewDecision`-returning exit. Measured on this tree with the
    # scanner below.
    # 88 -> 89 (+1): send-back on the above (MAJOR-1) — the final block no
    # longer unconditionally forces `decision.passed = False` (see the LINES
    # entry above for the full rationale); it now only attaches the
    # checklist item under a new `if not decision.passed:` guard, so the
    # item rides into `_record_review_feedback` when the round is ALREADY
    # failing for another reason, without ever failing a round on its own.
    # One new `If`, net. Measured on this tree with the scanner below.
    # 89 -> 90 (+1) (task 30a97d8a): the pre-review block's red `tests` event
    # now bounds `failing_tests` the same way TESTING's own red event does
    # (`_kept, _dropped = _bounded_failing_ids(failing_tests)`), so when this
    # event ends up being the ONLY red `tests` event of the round (TESTING
    # reused its render) it carries the same shape TESTING's would have —
    # `**({"failing_tests_dropped": _dropped} if _dropped else {})` is one
    # new conditional expression. Measured on this tree with the scanner
    # below.
    "core/orchestrator.py:Orchestrator._run_review": 90,
    # Crossed 60 (to 67) with the UI-evidence gate landed by task 389210fa.
    # 67 -> 70 (+3): follow-up to ce4d4a73 (#151) -- one new `if overlap:`
    # block (+1) plus two `stale.get(...) or []` BoolOps (+1 each). Measured
    # with the scanner below.
    # 70 -> 71 (+1): a rebased task branch could never be delivered (see the
    # FROZEN_FUNCTION_LINES entry above for the incident) -- one new `elif
    # stale.get("merged"):` branch narrates the merged case to the coder.
    # Measured with the scanner below.
    # 71 -> 79 (+8): pushed-tip rewrite fix (2026-09-13) -- the new
    # `merge_conflict` branch (see the FROZEN_FUNCTION_LINES entry above)
    # adds an `if merge_conflict:` / `if overlap:` pair and an `or []` and
    # an `or "the current base"` BoolOp-style fallback for the base_pin
    # lookup. Measured on this tree with the scanner below.
    "core/orchestrator.py:Orchestrator._build_implement_prompt": 79,
}

# 9 files > 2,500 lines.
#
# RE-ANCHORED 2026-08-26: the ratchet landed as b30a292da at 19:51:28 and
# three more branches landed in the same batch — b516f9da7 19:51:40 (grew
# core/scheduler.py 2499 -> 2522, crossing the 2,500 threshold), 1bc8e1fc8
# 19:52:45 (grew core/db.py 4131 -> 4149 and core/orchestrator.py
# 19222 -> 19300) — none of them measured against the ratchet on its merge
# result, so every full suite on main failed these two tests from the moment
# the batch finished. The values below are the first baseline measured on a
# tree that actually contains the ratchet; growth from here fails again.
# RE-ANCHORED 2026-08-27 (supervising session, on main 866cc50c3e): as with
# the 2026-08-26 batch above, three file entries had drifted red on main
# WITHOUT being re-frozen on a merge result — core/db.py 4149 -> 4251 and
# config.py 2888 -> 2889 are pre-existing fleet growth this session never
# touched; api/app.py 5032 -> 5153 was already over (5141 at this session's
# start, d3d7d3a82a) plus +12 from the bf413cc6 landed-override repair. The
# full suite failed test_no_frozen_entry_has_grown from before this session
# began; re-anchored to the measured baseline so the ratchet gates growth
# from here again. (A red ratchet the fleet lands through protects nothing.)
FROZEN_FILE_LINES = {
    # 19354 -> 19387 (+33) with the UI-evidence gate landed by task 389210fa.
    # 19599 -> 19630 (+31): three `WorktreeCheckFailed` catch-site audit
    # comments in `_run_reviewer` (task reviewer-worktree-returncode-audit) —
    # comments only, no behaviour change.
    # 19630 -> 19650 (+20): PR #929's fail-closed .git/common/config allowlist
    # (a benign shared-config reserialization no longer discards the reviewer
    # verdict; execution-surface keys still discard). Re-anchored on merge.
    # 19650 -> 19659 (+9): PR #877's tamper-base widening in _run_attempt (same
    # +9 lines as the function bump above). Re-anchored on merge.
    # 19659 -> 19724 (+65): PR #867 threads a backend kwarg through the budget/
    # pricing sites (output_extra_weight/weighted_tokens/class_breakdown callers).
    # 19724 -> 19763 (+39): consume-once semantics for the human resume_from
    # gate — `_consume_human_gate` (+ two call sites in `_run_attempt`),
    # `_honor_server_stop`/`_is_own_partial` routed through the shared
    # `human_gate_armed`/`is_human_provenance` predicates. Measured directly
    # against the rebased tree with the scanner below (19763 total lines).
    # 19763 -> 20029 (+266): D1.1 (short PR bodies) — the split of
    # `_verification_section` (short) from `_verification_appendix` (full),
    # the attempt-scoped artifact writer + display-path helper, the runtime
    # body-budget trim, and both rounds' anchored docstrings. Measured on the
    # D1.1 squash-merge result (re-anchored on merge, per this file's note).
    # 20029 -> 20263 (+234): D1.2 (visual proof) — `_maybe_capture_ui_evidence`
    # + `_deliver_ui_evidence` + the media-section renderer and their anchored
    # docstrings. Measured on the D1.2 cherry-pick result with the scanner below.
    # 20263 -> 20293 (+30): D3.1 (auto-activation) — the per-injection
    # learning_events audit loop + trigger_reason call in the injection site.
    # Measured on the D3.1 landing result.
    # 20293 -> 20296 (+3): `_gate_already_satisfied` docstring rewrite
    # (falsified-comment fix, PR #940) — comments only, no behaviour change.
    # Measured on this rebased tree with the scanner below (the scanner's
    # own `str.splitlines()` metric, not `wc -l`: the file's pre-existing
    # `_LINE_BREAKS` regex embeds literal U+0085/U+2028/U+2029 line-separator
    # characters that `splitlines()` treats as breaks and `wc -l` does not,
    # a 3-line difference between the two counts that predates this PR).
    # 20296 -> 20388 (+92): P2 (turn-cap convergence early-abort) —
    # `ConvergenceAbort`, `_abort_kind`, the `_TEST_RUNNER_RE`/
    # `_looks_like_test_run` helper, the `_agent_sink` tick/mark_progress
    # wiring, the per-attempt `ConvergenceTracker` reset, and the
    # `ConvergenceAbort` additions to five existing abort call sites — all
    # with anchored docstrings/comments explaining why. Measured on this
    # tree with the scanner below.
    # 20388 -> 20507 (+119): P2 review-fix round 2 — `_active_convergence`
    # (the task-id-scoped read helper, with its anchored docstring), the
    # agent-owned-scratch-write progress branch, the `_run_code_review`
    # diff-fetch explicit disarm, and the leading-token read-only rejection
    # in `_looks_like_test_run` (`_READ_ONLY_LEADING_TOKENS`/
    # `_GIT_READ_ONLY_SUBCOMMANDS`/`_SHELL_SEGMENT_RE`). Measured on this
    # tree with the scanner below.
    # 20507 -> 20513 (+6): efficiency pair — the batched
    # `record_learning_events` call replacing the per-injection loop at the
    # memory-injection site. Measured on the pair's merge result.
    # 20513 -> 20523 (+10): human-hold durability fix (SCRUM-22 regression) —
    # `_raise_blocker` and `_park_quota` now route their blocker-replacement
    # writes through `carry_human_hold` so a durable human pause survives a
    # machine blocker rewrite. Re-anchored on this rebased tree (main had
    # independently grown the file to 20513 lines by the time this landed).
    # 20577 -> 20592 (+15 net on this rebased tree, per the scanner's own
    # `len(Path(...).read_text().splitlines())` metric): `_maybe_capture_ui_
    # evidence` now emits the honesty-floor skip line (with remedy) instead
    # of `""` when the UI-evidence gate qualifies but playwright is
    # unavailable. Re-anchored on this rebase (main had independently grown
    # the file to 20577 lines by the time this landed).
    # 20592 -> 20625 (+33 per the scanner's own
    # `len(Path(...).read_text().splitlines())` metric, which counts 3
    # pre-existing Unicode line-separator characters — U+0085/U+2028/U+2029 —
    # that `wc -l` does not; the file grew +30 by `wc -l`): the honesty-floor
    # disclosure policy is now self-consistent — the three previously-silent
    # `return ""` exits (no manifest written, `ui_evidence.run` raising, and
    # a walk that ran but captured zero shots) now go through the same
    # `_ui_evidence_skipped` builder as the missing-playwright case, each
    # naming what was lost (sanitized: exception class only, reason
    # newline/backtick-stripped and truncated to ~120 chars — the PR body is
    # a public/untrusted-readable surface). Only the gate-says-no exit
    # (`ui_evidence_should_run` is False) stays silent, since there is
    # nothing to disclose. Measured on this landing.
    # 20625 -> 20636 (+11 per the scanner's own metric): the honesty-floor
    # comment above `_UI_EVIDENCE_SKIPPED_SECTION` and the
    # `_maybe_capture_ui_evidence` docstring no longer claim "every exit
    # discloses" — both now state the actual boundary: disclosure covers
    # every gated-but-no-shots outcome, but `_deliver_ui_evidence`'s own
    # pre-existing "" (shots captured, then delivery fails — push rejected,
    # non-GitHub remote, a `current_branch`/commit error) is unchanged and
    # out of scope here. Comments only, no behaviour change.
    # 20612+20636 -> 20673: merge of two independent landings — main's
    # profile-divergence advisory (+35: `_profile_divergence_warned` latch +
    # `_warn_profile_divergence` from `_usable_profile`) and this branch's
    # disclosure work above, plus the review-round comment completion (+2).
    # Measured on the merge result with the scanner below, never summed.
    # 20673 -> 20789 (+116): the declared-repro-files-committed
    # preflight — `_declared_files_preflight`, `_DECLARED_FILES_ROUND_TURNS`,
    # `declared_files_send_back_message`, and the `_repro_gate_step` /
    # `_repro_corrective_round` wiring for it, plus the three
    # reviewer-worktree-returncode-audit comment blocks rebased in from
    # origin/main's own history. Measured on this rebased tree with the
    # scanner below (the scanner's own `len(Path(...).read_text().
    # splitlines())` metric — confirmed by reading `scan_source()`'s
    # implementation, which returns `len(text.splitlines())`, not `wc -l`).
    #
    # 20789 + main's independent 20673 -> 20899 (+226, mechanical PR-body
    # fallback for a classifier-rejected coder final message —
    # `_mechanical_changes_summary`, `_render_mechanical`, `_trim_mechanical`,
    # and the new constants (`_MECHANICAL_LABEL`, `_MECH_MAX_COMMITS`/
    # `_MECH_MAX_FILES`, `_DERIVED_LEDGER_BASENAMES`, `_ABS_PATH_RE`), plus
    # threading `repo`/`base`/`mechanical` through `_summary_section` and
    # `_pr_body`) merge to 21015, the exact sum of both deltas (116 + 226 =
    # 342, 20673 + 342 = 21015) — confirmed via
    # `len(Path("src/no_human/core/orchestrator.py").read_text().
    # splitlines())`, the scanner's own metric, which (unlike `wc -l`) also
    # counts main's `_LINE_BREAKS` regex's three literal Unicode
    # line-separator characters (NEL/LS/PS) as line breaks.
    #
    # 21015 -> 21065 (+50): a second independent main landing on top of
    # this merge — the ui_evidence dev-server boot wiring in
    # `_maybe_capture_ui_evidence`/`_deliver_ui_evidence` (the harness now
    # boots the repo's configured `start_cmd` and discloses which server it
    # walked). Measured on this merge result with the scanner below, never
    # summed by hand.
    # 21065 -> 21123 (+58): the reviewer role-backend seam's disclosure and
    # construction wiring (6d part 1). Measured on the merge result with the
    # scanner below, never summed.
    #
    # 21123 -> 21134 (+11): `_maybe_capture_ui_evidence`'s boot-failed reason
    # now branches on `srv.cause` to render a distinct failed-to-start
    # sentence alongside the byte-unchanged timeout sentence (task:
    # _kill_dev_server test coverage). Measured with the scanner below after
    # rebasing onto the role-backend landing, never summed by hand.
    # 21136 -> 21147 (+11): the direct-unwind cancel-reason write (guard +
    # reasoning comment) in _run_attempt, landed on main (de401ec99) ahead of
    # this branch's base.
    #
    # 21147 -> 21188 (+41, rebased onto the above): `_maybe_capture_ui_evidence`
    # now arms `ui_evidence.hermetic_backend` (a throwaway-HOME `nh start`)
    # before `dev_server`, and skips the walk with a disclosed
    # `walk_skip::hermetic_backend_*` reason when it fails to arm or when a
    # pre-existing dev server can't be bound to it — the fix for a walk being
    # able to write into the operator's live `~/.no_human/config.yaml` via a
    # dev server that proxied straight at the real `:8420` board. Measured on
    # this rebased tree with the scanner below (`len(Path(...).read_text().
    # splitlines())`), never summed by hand.
    #
    # 21188 -> 21191 (+3): follow-up to the hermetic-backend walk (PR #1015
    # review) — `_maybe_capture_ui_evidence` now passes `auth_mode=` into
    # `ui_evidence.hermetic_backend` and, on a pre-existing dev server it
    # cannot bind to the hermetic target, DISCLOSES and still runs the walk
    # instead of skipping it. Measured with the scanner below, never summed
    # by hand.
    # 21191 -> 21198 (+7): `_maybe_capture_ui_evidence`'s `boot-failed`
    # reason branch gains two causes (`"build-timeout"`/`"build-failed"`) so
    # a `ui_evidence.build_cmd` failure/timeout names the build instead of
    # falling through to the generic dev-server sentence. Measured with the
    # scanner below, never summed by hand.
    #
    # 21198 -> 21275 (+77): dispatch-time intake-eval hoisted path for
    # grill/wizard-sourced tasks — the `elif ctx.get("eval_result")` branch
    # plus `_act_on_stored_eval`/`_write_eval_ctx` helpers and the
    # `_act_on_eval` merge-not-clobber rewrite, rebased onto the
    # `build_cmd` change above. Measured with the scanner's own
    # `len(Path(...).read_text().splitlines())` metric (which, unlike
    # `wc -l`, also counts the file's 3 pre-existing Unicode line-break
    # characters inside `_LINE_BREAKS`'s regex), never summed by hand.
    #
    # 21275 -> 21305 (+30): verifier-wall-park bugfix (task 57f38618,
    # re-homed from the pre-cutover world) — `api_wall_reason` import, the
    # `_raise_if_verifier_quota_wall` -> `_raise_if_verifier_wall` widening
    # (new `result` param, `AgentResult`/`_infra_sdk_failure` classification)
    # and its two call sites in `_run_review`. Measured with the scanner
    # below on this tree.
    #
    # 21191 -> 21323 (+132, rebased onto the above): the structural-budget
    # preflight — the `structural_budget` import, module-level
    # `structural_budget_send_back_message`, the
    # `_STRUCTURAL_BUDGET_ROUND_TURNS` constant, the
    # `_structural_budget_preflight` method, and its call site in
    # `_run_attempt` — so a diff that grows a frozen entry gets the guard's
    # own failure fed back and re-anchored inside the SAME attempt instead
    # of costing a whole extra one. Measured on this rebased tree with the
    # scanner below (`len(Path(...).read_text().splitlines())`), never
    # summed by hand.
    # 21323 -> 21329 (+6): the fix for the stale-.pyc race this same
    # preflight's corrective round could hit against its own guard file —
    # `structural_budget.invalidate_guard_cache` call site in
    # `_structural_budget_preflight`, right after the round returns and
    # before the post-round bounded re-run. Measured on this tree with the
    # scanner below.
    # 21329 -> 21398 (+69): the widened preflight — computing `scanned_root`/
    # `touches_scanned_root` alongside `frozen_paths`/`touched_frozen` so a
    # brand-new offender or a stale (shrunk/vanished) frozen entry also buys
    # a corrective round, switching the bounded re-run from
    # `bounded_growth_command` to `bounded_guard_command` (the whole guard
    # file, not just the growth node id), the generalized
    # `structural_budget_send_back_message` (now naming whichever paths
    # triggered it, not only a touched-frozen path), and the
    # `event_kind="structural_budget_corrective_round"` /
    # `cause="structural_budget"` overrides on the `_repro_corrective_round`
    # call so this preflight's corrective rounds are distinguishable from the
    # repro gate's own in telemetry. Measured on this tree with the scanner
    # below.
    # Re-home merge 2026-09-04: all of the above now live on ONE tree
    # (build_cmd +7, intake-eval +77, verifier-wall +30, preflight
    # chain +132/+6/+69 rebased together). Measured on this merged
    # tree with the scanner below, never summed by hand.
    #
    # 21191 -> 21213 (+22, per the scanner's own
    # `len(Path(...).read_text().splitlines())` metric — the pre-existing
    # `_LINE_BREAKS` regex's literal U+0085/U+2028/U+2029 characters keep the
    # splitlines() count 3 above `wc -l`, same discrepancy noted at the
    # 20293 -> 20296 entry above; `wc -l` alone reports +19/21210 for this
    # change): the WIP-checkpoint resume-digest sentence — the `base` kwarg
    # threaded through `_run_attempt`'s `_build_implement_prompt` call,
    # `_build_implement_prompt` itself, and `_resume_digest` (5 lines
    # touched, 0 net new — each just gained a `base=` argument on an existing
    # line), plus the `attempt_n` field written into the handoff dict by
    # `_record_wip_checkpoint` (+11) and by `_persist_handoff` (+12, including
    # the one-line `"attempt_n": attempt_n,` entry in its returned dict).
    # A same-session one-turn already-satisfied correction
    # (`_WIP_SUBJECT_REASON`, `_WIP_CLAIM_CORRECTION_MARKER`,
    # `_WIP_CLAIM_CORRECTION`, `_wip_claim_correction`) was tried and briefly
    # pushed this to 21354, then WITHDRAWN on independent review (task
    # bf645f3a: coder sessions never resume across attempts, so a
    # same-session correction turn cannot fix a cross-attempt mistaken-claim
    # bug, and its abort-exception path had no handler at its unique call
    # site inside `_gate_already_satisfied`) and removed in full, along with
    # its `tests/test_server_stop_checkpoint.py` registration — see
    # `tests/test_already_satisfied_wip_correction.py`. Measured with the
    # scanner below, never summed by hand.
    #
    # 21213 -> 21247 (+34, rebased onto the above): task a47e5330 —
    # `_mechanical_round` gained a third conjunct (the PASS-carrying attempt
    # row must not itself have ended `status="failed"`) so tests-failed /
    # CI-red / invocation-error rounds after a review PASS re-arm the
    # lifetime cap instead of reading as a free mechanical round forever,
    # plus the docstring naming it and a `_check_lifetime_budget` addition
    # that appends the last recorded failure reason to the BUDGET_EXHAUSTED
    # blocker. Measured with the scanner below, never summed by hand.
    # Re-home merge 2026-09-04 (279c03c5): the WIP-checkpoint resume
    # correction lands on the same tree as the chain above. Measured
    # on this merged tree with the scanner below, never summed.
    # 21502 -> 21564 (+62, `len(Path(...).read_text().splitlines())` — the
    # scanner's own metric, which counts 3 above `wc -l` for this
    # pre-existing file, same discrepancy noted elsewhere in this file):
    # 2026-09-04 lifetime-cap follow-ups — the `_attempt_recency`
    # module-level helper; `_mechanical_round` gained the
    # `require_mechanical_feedback` kwarg and its third conjunct (no attempt
    # newer than the PASS-carrying row recorded a failure, via the new
    # `Store.latest_failed_attempt`); `_budget_exhausted_blocker` now quotes
    # the newest FAILED attempt's `failure_reason` (not the newest verdict-
    # carrying attempt) via a fail-open read; `_resume_human_gated`'s
    # `mechanical=` stamp routes through `_mechanical_round(...,
    # require_mechanical_feedback=False)` instead of a bare
    # `latest_review_verdict(...) == 1` check. Measured on this tree with the
    # scanner below, never summed by hand.
    #
    # 21502 -> 21572 (+70, rebased onto the above): default UI walk when the
    # coder writes no manifest (2026-09-04) — new `_default_walk_manifest`
    # static helper builds the harness's OWN fallback `ui_evidence.Manifest`
    # from the confirmed profile's `base_url`/`ready_path` when no coder
    # `.no_human/ui_evidence.json` exists, `_maybe_capture_ui_evidence` wires
    # it through `ui_evidence.run(..., manifest=...)` and threads a new
    # `default_walk` flag into `_deliver_ui_evidence`, which labels the
    # rendered section "default walk (no coder manifest)" instead of the old
    # unconditional skip text — visual proof must not depend on coder
    # compliance. Measured with the scanner below, never summed by hand.
    # Supervised-land merge 2026-09-04: lifetime-cap (+62) and default-walk
    # (+70) now on one tree. Measured on this merged tree with the scanner
    # below, never summed by hand.
    # 21502 -> 21503 (+1): the reviewer-worktree non-benign-config-key naming
    # task threads `nonbenign_config_keys=list(getattr(delta, "nonbenign_keys",
    # []))` into the `reviewer_wrote` event emit — one kwarg. Measured with
    # the scanner below (`len(Path(...).read_text().splitlines())`).
    # Supervised-land merge 2026-09-04: default-walk chain + the reviewer
    # config-key naming (+1) on one tree. Measured on this merged tree.
    # 21502 -> 21516 (+14): already-satisfied approve-on-DONE landing fix —
    # `_gate_already_satisfied` now persists `already_satisfied_landing`
    # (on_base/sha/branch/ship_ref) and the state/blocker detail text names
    # which of the two landing cases applied. Measured via
    # `wc -l src/no_human/core/orchestrator.py`.
    # Supervised-land merge 2026-09-04: already-satisfied-land fix on the
    # current tree. Measured on this merged tree.
    # 21635 -> 21748 (+113, rebased onto the above): attribution guard
    # hardening — the base-sha pin capture in `_run_attempt` (docstring
    # above the call site plus the fail-closed advisory/log branch),
    # `_base_exclusion_refs` and its `base_pin`-aware paragraph on
    # `_foreign_authored_commits`'s docstring, and the `_protect_base_branch`
    # hook-refresh call, rebased onto the reviewer-worktree tree above.
    # Measured on this merged tree with the scanner below, never summed by
    # hand.
    # 21762 -> 21788 (+26): task_failed reason_category wiring — threads an
    # optional `reason_category` kwarg through `_raise_blocker` (signature +
    # docstring note), stamps `blocker_category=blocker.category.value` onto
    # its emit so `_telemetry_hook` can resolve BUDGET_EXHAUSTED-caused
    # failures correctly, and tags the max_attempts/tamper_blocked call
    # sites explicitly. Measured via `wc -l`/the scanner below; growth is
    # the minimal functional diff after trimming comments/docstrings.
    # 21788 -> 21860 (+72, corrected from a stale "+69" -- `wc -l`, the
    # scanner and `git diff --numstat` all agreed at +72 measured on that
    # tree): profile-declared worktree setup commands, MAJOR-2 fix -- setup
    # ran *inside* `_drive_watched` via a `setup_in` kwarg and the new
    # `_run_worktree_setup` helper, so the cancellation watcher was already
    # alive and `nh task cancel` was observed during a long setup command
    # instead of being ignored for its whole duration. The helper resolves
    # `_usable_profile`, marshals every `run_setup_commands` emit onto the
    # loop thread with `loop.call_soon_threadsafe(self.emit, ...)` (the
    # worker thread from `asyncio.to_thread` is not safe to call
    # `self.emit` from directly), polls `_pending_cancel` against the
    # module-level `_CANCEL_POLL_SECONDS`, and kills the setup process tree
    # via `runner.terminate_running(wt_path)` + `_honor_cancel` on cancel.
    # 21860 -> 21880 (+20): round 4 fix. The `setup_in` kwarg above made
    # `_drive_watched` a 3-arg method, which TypeErrors the plain
    # `(task, repo)` fakes in tests/test_worktree_isolation.py and
    # tests/test_worktree_teardown.py -- those two files are never edited,
    # so `_drive_watched` is restored to exactly that signature and the
    # worktree path now travels via a single-use instance attribute
    # (`self._pending_setup_path`, set by `_run_task_body`'s worktree
    # branch immediately before the call, consumed -- read then cleared --
    # the moment `_drive_watched` runs) instead of a keyword argument.
    # Also corrects `_run_worktree_setup`'s docstring, which used to claim
    # a repo-supplied `.no_human/project.yml` "can never get to run shell
    # in this worktree" -- false: with no DB row, `_usable_profile` falls
    # back to that file, and its `setup_cmds` run exactly as its
    # `test_cmd` already does.
    # Measured via the scanner's own `len(text.splitlines())`: `wc -l`
    # reads 21877 against origin/main's 21785 (+92); `git diff --numstat
    # origin/main` reports 94 insertions/2 deletions (net +92 too); the
    # scanner counts 21880 against origin/main's 21788 (+92) -- all three
    # agree. (The scanner runs 3 higher than `wc -l` throughout, on the
    # single pre-existing NEL/LS/PS characters inside the `_LINE_BREAKS`
    # regex literal -- unrelated to this diff, unchanged by this branch.)
    # The scanner's own metric is what this test compares against, so
    # that is the value recorded here, not `wc -l`'s.
    # 21880 -> 22081 (+201): merge of environment-error classification
    # rounds 1-3 (no-human/a6cc4d39 @ fed228cc) on top of the setup_cmds
    # entry immediately above, additive with it (disjoint code regions):
    # round 2 (+175) extracted `_run_attempt`'s two failure-billing call
    # sites into `_layered_tests_failed_outcome` and `_failed_tests_outcome`
    # (own docstrings/comments, so `_run_attempt` itself nets DOWN against
    # its own frozen line/CC entry below); round 3 (+26) made
    # `_environment_test_failure`'s gate unconditional instead of `if not
    # invocation_error`, plus a comment/docstring paragraph each on
    # `owned_failing` and on node ids now reaching `failing_tests`
    # (`_node_tap_failing_tests`, `no_human/testing/runner.py`).
    # 22081 -> 22125 (+44) round-4 review fix, same merge: (1) MAJOR-1 —
    # `owned` (computed before `_environment_test_failure`, see
    # `_owned_failing_tests`) must win over the `invocation_error` branch's
    # base-tree check too, checked in `_run_attempt` right after the
    # `getattr(test_result, "invocation_error", False)` guard and BEFORE
    # `_invocation_error_reproduces_on_base` is awaited — previously an
    # owned id whose text also matched `_INVOCATION_ERROR_PATTERNS` (e.g.
    # "Cannot find module" from a test the attempt itself added) fell
    # through to that base-tree check, which reports "genuinely
    # environmental" against a diff-independent command and let the attempt
    # SUCCEED with a PR opened; (2) MINOR-2 — `_layered_tests_failed_
    # outcome`'s docstring corrected: it claimed "Extracted verbatim" but
    # carries ~19 lines of ownership/environment-classification logic a
    # pure code-move would not have added. `_run_attempt`'s own frozen
    # line/CC entry below is unchanged by either fix (the new code lives
    # inside the existing `if getattr(test_result, "invocation_error", ...)`
    # branch, no new call site). Measured via the scanner's own
    # `len(text.splitlines())`: `wc -l` reads 22122; the scanner counts
    # 22125 — the same +3 NEL/LS/PS offset noted above, unrelated to this
    # diff. The scanner's metric (22125) is what is recorded here.
    # 22125 -> 22271 (+146, on top of the environment-error entry above,
    # disjoint code regions): the no-op
    # send-back-resume feature (`_send_back_resume_round`,
    # `_land_no_changes_needed`, and their `_run_attempt` call site)
    # across all of its rounds, including round 5's replacement of the
    # round-3 `task.context["review_history"]` read with a predicate
    # keyed directly on the `attempts` table (`review_passed`,
    # `commit_sha`, `started_at`) — the real incident's rows never carry
    # a `review_history[].at` entry, so round 3's read could never have
    # fired on the data it names. Measured via the scanner's own
    # `len(Path(...).read_text().splitlines())` on this merged tree
    # (`wc -l` reads 22268; the same pre-existing +3 NEL/LS/PS-regex
    # offset noted above accounts for the gap).
    # 22271 -> 22285 (+14, `wc -l` 22268 -> 22282, `git diff --numstat`
    # 18 insertions/4 deletions net +14, all agree): follow-up to ce4d4a73
    # (#151) -- should_rebase-driven preamble narration, overlapping_files
    # on the base_staleness emit, and the docstring/comment updates below.
    # +172 (21880 -> 22052): "Delivery refuses reviewed commits" round 2.
    # Corrects the wrong "stale/unfetched remote tip" / "different
    # checkout" diagnosis in docstrings across git.py and this file
    # (`_reconcile_remote_branch`, `_ahead_reviewed_candidate`,
    # `_assert_delivery_sha`), replaces `_ahead_reviewed_candidate`'s
    # lexicographic-`min()` tie-break with a HEAD-preferring,
    # ancestry-aware selection that refuses on genuinely unrelated
    # stamped candidates, and adds the `_review_history_records` /
    # `_passing_review_shas_in_order` helpers `_ahead_reviewed_candidate`
    # needs to prefer the newest round's stamp. `wc -l` reads 22049
    # against this same scanner's 22052 (the pre-existing +3 offset noted
    # above, unchanged).
    # +4 (22052 -> 22056): task 0847f2c2 round-2 send-back — corrects
    # `_passing_review_shas`'s docstring (it never consulted list order,
    # `_ahead_reviewed_candidate` picks by DAG ancestry). `wc -l` reads
    # 22053 against this same scanner's 22056 (same pre-existing +3
    # offset). git.py's own `ProtectedBranch` check added in
    # `fast_forward_local_branch` isn't in this file, so isn't counted
    # here.
    # Re-measured on the fully rebased tree by the scanner's own metric.
    # 22461 -> 22473 (+12): task c1a0416d follow-up — `_build_supervisor` now
    # reads `task.context["send_back_feedback"]` inside a fail-closed
    # try/except (falling back to the `SEND_BACK_UNREADABLE` sentinel on any
    # exception) and threads it into the `SupervisorHook(...)` call, so the
    # in-run supervisor tier can honour a human send-back that supersedes the
    # original acceptance criteria instead of contradicting it. Measured on
    # this tree by the scanner below.
    # 22473 -> 22554 (+81, `wc -l` reads 22551 against this same scanner's
    # 22554, same +3 offset): task 90097 (round 3 of 0847f2c2, WIP-PARTIAL
    # checkpoint routing). `_run_attempt` hoists a `_route_unjudged_head`
    # call ahead of BOTH zero-diff terminals (the claim parse and the
    # silent no-file-changes fall-through) so a wake/machine resume
    # branching from its own `[WIP-BLOCKED]`/`[WIP-PARTIAL]` checkpoint is
    # routed to a full review instead of burning the attempt;
    # `_already_satisfied_eligible` gains the widened rule and its
    # docstring is rewritten; two new methods, `_head_is_wip_checkpoint`
    # (renamed/widened from `_head_is_blocked_checkpoint` to match both
    # prefixes) and `_route_unjudged_head`, are added ahead of
    # `_append_review_history`. Re-measured on the fully rebased tree
    # (trunk's delivery fast-forward fix merged first, this task's
    # checkpoint-routing fix on top) by the scanner's own metric.
    # 22554 -> 22585 (+31, `wc -l` 22582): task 90097 (round 4, same incident). Round 3's
    # unjudged-checkpoint-head instance flag — set in `_route_unjudged_head`,
    # read-then-reset in `_run_review`, guarding a `ReviewerUnavailable`
    # fail-closed branch — is removed: an undisclosed, out-of-brief instance
    # flag with a lifetime hole (armed across every non-review exit between
    # the hoist and the next `_run_review` call). `tests/
    # test_resume_wiring_round2.py::
    # test_a_machine_resume_is_not_credited_as_human_gated` and
    # `::test_a_revision_branch_sitting_on_an_abandoned_partial_is_not_
    # credited` now cover that routing with a FakeReviewer that fails the
    # diff, instead of relying on this flag. Deleting the flag outright
    # (net -31, back to a measured 22554 on the merged tree) reopened a different, pre-existing
    # bug: the no-reviewer/`allow_advisory` branch in `_run_review` then
    # rubber-stamped a routed diff unconditionally, silently re-breaking two
    # honesty-gate tests outside this ticket's file list (`tests/
    # test_resume_wiring.py::
    # test_inherited_work_is_not_credited_as_a_do_nothing_attempts_own` and
    # `tests/test_blocker_challenge.py::
    # test_an_empty_attempt_after_a_challenge_is_not_credited`). The flag is
    # replaced, not restored: `_run_review` now re-derives the same fact
    # statelessly via `self._is_own_partial(repo, task.context or {},
    # repo.head_sha())`, so the net line count lands back at 22585 — same
    # number as the flag, different (correct, flagless) mechanism. Measured
    # on this tree by the scanner's own metric.
    # 22585 -> 22691 (+106): a red test run's `tests` event now carries its
    # FAILING blocks (`_red_test_detail`, `_test_output_artifact_path`,
    # `_write_test_output_artifact` next to `_verification_artifact_path`)
    # instead of the last 1200 bytes of the stream, and the full runner
    # output is written to an attempt-scoped `tests-attempt-<n>.log`
    # artifact. Measured on this tree by the scanner's own metric.
    # 22691 -> 22703 (+12): `_failed_tests_outcome` gained a `blocks`
    # parameter so its owned-failure and flaky-excused `update_attempt`
    # calls carry `failure_blocks` too — those calls REPLACE the
    # `test_results` column rather than merge it, so without this the
    # blocks the plain branch had just written were dropped the moment a
    # red run got attributed and billed.
    # 22703 -> 22699 (-4): net of the round-3 dict-passthrough refactor of
    # `_layered_tests_failed_outcome` (-5), the round-4 render/persist
    # parity fix (+3) and the landing's wiring of `_red_test_detail` onto
    # `runner.render_failure_blocks` instead of its own copy of the join
    # (-2). Main measures 22585, so the whole fix is +114 lines on this
    # file. Measured on this tree by the scanner's own metric.
    # 22699 -> 22952 (+253): the background-run report nudge (task
    # 7a7713e3: `_REPORT_NUDGE*` constants, `_looks_like_background_wait`,
    # `Orchestrator._report_nudge`, its call site in `_run_attempt`, the
    # `_abort_during_nudge` docstring sentence and the guard-set pop in
    # `_begin_attempt_accounting`), landed on top of 3bccb499's 22699.
    # Measured on this tree by the scanner's own metric.
    # 22952 -> 23028 (+76): a rebased task branch could never be delivered
    # (task e068e0cf; see the `_build_implement_prompt` entry above for the
    # incident). `base_staleness.staleness_mode`/`GitRepo.merge_base_into_branch`
    # add the merge path, `_refresh_stale_base` dispatches merge vs rebase off
    # the branch's live remote tip and asks `remote_branch_confirmed_absent`
    # before treating a falsy tip as never pushed (only that positive
    # confirmation may choose rebase). Landed on top of 7a7713e3's 22952.
    # Measured on this tree by the scanner's own metric.
    # 23028 -> 23117 (+89): the new `_node_serial_rerun` sibling helper next
    # to `_run_tests_once`, plus its call site in `_run_attempt`'s plain-red
    # branch — one serial re-run of only the failing files before a cold
    # `node --test` run is blamed on the change (tasks ba602e95 / f8af7f46,
    # 2026-09-08). Measured on this tree by the scanner's own metric.
    # 23117 -> 23153 (+36): the base-refresh divergence advisory and its
    # `diverged` record in `_refresh_stale_base` (task e83b0b6d), landed on
    # top of a9db9cba's 23117. Measured on this tree by the scanner's own
    # metric.
    # 23117 -> 23246 (+129, `len(Path(...).read_text().splitlines())`, the
    # scanner's own metric): a re-created worktree's `.no_human/**` is
    # gitignored, so a repro manifest a passing gate wrote there was
    # untracked, per-worktree state that a worktree re-create (new pid ->
    # new path) silently lost. Added `_restore_repro_manifest` next to
    # `_repro_gate_step`, its call site in `_run_task_body` right after
    # `_acquire_worktree`, the gate-time zero-LLM-spend restore backstop and
    # one-turn/`effort="low"` regenerate nudge (`_REPRO_REGENERATE_NUDGE`)
    # inside `_repro_gate_step`, the persist-on-pass call in its `_run_gate`
    # closure, and the new `effort` override on `_repro_corrective_round`
    # (2026-09-09).
    # (the branch's entry above was measured on a9db9cba's 23117; landed on top
    # of e83b0b6d's 23153 → +129 here.) Measured on this tree by the
    # scanner's own metric.
    # 23282 -> 23285 (+3): landing edits — the regenerate-nudge prompt says
    # "no usable manifest (missing or malformed)" and its once-guard is popped
    # at the attempt boundary like `_reformat_nudged`/`_report_nudged`.
    # 23285 -> 23403 (+118): 99fa5ba5 (bounded failing_tests and joins at
    # every write site; the carried dropped count reaches the coder prompt)
    # landed on top of cf6f6c19. Measured on this tree by the scanner's own metric.
    # 23403 -> 23534 (+131): fix for "a red pre-review test run reaches the
    # coder even when the review fails" — `_run_review` now emits its own
    # `tests` event and writes `test_results` for a red pre-review run
    # (reusing the existing `_red_test_detail`), unconditional of the
    # verdict that follows, plus the module-level `_FAILING_TEST_ID_CAP`/
    # `_bound_failing_test_ids` helper and the `_pre_review_red_checklist_
    # item()` nested helper shared by all three `ReviewDecision`-returning
    # exits, so the failing ids ride into `_record_review_feedback` on
    # every FAIL exit, not just an LLM-reviewer FAIL.
    #
    # An earlier revision of this same fix (03267ead / PR #198) additionally
    # added a `_pre_review_red_is_blocking` classifier that force-failed
    # `decision.passed` off its own reclassification of the SAME red run.
    # That revision was sent back (MAJOR-1): the classifier never called
    # `_flaky_on_rerun` — the tiebreaker the post-review TESTING step's real
    # plain-red path uses before billing a non-owned failure — so forcing
    # the verdict off it made the tiebreaker unreachable for every red
    # pre-review run, and on an `env_setup` project `_newly_failing_vs_base`
    # fail-closes to `None`, which `None != []` maps to "blocking" with no
    # re-run evidence at all. This landing drops the forced verdict
    # entirely: the pre-review block is visibility-only, and the unchanged
    # post-review TESTING step remains the SOLE place a red run is
    # classified and billed or excused, via the tiebreaker it already had.
    # A PASS verdict over a red run still cannot silently succeed — it just
    # fails "at tests" (TESTING) rather than "at review". Net +131, not the
    # prior revision's +106 then +48 (+154): this version has strictly less
    # code (no classifier method, no forcing block) than the sent-back one.
    # Landed on top of 99fa5ba5's 23403. Measured on this tree by the
    # scanner's own metric.
    # 23534 -> 23556 (+22): the hand-finish at landing — the shared
    # `_PRE_REVIEW_RED_LABEL` constant and its comment, the D6 exclusion,
    # and the corrected block comments above. Measured on this tree by the
    # scanner's own metric.
    # 23556 -> 23564 (+8): second landing pass (`_reviewer_items` shared by
    # both D6 halves; comments name it). Measured on this tree by the
    # scanner's own metric.
    # 23564 -> 23643 (+79) over the three rounds below, re-based on trunk's
    # 23564 after this branch's base-refresh merge of e44af234. The
    # per-round numbers below were measured before that merge, against
    # 23403; they record what each round added, not absolute positions.
    # round 1 (+39): every task end state now emits a terminal
    # telemetry event, not only done/failed — `_TASK_END_KINDS`,
    # `_task_end_outcome`, and the new `task_ended` branch appended after
    # the existing `failed` branch in `_telemetry_hook`. Measured on this
    # tree by the scanner's own metric.
    # 23442 -> 23472 (+30): reviewer round 2 fixes on the above — (1) the
    # real hard-cancel end state now actually fires terminal telemetry
    # in-process (`cancelled_hard` emit + comment in `_run_attempt`'s
    # `CancelledError` branch, the round-1 version never wired this in);
    # (2) `_telemetry_hook`'s `task_completed` branch is broadened from
    # `kind == "state"` to `kind in ("state", "pr_open")` (plus its
    # explanatory comment) so the ordinary PR-opened delivery leg —
    # `_open_pr`'s `kind="pr_open"`/`status="awaiting_approval"` emit,
    # which never had a companion `kind="state"` event — actually reaches
    # `task_completed`, not only `nh approve`'s `done` leg; (3) the dead
    # `blocked`/`QUOTA` branch in `_task_end_outcome` is removed (taxonomy.py
    # never routes QUOTA to `blocked`, only to `paused_quota`), a small net
    # decrease folded into this same total. Measured on this tree by the
    # scanner's own metric.
    # round 3 and the landing hand-finish (+10 over the merged 23633): the
    # unreachable `blocked`/`USER_PAUSED` mapping is removed as well (that
    # category is harness-only and its only writer is the pause path,
    # which emits kind "cancelled" — not a terminal kind), and three
    # narratives claiming `nh approve` fires `task_completed` are
    # corrected: it writes DONE through the store with no Orchestrator,
    # so it never reaches the telemetry sink. Measured on this tree by
    # the scanner's own metric.
    # 23643 -> 23648 (+5): the landing review found the deletion's new
    # docstring claimed the pause path is USER_PAUSED's only writer. Four
    # sites stamp it (`_honor_cancel`, `nh task pause`, `POST /pause` and
    # the HOLD endpoint); the docstring now names them and says why none
    # reaches the mapper — neither `api/app.py` nor `cli/commands.py`
    # constructs an `Orchestrator`, so nothing there emits into
    # `_telemetry_hook`. Measured on this tree by the scanner's own metric.
    # 23564 -> 23640 (+76) (task 30a97d8a): the single-write/unclassified-
    # marker fix — `_bounded_test_results`'s `classified: True` default,
    # `__init__`'s new `_pre_review_red_render` field, the pre-review
    # block's render-cache assignment plus its bounded `failing_tests`/
    # `failing_tests_dropped` red-event shape and `classified: False` write,
    # and TESTING's `reused_pre_review` reuse branch with its accompanying
    # comments. Measured on this tree with the scanner below.
    # 23724 -> 23730 (+6): landing hand-finish — the helper's comment said
    # "all ten call sites" (nine; the tenth grep match is the `def` line)
    # and justified the pre-review write's bypass in a way that read as if
    # that row were bounded too. Measured: 500 failing ids persist whole on
    # the pre-review row while the TESTING row keeps 200 and records
    # `failing_tests_dropped: 300`. The comment now says so and names the
    # follow-up (task 4a23ed43). Measured on this tree by the scanner's own
    # metric.
    # 23730 -> 23727 (-3) (task 4a23ed43): that follow-up itself — the
    # pre-review write in `_run_review` now goes through
    # `_bounded_test_results` too (tenth call site for real), and the
    # helper's comment was rewritten to say so instead of describing the
    # gap. The rewritten comment is three lines shorter than the one it
    # replaced. Measured on this tree by the scanner's own metric.
    # 23727 -> 23731 (+4): landing hand-finish — the branch's new comment
    # said "Every `test_results` write goes through this helper now",
    # dropping the scoping word the line it replaced had. Measured by an
    # AST walk over every `store.update_attempt(..., test_results=...)`
    # call: eleven writes, ten wrapped, one deliberately not — the
    # tamper-fire row, which carries no id list and must not be stamped
    # classified. The comment now says that. Measured on this tree by the
    # scanner's own metric.
    # 23730 -> 23818 (+88) 2026-09-09: edit-loop/convergence progress fix —
    # `_TEST_RUNNER_RE` widened in place (no separate `_HARNESS_RUN_RE`) to
    # recognize a bare `node <script>` positional harness run, test-adjacent
    # `npm run (test|check|verify|spec)`, and `npx playwright`, feeding BOTH
    # `ConvergenceTracker.mark_progress` and `StuckDetector.note_test_run`
    # through the one shared `_looks_like_test_run` predicate; `_agent_sink`'s
    # inline Bash/tool_result handling was replaced with a single call to
    # `_note_test_activity`, which now does that dual dispatch itself; and the
    # Write/Edit/MultiEdit/NotebookEdit branch gates `_agent_edited_files`/
    # `record_edit` on `is_agent_owned(...) or is_outside_repo(...)`, so an
    # edit to a path outside the repo root (task 0ab78498 attempt 1) is
    # counted as convergence progress but not as a committable, loop-able
    # edit. Measured on this tree by the scanner's own metric.
    # 23818 -> 23820 (+2): `_note_test_activity`'s docstring cited an
    # unverified "154 Bash calls, 29 Reads, 15 Edits" for task 0ab78498
    # attempt 2; a direct query against the recorded `task_events` for that
    # attempt's window found 96 Bash calls (14 of them repeated
    # `node /tmp/dcrace/harness.mjs`), 22 Reads, 7 Edits instead, so the
    # comment now cites those measured counts and the recorded
    # `failure_reason` verbatim. Measured on this tree by the scanner's own
    # metric.
    # 23820 -> 23824 (+4): that "14... repeated `node /tmp/dcrace/harness.mjs`
    # runs" count was itself wrong — it counted every recorded Bash call that
    # merely mentions `harness.mjs` (including read-only `grep`/`wc`
    # inspections of the script), not calls that actually execute it.
    # Re-running the current `_looks_like_test_run` against the recorded
    # attempt-2 stream matches exactly 7 commands, all genuine
    # `node /tmp/dcrace/harness.mjs` executions; the other 7 "harness.mjs"
    # mentions are grep/wc reads that `_looks_like_test_run` correctly does
    # not match. The docstring now cites 7 and explains the discrepancy.
    # Measured on this tree by the scanner's own metric.
    # 23824 -> 23828 (+4) (landing merge and hand-finish): this branch and
    # trunk's own 4a23ed43 follow-up both edited `core/orchestrator.py`, so
    # the squash carries both and the merged total is neither side's frozen
    # number; the remainder is the hand-finish, which rewrote the
    # `_agent_sink` else-branch comment after that branch widened it to
    # carry every out-of-repo path. Measured on the MERGED tree by the
    # scanner's own metric.
    # 23828 -> 23846 (+18) (2026-09-10, already-satisfied sibling-branch
    # ordering fix): `_already_satisfied_subject` hoisted the remote
    # preconditions and the existing sibling-branch lookup above the
    # local-pointer refusal so a lagging local delivery branch falls
    # through to the sibling fallback instead of returning early, and
    # split one refusal message into "never pushed anywhere" vs. "local
    # pointer lags a branch that WAS pushed". Measured on this tree by
    # the scanner's own metric (ast/splitlines-based, not `wc -l` — this
    # file has a few non-`\n` line separators that make the two differ
    # by a constant 3 lines).
    # 23846 -> 23893 (+47): #114 phase 2 — `_build_type_hook`, its call
    # site, the `type_hook` parameter threaded through both PostToolUse
    # compose helpers, and the order docstring recording why the type
    # hook runs ahead of the scope guard. Re-measured on the merge result.
    # 23893 -> 24079 (+186): REFILE bugfix — pre-review red runs now share
    # the SAME NEW-vs-pre-existing-vs-unknown split TESTING already computes
    # post-review, instead of handing the reviewer an undifferentiated
    # failing-id list under `classified: False`. New genuinely-required
    # machinery, not duplicated/movable code: `_FailureAttribution` /
    # `_attribution_buckets` / `_render_failing_attribution` (the ownership-
    # as-annotation render), `_round_failure_attribution` (computes the
    # split ONCE per round, identity-cached, shared by the reviewer render
    # and the round's billing), the by-name/command-identity/timeout guards
    # added to `_newly_failing_vs_base` so a substituted or partial base run
    # is never mistaken for a trustworthy verdict, and `_handle_pre_review_
    # red` (extracted out of `_run_review`, which was already at its own
    # frozen ceiling, so that function's own entry did not have to grow).
    # Measured after extracting/trimming as far as possible without cutting
    # the fail-closed guards' rationale comments.
    # 24073 -> 24133 (+60): pushed-tip rewrite fix (2026-09-13) -- sum of
    # this file's own FROZEN_FUNCTION_LINES/CC deltas above
    # (`_finalize` +4, `_build_implement_prompt` +29) plus the PR-open
    # retry's comment expansion and the new `base_merge_conflict_instruction`
    # import/threading, applied on top of current main (24073, this file's
    # scanner count on main `dded20a5`) and re-measured AFTER the base-refresh
    # merge into this tip (`c15b1145`) landed the same +60 net — not the
    # pre-merge parent's own 24139, which this comment previously (and
    # wrongly) cited. Measured on THIS tree with the scanner below
    # (`scan_source`'s `len(text.splitlines())`, not `wc -l` — they disagree
    # by a constant 3 lines on this file, see the note two entries above).
    # 24133 -> 24141 (+8): B2 fix (pushed-tip guard review round), same +8
    # as `Orchestrator._build_implement_prompt`'s own entry above — no other
    # function in this file changed line count this round. Measured on THIS
    # tree with the scanner below.
    # CORRECTION (Finding G, this task's send-back): the 24079 anchor below
    # was never independently re-measured against the actual merge base —
    # it was carried forward by hand from the prior comment's own arithmetic.
    # Directly measuring `git merge-base HEAD main`
    # (`dded20a5a92347b7e57f5851cbf515b18437d5fe`) with THIS module's own
    # `scan_tree`, via a non-destructive `git archive <sha> -- src/no_human`
    # extraction (never a hand diff, never a working-tree checkout), gives
    # 24073, not 24079 — an unreconciled 6-line gap that predates the
    # citation-drift preflight entirely (something else on `main` between
    # the REFILE bugfix step above and this merge base; not this feature's
    # doing) and does not implicate any of the per-step deltas below, each
    # of which was independently re-measured with `scan_tree` against its
    # own actual commit (24294, 24318, 24341, 24335, 24396 in step order —
    # the dip from 24341 to 24335 is that step's merge of `main` back in,
    # not a regression). Branch commit ids are omitted deliberately: they do
    # not survive the squash, so they would not resolve for any reader.
    # 24073 -> 24294 (+221): citation-drift preflight, added on top of the
    # above — the `citation_drift_send_back_message` helper, the
    # `_CITATION_DRIFT_ROUND_TURNS` constant, the `_citation_drift_preflight`
    # method (reads the target repo's own `scripts/reanchor_citations.py`
    # via `no_human.testing.citation_drift`, auto-reanchors or buys one
    # `_repro_corrective_round` before review, latched once per attempt),
    # its call site in `_run_attempt`, and the additive `allow_paths`
    # parameter threaded through `_repro_corrective_round` ->
    # `_repro_round_out_of_scope(changed, extra_ok=allow_paths)`.
    # Re-measured at that step's own commit with the scanner's own metric
    # (`tests/test_structural_budget.py::scan_tree`).
    # 24294 -> 24318 (+24): a `GitError` from `commit_with_manifest_repair`
    # after a mechanical re-anchor must not be silently reported as
    # "auto-re-anchored" success — the write is now reverted and the run is
    # re-labelled UNKNOWN so it falls through to the same bounded corrective
    # round an UNFIXABLE/UNKNOWN run gets, plus the post-round verification
    # re-check now passes `apply=False` so it can never itself mutate the
    # worktree. Re-measured on the merged tree with the scanner's own metric.
    # 24318 -> 24335 (+17): two send-back fixes to the same preflight. (1)
    # `_citation_drift_preflight`'s own mechanical-fix commit no longer
    # calls `commit_with_manifest_repair(repo, None, ...)` (which sweeps
    # EVERY current worktree change, including anything unrelated already
    # sitting uncommitted, into a commit that claims to be the re-anchor
    # script's own output) — it now diffs `_worktree_state` before/after and
    # commits exactly that delta. (2) the UNFIXABLE/UNKNOWN branch's
    # `_repro_corrective_round` call no longer passes `allow_paths=
    # outcome.docs` unconditionally — a raw `Status.UNKNOWN` (crash,
    # timeout, self-contradictory verdict) always carries `docs=()`, which
    # silently discarded the bought round's own hand fix as "out of scope";
    # when `outcome.docs` is empty, `allow_paths` now falls back to every
    # `docs/*.md` file that concretely exists on disk. Re-measured on this
    # tree with the scanner's own metric.
    # 24335 -> 24396 (+61): code-review send-back on the same preflight,
    # three more fixes plus their rationale comments. (1) BLOCKER: the
    # mechanical-fix commit branch was gated on `repo.has_changes()` (true
    # for ANY dirty path in the whole worktree) instead of `changed` (this
    # run's own before/after delta) — an UNFIXABLE run that writes nothing
    # (`changed` empty) with an unrelated dirty stray file elsewhere in the
    # tree could still enter the commit branch and fall through to
    # `commit_with_manifest_repair`'s `commit_all` (empty list is as falsy
    # as `None`), sweeping the stray file into a commit that falsely
    # credits the re-anchor script. Both `if outcome.status is UNKNOWN and
    # repo.has_changes():` and `elif repo.has_changes():` now read `changed`
    # instead, with a rationale comment explaining the distinction and a
    # second comment documenting the accepted, narrower status-code-only
    # limit this still carries. (2) `_revert_worktree_writes`'s exception-
    # fallback advisory hardcoded "the reformat nudge" as the blamed
    # component even when called from this preflight (whose actual writer
    # is `scripts/reanchor_citations.py`) — it now takes a `component`
    # keyword-only parameter (default preserves the existing text at every
    # other call site), and this preflight's two call sites pass "the
    # citation drift preflight". (3) a documentation-only comment
    # explaining why an absent `docs/` directory's resulting empty
    # `allow_paths` is an accepted limitation rather than a widened,
    # directory-prefix `_repro_round_out_of_scope` allowlist shared by every
    # other corrective-round caller. Re-measured on this tree with the
    # scanner's own metric.
    # 24396 -> 24441 (+45): this task's send-back-fix round, three more
    # blockers on the same preflight. (1) BLOCKER: `_revert_worktree_writes`
    # /`_revert_worktree_writes_unguarded`'s success-path advisory hardcoded
    # "the reformat nudge" as the writer it reverted, even when the actual
    # caller was the citation-drift preflight's own mechanical re-anchor or
    # its commit-failure fallback — `component` is now threaded into (and,
    # on `_unguarded`, required by) the success-path advisory too, not just
    # the pre-existing exception-fallback one. (2) BLOCKER:
    # `citation_drift.classify`'s `VERDICT=OK` branch trusted the "OK" half
    # of a self-contradictory `OK`-with-`FAIL:`-lines (or `OK`-with-
    # `applied`-plus-`FAIL:`-lines) shape from the target repo's script,
    # silently dropping a named unfixable citation as CLEAN or REANCHORED —
    # both now block as `Status.UNKNOWN`. (3) a corrected, no-longer-
    # misleading comment on the `elif changed:` branch explaining why an
    # UNFIXABLE run can still legitimately have written and earned a commit
    # (the script's own `_apply_all` batch is all-or-nothing for itself, but
    # is a different, narrower set than the plan-level `unfixable` findings
    # that force `VERDICT=FAIL` regardless of whether that batch wrote), and
    # bounded `failures`/`detail` in `citation_drift_send_back_message`
    # (`_CITATION_DRIFT_FAILURES_NAMED`, `_CITATION_DRIFT_DETAIL_CHARS`) so a
    # whole-repo scan with many drifted citations cannot hand the coder an
    # unbounded prompt. Re-measured on this tree with the scanner's own
    # metric.
    # 24441 -> 24481 (+40): documentation only, no behaviour change — a new
    # docstring paragraph on `_citation_drift_preflight` (Finding D) stating,
    # with reasons, that the preflight is deliberately whole-repo-scoped
    # rather than diff-scoped (the target repo's own script has no scoping
    # argument, and TESTING's own backstop covers a different scope anyway —
    # more docs, via `test_windows_md_code_line_citations_resolve`, but a
    # narrower in-window drift tolerance on the table's three — so diff-
    # scoping this preflight would not even line up with what TESTING
    # checks);
    # and a comment on the REANCHORED branch recording that a mechanical
    # re-anchor commit cannot spend an LLM tamper-adjudication turn —
    # `_handle_tamper_fire`'s first line is `if not report.tampered: return
    # None`, gating both the fail-attempt path and the one call to
    # `_adjudicate_tamper` on the same bool, and `tamper_guard.check`
    # (verified directly, not read) returns `tampered=False` for a
    # before/after pair differing by exactly one re-anchored line number.
    # Re-measured on this tree with the scanner's own metric.
    # 24481 -> 24606 (+125): send-back round N1-N8 on the same preflight.
    # (N5) a new backend/test pins the once-per-attempt latch's OTHER case —
    # it must hold even when the corrective round's own fix does not
    # actually resolve the drift, not just the already-covered case where it
    # does. (N6/N7) `citation_drift_send_back_message` gained no new
    # branches, but its two shapes (named failures vs. an indeterminate
    # crash/timeout run naming none) are now exercised directly by four
    # tests calling the function itself. (N1/N7) the citation-drift
    # corrective round's own SCOPE prompt text (`_REPRO_ROUND_SCOPE_NOTE`)
    # was appended unconditionally to every `_repro_corrective_round` caller
    # and unconditionally said "nothing else... discarded uncommitted",
    # directly contradicting this preflight's own instruction to fix a doc
    # citation — `_repro_round_out_of_scope`'s `extra_ok`/`allow_paths`
    # already admitted the doc path at the enforcement layer, but the coder
    # reading only the prompt had no way to know that. New module-level pure
    # function `_repro_round_scope_note(allow_paths=())` returns the
    # existing constant byte-for-byte when `allow_paths` is empty (the three
    # pre-existing callers), else appends a sentence naming the admitted doc
    # path(s); the one call site now uses it. (N8) the citation-drift
    # auto-reanchor commit was the one `commit_with_manifest_repair` call
    # site in this file that never passed `on_repair`, unlike
    # `_checkpoint_commit` and the main pipeline commit — a repair made
    # while re-anchoring a doc citation (e.g. re-approving a stale export
    # pin the same doc also happens to be watching) was dropped silently
    # instead of surfacing as the `manifest_repaired` event every sibling
    # call site already reports; it now threads `on_repair` and drains it
    # via `self._emit_manifest_repairs` in a `finally`. Re-measured on this
    # tree with the scanner's own metric (`tests/test_structural_budget.py`'s
    # `scan_tree`, not carried-over arithmetic).
    # 24606 -> 24674 (+68): no behaviour change in this file — main was
    # merged back in (three unrelated main-branch features landed on top of
    # this task's own last content-bearing commit) and the merge conflicted
    # in this file's own frozen table, resolved by re-measuring THIS tree
    # (post-merge, `git merge-tree`'s actual write-tree result) with the
    # scanner's own metric rather than carrying either parent's number
    # forward by hand.
    # 24674 -> 24713 (+39): a DO-NOT-LAND round with four numbered
    # blockers. Two of them landed in this file: the
    # `_citation_drift_preflight` docstring's closing paragraph was rewritten
    # from a one-sentence "changes nothing about the BAR" claim into an
    # honest disclosure that this preflight's notion of "a citation" is
    # narrower than TESTING's own checker run (+18 net), and
    # `_revert_worktree_writes`/`_revert_worktree_writes_unguarded` gained an
    # optional `reason=` parameter (plus docstring) so the citation-drift
    # preflight's two revert call sites stop blaming themselves for "writing
    # despite being told not to" when writing mechanically re-anchored
    # content IS the job (+21 net, including the two call sites' own
    # `reason=` arguments). Re-measured on this tree with the scanner's own
    # metric, not carried-over arithmetic.
    # 24713 -> 24728 (+15): the send-back round correcting five evidence
    # gaps named in review — the "Honest limit" paragraph's
    # closed-set `_CITATION_DOC_PATHS` list and "strictly wider" ->
    # "incomparable" rewrite, the leading summary sentence's qualifying
    # clause, the `_repro_corrective_round` docstring's caller enumeration,
    # and the `reason if reason is not None else ...` fix replacing `reason
    # or ...` in `_revert_worktree_writes_unguarded`. Re-measured on this
    # tree with `scan_tree`, not carried over as a stale delta.
    "core/orchestrator.py": 24728,

    # +163: Codex account section in the Settings Account tab —
    # _codex_status_payload + endpoints (app.py) and the I4 AI-history repo
    # scoping filter in _gather_history.
    # cli/commands.py 7801 -> 8111: +59 the `_print_learning_harvest` helper
    # shared by `nh learnings --harvest` and the scheduled `HarvestJob` plus
    # the `--no-harvest` serve flag; +251 `nh approve --ready [--yes]` (batch
    # listing/landing) and the `approve` refactor into named top-level helpers
    # (_approve_find_ready / _approve_go_ready / _approve_go_landed /
    # _approve_go_single / _ready_batch_non_merge_message). Measured via
    # `len(Path(...).read_text().splitlines())` (the scanner's own metric) on
    # the combined tree.
    # cli/commands.py 8111 -> 8122 (+11): the reject/reply-dispatch budget-
    # floor warning (`check_budget_floor`) — a pre-dispatch advisory so a
    # reject/reply that would burn the rest of a task's lifetime budget and
    # die mid-attempt on BUDGET_EXHAUSTED warns instead of silently losing
    # the human's feedback. Each of `reject`/`reply` gets its own 2-import +
    # 4-line block, matching this file's existing per-command local-import
    # convention; a shared helper was evaluated and rejected — with only two
    # call sites, the helper's own def+docstring line cost exceeds what
    # de-duplicating the 4-line block would save.
    # cli/commands.py 8122 -> 8173 (+51): `bench compare` gained a cost
    # section — per-spec priced-token/cost-ratio deltas were being computed
    # in `eval/bench_compare.py` but thrown away before the CLI printed
    # anything, so a real cost regression (per-spec ratio 0.107 -> 0.336)
    # went unattributed for a full release cycle. Adds `--cost-top`/
    # `--cost-threshold` options (literal defaults, not the eval module's
    # constants, to keep this file's lazy `..eval` import convention) and the
    # aggregate/top-N/flagged rendering, each line escape()-wrapped per the
    # AST guard in `tests/_bench_ast_guard.py`. 2026-08-30.
    # 8173 -> 8204 (+31): D1.1 — `nh logs` now names and tails the attempt's
    # verification artifact (review finding #3). Measured on the merge result.
    # 8204 -> 8214 (+10): D3.1 — `nh serve` threads learning.auto_manage /
    # auto_activate_daily_cap into HarvestJob (kill-switch wiring). Measured
    # on the D3.1 landing result.
    # 8214 -> 8285 (+71): `nh doctor` gains the visual-proof-walks row and
    # `--fix-walks`/`--dry-run` consent-first provisioning flow. Measured on
    # this landing.
    # 8285 -> 8297 (+12): `visual_walks_row()`'s docstring no longer claims
    # "Pure and read-only" (it now names the loop-safe `playwright.async_api`
    # -import probe it actually calls), and the `--fix-walks` "already
    # available" branch's chromium remedy text grew a one-line clarification
    # so it agrees, by construction, with `nh doctor`'s plain row. Measured
    # on this landing.
    # 8262+8297 -> 8350: merge of two independent landings — main's
    # no-human-67 follow-up (`nh onboard` one-confirm ui_evidence offer +
    # per-repo doctor rendering, incl. the ProjectYmlPersistError review fix)
    # and this branch's `--fix-walks` flow above, plus the two-layer
    # reconciliation comment in `doctor` (dependency row first, per-repo
    # config rows after; the whole block then extracted to
    # _print_visual_walks so the merged doctor() stays under the 300-line
    # function budget). Measured on the merge result with the scanner
    # below, never summed.
    # 8352 -> 8356 (+4): `_print_visual_walks` now derives `walks_colour`
    # from the (package, chromium) pair instead of the package layer alone
    # — a package-present/chromium-missing install must not render green —
    # plus one extra docstring sentence naming the new third row state.
    # 8356 -> 8377 (+21): quota-saturation mid-run halt (`bench_run` growth
    # above) plus the `quota_halt` import block. Measured on this tree with
    # the scanner below.
    # 8377 -> 8411 (+34): pin-rederivation follow-up (no-human/89acc73f-2).
    # `bench_build` reloads the freshly-written specs and prints the
    # repaired/not-re-derivable disclosure counts instead of a bare "N specs
    # written" (+17, incl. the AST-guard docstring explaining the int(...)
    # wrapping); `bench_run` and `bench_report` each gain one
    # `console.print(escape(pin_rederivation_note(card)))` call plus its
    # import (+4); `_compare_side` gains the `recorded` parameter, its
    # disambiguating branch, and an expanded docstring (+16); the two
    # `bench_compare` call sites thread `cmp.rederived_recorded_a/b` (net
    # +0, existing lines widened); `bench_report`'s import line wraps to two
    # lines for `pin_rederivation_note` (+1); net -4 from removed/collapsed
    # lines the disclosure and docstring rewrites replaced. Measured on this
    # tree with the scanner below (43 added - 9 removed per
    # `git diff --numstat 8356553f6 -- src/no_human/cli/commands.py`).
    # 8411 -> 8412 (net +3, -2 pre-existing unrelated drift already baked
    # into the 8411 baseline): the approval-supersede repair path —
    # `task_restore_approval`'s `cleared` tuple and its `merge_context` call
    # both gained `"approval_superseded_at"`, and `approve()`'s
    # `merge_context` call clears the marker on fresh approval (+7/-4 per
    # `git diff --numstat 4db9fd316 c39dd98aa -- src/no_human/cli/commands.py`).
    # Measured on this tree with the scanner below.
    # 8412 -> 8423 (+11): `_approve_go_single` echoes `result.gate_reason` on
    # both the `failed` (+8) and `done` (+2) branches so a full-gate failure
    # isn't mistaken for the cheaper focused one, and `approve()` resolves
    # the attempt's tested commit via `latest_attempt_branch` and threads it
    # into `land_task(..., tested_commit_sha=tested)` (+1 net: 2 added lines,
    # 1 modified in place). Measured on this tree with the scanner below.
    # 8423 -> 8495 (+72): `_land_one`'s already-satisfied branch now calls
    # the shared `land_already_satisfied_claim` helper (`vcs/task_pr.py`)
    # instead of unconditionally marking the task done, so a satisfying
    # commit that lives only on the task branch gets landed through the
    # normal PR/squash path rather than silently completing unmerged.
    # Measured via `wc -l src/no_human/cli/commands.py`.
    # 8423 -> 8427 (+4): the `bench` click group now self-marks every
    # `nh bench …` subprocess with `os.environ.setdefault("NH_ENV", "bench")`
    # (a comment + the setdefault call) so telemetry.environment() tags
    # bench-fleet events "bench" instead of counting them as real installs.
    # Measured on this tree with the scanner below.
    # cli/commands.py 8423 -> 8441 (+18): `_ensure_board_fresh()` (new
    # helper, +10 incl. docstring) wraps `core.web_build.ensure_fresh_board`
    # and is called at the top of `start()` (+1 call, +7 comment/blank lines
    # explaining the load-bearing placement) before `from ..api.app import
    # app as _app` — a stale `web/dist` on a source checkout must be
    # detected/rebuilt before the `StaticFiles` mount is decided at import
    # time (no-human/2026-09-04 web-dist-staleness task). Measured on this
    # tree with the scanner below.
    # 8423 -> 8462 (+39): setup-mode boot — `allow_setup_mode` on `_bootstrap`
    # (with its advisory banner) and `_server_setup_reason` (the no-credential
    # probe that lets `nh start` boot into a restricted setup state instead of
    # `sys.exit(2)`), plus the `MissingCredentialError` import. `start()`
    # itself reuses `_bootstrap`'s banner rather than printing its own second
    # one. Measured on this tree with the scanner below, rebased onto the
    # 8517 web-dist-staleness/bench/land-satisfied tree above (8517 -> 8556).
    # 8499 -> 8510 (+11): `nh verifiers` (list/add/check/propose) registered
    # via `cli.add_command(verifiers_group)` — the import (with its
    # `noqa`-free comment block) and the group registration itself, next to
    # `rules`/`skills`. The command bodies live in the new
    # `cli/verifiers_cmd.py`, not here, to keep this file's growth to just
    # the registration. Measured via `wc -l src/no_human/cli/commands.py`.
    # 8567 -> 8633 (+66): profile-declared worktree setup commands,
    # MAJOR-3 fix — `nh repo setup-cmds` now refuses to write a DB row
    # when none already exists ("run `nh onboard <repo>` first"), naming
    # the exact operator remedy, instead of letting a repo's own
    # `.no_human/project.yml` confer trust into a fresh CONFIRMED row.
    # The command's docstring was also rewritten to state the true trust
    # model: `_usable_profile`'s existing DB-first-then-file-fallback
    # behaviour for *running* an already-confirmed profile is untouched.
    # Measured via `wc -l src/no_human/cli/commands.py`.
    # 8633 -> 8642 (+9): `start()`'s existing `finally` block now unwinds
    # `setup_mode`/`setup_reason`/`_worker_opts` off the PROCESS-WIDE
    # `_app.state` on every exit path (normal return, ctrl-c, or a test's
    # stubbed `server.serve()`), not just the ones where uvicorn's ASGI
    # lifespan actually fires — closes the "nh start tests leave setup_mode
    # on the shared app" leak (`dashboard` is covered too, since it forwards
    # to `start` via `ctx.invoke`). Measured via `wc -l
    # src/no_human/cli/commands.py`.
    # 8642 -> 8651 (+9): MAJOR-1(b) — `task_cancel`'s direct-write branch
    # (queued/parked task, no live server-owned session) now calls the
    # shared `telemetry.record_task_cancelled(store, t, config=config.data)`
    # helper right after its `store.set_status(..., FAILED, ...)` write, plus
    # its local `from .. import telemetry` import and explanatory comment —
    # this is the only branch that can ever fire `task_ended(outcome=
    # cancelled)` for a task with no live in-process attempt at all. Measured
    # via `wc -l src/no_human/cli/commands.py`.
    # 8651 -> 8666 (+15): "a verifier with no verdict is advisory" review
    # send-back MINOR-2 — `_verifiers_advisory_note()` (a new module-level
    # helper) pulls the `verifiers_all_satisfied` rule's "N no verdict
    # (advisory): ..." parenthetical out of its detail text, and
    # `_approve_find_ready`/`_approve_go_ready` thread it through so
    # `nh approve --ready`'s one-line summary no longer silently drops the
    # only signal telling the operator a verifier never answered. Measured
    # via `wc -l src/no_human/cli/commands.py`.
    # 8666 -> 8679 (+13): `nh status`'s pause-reason branch gains an explicit
    # `pause.get("reason") == "lease_lost"` arm (restart-only message, no
    # ETA) ahead of the existing "infra"/quota checks, plus an honest
    # `elif pause:` fallthrough for any future unrecognised reason — closing
    # the old silent-fallthrough gap where an unmatched reason printed
    # nothing and could be misread as "not paused". Measured via `wc -l
    # src/no_human/cli/commands.py`.
    # 8679 -> 8851 (+172): `nh task retitle` — the state gate, the
    # `--description`/`--criteria` refusal callback, the PR-evidence branch
    # (refuse/update-both/landed-refuse), and the audit event.
    # 8851 -> 8920 (+69): `task_show` renders operator-owned text with markup
    # off, the blocker line becomes one `Text` with a styled label span, and
    # the slot-wait lines stop interpolating the operator's auth-profile name
    # into a markup string -- the last of those built span by span, because a
    # base style there bled blue into a note that must stay dim. Measured with
    # this module's own `scan_source` on the merge result. (`wc -l` happens to
    # agree on THIS file -- it reports 8920 too; the two disagree only on
    # `core/orchestrator.py`, 24073 vs 24070. An earlier version of this
    # comment, and the commit message, claimed they disagreed here. They do
    # not.)
    # 8920 -> 8935 (+15): #227 (PR #308) routes five bare `ClaudeBackend(...)`
    # constructions in this file through `make_backend` (+2 each) and adds a
    # 6-line `permission_mode` validation to `doctor`, so an invalid
    # `llm.permission_mode` is reported as a contradiction instead of dying at
    # the first task. Measured on the merge result with the scanner below.
    # 8935 -> 9039 (+104): `--ready` gains a live mergeability check against
    # the CURRENT base (never cached), fixing the bug where a stale-base
    # branch stayed listed as merge-ready after a sibling PR landed and
    # rewrote `RELEASE_MANIFEST.txt`. `_approve_find_ready` now returns a
    # `_ReadyTask` NamedTuple carrying a `vcs.landability.check_landability`
    # verdict per task instead of a plain tuple; `_approve_go_ready` renders
    # the two-part `rules P/T · merge: ...` status, splits the listing into
    # ready-to-land vs. quality-passing-but-conflicted, and skips conflicted
    # tasks under `--yes` (never auto-resolved, never hidden) via two new
    # helpers, `_format_conflict_paths`/`_format_merge_status`. Measured via
    # `wc -l src/no_human/cli/commands.py`.
    # 9039 -> 9048 (+9): `_approve_find_ready` probes `check_landability`
    # with the resolved HEAD SHA instead of the bare PR branch name — a
    # remote-only branch (the norm for this repo's squash-from-worktree
    # landing checkout) has no local ref, so the bare name silently
    # degraded the verdict to `state="unknown"` and masked a real conflict
    # as fail-open-landable.
    # 9048 -> 9085 (+37): new `nh gate` verb, a thin click wrapper over
    # `review.oneshot.run_gate` that runs the fresh-session reviewer and the
    # tamper guard over the current branch or a GitHub PR with no daemon, no
    # server, and no Store.
    # 9085 -> 9096 (+11): merged with #343 (PR #379), which gives task
    # `config` its own writer, so the three `apply_action` call sites in this
    # file each persist the raise through `update_task_config` beside the
    # action that produced it, instead of letting a generic save write a
    # stale blob back.
    # 9096 -> 9101 (+5): `gate`'s docstring now states the write surface in
    # full (config.yaml is read, never created; --pr mode's `git fetch`
    # writes only `FETCH_HEAD` and fetched objects) instead of the shorter
    # "reads and reports only" claim a staff review found to be inaccurate,
    # and the `GateUnavailable` refusal now prints with `soft_wrap=True` so a
    # long credential path or PR URL cannot fold mid-token in a narrow
    # terminal.
    # 9101 -> 9191 (+90): merged with origin/main, which wires `nh task add
    # --follows` (#232, resolves a predecessor by id/prefix and records
    # `follows_id`) and makes `nh approve` refuse — with
    # `--force-superseded` as the explicit override — a task a later task's
    # `follows_id` already names as followed-up-on, in both the single-task
    # and `--ready --yes` paths.
    # Measured via `wc -l src/no_human/cli/commands.py` (agrees: 9191).
    "cli/commands.py": 9191,
    # api/app.py 5338 -> 5346 (+8): same budget-floor warning surfaced by
    # `send-back`/`reply` as `budget_warning` in the JSON response. Net cost
    # was trimmed from a naive +14 to +8 by computing `Bounds.from_config(...)`
    # once in `reply_task` and reusing it for both the new warning check and
    # the pre-existing `apply_action` bounds argument (previously recomputed
    # inline), and by dropping a redundant function-local `Bounds` re-import
    # in `send_back` — `Bounds` is already imported at module level (line 53).
    # Grew to 5372 (+26) when the approve-refusal surfacing landed
    # (ee101fc460: every `nh approve` refusal path must surface its exact
    # refusal text in the UI, loudly — new refusal-detail plumbing in the
    # approve endpoints). Reviewed on its merits by that task's independent
    # review; the landing omitted this re-anchor, which turned main red at
    # the next full-suite run (2026-08-30). Frozen at its landing baseline;
    # growth from here fails.
    # Grew to 5471 (+99) with the 0.1.8 worker-count feature: the
    # GET/PUT /api/config/workers endpoints + their _workers_payload helper, so
    # the concurrent-worker count is configurable from the Settings Models pane
    # (config.set_concurrency). Re-anchored in the same session it landed.
    # Grew to 5488 (+17) when PR #913's _loaded_code_stale fix landed (a failed
    # head_sha() no longer clobbers a cached 'behind HEAD' verdict) — re-anchored
    # here on the next merge (#913 landed without re-measuring the ratchet).
    # 5488 -> 5489 (+1): PR #867 backend kwarg at the drawer pricing site.
    # 5489 -> 5572 (+83): POST /api/tasks/{id}/split — the 1-click feasibility
    # split creates the confirmed sub-task drafts as child tasks (feature #1).
    # 5572 -> 5596 (+24): the /split reservation race-fix (cancel-parent-first CAS).
    # 5596 -> 5615 (+19): create-time feasibility-hint wiring (feature #1).
    # 5615 -> 5639 (+24): GET /split-drafts (lazy draft generation) + the
    # contract-fold into child descriptions (feature #1 UI backend).
    # 5639 -> 5648 (+9): GET /split-drafts PENDING guard (review A1 — no paid
    # draft call for a task that can never be split).
    # 5657 -> 5714 (+57): D3.1 — the `/api/learnings/{id}/pause` and
    # `.../delete` routes, `restore`'s pause-aware rewrite (undoes archive
    # AND pause in one call), and the `RetirementSweepJob` construction's
    # `auto_manage`/`auto_retire_days` config threading. Re-anchored on merge.
    # 5714 -> 5723 (+9): D3.2 — `GET /api/learnings` grows an `include_paused`
    # query param (and its docstring) so the Second-brain UI's list can ask
    # for a paused row back after it stops excluding it by default. Measured
    # directly against this branch's tree.
    # 5723 -> 5737 (+14): D3.2 review-round fix #1 — the same route grows an
    # `include_archived` param (and its docstring) so the Second-brain UI's
    # archived-count footer can ask a Delete-archived row back too. Measured
    # directly against this branch's tree.
    # 5737 -> 5760 (+23): P1 (running-task page slow-open) — the new
    # `GET /api/tasks/{task_id}/attempts/{attempt_number}/details` lazy
    # endpoint, which serves the three heavy per-attempt blobs
    # (review_checklist/verifier_results/test_results) `AttemptOut` no longer
    # inlines. Measured directly against this branch's tree
    # (`len(Path(...).read_text().splitlines())`, the scanner's own metric).
    # 5760 -> 5797 (+37): P5 — opt-in ?limit/?offset pagination on GET
    # /api/tasks (validated Query params + docstring rationale). Measured on
    # the P5 merge result.
    # 5797 -> 5807 (+10): GET /api/metrics/window — attempt-attributed "last
    # 24h" spend (core/metrics.py:window_spend), fixing the board banner
    # sweeping a closed task's LIFETIME cost into the window on a bare
    # `updated_at` touch. Re-anchored on rebase onto the P5 merge result.
    # 5807 -> 5882 (+75): no-human-67 follow-up — `RepoUiEvidenceRequest` +
    # `POST /api/onboarding/repos/ui-evidence` (the wizard's one-action
    # confirm; re-derives the suggestion server-side, dual-writes via
    # `persist_profile`), plus the `ui_evidence` carry-forward fix and
    # response block in `onboarding_onboard_repo` (a re-derive no longer
    # silently wipes a previously-accepted ui_evidence). Measured via
    # `len(Path(...).read_text().splitlines())` on the landing tree.
    # 5882 -> 5889 (+7): review-round fix — `onboarding_ui_evidence` now
    # catches `ProjectYmlPersistError` and 500s instead of answering
    # `{"ok": True, "enabled": True}` when project.yml could not be written
    # (persist_profile also skips the DB write in that case, so the two
    # artifacts never disagree). Measured on this branch's tree.
    # 5889 -> 5904 (+15): rebased in — show_config grows
    # `coder_backend_effective` / `coder_backend_default`
    # (resolve_backend_name(cfg.data) vs DEFAULT_CONFIG["worker"]["backend"])
    # so the composer's coder-backend disclosure caption can gate on the
    # EFFECTIVE backend, not just the picker. Measured directly against the
    # rebased tree (`wc -l src/no_human/api/app.py`).
    # 5904 -> 5914 (+10): whitelist `role_backends` through `_format_events`
    # and `task_events_stream` (§6d part 2) — the non-default reviewer
    # disclosure kwarg was dropped before reaching the board otherwise.
    # Measured directly (`wc -l src/no_human/api/app.py`).
    # 5914 -> 5919 (+5): feasibility hint calibration — `create_task` now
    # loads the app config and threads it into `estimate_feasibility`, and
    # persists the hint's `signals`/`hint_reasons` alongside band/tier/offer
    # so the pre-flight card can surface hint-only families. Measured
    # directly (`wc -l src/no_human/api/app.py`).
    # 5919 -> 5928 (+9): grill-sourced tasks are annotated but never enriched
    # — `create_task` now carries the grill's intake-eval verdict
    # (`body.eval_result`) onto the created task's context, the missing
    # production path that makes the orchestrator's stored-verdict dispatch
    # branch (`_act_on_stored_eval`) reachable for grill/wizard tasks.
    # Measured directly (`wc -l src/no_human/api/app.py`), rebased onto the
    # feasibility-hint-calibration change above.
    #
    # 5914 -> 5919 (+5): _start_telemetry in lifespan (startup instance_id
    # mint via telemetry.ensure_instance_id). Measured via
    # `wc -l src/no_human/api/app.py`.
    # 5919 -> 5974 (+55): /api/worker/status event-loop stall fix — the
    # inline `await asyncio.to_thread(_loaded_code_stale)` git measurement
    # on the request path is replaced by a `_stale_cache` snapshot read
    # (`_stale_note_cached`) plus a periodic `_refresh_stale_note`
    # background task started/cancelled in `lifespan`. Measured via
    # `wc -l src/no_human/api/app.py`.
    # Re-home merge 2026-09-04: the feasibility-hint + grill-eval carry
    # changes and the /api/worker/status stall fix now live on ONE tree.
    # Measured on this merged tree, never summed by hand.
    # 5983 -> 6037 (+54): `approve_task`'s already-satisfied branch now
    # calls the shared `land_already_satisfied_claim` helper
    # (`vcs/task_pr.py`) instead of unconditionally marking the task done,
    # so a satisfying commit that lives only on the task branch gets landed
    # through the normal PR-merge path rather than silently completing
    # unmerged. Measured via `wc -l src/no_human/api/app.py`.
    # 5928 -> 5998 (+70): setup-mode boot — `_require_credentials` (the
    # dispatch-time 503 gate naming the missing `.env` variable) applied to
    # `create_task`/`split_task`/`grill_step_endpoint`/`grill_stream_endpoint`/
    # `split_drafts` (the paid draft-generation lookup), setup-state computed
    # in `lifespan` and stored on `app.state`, and `setup_mode` added to
    # `GET /api/config`. `_require_credentials`/`show_config` gate on
    # `hasattr(state, "setup_mode")` rather than a bare `cfg is None` check —
    # needed so a test app that hand-builds `app.state.config` without
    # opting into setup-mode tracking (e.g. tests/test_api.py's `client`
    # fixture, which predates this feature) is not spuriously gated by a
    # default-subscription-mode config with no credential on file. Measured
    # on this tree with the scanner below, rebased onto the 6037
    # already-satisfied-landing tree above (6037 -> 6107).
    # 6107 -> 6108 (+1): review fix — `get_split_drafts` (GET /split-drafts)
    # was missing its `_require_credentials(request)` call; it runs the same
    # paid utility-model draft call as `split_task` and must be gated too.
    # 6108 -> 6126 (+18): the local-backend board preflight (task 05a9cee0,
    # re-home) — `discover_repos`/`repos/discover` gains the pre-submit
    # backend-config validation so an unrunnable LOCAL config is rejected
    # before a task dies attempt 1 on BackendUnavailable. Measured on this
    # merged tree with the scanner below.
    # 6126 -> 6131 (+5): `metrics` (GET /api/metrics) now imports and calls
    # `verification_receipt_rate` and merges its result into
    # `data["verification_receipts"]` (verification-receipt aggregate, task
    # 33e958ed, re-home). Measured on this merged tree with the scanner below.
    # 6131 -> 6138 (+7): `_workers_payload` gains `cpu_count`/`hardware_ceiling`
    # (this machine's detected cores and the derived pool ceiling) so the
    # stranded Settings Workers panel UI can state where the limit comes
    # from, instead of re-deriving it client-side (task 5caad018, re-home).
    # Measured on this merged tree with the scanner below.
    # 6138 -> 6140 (+2): `discover_repositories`'s docstring corrected to
    # describe the typed-`root` containment fix (scanned wherever it resolves;
    # configured `extra_scan_roots` stay home-contained) — comment only, no
    # behaviour change (task bf0cfd72, re-home). Measured on this merged tree
    # with the scanner below.
    # 6140 -> 6148 (+8): lifespan shutdown tears down the per-boot
    # setup-mode flags (`setup_mode`/`setup_reason`) so a second lifespan
    # cycle in the same process never inherits them (task 302012e3) — one
    # comment block and two hasattr/del pairs, nothing else in this file
    # changed. Measured on this tree with the scanner below.
    # 6148 -> 6171 (+23): `_record_tasks_orphaned` — one `tasks_orphaned`
    # telemetry event per server start, bucketing mid-run tasks whose
    # attempt heartbeat is dead (an app/server closed mid-run). Called once
    # from `lifespan`, after `app_started`. Measured on this tree with the
    # scanner below.
    # 6171 -> 6183 (+12): MAJOR-1(b) — `cancel_task`, right after computing
    # `stopped`, now calls the shared `telemetry.record_task_cancelled`
    # helper when `not stopped` (no live in-process session found for this
    # cancel — a queued/parked task, or the owning orchestrator is gone),
    # so `task_ended(outcome=cancelled)` fires for THIS out-of-process cancel
    # path too, gated to avoid double-firing against `_run_attempt`'s own
    # in-process `cancelled_hard` emit when `stopped` is True. Measured on
    # this tree with the scanner below.
    # 6182 -> 6193 (+11): the baseline is the MEASURED line count, not the
    # frozen entry. main's entry says 6183 while the tree is 6182, and
    # `offenders()` reports grown (measured > frozen) and stale (measured <=
    # threshold) but never a stale-HIGH baseline, so that one-line gap sat
    # unnoticed and an earlier version of this comment inherited it instead
    # of measuring. This entry closes the gap. `worker_status`'s `healthy` conjunction gains
    # `and not out.get("lease_lost")` (a lost lease is permanent and
    # `tick_stalled` does not cover it), and `queue_health_endpoint` reads
    # `sched.lease_lost` and threads it into `queue_health(...)` alongside
    # the existing cooldown kwargs. Measured on this tree with the scanner
    # below.
    # 6330 -> 6332 (+2): #227 (PR #308) routes the onboarding docs-generate
    # backend through `make_backend`, so that path honours
    # `llm.permission_mode` like every other. Measured on the merge result.
    # 6332 -> 6340 (+8): board/detail/subtask/post-approve sites now pass
    # `ledger=` through to TaskOut/TaskSummaryOut.from_task so owned
    # unattributed_usage spend folds into a task's displayed cost.
    # 6340 -> 6346 (+6): #343 (PR #379) persists a blocker-action config change
    # through `update_task_config` at the API call site, same reason as
    # `cli/commands.py` above.
    # Measured on the squashed tree with the scanner below.
    # 6346 -> 6357 (+11): `onboarding_register_email` now forwards the newly
    # registered address to `email/register.py:register_email` off-thread
    # after the local persist, and threads `registration_status` into the
    # persisted onboarding state and the response body. Measured on this tree.
    "api/app.py": 6357,
    # +51: W5 active-time phase writer (phase instrumentation).
    # +84: `list_escalations`/`list_review_fails`/`list_tamper_trips` — the
    # three new failure-signal sources the recurring learning harvest mines.
    # 4388 -> 4411 (+23): Store.done_rate_by_tier — per-tier done-rate
    # calibration for the feasibility hint (feature #1).
    # 4412 -> 4656 (+244): D3.1 — the auto-activation pipeline's schema
    # (`_ensure_d3_learning_columns`: `paused`/`activated_at`/
    # `learning_events`) and store methods (`activate_memory_auto`,
    # `count_auto_activated_since`, `set_paused`, `record_learning_event`,
    # `list_learning_events`, `archive_stale_auto_activated`). Re-anchored
    # on merge.
    # 4656 -> 4691 (+35): efficiency fix — `record_learning_events`, the
    # batched sibling of `record_learning_event` (one `executemany` + one
    # commit for every injected memory's audit row, matching the idiom of
    # the already-batched `record_memory_uses`/`touch_memories_used`).
    # Measured on this tree with the scanner below.
    # 4691 -> 4784 (+93): orphan landed-reconciliation. `set_status` now
    # delegates its CAS-write/DONE-event-guard/phase-recording tail to a new
    # `_write_status` helper (shared, no legality check of its own) so
    # `Store.reconcile_landed_orphan` can complete an orphaned-but-landed
    # row through `assert_landed_reconciliation` — its own narrower gate —
    # without widening the general `ALLOWED_TRANSITIONS` map (that would
    # also legitimize IMPLEMENTING/TESTING->DONE for `Orchestrator.
    # _advance_after_review`'s plain `set_status` call, defeating
    # tests/test_post_review_transition_6408aba0.py). `reconcile_landed_
    # orphan` itself gained a `@serialized_write` decorator, needed once its
    # tail call moved from `set_status` (already decorated) to the
    # undecorated `_write_status`.
    # 4505 -> 4553 (+48): review finding fix — `reconcile_landed_orphan`'s
    # DONE write now goes through `set_status` itself (a new optional
    # `reconciliation_gate` parameter) instead of calling `_write_status`
    # directly, so the general `ALLOWED_TRANSITIONS` map (`assert_transition`)
    # is always consulted first, unconditionally, for this write too — the
    # narrower `assert_landed_reconciliation` gate is now only a fallback
    # `set_status` consults if the general map refuses, never a replacement
    # for it. The general map itself is NOT widened (still refuses
    # IMPLEMENTING/TESTING->DONE), so
    # `test_recovery_never_launders_an_illegal_jump` and
    # `tests/test_state_machine.py::
    # test_landed_reconciliation_edges_are_legal_only_via_the_narrow_gate`
    # both keep passing unchanged.
    # 4553 -> 4562 (+9): `_write_status` gained its own `@serialized_write` —
    # `test_every_committing_store_method_is_serialized` (test_db_concurrency.py)
    # flags any `Store` coroutine that commits without the decorator, and the
    # write-path consolidation above left `_write_status` as a plain, un-
    # decorated `self.db.commit()`-er with exactly one caller left
    # (`set_status`, already decorated). Adding the decorator here matches the
    # exact precedent `Store.create_wiki_job`/`update_wiki_job` set for this
    # same guard, and nests safely into `set_status`'s already-held lock:
    # `_critical()` is reentrant per (Store, owning asyncio task).
    # 4562 -> 4841 (+279): re-anchored on rebase onto main (measured directly
    # on this tree with the scanner below).
    # 4841 -> 4859 (+18): P5 — SQL-pushed pagination in Store.list_tasks with
    # the rowid tie-break + its rationale docstring. Measured on the P5 merge
    # result.
    # 4859 -> 4945 (+86): terminal-landed-reconciliation (narrowed refile of
    # the shipped-metrics-blindness ticket) — the `terminal_reconcile` CAS
    # mode threaded through `set_status`/`_write_status`, plus
    # `Store.reconcile_landed_terminal` and
    # `Store.landed_reconcilable_terminal_tasks`, the TERMINAL-row twin of
    # `Store.reconcile_landed_orphan` above. Measured on this tree with the
    # scanner below.
    # 4945 -> 4991 (+46): Store.record_cancel_reason (validated cancel-reason
    # write with the json_patch carry-forward) and its docstring. Measured on
    # this tree with the scanner below.
    # 4991 -> 5052 (+61): approval-supersede write site — `_write_status`'s
    # CASE-clause stamp of `context.approval_superseded_at` on any exit from
    # `awaiting_approval` other than `done` (write-once, all three CAS
    # branches), the in-process mirror, and the docstring explaining the
    # contract. Measured on this tree with the scanner below.
    # 5052 -> 5082 (+30): 2026-09-04 lifetime-cap follow-ups —
    # `latest_review_attempt` (the shared NEWEST-verdict-row query) and
    # `latest_failed_attempt` (the shared NEWEST-failed-row query) added;
    # `latest_review_verdict` rewritten to delegate to
    # `latest_review_attempt` so there is one ordering to maintain. Measured
    # on this tree with the scanner below, never summed by hand.
    # 5082 -> 5092 (+10, rebased onto the above): `base_pin_sha` added to
    # `Store._ensure_task_columns`'s `wanted` dict (attribution guard
    # hardening) plus its explanatory comment. Measured on this merged tree
    # with the scanner below.
    # 5092 -> 5112 (+20): MINOR-1 — `Store.latest_attempt(task_id)`, the
    # reliable `ORDER BY started_at DESC, rowid DESC LIMIT 1` recency lookup
    # (unfiltered by status) that `count_dead_attempt_tasks` needs to find a
    # non-terminal task's most-recent attempt even when it has no OPEN
    # attempt at all — the gracefully-interrupted-by-`_honor_server_stop`
    # case. Placed next to `latest_open_attempt`/`latest_review_attempt`, its
    # existing siblings. Measured on this tree with the scanner below.
    # 5112 -> 5123 (+11): `Store.update_task_title(task_id, title)` — a
    # single-column `UPDATE tasks SET title=?, updated_at=? WHERE id=?`
    # for the new `nh task retitle` command, avoiding a read-modify-write
    # race. Measured on this tree with the scanner below.
    # 5123 -> 5149 (+26): review-round F2 fix — `update_task` and
    # `update_task_columns` both keyed their `title=:title` SET clause off
    # an `updated_at`-compared CASE (mirroring the existing `cancel_reason`
    # CASE just above it), so a stale in-memory `Task` handle snapshotted
    # BEFORE a `retitle` can no longer stomp the row back to the old title
    # once that retitle has landed — the same stale-handle protection
    # `status` already had, applied to `title` without dropping it from
    # the column list (unlike status, ordinary callers legitimately mutate
    # `task.title` and expect it persisted). Measured via
    # `wc -l src/no_human/core/db.py` on this merge result.
    # 5149 -> 5193 (+44): that F2 fix keyed off `updated_at`, which is bumped
    # by ANY row write (e.g. a plain `set_status`), not just a retitle, so it
    # could not tell "someone retitled since I read this handle" from
    # "something unrelated wrote this row" — a legitimate title edit on a
    # handle whose row had merely advanced in status was silently dropped
    # (broke `test_update_task_never_moves_status`). Reworked to stamp
    # `context.title_updated_at` in the same statement as the title write
    # (`update_task_title`) and compare THAT marker instead, carrying the
    # winning marker forward into the new context blob the same way
    # `cancel_reason` already is, plus the expanded docstrings explaining why.
    # Measured via `wc -l src/no_human/core/db.py` on this merge result.
    # 5193 -> 5285 (+92): OWNED_LEDGER_SQL, the `owned=` split on
    # unattributed_usage_totals, and usage_ledger_rows_by_task/
    # task_usage_ledger_rows — the per-task ledger fold's query layer.
    # 5285 -> 5322 (+37): #343 (PR #379) adds `update_task_config`, the single
    # writer for the human-only per-task override blob, and documents on both
    # `update_task` and `update_task_columns` why `config` is now excluded — the
    # shape `set_status` already has for `status`. Most of the +37 is that
    # reasoning: the next person adding a column to those writers needs to know
    # `config` is deliberately absent rather than forgotten.
    # Measured on the squashed tree with the scanner below.
    # 5322 -> 5331 (+9): #232 adds `list_tasks_following`, the query `nh
    # approve` uses to find the task(s) that name a given task in
    # `follows_id` — used to warn/refuse landing a task a later one already
    # follows up on. Measured via `wc -l src/no_human/core/db.py`.
    "core/db.py": 5331,
    # +71: set_local_backend_fields — the config-write helper for the Settings
    # pane's local coder-backend fields (llm.local_model / llm.local_base_url).
    # +75: Codex account config helpers.
    # +27: the `harvest` config section (interval_hours, enabled) for the
    # recurring learning-harvest cadence.
    # +164: the 0.1.8 worker-count feature's concurrency support —
    # set_concurrency (validated max_workers 1..64 + .enabled write, with
    # reload-verify and restore-on-failure), the _CONCURRENCY_HEADER_RE /
    # _splice_concurrency_scalar plumbing, and the concurrency config section.
    # Re-anchored in the same session it landed.
    # +41: D1.2's `ui_evidence_should_run` (the diff-aware default) plus its
    # docstring and the `UI_EVIDENCE_DEFAULT_GLOBS` constant.
    # 3281 -> 3305 (+24): D3.1 — `learning.auto_manage` and
    # `learning.auto_activate_daily_cap` defaults, with the docstring
    # explaining the kill switch. Re-anchored on merge.
    # 3305 -> 3307 (+2): D3.1 review fix round — scoped the `auto_manage`
    # comment to the auto-activation write path specifically (it does not
    # revert the trigger-matching fix or reject-aliases-pause). Re-anchored
    # on merge.
    # 3307 -> 3328 (+21): P2 (turn-cap convergence early-abort) —
    # `worker.abort_non_converging`/`convergence_check_after_turns`/
    # `convergence_window_turns` defaults, with the docstring justifying
    # them (see `core.bounds.ConvergenceTracker`). Measured on this tree.
    # 3328 -> 3340 (+12): the `git.merge_identity_name`/`_email` flat-alias
    # config keys (second-tier resolution between `approve_identity` and the
    # repo's own git config, never a fallback to `agent_identity_*`) and
    # their explanatory comment. Re-anchored on rebase.
    # 3340 -> 3544 (+204): the role_backends config surface (single-write-path
    # validation, load-time catalog/availability alignment, set_role_backend).
    # Measured on the merge result with the scanner below, never summed.
    # 3544 -> 3546 (+2): telemetry.endpoint's DEFAULT_CONFIG comment
    # re-worded for PostHog-by-default routing (telemetry.py:_destination).
    # Measured on this tree.
    # 3546 -> 3554 (+8): `approve_merge.full_test_timeout_seconds` (5400s) and
    # its explanatory comment — the timeout for the merge-time FULL gate that
    # runs when the squash tree diverges from the tested attempt's tree
    # (vcs/approve_merge.py `_decide_gate`). Measured on this tree.
    # 3554 -> 3562 (+8): `feasibility.hint_signals_enabled` (default true) and
    # its explanatory comment — the off-switch the hint-only signal path
    # (`core/complexity.py:hint_signals`) reads before folding `multi_family`
    # into the pre-flight card. Measured on this tree.
    # 3562 -> 3596 (+34): setup-mode boot — `MissingCredentialError` (the
    # subclass of `AuthError` that lets a caller opt into setup mode instead
    # of a hard exit) and `subscription_credential_missing`, the non-raising
    # probe `nh start`/the API lifespan use to detect a missing credential
    # without triggering the scrub. Measured on this tree with the scanner
    # below.
    # 3646 -> 3657 (+11): the `hooks.per_edit_type` default (#114 phase 2)
    # and the comment recording why it ships off while `per_edit_lint`
    # ships on. Re-measured on the merge result.
    # 3657 -> 3661 (+4): `RESEND_API_KEY_VAR` — the welcome email's transport
    # (email/send.py) reads it from ~/.no_human/.env or the process
    # environment only, and it joins the `_reject_api_key_in_config` banned
    # set. Re-measured on this tree.
    # 3661 -> 3672 (+11): `load_config`'s `ensure_private_dir` call is now
    # gated on `create_if_missing or NO_HUMAN_HOME.exists()` instead of
    # running unconditionally, so a `create_if_missing=False` read (`nh
    # gate`'s config read among others) no longer materializes
    # `~/.no_human` on a machine that has never run `nh init`.
    # set.
    # 3661 -> 3668 (+7): `onboarding.registration_endpoint` (default null)
    # and its explanatory comment -- the hosted-intake gate `email/register.py`
    # reads before forwarding an onboarding address off-machine. Measured on
    # the merge result.
    "config.py": 3679,
    # +61: the tamper-adjudication one-bounded-retry contract (mechanical-
    # failure classification + the extracted `_review_tamper_adjudication`
    # helper that keeps `AdversarialReviewer.review` itself under the
    # function-line threshold — see the FROZEN_FUNCTION_LINES deletion note).
    # 2896 -> 2915 (+19): last-block-wins verdict selection — the complete-block
    # path now takes the LAST well-formed REVIEW_JSON_START…END match instead
    # of the first (closing the forged-early-block preemption hole), and the
    # missing-END recovery path scans START occurrences last-first to match.
    # 2915 -> 2945 (+30): reviewer-backend construction honoring the explicit
    # Settings choice (6d part 1). Measured on the merge result with the
    # scanner below, never summed.
    # --- main's line of history from 2945 ---
    # 2945 -> 2974 (+29): fix for "a red pre-review test run reaches the
    # coder even when the review fails" — `_build_review_prompt` and
    # `review()` (gate mode) gain the `failing_test_ids`/
    # `failing_test_ids_dropped` keyword-only params and the fixed
    # `failing_ids_section` template, independent of the 4,000-char
    # `_OUTPUT_CAP`'d excerpt. Measured on this tree with the scanner below.
    # 2974 -> 2978 (+4): send-back on the above (MAJOR-1) — the comment
    # above `failing_ids_section` claimed `Orchestrator._run_review` "forces
    # `decision.passed = False`... so a PASS verdict here can never demote a
    # real red run", which stopped being true once that forcing was removed
    # (orchestrator.py: the post-review TESTING step is now the sole gate
    # that bills or excuses a red run; see its frozen-line entry above).
    # Comment reworded to match; the rendered `failing_ids_section` text
    # itself is unchanged (asserted verbatim by
    # `test_build_review_prompt_carries_fixed_failing_ids_section`).
    # Measured on this tree with the scanner below.
    # 2978 -> 2979 (+1): the corrected failing-ids sentence at landing.
    # Measured on this tree by the scanner's own metric.
    # 2979 -> 2994 (+15): follow-up to 4e0299ad — the failing-ids section now
    # says the harness has NOT yet attributed these ids to the diff, that a
    # base-tree-red id is not this change's defect, that the post-review
    # TESTING step decides, and conditions "critical severity" on the diff
    # plausibly explaining the failure (see the matching FROZEN_FUNCTION_
    # LINES entry for `_build_review_prompt` for the incident this fixes).
    # Measured on this tree with the scanner below.
    # --- PR #164's line of history, branched from the same 2945 ---
    # 2945 -> 3004 (+59): net-new type diagnostics as a third deterministic
    # evidence collector (issue #114 phase 1) — the `type_evidence` parameter
    # and section, its READING SCOPE branch, and the two extracted helpers
    # (`_collect_gate_evidence`, `_evidence_section`) that keep BOTH
    # `AdversarialReviewer.review` and `_build_review_prompt` under the
    # function-line threshold rather than adding two new frozen entries.
    # Measured on that tree with the scanner below.
    # 3004 -> 3029 (+25): PR #164 review round 1 — `_collect_gate_evidence`
    # becomes async and takes `with_type_evidence`, so the expensive collector
    # runs off the event loop (`asyncio.to_thread`) and is skipped entirely on
    # the single-turn route, whose whole purpose is to spend less on a small
    # diff. The route decision moves ABOVE the collection because it is now an
    # input to it. Measured on that tree with the scanner below.
    # --- the two lines meet here ---
    # 2994 -> 3078 (+84): PR #164 lands the type-evidence collector on a main
    # that had itself moved to 2994 in unrelated work. MEASURED on the merge
    # result by the scanner's own metric, never summed: an earlier revision of
    # this comment recorded 3063 from an older base and was left behind when
    # main moved, which is exactly the drift this ledger exists to prevent.
    # 3078 -> 3084 (+6): the same comment-only rewrite as the
    # `_build_review_prompt` entry above — nothing executable changed, and
    # the whole rewritten block lies inside that function, so the file and
    # the function move by the same amount.
    # 3084 -> 3098 (+14): #114 phase 2 review round — `_reading_scope` must
    # not count a NOT-COLLECTED type block as evidence the prompt carries
    # (it is non-empty, so the emptiness test alone announced type
    # diagnostics for a collector that never ran). The multi-line import
    # of `NOT_COLLECTED_PREFIX`, the added condition and the comment
    # recording why. Measured on the merge result with the scanner below.
    # 3098 -> 3120 (+22): REFILE bugfix, same round as the orchestrator.py
    # bump above — `_build_review_prompt` and `AdversarialReviewer.review`
    # gained a `failing_test_attribution` parameter so the reviewer can be
    # told the NEW-vs-pre-existing split `_run_review` now computes, and
    # `_failing_test_attribution_sentence` (extracted so `_build_review_
    # prompt` itself did not have to grow past its own frozen ceiling)
    # renders the two cases. `_build_review_prompt` had zero headroom on
    # this file's budget already, so the new parameter/plumbing could not
    # be added at zero net file growth.
    # 3120 -> 3134 (+14): post-merge review send-back on the same REFILE
    # bugfix ("a red suite reaches the reviewer unattributed") fixed F2 —
    # `_failing_test_attribution_sentence`'s lead-in falsely claimed "the
    # harness has already checked each one against the base tree" even when
    # the base check was fully inconclusive (an ATTRIBUTION-UNKNOWN-only
    # render). Added a branch (plus docstring) giving that case its own
    # honest lead-in instead of reusing the "determined" one. Measured on
    # this merge.
    # 3134 -> 3147 (+13): `_git_diff` gets `--no-ext-diff --no-textconv`, an
    # env scrubbed of foreign secrets, and the docstring explaining why a
    # coder-planted `diff.external`/`diff.<x>.textconv` must not run in the
    # reviewer process. Security hardening; measured on this tree.
    "review/reviewer.py": 3147,
    # 2706 -> 2711 (+5): pre-existing red on main at 03b262d23 (e922e9b4's
    # landing, change-scoped tests missed the ratchet) — repaired, measured,
    # on this merge; same cause as the two function-level wake.py bumps above.
    # 2711 -> 2718 (+7): 2026-09-01 trust-the-local-merge fix, same cause as
    # the FROZEN_FUNCTION_LINES entry above — the whole-file delta equals the
    # function's delta since no other function in the file changed. Measured
    # on this merge.
    # 2740 -> 2752 (+12): mechanical resolution extended to cover structural
    # budget conflicts, same cause as the FROZEN_FUNCTION_LINES entry above.
    # Measured on this tree with the scanner below.
    # 2752 -> 2757 (+5): same cause as the FROZEN_FUNCTION_LINES entry above
    # -- the whole-file delta equals the function's delta. Measured on this
    # tree.
    # 2757 -> 2763 (+6): pushed-tip rewrite fix (2026-09-13) -- same cause as
    # the FROZEN_FUNCTION_LINES `_check_pr_conflict` entry above (the
    # whole-file delta equals that function's delta). Measured on this tree
    # with the scanner below.
    "blockers/wake.py": 2763,
    # +91: `_SCAN_WRAPPER_NAMES` + `_peel_scan_wrappers` — peels
    # timeout/xargs/nice/stdbuf (and siblings) for the scan-severity check
    # only, so a wrapped `find … -delete` in a denied compound classifies
    # DESTRUCTIVE instead of HYGIENE. Local sibling list, `_WRAPPERS` untouched.
    # 2892 -> 2917 (+25): `_resolve_or_self` helper plus switching both venv
    # probes in `_protected_venvs` to the tri-state
    # `venv_install_guard._probe_is_dir`/`_probe_is_file` (fail-closed on
    # `None`) instead of `Path.is_dir()`/`Path.is_file()`, which raised
    # `PermissionError` on a chmod'd-unreadable venv and got swallowed by
    # the old `except OSError: pass`, turning a DENY into an ALLOW.
    # 2917 -> 2926 (+9): `_primary_checkout` now probes its `__init__.py`
    # marker via the tri-state `venv_install_guard._probe_is_file` instead of
    # a bare `Path.is_file()`, so an unreadable ancestor directory (e.g. a
    # chmod'd `<checkout>/src`) can't raise `PermissionError` out of this
    # unguarded helper and abort the guard instead of denying.
    # 2926 -> 2945 (+19): pushed-tip rewrite fix (2026-09-13) -- the module
    # docstring gains a bullet naming the new pushed-tip-rewrite denial
    # (+10), the `pushed_tip_guard` import is added, and `evaluate` gains
    # the `pushed_reason = pushed_tip_guard.denial_reason(...)` dispatch
    # plus its rationale comment (+9). No change to any pre-existing
    # function's own frozen entry -- this is new code, not growth of an
    # existing offender.
    # 2945 -> 2947 (+2): corrected the pre-existing `_FORGE_RUNNER_NAMES`
    # comment above `_forge_invocations`, which wrongly claimed
    # `_git_invocations` kept matching on `_SHELL_RUNNERS` alone,
    # "byte-identical" to before that name existed -- the function actually
    # reads `_FORGE_RUNNER_NAMES` (verified by
    # `tests/test_pushed_tip_rewrite_guard.py::test_the_pushed_tip_path_sees_every_runner_the_guard_knows`).
    # Measured on this tree with the scanner below.
    # 2954 -> 2959 (+5): N5 fix (pushed-tip guard review round) — the module
    # docstring's pushed-tip-rewrite bullet no longer overclaims "any
    # spelling" for `git rebase`; it now says "every lexical spelling this
    # module recognizes" and cross-references `pushed_tip_guard`'s own
    # disclosed gaps (shell/interpreter indirection, brace groups, command
    # substitution). Measured on this tree with the scanner below.
    # 2925 -> 2954 (+29): runner-recursion case fold (#328's remaining half).
    # `_forge_invocations`/`_git_invocations` resolved a wrapped runner's
    # nested command name via `PurePosixPath(tok).name`, case-blind, while
    # the top-level `argv[0]` path already folded through
    # `exec_names.command_name`; `timeout 30 GH pr merge 7` and
    # `sh -c "GIT push origin main"` recursed past their capitalised name
    # instead of finding it. Both `PurePosixPath(tok).name` comparisons now
    # go through `command_name` too, and the two mention-gates
    # (`_FORGE_MENTION`, new `_GIT_MENTION`) are precompiled with
    # `exec_names.case_flags()` so the recursion into a quoted payload stays
    # host-gated and linear. `_FORGE_MERGE` gained the same `case_flags()`
    # it was missing relative to `_RM_RF`/`_GIT_DESTRUCTIVE`, so a
    # capitalised merge spelling denies lexically even where no structural
    # path reaches it (`GH api .../pulls/7/merge`). `_forge_subcommand`
    # folds its returned noun/verb unconditionally (a CLI subcommand word,
    # not a filesystem name) so `gh pr MERGE 7` pairs against
    # `_FORGE_MERGE_PAIRS` lowercase. Measured on this tree.
    # 2954 -> 2958 (+4): the runner-recursion fix above left one sibling gate
    # unfolded — `evaluate`'s whole-string `git ... push` lexical fallback
    # (the one catching a push spelled where argv analysis can't reach: a
    # heredoc, an alias, OR a capitalised `GIT` behind a trailing-argv runner
    # like `timeout`/`xargs` with no quoting, which `_git_push_invocations`'s
    # per-token recursion can't resolve either). It now carries
    # `exec_names.case_flags()` like its siblings, so `timeout 30 GIT push
    # origin main` denies in the default (non-readonly) session too, not
    # only the read-only write-block path the earlier `_git_invocations` fix
    # covers. Measured on this tree.
    # 2958 -> 2969 (+11): post-review send-back on #328. Rewrote the
    # `_FORGE_MENTION` comment, which falsely claimed `sh -c "GH pr merge 7"`
    # "recurses into the quoted payload either way" -- measured false, this
    # gate is what decides whether the recursion happens at all (without
    # `case_flags()` here, `_forge_invocations` returns `[]`, never reaching
    # the later `command_name` fold). Also amended the `nh merge-stack run`
    # comment's parity claim ("denied in EVERY mode, exactly like
    # `_FORGE_MERGE` above"): `_FORGE_MERGE`/`_forge_subcommand` fold case via
    # #328, but `_LEXICAL_MERGE_STACK`/`_APPROVE_VERBS`/`_MERGE_VERB_PAIRS` do
    # not, so `nh MERGE-STACK run`/`nh merge-stack RUN` are not denied by
    # either gate -- a disclosed, pre-existing, out-of-scope gap, not a
    # parity claim. Measured on this tree.
    # 2969 -> 2985 (+16): Blocker 3 follow-up on the same send-back -- the
    # amended comment above was amended AGAIN, back to an actual parity claim,
    # by folding the gap it disclosed instead of just naming it:
    # `_is_approve_verb` and the `_MERGE_VERB_PAIRS` argv comparison now fold
    # case unconditionally (same rationale as `_forge_subcommand`'s fold -- a
    # CLI subcommand spelling is not a filesystem name), and
    # `_LEXICAL_MERGE_STACK` now carries `re.IGNORECASE`. Measured before:
    # `nh APPROVE 7`, `nh Approve 7`, `nh MERGE-STACK run`, `nh merge-stack
    # RUN` all reached ALLOW while `nh approve 7`/`nh merge-stack run` were
    # DENY. Measured after: all six rows DENY.
    # `_LIVE_VERBS`/`_LIVE_VERB_PAIRS` left untouched -- out of #328's scope,
    # which is the merge door specifically (approve, merge-stack), not the
    # live-server verbs. Measured on this tree.
    # 2985 -> 3019 (+34): merge of main 0b8c2dc4 into this branch -- main's own
    # growth in this file over the same window, no change to it from this
    # branch. Measured on this tree with the scanner below, not by arithmetic:
    # base 680d6889 actual 2925 (frozen was stale-high at 2926), main
    # 0b8c2dc4 actual 2959, this branch's own tip before the merge (a185a275)
    # actual 2985, and after the merge (this tree) actual 3019, which is what
    # this entry froze. Later entries below move it further.
    # 3019 -> 3022 (+3): send-back N2 fix on #328 -- guard.py:1131's comment
    # falsely claimed `_FORGE_MERGE` "was the one lexical gate WITHOUT"
    # `case_flags()`; `_FORGE_WRITE`, `_GIT_WRITE` and `_LEXICAL_LIVE_SERVER`
    # were exact-case too (and `_LEXICAL_MERGE_STACK` was, until #328 folded
    # it below), so the sentence was self-refuting within this same diff.
    # Reworded to name the actual exact-case set. Measured on this tree with
    # the scanner below.
    # 3022 -> 3031 (+9): send-back N2-adjacent precision fix on #328 --
    # guard.py:1621's comment claimed "only once this gate fires does the
    # later fold in `command_name` ... get a chance to run", implying a
    # two-stage pipeline where `_FORGE_MENTION` gates the `elif`'s
    # `command_name` fold. It doesn't: the two are alternatives tried per
    # token (`if`/`elif`) over disjoint token shapes -- quoted-payload vs
    # trailing-argv -- so one never waits on the other. Reworded to say so.
    # Measured on this tree with the scanner below.
    # 3031 -> 3036 (+5): third-round send-back on #328, blocker N2 -- the
    # `_FORGE_MERGE` comment still ranked it as "the one whose miss opens the
    # merge door specifically" one clause after naming `_LEXICAL_MERGE_STACK`
    # in the same sentence, and a companion send-back (NB1) measured
    # `_LEXICAL_MERGE_STACK`'s own `IGNORECASE` miss reopening a merge door
    # too (`nh MERGE-STACK run` inside a heredoc body, which never reaches
    # `_approve_denial`'s argv path). Deleted the ranking clause and replaced
    # it with the two rows the revert actually reopens (`GH api
    # .../pulls/7/merge --method PUT` and the unbalanced-quote `sh -c "GH pr
    # merge 7`), both pinned by
    # `test_every_capitalised_merge_spelling_is_denied_on_a_folding_host`.
    # Measured on this tree with the scanner below.
    "agent/guard.py": 3036,
    # +44: idle-path recover_quota_cooldown gate in tick() and the
    # never-shorten-a-live-wall guard in _run — the quota-wall storm cost fix.
    # +129: `HarvestJob` — the cadence job (`due()`/`maybe_run()`) that runs
    # both harvest passes from inside `nh serve`'s existing wake-watcher loop.
    # 2695 -> 2700 (+5): `_inherited_checkpoint` routed through the shared
    # `human_gate_armed` predicate for consume-once human resume_from gates.
    # 2700 -> 2776 (+76): D3.1 — HarvestJob auto-activation branch,
    # RetirementSweepJob 90-day auto-retire branch, and the rewritten
    # operator-directive docstrings. Measured on the D3.1 landing result.
    # 2776 -> 2848 (+72): `_reconcile_landed_orphan` — probes the attempt's
    # commit/PR against the base branch via `vcs.pr_watcher.
    # orphan_landed_evidence` (local-git-only, no network) and, on landed
    # evidence, calls `Store.reconcile_landed_orphan` instead of requeuing;
    # wired into `_recover_orphans` after the existing `_row_is_live` check.
    # Re-anchored on rebase onto main (measured directly on this tree with
    # the scanner below).
    # 2848 -> 2973 (+125): terminal-landed-reconciliation (narrowed refile of
    # the shipped-metrics-blindness ticket) — the TERMINAL-row twin of the
    # orphan sweep above: `_terminal_landed_evidence`,
    # `_reconcile_one_landed_terminal`, and `_reconcile_landed_terminal`
    # (probes a FAILED/cancelled row's `cancel_reason`/attempt commit/PR
    # against the base branch, reusing `orphan_landed_evidence` verbatim, and
    # calls `Store.reconcile_landed_terminal` on landed evidence instead of
    # leaving the row failed forever); wired into `_run`'s startup sequence
    # next to `_reconcile_terminal_task_attempts`. Measured on this tree with
    # the scanner below.
    # 2973 -> 3002 (+29): setup-mode boot — the optional `auth_check` ctor
    # kwarg and `_auth_advisory` dedup state, plus the `tick()` pre-dispatch
    # check that idles (returns `[]`) and emits `setup_required`/
    # `setup_complete` instead of crash-looping when no credential is on
    # file. Measured on this tree with the scanner below.
    # 3002 -> 3037 (+35): worker-death instrumentation in `_run`'s pool-crash
    # handler — an all-time `_worker_deaths_total` counter (surfaced via
    # `health_snapshot`'s `worker_deaths_total`), plus `exit_code`/
    # `termination_reason`/capped `stderr_excerpt` on the durable
    # `task_crashed` event. Measured on this tree with the scanner below.
    # 3037 -> 3083 (+46): `MID_RUN_STATUSES` + the read-only
    # `count_dead_attempt_tasks` (mid-run tasks with an open attempt and a
    # dead heartbeat, for the `tasks_orphaned` startup event). Mutates
    # nothing. Measured on this tree with the scanner below.
    # 3083 -> 3098 (+15): MINOR-1 — `count_dead_attempt_tasks` also counts a
    # non-terminal task with NO open attempt at all whose most-recent attempt
    # (`store.latest_attempt`) is `status == "interrupted"` — the graceful
    # `_honor_server_stop` close leaves exactly this shape, and the old
    # open-attempt-only staleness check under-counted it. Still read-only:
    # counts, never mutates. Measured on this tree with the scanner below.
    # 3098 -> 3196 (+98): pool-lease CAS-write retry — `_is_transient_db_lock`
    # (module-level; narrows to `sqlite3.OperationalError` naming a lock, so
    # every other exception still fails closed), `_LEASE_WRITE_ATTEMPTS`/
    # `_LEASE_WRITE_BACKOFF_S`, and `_cas_heartbeat_with_retry` — a bounded,
    # exponential-backoff retry of `_claim_pool_lease`'s CAS write that
    # forwards the SAME `expect` row on every attempt, so a competitor's
    # legitimately-claimed row still cannot be overwritten. Plus the
    # read-only `lease_lost` property mirroring `_lease_lost` for
    # `health.py`/`api/app.py`. Measured on this tree with the scanner below.
    # 3196 -> 3205 (+9): explanatory comment on the un-timeout-wrapped
    # `self.wake.tick` await in the dispatch loop, clarifying that
    # `_CLI_TIMEOUT` bounds only a single `pr_watcher._run_cli` call, not the
    # whole sequential sweep over parked tasks. No behavior change.
    "core/scheduler.py": 3205,
}


@dataclass(frozen=True)
class Entry:
    key: str
    lines: int
    cc: int


def _cyclomatic(fn: ast.AST) -> int:
    """Cyclomatic complexity estimate for a function node. See module
    docstring for the exact formula; `AsyncWith` is deliberately excluded."""
    cc = 1
    for node in ast.walk(fn):
        if isinstance(
            node,
            (
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.ExceptHandler,
                ast.With,
                ast.IfExp,
                ast.match_case,
            ),
        ):
            cc += 1
        elif isinstance(node, ast.BoolOp):
            cc += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            cc += len(node.ifs)
    return cc


def _walk_defs(node: ast.AST, prefix: str, path_key: str, out: list[Entry]) -> None:
    """Explicit recursive descent through ClassDef/FunctionDef/AsyncFunctionDef,
    building dotted qualnames. NOT `ast.walk` — that loses nesting and would
    collide e.g. two different classes' `_run` methods under one key."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            _walk_defs(child, f"{prefix}{child.name}.", path_key, out)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = f"{prefix}{child.name}"
            lines = child.end_lineno - child.lineno + 1
            out.append(Entry(f"{path_key}:{qualname}", lines, _cyclomatic(child)))
            _walk_defs(child, f"{qualname}.", path_key, out)


def scan_source(text: str, path: str) -> tuple[list[Entry], int]:
    """Parse `text` (as if it were `path`) and return (every function Entry,
    total line count of the file). Unfiltered — callers apply thresholds."""
    tree = ast.parse(text, filename=path)
    entries: list[Entry] = []
    _walk_defs(tree, "", path, entries)
    return entries, len(text.splitlines())


def scan_tree(root: Path) -> tuple[dict[str, int], dict[str, int], dict[str, int], int, int]:
    """Walk every `*.py` under `root` once. Returns three dicts of only the
    OFFENDING entries (over their respective threshold) — function line
    counts, function cc, file line counts — plus (total files scanned, total
    functions scanned) for the fail-closed floor check."""
    function_lines: dict[str, int] = {}
    function_cc: dict[str, int] = {}
    file_lines: dict[str, int] = {}
    total_functions = 0
    files = sorted(root.rglob("*.py"))
    for path in files:
        rel = path.relative_to(root).as_posix()
        entries, lines = scan_source(path.read_text(encoding="utf-8"), rel)
        total_functions += len(entries)
        if lines > MAX_FILE_LINES:
            file_lines[rel] = lines
        for entry in entries:
            if entry.lines > MAX_FUNCTION_LINES:
                function_lines[entry.key] = entry.lines
            if entry.cc > MAX_FUNCTION_CC:
                function_cc[entry.key] = entry.cc
    return function_lines, function_cc, file_lines, len(files), total_functions


def offenders(
    measured: dict[str, int], frozen: dict[str, int], threshold: int, list_name: str
) -> tuple[list[str], list[str], list[str]]:
    """Pure comparison of a measured dict against a frozen allow-list.
    Returns (new, grown, stale) as rendered message strings, each naming the
    offending key so a failure is actionable without re-running the scan.

      new   -- measured[key] > threshold and key not in frozen
      grown -- key in frozen, measured[key] > frozen[key]
      stale -- key in frozen but either absent from measured (symbol
               renamed/deleted) or measured[key] <= threshold (shrunk below
               the line/cc it was frozen for) -- both causes emit the same
               "delete it" instruction, because both mean the entry no
               longer protects anything.
    """
    new: list[str] = []
    for key, value in measured.items():
        if value > threshold and key not in frozen:
            new.append(f"{list_name}: {key} is {value} (> {threshold}) and is not frozen")

    grown: list[str] = []
    stale: list[str] = []
    for key, frozen_value in frozen.items():
        current = measured.get(key)
        if current is None or current <= threshold:
            stale.append(f"delete `{key}` from {list_name} in tests/test_structural_budget.py")
        elif current > frozen_value:
            grown.append(
                f"{key}: frozen {frozen_value}, now {current} "
                f"(+{current - frozen_value}); this budget only ratchets down"
            )
    return new, grown, stale


@pytest.fixture(scope="session")
def scanned() -> tuple[dict[str, int], dict[str, int], dict[str, int], int, int]:
    return scan_tree(SRC)


# ── fail-closed floors + baseline sanity ──────────────────────────────── #


def test_the_scanner_sees_the_whole_package(scanned):
    _, _, _, total_files, total_functions = scanned
    assert SRC.is_dir()
    assert total_files >= 100, f"only found {total_files} files under {SRC} -- scanner is walking nothing"
    assert total_functions >= 300, f"only found {total_functions} functions -- scanner is walking nothing"


def test_frozen_lists_are_the_measured_baseline():
    assert FROZEN_FUNCTION_LINES
    assert FROZEN_FUNCTION_CC
    assert FROZEN_FILE_LINES
    assert "core/orchestrator.py:Orchestrator._run_attempt" in FROZEN_FUNCTION_LINES
    assert "core/orchestrator.py:Orchestrator._run_attempt" in FROZEN_FUNCTION_CC
    assert "core/orchestrator.py" in FROZEN_FILE_LINES


# ── no new offender ────────────────────────────────────────────────────── #


def test_no_new_oversized_functions(scanned):
    function_lines, _, _, _, _ = scanned
    new, _, _ = offenders(function_lines, FROZEN_FUNCTION_LINES, MAX_FUNCTION_LINES, "FROZEN_FUNCTION_LINES")
    assert new == [], "\n".join(new)


def test_no_new_complex_functions(scanned):
    _, function_cc, _, _, _ = scanned
    new, _, _ = offenders(function_cc, FROZEN_FUNCTION_CC, MAX_FUNCTION_CC, "FROZEN_FUNCTION_CC")
    assert new == [], "\n".join(new)


def test_no_new_oversized_files(scanned):
    _, _, file_lines, _, _ = scanned
    new, _, _ = offenders(file_lines, FROZEN_FILE_LINES, MAX_FILE_LINES, "FROZEN_FILE_LINES")
    assert new == [], "\n".join(new)


# ── no growth ───────────────────────────────────────────────────────────── #


def test_no_frozen_entry_has_grown(scanned):
    function_lines, function_cc, file_lines, _, _ = scanned
    checks = [
        (function_lines, FROZEN_FUNCTION_LINES, MAX_FUNCTION_LINES, "FROZEN_FUNCTION_LINES"),
        (function_cc, FROZEN_FUNCTION_CC, MAX_FUNCTION_CC, "FROZEN_FUNCTION_CC"),
        (file_lines, FROZEN_FILE_LINES, MAX_FILE_LINES, "FROZEN_FILE_LINES"),
    ]
    for measured, frozen, threshold, name in checks:
        _, grown, _ = offenders(measured, frozen, threshold, name)
        assert grown == [], "\n".join(grown)


# ── ratchet: shrunk-below-threshold / vanished entries must be deleted ──── #


def test_a_frozen_entry_that_dropped_below_threshold_must_be_deleted(scanned):
    """Real-tree enforcement: if any frozen entry has shrunk below its
    threshold on the live tree, this must fail naming it -- that is the
    instruction to delete it from the allow-list, not to widen anything."""
    function_lines, function_cc, file_lines, _, _ = scanned
    checks = [
        (function_lines, FROZEN_FUNCTION_LINES, MAX_FUNCTION_LINES, "FROZEN_FUNCTION_LINES"),
        (function_cc, FROZEN_FUNCTION_CC, MAX_FUNCTION_CC, "FROZEN_FUNCTION_CC"),
        (file_lines, FROZEN_FILE_LINES, MAX_FILE_LINES, "FROZEN_FILE_LINES"),
    ]
    for measured, frozen, threshold, name in checks:
        _, _, stale = offenders(measured, frozen, threshold, name)
        assert stale == [], "\n".join(stale)


def test_a_frozen_entry_whose_symbol_vanished_must_be_deleted():
    """Mechanism proof: a frozen key entirely absent from the measured dict
    (function renamed or deleted) is reported stale with a delete
    instruction naming it -- distinct from, but handled the same as, a
    value that merely shrank below threshold (see the test above)."""
    measured = dict(FROZEN_FUNCTION_LINES)
    key = next(iter(FROZEN_FUNCTION_LINES))
    del measured[key]
    new, grown, stale = offenders(measured, FROZEN_FUNCTION_LINES, MAX_FUNCTION_LINES, "FROZEN_FUNCTION_LINES")
    assert new == []
    assert grown == []
    assert stale == [f"delete `{key}` from FROZEN_FUNCTION_LINES in tests/test_structural_budget.py"]


# ── negative x3: offenders() on fully synthetic dicts ──────────────────── #


def test_offenders_reports_a_new_entry():
    measured = {"pkg/mod.py:foo": 350}
    new, grown, stale = offenders(measured, {}, 300, "FROZEN_FUNCTION_LINES")
    assert grown == []
    assert stale == []
    assert len(new) == 1
    assert "pkg/mod.py:foo" in new[0]


def test_offenders_reports_growth():
    measured = {"pkg/mod.py:foo": 320}
    frozen = {"pkg/mod.py:foo": 300}
    new, grown, stale = offenders(measured, frozen, 300, "FROZEN_FUNCTION_LINES")
    assert new == []
    assert stale == []
    assert len(grown) == 1
    assert "pkg/mod.py:foo" in grown[0]


def test_offenders_reports_a_stale_entry():
    # `foo` vanished entirely; `bar` is still present but shrank <= threshold.
    frozen = {"pkg/mod.py:foo": 300, "pkg/mod.py:bar": 300}
    measured = {"pkg/mod.py:bar": 250}
    new, grown, stale = offenders(measured, frozen, 300, "FROZEN_FUNCTION_LINES")
    assert new == []
    assert grown == []
    assert len(stale) == 2
    assert any("pkg/mod.py:foo" in msg for msg in stale)
    assert any("pkg/mod.py:bar" in msg for msg in stale)


# ── known-positive probes, each with a negative twin ────────────────────── #


def _function_source(body_lines: int) -> str:
    return "def f():\n" + "    pass\n" * body_lines


def test_a_301_line_function_is_flagged_a_299_line_function_is_not():
    positive, _ = scan_source(_function_source(300), "synthetic.py")
    negative, _ = scan_source(_function_source(298), "synthetic.py")
    assert positive[0].lines == 301
    assert positive[0].lines > MAX_FUNCTION_LINES
    assert negative[0].lines == 299
    assert negative[0].lines <= MAX_FUNCTION_LINES


def _cc_source(n_ifs: int) -> str:
    body = "\n".join("    if True:\n        pass" for _ in range(n_ifs))
    return "def f():\n" + body + "\n"


def test_a_cc_61_function_is_flagged_a_cc_59_function_is_not():
    positive, _ = scan_source(_cc_source(60), "synthetic.py")
    negative, _ = scan_source(_cc_source(58), "synthetic.py")
    assert positive[0].cc == 61
    assert positive[0].cc > MAX_FUNCTION_CC
    assert negative[0].cc == 59
    assert negative[0].cc <= MAX_FUNCTION_CC


def _file_source(n_lines: int) -> str:
    return "x = 1\n" * n_lines


def test_a_2501_line_file_is_flagged_a_2500_line_file_is_not():
    _, positive_lines = scan_source(_file_source(2501), "synthetic.py")
    _, negative_lines = scan_source(_file_source(2500), "synthetic.py")
    assert positive_lines == 2501
    assert positive_lines > MAX_FILE_LINES
    assert negative_lines == 2500
    assert negative_lines <= MAX_FILE_LINES


# ── formula pin ──────────────────────────────────────────────────────────── #


_CC_SNIPPET = textwrap.dedent(
    '''\
    def f(a, b, c, x):
        if a:
            pass
        for i in x:
            pass
        while b:
            pass
        try:
            pass
        except Exception:
            pass
        with open("f") as fh:
            pass
        z = a and b and c
        y = 1 if a else 2
        lst = [i for i in x if i]
        match a:
            case 1:
                pass
            case 2:
                pass

        async def g():
            async with open("f") as fh2:
                pass
    '''
)


def test_cc_formula_on_a_hand_counted_snippet():
    # if(+1) for(+1) while(+1) except(+1) with(+1) boolop-of-3(+2) ternary(+1)
    # comprehension-if(+1) match-2-cases(+2) = 11, plus the base 1 = 12.
    # The nested `async with` is walked (it is inside f's subtree) but must
    # NOT be counted -- that is the asymmetry this test exists to pin.
    entries, _ = scan_source(_CC_SNIPPET, "synthetic.py")
    outer = next(e for e in entries if e.key == "synthetic.py:f")
    assert outer.cc == 12, outer.cc


# ── qualname correctness ─────────────────────────────────────────────────── #


def test_qualnames_cover_methods_and_nested_functions():
    src = textwrap.dedent(
        """\
        class A:
            def m(self):
                def inner():
                    pass
                return inner
        """
    )
    entries, _ = scan_source(src, "mod.py")
    keys = {e.key for e in entries}
    assert "mod.py:A.m" in keys
    assert "mod.py:A.m.inner" in keys


# ── runtime bound ─────────────────────────────────────────────────────────── #


def test_the_whole_walk_finishes_under_five_seconds():
    start = time.perf_counter()
    scan_tree(SRC)
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"scan_tree(SRC) took {elapsed:.2f}s (must be < 5.0s)"


# ── docs ─────────────────────────────────────────────────────────────────── #


def test_verification_doc_names_this_guard_and_its_thresholds():
    doc = (REPO_ROOT / "docs" / "verification.md").read_text(encoding="utf-8")
    assert "test_structural_budget.py" in doc
    assert "300" in doc
    assert "60" in doc
    assert "2,500" in doc


# ── ledger self-check ─────────────────────────────────────────────────────── #

# The rot-prone claim form: an entry asserting equality/match against "the
# frozen value" it precedes. Such a claim is true only until a LATER entry
# moves that same frozen value -- at which point the earlier sentence goes
# false with no editor touching it. See LEDGER CONVENTION above
# FROZEN_FUNCTION_LINES.
_LEDGER_CLAIM_FORMS = re.compile(
    r"match(?:ing|es)? the frozen value"
    r"|equals? the frozen value"
    r"|the frozen value below(?: exactly)?"
    r"|same as the frozen (?:value|entry) below",
    re.IGNORECASE,
)
_LEDGER_ROW_RE = re.compile(r'^\s*"([^"]+)":\s*(\d+),\s*$')
_LEDGER_CHAIN_RE = re.compile(r"(\d+)\s*->\s*(\d+)")
_LEDGER_DICT_NAMES = ("FROZEN_FUNCTION_LINES", "FROZEN_FUNCTION_CC", "FROZEN_FILE_LINES")


def _ledger_dict_blocks(lines: list[str]) -> list[tuple[str, int, int]]:
    """(dict_name, open_brace_line_idx, close_brace_line_idx) for each frozen dict."""
    open_re = re.compile(r"^(" + "|".join(_LEDGER_DICT_NAMES) + r") = \{\s*$")
    blocks = []
    name, start = None, None
    for i, line in enumerate(lines):
        m = open_re.match(line)
        if m:
            name, start = m.group(1), i
            continue
        if start is not None and line == "}":
            blocks.append((name, start, i))
            start = None
    return blocks


def _comment_run_above(lines: list[str], row_idx: int) -> str:
    j = row_idx - 1
    run = []
    while j >= 0 and lines[j].strip().startswith("#"):
        run.append(lines[j].strip().lstrip("#").strip())
        j -= 1
    return " ".join(reversed(run))


def test_no_ledger_entry_claims_equality_with_a_frozen_value():
    # Positive control FIRST: prove the matcher actually catches the known
    # pre-fix sentence (the "2985 -> 3019 (+34)" entry, once worded "...
    # actual 3019 -- matching the frozen value below exactly."). A clean
    # sweep below is worthless if the matcher can't even find this.
    synthetic = (
        "2985 -> 3019 (+34): ... and after the merge (this tree) actual "
        "3019 -- matching the frozen value below exactly."
    )
    assert _LEDGER_CLAIM_FORMS.search(synthetic), (
        "the banned-claim matcher failed its positive control -- it does "
        "not catch the known pre-fix sentence, so a clean sweep proves nothing"
    )

    lines = Path(__file__).read_text(encoding="utf-8").splitlines()
    blocks = _ledger_dict_blocks(lines)
    assert len(blocks) == 3, f"expected 3 frozen dicts, found {[b[0] for b in blocks]}"

    claim_violations = []
    chain_breaks = []
    checked_rows = 0
    for dict_name, start, end in blocks:
        for i in range(start + 1, end):
            m = _LEDGER_ROW_RE.match(lines[i])
            if not m:
                continue
            key, frozen_value = m.group(1), int(m.group(2))
            run = _comment_run_above(lines, i)
            if not run:
                continue
            checked_rows += 1

            # Every non-terminal AND terminal ledger sub-entry is banned from
            # asserting equality with the frozen value: even a currently-true
            # terminal claim rots the moment the next entry lands.
            sub_entries = [s for s in re.split(r"(?=\d+\s*->\s*\d+)", run) if s.strip()]
            for sub in sub_entries:
                if _LEDGER_CLAIM_FORMS.search(sub):
                    claim_violations.append(
                        f'{dict_name}["{key}"] = {frozen_value} (line {i + 1}): {sub.strip()!r}'
                    )

            # Chain-tail invariant: the last "A -> B" target in the run must
            # equal the frozen value the row lands on.
            chain_matches = list(_LEDGER_CHAIN_RE.finditer(run))
            if chain_matches:
                last_target = int(chain_matches[-1].group(2))
                if last_target != frozen_value:
                    chain_breaks.append(
                        f'{dict_name}["{key}"] (line {i + 1}): chain ends at '
                        f"{last_target}, frozen value is {frozen_value}"
                    )

    assert checked_rows >= 30, f"only resolved {checked_rows} ledger rows -- block detection is broken"

    assert not claim_violations, (
        "ledger entries assert equality/match against a frozen value that a "
        "later entry is expected to move -- state only what was measured at "
        "that point in time instead:\n" + "\n".join(claim_violations)
    )
    assert not chain_breaks, (
        "ledger chain's last recorded value does not match the row's frozen "
        "value:\n" + "\n".join(chain_breaks)
    )
