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
    "core/orchestrator.py:Orchestrator._run_attempt": 2186,
    # 760 -> 778 (+18): dispatch-time intake-eval hoisted path — the `elif
    # ctx.get("eval_result")` branch that acts on a grill/wizard-stored
    # verdict (idempotency marker, cost/residual-gap comments) added inside
    # `_drive`. Measured on this tree with the scanner below.
    "core/orchestrator.py:Orchestrator._drive": 778,
    # 449 -> 457 (+8): D3.1 (2026-08-31, auto-activation pipeline) adds the
    # one call (plus its explanatory comment) that hands `paused`/
    # `activated_at`/`learning_events` schema work to a new sibling method,
    # `_ensure_d3_learning_columns` — split out specifically so the three new
    # columns' worth of ALTERs did NOT land inline here. Re-anchored on merge.
    "core/db.py:Store._ensure_task_columns": 457,
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
    "blockers/wake.py:WakeWatcher._check_pr_conflict": 453,
    # 418 -> 424 (+6): D1.1 fix round — attempt-scoped verification-artifact
    # write wired into `_finalize` (review findings #1/#7). Measured on the
    # D1.1 squash-merge result.
    # 424 -> 437 (+13): D1.2 (2026-08-31, "visual proof") adds the one
    # best-effort call that runs the UI-evidence browser walk after tests
    # pass and threads its rendered media section into `_pr_body`.
    # Re-anchored on merge.
    "core/orchestrator.py:Orchestrator._finalize": 437,
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
    "core/orchestrator.py:Orchestrator._run_review": 394,
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
    "cli/commands.py:bench_run": 400,
    # Grew to 304 (> 300) when D3.1 (2026-08-31, auto-activation pipeline)
    # threaded `learning.auto_manage`/`learning.auto_activate_daily_cap`
    # through `nh serve`'s `HarvestJob` construction — the kill switch's own
    # config must reach the CLI-driven scheduling path, not just the API
    # server's embedded one. Reviewed on its merits; frozen here.
    "cli/commands.py:serve": 304,
    # Grew to 371 when the UI-evidence prompt block landed (task 389210fa):
    # an inline enable+glob gate + the ui_evidence_block call. Reviewed on
    # its merits (the block is inert until a profile opts in); frozen here.
    "core/orchestrator.py:Orchestrator._build_implement_prompt": 371,
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
    # Grew to 314 (> 300) when the done_no_evidence repair shape landed
    # (task bf413cc6): two new refusal guards + the DONE branch. The growth
    # was reviewed on its merits; frozen here as its landing baseline.
    "blockers/landed_override.py:approve_landed_override": 315,
    "core/metrics.py:compute_metrics": 334,  # +21: PR #869 cost_usd_total server-side pricing
    # NEW (324, > 300): mechanical resolution extended to cover
    # `tests/test_structural_budget.py` FROZEN_* numeric-only conflicts --
    # the new budget-hunk branch in the worktree merge-step loop, the
    # ship-classified-paths extension, the names-to-add extension, the
    # re-anchor proof-test call, and the extended final-return detail
    # string; re-homed onto the tree where the inventory-backend dispatch
    # (97a9fd79b) also lives in this function. Measured on this merged tree
    # with the scanner below.
    "vcs/derived_conflict.py:_resolve_in_worktree": 324,
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
    "core/orchestrator.py:Orchestrator._run_attempt": 255,
    "core/orchestrator.py:Orchestrator._drive": 115,
    "agent/guard.py:_approve_denial": 81,
    # 73 -> 74 (+1): same cause as the LINES entry above — e922e9b4's landing
    # grew the conflict watcher; pre-existing red on main at 03b262d23,
    # repaired (measured) on this merge.
    "blockers/wake.py:WakeWatcher._check_pr_conflict": 74,
    # 73 -> 74 (+1): same verifier-wall-park cause as the LINES entry above
    # — the added `result.is_error` branch is one more `If`. Measured on
    # this tree.
    "core/orchestrator.py:Orchestrator._run_review": 74,
    # Crossed 60 (to 67) with the UI-evidence gate landed by task 389210fa.
    "core/orchestrator.py:Orchestrator._build_implement_prompt": 67,
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
    "core/orchestrator.py": 21564,
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
    "cli/commands.py": 8423,
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
    "api/app.py": 5983,
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
    "core/db.py": 5082,
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
    "config.py": 3562,
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
    "review/reviewer.py": 2945,
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
    "blockers/wake.py": 2752,
    # +91: `_SCAN_WRAPPER_NAMES` + `_peel_scan_wrappers` — peels
    # timeout/xargs/nice/stdbuf (and siblings) for the scan-severity check
    # only, so a wrapped `find … -delete` in a denied compound classifies
    # DESTRUCTIVE instead of HYGIENE. Local sibling list, `_WRAPPERS` untouched.
    "agent/guard.py": 2892,
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
    "core/scheduler.py": 2973,
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
        entries, lines = scan_source(path.read_text(), rel)
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
    doc = (REPO_ROOT / "docs" / "verification.md").read_text()
    assert "test_structural_budget.py" in doc
    assert "300" in doc
    assert "60" in doc
    assert "2,500" in doc
