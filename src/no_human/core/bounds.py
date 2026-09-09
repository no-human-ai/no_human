"""Termination bounds and stuck detection (PLAN.md 4.3, constraint §3.5).

These are hard, enforced limits — not advisory. They keep the loop from
doom-looping on an impossible task or stacking corrections on a stale context.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import hashlib
import re
from dataclasses import dataclass, field, fields


@dataclass
class Bounds:
    """Per-task / per-attempt caps, sourced from config ``bounds``."""

    max_attempts: int = 3
    # Measured on task 84251cb2 (docs/BASELINE_M0.md): 22 turns to reach the
    # reviewer, and >40 to act on its three findings — that run escalated
    # mid-fix at a cap of 40, having already committed a correct fix. A cap
    # that stops a nearly-finished attempt wastes the whole attempt's burn.
    #
    # Set to 500 by the user (2026-07-10): give a real task room to finish.
    # Know what this cap is and is not. It is NOT a cost control — it is the
    # last-resort stop for an agent that has stopped making progress. It is no
    # longer the only one: since ARCH_REVIEW B2 #1 the stuck detectors have a
    # HARD tier (doom_loop_abort/edit_abort below) that ends the attempt
    # deterministically, work checkpointed — the advisory tier still only
    # emits telemetry. Attempt 11 once burned 3.4M cache-read in 41 turns
    # looping on one file with ~12x that headroom here; that class of runaway
    # now aborts at the hard threshold instead of burning to this cap.
    max_turns_per_attempt: int = 500
    # P3 (megaplan): complex tasks (many files / large plan / decompose verdict)
    # get a larger turn budget so they don't exhaust turns mid-implementation and
    # fail with an empty diff (B5). Applied only to the complexity-flagged subset.
    complex_multiplier: float = 1.5
    # Lifetime caps across the task's WHOLE life, resumes included.
    # `max_attempts` bounds ONE loop; every `nh reply`/resume starts a fresh
    # loop, which is how task 84251cb2 reached attempt 17 and 21.2M cache-read
    # tokens without any cap ever firing. Exceeding either cap raises a
    # BUDGET_EXHAUSTED blocker whose option can raise the budget for that one
    # task — the human decides, the loop never silently continues. 9 attempts =
    # three full bounded loops. 8M tokens: the enterprise profile now bills PER
    # TOKEN, so the cap is a cost guardrail, not just a doom-loop stop. Measured
    # 2026-07-13 (29 tasks): the largest PR-producing task burned 6.15M and every
    # other success ≤1.71M — NO success sat above 6.15M. Six failed/parked tasks
    # sat above 8M (the CI-gate outliers at 61.5M and 10.18M, another at 20.8M, and
    # tasks at 22.8M/15M/12.7M). So 8M clears every real success with headroom and
    # parks a runaway an order of magnitude sooner than the old 25M did (which the
    # 61.5M task blew past entirely, across resumes).
    #
    # UNIT CHANGE, 2026-07-31: `lifetime_tokens` and `attempt_tokens` are now
    # COST-WEIGHTED tokens (`core.pricing.weighted_tokens`) — fresh in/out at
    # 1.0, cache creation at 1.25, cache read at 0.1 — not raw token counts.
    # The raw counter was measuring conversation LENGTH and calling it spend:
    # task d6e4b72a died at "12,367,237/12,000,000" on an attempt whose real
    # burn was 877,127 fresh-equivalent tokens, 97% of it prefix re-reads that
    # bill at a tenth of the rate the cap charged them.
    #
    # The numbers below are the old raw caps CONVERTED, not relaxed. Measured
    # over this install's whole ledger (193 tasks / 602 attempts / 1.718B raw
    # tokens): weighted spend is 0.1985x raw. 8,000,000 x 0.1985 = 1,588,000,
    # and 1,600,000 is also the empirical optimum — swept over 400k..4M in 50k
    # steps it reproduces the raw 8M cap's park-or-spare verdict on 191 of the
    # 193 real tasks (0 tasks newly spared, 2 newly parked). So the DOLLAR
    # bound is unchanged; what changes is that it is now the SAME dollar bound
    # for every task. The per-task weighted/raw ratio ranges 0.122..0.697 in
    # that corpus, so the raw cap was handing a cache-heavy task 5.7x the real
    # budget of a fresh-heavy one while printing the same number at both.
    #
    # CUTOVER, and it is handled — see `core.pricing.raw_cap_as_weighted`.
    # Per-task raises written before this change are RAW numbers: 165 tasks on
    # this install carry a `task.config["lifetime_tokens"]` of 12M-68M and 162
    # carry a raw `attempt_tokens`. An earlier draft of this comment said "156
    # ... on mostly-finished tasks"; both halves were wrong. 94 of the 165 are
    # escalated/failed/paused — precisely what a mass retry re-runs — and read
    # verbatim they would have granted 1.388 BILLION of ceiling across the 91
    # escalated+failed where ~275M was intended. Overrides are therefore read
    # through a unit guard that treats an UNMARKED config as raw and converts
    # it; only a config carrying `budget_unit: "weighted"` (stamped by every
    # write in blockers/actions.py) is taken at face value.
    lifetime_attempts: int = 9
    # RAISE, 2026-08-03: 1.6M -> 4M, derived from the honest ledger. The 1.6M
    # above was derived honestly — but in a unit that no longer exists: its
    # ledger recorded subagent spend through a gauge later measured at ~17% of
    # the bill (fixed by the four-tier accounting change), and the three
    # non-coder tiers are 37-40% of real weighted spend, so honest accounting
    # reads ~1.5-1.6x the old numbers on accounting alone. On top of that,
    # runs also changed shape (turn-count ratios 0.57-2.53x across the 10
    # same-spec trials vs 7-26 — most grew, one spec shrank; per-turn spend
    # only ~1.2x) — behavior change, not artifact, and the cap must fit what
    # the product now does.
    # THE DERIVATION (independent review, 2026-08-03, this install's ledger at
    # 221 tasks / 690 attempts, four-tier weighted): 1.6M parks 117/221 tasks
    # (52.9%) — half of all real work; 4.0M parks 15/221 (6.8%) and sits at
    # the knee of the curve (3.5M: 10.4%, 4.5M: 5.0%); the attempt-17 runaway
    # this cap exists for reads 9,127,714 weighted and still parks with 2.3x
    # margin. Corroborating, July's run distribution: p95 = 1.66M, and the
    # 1.6M cap already parked ~10% of those rows AS UNDER-counted.
    # First-look claims corrected by that review, kept so they are not
    # re-derived: only 1 of the first 4 baseline specs budget-parked (3/3
    # trials, ns-02fbd7b8), not 3 of 4 — the others escalated for non-budget
    # reasons; and the early "2.0-2.4x multiplier (n=4)" required dropping a
    # spec that got CHEAPER (per-spec medians 0.68/1.43/2.39/2.67 over 10
    # trials). The ledger sweep above replaces both as the basis.
    # Re-sweep after the batch-3 baseline completes under this cap; margins
    # are thinner now (runaway margin 2.3x, was ~5.7x) — a further raise
    # needs a new sweep, not a multiplier.
    lifetime_tokens: int = 4_000_000
    # Per-ATTEMPT spend cap (v6 taxonomy, 2026-07-16): four live specs burned
    # the entire 8M lifetime budget in attempt #1 — the mid-attempt watch was
    # armed with the remaining LIFETIME budget, so the bounded loop never got a
    # second attempt. Crossing THIS cap ends the attempt (work checkpointed,
    # loop retries with fresh context — the StuckAbort semantics); only the
    # lifetime cap parks behind BUDGET_EXHAUSTED. 4M clears the largest
    # measured successful attempt (complex tier: 3.06M cache-read) with ~30%
    # headroom and leaves the loop at least two real attempts inside 8M.
    # Per-task overridable via task.config["attempt_tokens"] (human-only).
    #
    # Also cost-weighted since 2026-07-31, converted the same way: the old raw
    # 4,000,000 x 0.1985 = 794,000, rounded to 800,000. Swept over the same
    # ledger's 602 attempt rows, 800,000 weighted reproduces the raw 4M cap's
    # verdict on 566 of them (750,000 peaks at 571 — inside the noise, since
    # attempt rows pile up exactly AT the cap that truncated them, so the
    # rounder number is taken). The measured complex-tier attempt this cap has
    # to clear — 3.06M raw, almost all cache-read — is 306,000 weighted.
    # Raised with the lifetime cap, from the same ledger sweep: 800k fired on
    # 215/690 real attempts (31%) — a cap that ends a third of all attempts is
    # not bounding runaways, it is the workload; 2M fires on 4/690 (0.6%).
    # Keeps the 2:1 lifetime:attempt shape (now at exact equality with the
    # `attempt <= lifetime // 2` invariant — deliberate, the loop keeps two
    # real attempts) and clears the largest measured successful attempt (306k
    # weighted) by 6.5x.
    attempt_tokens: int = 2_000_000
    # The smallest COST-WEIGHTED spend an attempt costs before its first model
    # turn does any work: attempt_distill + the implement prompt + skills + map,
    # re-accumulated per attempt. Used only when a task has no measured history
    # (see `Orchestrator._min_viable_attempt_cost`, which prefers a real prior
    # attempt's first-10-message `cache_burn` figure over this floor).
    # Derived from run 123dea00 (2026-08-20): attempt 11's startup was 884,932
    # raw = 157 fresh + 59,837 cache-write + 824,938 cache-read = 157,447
    # weighted (1.0 / 1.25 / 0.1, core.pricing), and attempt 10's first
    # cache_burn (10 messages) was 479k read + 52k write = ~113k weighted.
    # 250,000 is ~1.6x the largest of those — a floor that refuses a
    # guaranteed-dead attempt without refusing one that could do real work.
    min_viable_attempt_weighted_tokens: int = 250_000

    @staticmethod
    def from_config(cfg: dict | None) -> "Bounds":
        """Config overrides; every default lives on the dataclass, once.

        This used to repeat each default as a literal here, so the dataclass,
        this function and ``DEFAULT_CONFIG`` were three places to change and
        two places to forget. Unknown keys are ignored rather than crashing on
        a hand-edited config.
        """
        known = {f.name for f in fields(Bounds)}
        return Bounds(**{k: v for k, v in (cfg or {}).items() if k in known})

    def turns_for(self, *, complex_task: bool = False) -> int:
        """Turn budget for one attempt. Complex tasks get
        ``max_turns_per_attempt × complex_multiplier`` (megaplan P3 / B5)."""
        base = self.max_turns_per_attempt
        if complex_task and self.complex_multiplier > 1:
            return int(round(base * self.complex_multiplier))
        return base


#: Strips the trailing byte-count off a `_test_run_summary`-shaped string
#: (`"{status}, {n} chars"`), leaving just the `exit {code}`/`ok`/`failed`
#: portion. Used by `StuckDetector.record_test_outcome` so "the outcome
#: changed" means a status transition, not output-size churn.
_STATUS_ONLY_RE = re.compile(r",\s*\d+\s*chars\s*$")


def _status_only(summary: str) -> str:
    return _STATUS_ONLY_RE.sub("", summary)


def error_signature(text: str) -> str:
    """Reduce an error/output blob to a stable signature for stuck detection.

    Strips volatile tokens (hex ids, line/col numbers, timestamps, paths) so
    that "the same error twice" is recognized even when incidental details
    differ. Two genuinely-identical failures hash equal; progress changes it.
    """
    norm = text.lower()
    norm = re.sub(r"0x[0-9a-f]+", "<hex>", norm)
    norm = re.sub(r"\b[0-9a-f]{8,}\b", "<hash>", norm)
    norm = re.sub(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}", "<ts>", norm)
    norm = re.sub(r":\d+(:\d+)?", ":<n>", norm)        # file:line:col
    norm = re.sub(r"/[^\s'\"]+", "<path>", norm)        # absolute paths
    norm = re.sub(r"\s+", " ", norm).strip()
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


@dataclass
class StuckDetector:
    """Tracks repeated error signatures within an attempt.

    Per §3.5: the *same* error signature seen twice means zero progress — the
    correct response is to reset context in a fresh session, not to keep
    appending corrections to a stale one.

    Three detection layers (R2.3, AgentPatterns):
      1. **Edit-count per file** — same file edited ≥ ``edit_threshold`` times
         (advisory, raw count) / the HARD tier's progress-gated twin below.
      2. **Doom-loop** — identical tool+input repeated consecutively.
      3. **Ping-pong** — A-B-A-B alternating pattern (R2.1, Broker).
    The hard iteration cap (``max_turns``) is Layer 3 — outside this class.
    """

    threshold: int = 2
    doom_loop_threshold: int = 3
    # R2.3 Layer 1: edit-count per file.
    edit_threshold: int = 5
    # Hard-abort tier (ARCH_REVIEW B2 #1). The advisory thresholds above emit
    # telemetry; crossing a hard threshold ends the ATTEMPT (StuckAbort in the
    # orchestrator's sink — work checkpointed, bounded loop retries with fresh
    # context). Set far above the advisory tier so they fire only on
    # unambiguous runaways: 9 identical consecutive calls, one file edited
    # 15× WITH NO OBSERVED PROGRESS (see `record_edit`/`record_test_outcome`
    # below — the tier now resets a file's hard count only on a recognised
    # test-runner invocation whose outcome STATUS changed since the last one).
    # task f6e626fd attempt 1 was hard-aborted on a raw 15-edit count of
    # `web/e2e/dead-click-race.mjs`; the round-1 review measured only 2 of its
    # 117 Bash calls were recognised as test runs by the THEN-current
    # predicate, both before the loop started, so whether that attempt's test
    # outcome changed across the loop is NOT established by the recorded
    # stream — this comment used to claim otherwise and that claim is struck.
    # What IS established: the widened shared predicate (`_TEST_RUNNER_RE`)
    # now recognises the `node <script>.mjs` harness form that attempt used,
    # so a re-run under the current code would at least see its test-runner
    # calls. 61406d02 remains the documented agent-owned-path casualty of the
    # same earlier raw-count-only rule (see `scope_guard.py`), or 12
    # consecutive calls alternating between the same two actions.
    doom_loop_abort: int = 9
    edit_abort: int = 15
    ping_pong_abort_window: int = 12
    # ABSOLUTE per-file ceiling (send-back review round 2, MAJOR-1/property
    # 2): unlike `edit_abort` above, NO progress signal resets this one — it
    # reads the raw `_edit_counts` this class already keeps for the advisory
    # tier. Removing the cross-file reset from `record_edit` means a file
    # can now accumulate an unbounded `_hard_edit_counts` run as long as
    # SOME test outcome keeps changing between edits; this backstop still
    # ends the attempt if one file is edited this many times regardless of
    # what else happened. 2× `edit_abort`: `edit_abort` (15) is calibrated to
    # fire on a run with NO progress signal at all; a file edited twice that
    # many times, even with real test-outcome churn between edits, has
    # exhausted more attempts on one file than any single-file fix in the
    # measured corpus needed (see the 26/27-attempt "stuck-abort: edit-loop"
    # corpus replay run against this detector, PR body — no genuine
    # non-abort corpus attempt drives one file's raw edit count anywhere
    # near 30).
    edit_ceiling: int = 30
    _seen: dict[str, int] = field(default_factory=dict)
    _last: str | None = None
    _tool_signatures: list[str] = field(default_factory=list)
    _consecutive_repeats: int = 0
    # R2.3 Layer 1: per-file edit counts (raw — advisory tier only).
    _edit_counts: dict[str, int] = field(default_factory=dict)
    # HARD tier's own per-file counter: unlike `_edit_counts` above, this one
    # resets to 1 whenever `record_edit` observes progress since the file's
    # previous edit — a changed test-runner outcome (`record_test_outcome`
    # set `_progress_since_last_edit`) — see `record_edit`. An edit LOOP is
    # edits whose observed outcome does not change; edits that keep
    # converging on a fix, however many, are not one, however many test runs
    # it takes. Editing a DIFFERENT file in between is deliberately NOT
    # treated as progress on its own (send-back review round 2, MAJOR-1): an
    # agent bouncing between two files with no observed outcome change is
    # still a loop, and a bare "different file" reset let exactly that
    # escape both this tier and ping-pong (see `edit_ceiling` below for the
    # backstop that still bounds it).
    _hard_edit_counts: dict[str, int] = field(default_factory=dict)
    _progress_since_last_edit: bool = False
    # Up to the last 2 test-outcome summaries (most recent last), carried
    # into the hard `edit-loop` reason so a human can see the outcome really
    # did not change.
    _test_summaries: list[str] = field(default_factory=list)
    # tool_use_id -> True, for a Bash/Terminal call recognized as a
    # test-runner invocation whose matching `tool_result` has not arrived
    # yet. Bounded so a long attempt cannot grow it unbounded.
    _pending_test_runs: dict[str, bool] = field(default_factory=dict)

    def record(self, error_text: str) -> bool:
        """Record a failure. Return True if we are now stuck (reset context)."""
        sig = error_signature(error_text)
        self._seen[sig] = self._seen.get(sig, 0) + 1
        self._last = sig
        return self._seen[sig] >= self.threshold

    def record_tool_call(self, tool_name: str, tool_input_summary: str) -> bool:
        """Record a tool call signature. Return True if doom-looping.

        A doom-loop is the same tool+input repeated consecutively — the agent
        is retrying the exact same action expecting different results.
        """
        # The summary's discriminating parameters (offset/limit, content
        # hash) ride at the END, after a path that can alone exceed 100
        # chars in a worktree - a prefix truncation here re-collapses what
        # _summarize_tool_sig just distinguished (the 2026-08-16 false
        # doom-loop aborts). Keep a readable head, always keep the tail.
        head, tail = tool_input_summary[:100], tool_input_summary[-24:]
        sig = f"{tool_name}:{head}…{tail}"
        if self._tool_signatures and self._tool_signatures[-1] == sig:
            self._consecutive_repeats += 1
        else:
            self._consecutive_repeats = 1
        self._tool_signatures.append(sig)
        # Keep bounded
        if len(self._tool_signatures) > 50:
            self._tool_signatures = self._tool_signatures[-50:]
        return self._consecutive_repeats >= self.doom_loop_threshold

    def record_edit(self, file_path: str) -> bool:
        """R2.3 Layer 1: track per-file edit count. Return True if the
        ADVISORY tier fires (raw count, unchanged by progress).

        Also maintains the HARD tier's own progress-gated counter
        (`_hard_edit_counts`, read by `hard_stuck_reason`): it resets to 1
        whenever this edit followed observed progress since the file's
        previous edit — a test-runner invocation whose outcome summary
        changed (`record_test_outcome` set `_progress_since_last_edit`).
        Editing a different file in between is NOT progress by itself (see
        the field comment on `_hard_edit_counts`). Progress is consumed here
        (reset to False) so it counts for exactly the one edit that follows
        it.
        """
        self._edit_counts[file_path] = self._edit_counts.get(file_path, 0) + 1
        progressed = self._progress_since_last_edit
        self._hard_edit_counts[file_path] = (
            1 if progressed else self._hard_edit_counts.get(file_path, 0) + 1
        )
        self._progress_since_last_edit = False
        return self._edit_counts[file_path] >= self.edit_threshold

    def note_test_run(self, tool_use_id: str | None) -> None:
        """Record a Bash/Terminal call recognized as a test-runner
        invocation, pending the matching `tool_result` (`record_test_outcome`).
        A missing/empty id is ignored — unjoinable, so it can never register
        as progress (the fallback is today's behaviour: no progress signal)."""
        if not tool_use_id:
            return
        self._pending_test_runs[tool_use_id] = True
        if len(self._pending_test_runs) > 32:
            oldest = next(iter(self._pending_test_runs))
            del self._pending_test_runs[oldest]

    def record_test_outcome(self, tool_use_id: str | None, summary: str) -> bool:
        """Resolve a pending test run and compare its outcome to the
        previous one. Return True iff this is PROGRESS: `tool_use_id` was
        registered by `note_test_run` (a `tool_result` whose id was never
        seen as a test run — an unpaired backend, or one that emits no
        result at all — is NOT progress; that is the existing fallback,
        preserved on purpose) AND the outcome's STATUS (`_status_only`)
        differs from the last recorded one's status (or none has been
        recorded yet).

        `summary` is expected to be `_test_run_summary`'s
        `"{status}, {chars} chars"` string, not pass/fail counts — the event
        stream never carries test output text (`claude_backend._exit_status`'s
        docstring: only size, and an exit code on failure, by design, so a
        printed credential is never captured), so "5 passed, 2 failed" is not
        obtainable here. The comparison itself, though, looks at `status`
        ONLY (send-back review round 2, MAJOR-2): `result_chars` alone
        drifts on almost every run of a real harness (timestamps, stack
        traces, log noise) with no change in outcome at all — pairing the
        progress signal to the one component that cannot drift for free
        (an `exit {code}`/`ok`/`failed` transition) is what makes this
        "progress", not raw output-size churn. The full summary (with
        chars) is still what gets stored/displayed in `_test_summaries`, so
        a human reading the stuck-event text still sees the byte counts.
        """
        if not tool_use_id or tool_use_id not in self._pending_test_runs:
            return False
        del self._pending_test_runs[tool_use_id]
        changed = (
            not self._test_summaries
            or _status_only(self._test_summaries[-1]) != _status_only(summary)
        )
        self._test_summaries.append(summary)
        if len(self._test_summaries) > 2:
            self._test_summaries = self._test_summaries[-2:]
        if changed:
            self._progress_since_last_edit = True
        return changed

    @property
    def last_test_summaries(self) -> list[str]:
        """Up to the last 2 recorded test-outcome summaries, most recent last."""
        return list(self._test_summaries)

    def detect_ping_pong(self, window: int = 4) -> bool:
        """R2.1: detect an A-B-A-B alternating pattern in the last ``window``
        tool calls (4 = advisory; ``ping_pong_abort_window`` = hard)."""
        sigs = self._tool_signatures
        if len(sigs) < window:
            return False
        tail = sigs[-window:]
        a, b = tail[0], tail[1]
        if a == b:
            return False
        return all(s == (a if i % 2 == 0 else b) for i, s in enumerate(tail))

    @property
    def stuck_reason(self) -> str | None:
        """Return a human-readable reason if any detector fired, else None."""
        if self._consecutive_repeats >= self.doom_loop_threshold:
            return (
                f"doom-loop: identical tool call repeated "
                f"{self.doom_loop_threshold}× consecutively"
            )
        hot_files = [f for f, c in self._edit_counts.items()
                     if c >= self.edit_threshold]
        if hot_files:
            return (
                f"edit-loop: {hot_files[0]} edited {self._edit_counts[hot_files[0]]}× "
                f"— consider a different approach"
            )
        if self.detect_ping_pong():
            return "ping-pong: alternating between two actions (A-B-A-B pattern)"
        return None

    @property
    def hard_stuck_reason(self) -> str | None:
        """A reason iff a HARD threshold is crossed — the abort tier, not the
        advisory one. The orchestrator's sink raises StuckAbort on this."""
        if self._consecutive_repeats >= self.doom_loop_abort:
            return (
                f"doom-loop: identical tool call repeated "
                f"{self._consecutive_repeats}× consecutively"
            )
        # Progress-gated: `_hard_edit_counts`, not the raw `_edit_counts` the
        # advisory tier above reads — see `record_edit`.
        hot = [(f, c) for f, c in self._hard_edit_counts.items()
               if c >= self.edit_abort]
        if hot:
            path, count = max(hot, key=lambda fc: fc[1])
            reason = f"edit-loop: {path} edited {count}× with no observed progress"
            if self._test_summaries:
                reason += " — last test outcomes: " + " → ".join(self._test_summaries)
            return reason
        # ABSOLUTE ceiling (property 2): reads the raw, never-reset
        # `_edit_counts` — no progress signal can lift this one.
        ceiling_hot = [(f, c) for f, c in self._edit_counts.items()
                       if c >= self.edit_ceiling]
        if ceiling_hot:
            path, count = max(ceiling_hot, key=lambda fc: fc[1])
            reason = f"edit-loop: {path} edited {count}× (absolute per-file ceiling)"
            if self._test_summaries:
                reason += " — last test outcomes: " + " → ".join(self._test_summaries)
            return reason
        if self.detect_ping_pong(self.ping_pong_abort_window):
            return (
                f"ping-pong: alternating between two actions for "
                f"{self.ping_pong_abort_window} consecutive calls"
            )
        return None

    def reset(self) -> None:
        self._seen.clear()
        self._last = None
        self._tool_signatures.clear()
        self._consecutive_repeats = 0
        self._edit_counts.clear()
        self._hard_edit_counts.clear()
        self._progress_since_last_edit = False
        self._test_summaries.clear()
        self._pending_test_runs.clear()


@dataclass
class ConvergenceTracker:
    """Turn-cap convergence early-abort (P2).

    `StuckDetector.hard_stuck_reason` above ends a DETERMINISTIC runaway — the
    same tool call, or the same alternating pair, repeated. It deliberately
    does not fire on an attempt that keeps VARYING its tool calls — a new file
    read here, a new grep there, never the same signature twice — while never
    converging on a fix: every call is "new" by the doom-loop signature, so
    that counter never crosses its threshold and the attempt is free to spend
    the whole ``max_turns_per_attempt`` (500) looking around without ever
    writing or verifying anything. This class ends THAT class instead, so the
    raw cap (measured against real ~328-turn successful runs, PLAN.md 4.3)
    never has to be lowered to catch it.

    A "turn" is one assistant message with a usage block — the SAME proxy
    ``Orchestrator._attempt_usage["assistant_messages"]`` already counts (its
    own comment: "enough to tell a spinning attempt (hundreds) from an
    attempt whose context is simply too big (a handful)"). "Progress" is a
    file-modifying tool call (Write/Edit/MultiEdit/NotebookEdit, agent-owned
    scratch writes included) or a test-runner Bash invocation — the two
    signals the live event stream can actually see cheaply. It is
    deliberately NOT "a new test result" in the literal pass/fail sense:
    `claude_backend._exit_status` documents, with a measured corpus, that a
    ``tool_result`` event carries only size and (on a FAILURE only) an exit
    code — never the command's output text, by design, so a printed
    credential is never captured. Treating "a test command ran" as progress
    is the honest signal available at this seam.

    THIS UNDERCOUNTS STALLING, IT DOES NOT ONLY UNDERCOUNT PROGRESS — an
    earlier version of this docstring claimed the substitute "never
    overcounts" a truly stuck attempt, and a round-2 review disproved that
    against a real corpus. Two concrete residual classes evade it entirely:
    (1) a command that merely MENTIONS a runner's name without executing it
    (`rg pytest`, `git log --grep pytest`) — filtered at the Bash-command
    layer by `Orchestrator._looks_like_test_run`'s leading-token check, but
    that filter is a denylist of known read-only tools, not a proof no
    executable can print a runner's name without running it; and (2) an
    attempt that keeps varying its tool calls enough to dodge the doom-loop
    signature while periodically touching a file or re-running tests WITHOUT
    making real progress (e.g. re-reading the same content into slightly
    different windows, or re-running a test suite whose result it discards)
    — the hard stuck tier only catches that shape at 9 identical consecutive
    calls or a 12-call A-B-A-B alternation, both far above what this
    heuristic can distinguish from genuine, varied, converging work. Both are
    accepted trade-offs of a cheap, mid-attempt, no-tool-result-text signal —
    named here so a future reader does not re-derive "never overcounts" as
    fact.

    "Test-runner Bash invocation" above is `orchestrator._looks_like_test_run`
    against the SAME shared `_TEST_RUNNER_RE` the hard edit tier's
    `note_test_run` reads (task 0ab78498 attempt 2: 96 recorded Bash calls, 14
    of them repeated `node /tmp/dcrace/harness.mjs` runs, 22 Reads, 7 Edits,
    killed by this tracker at "no file edit or test run in 40 turns"
    (recorded `failure_reason`: "non-converging-abort: no file edit or test
    run in 40 turns (turn 122, threshold 80, window 40)") because the
    then-current predicate matched `node --test`/`npm run test` but not a
    bare `node <script>` harness). Widening the shared predicate to recognise
    that form makes this tracker slightly easier to keep alive on a coder
    whose test runner is a harness script — the intended correction, not an
    oversight.

    Two knobs, both under ``worker.*`` (not ``bounds.*`` — this is a
    kill-switched heuristic sitting BESIDE the hard caps, not one of them):

    * ``min_turns`` (default 80) — below this, no check at all. Early
      exploration (reading the ticket, the code, the tests) is normal and
      must never be mistaken for stalling. Clamped to at most half of the
      running attempt's real per-attempt turn budget when one is known
      narrower than the default (`from_config`'s ``cap`` argument) — see
      that method's docstring for the corpus evidence this closes.
    * ``window`` (default 40) — turns since the last progress signal that
      constitute stalling, once past ``min_turns``. Half of the DEFAULT
      ``min_turns``: short enough that a genuinely stuck attempt is caught
      tens of turns into a 500-turn budget rather than at the very end, long
      enough that a normal edit/verify/edit cadence never trips it (a coder
      that reads for a while before its first edit, then edits or tests at
      least once every 40 turns thereafter, never fires this).

    DEFAULTS' ANCHOR: the round-2 independent review replayed these defaults
    against ~758k real recorded events (this install's attempt event
    history) and reported that they correctly aborted the 3 attempts that
    replay identified as genuine non-converging burners (activity with no
    committable or test-verifying progress for the rest of their run) and
    never aborted a converging one — the widest progress gap the reviewer
    measured inside any converging attempt was 25 turns, comfortably under
    the 40-turn window. Cited here as the reproducible anchor behind the
    numbers; re-derive by replaying `_agent_sink`'s tool_use/usage events per
    attempt against a fresh `ConvergenceTracker()` and diffing the fire/
    no-fire verdict against that attempt's actual outcome.

    No advisory tier, unlike ``StuckDetector``: past ``min_turns``, ``window``
    consecutive turns with neither signal IS the rule, with nothing softer
    beneath it — the false-positive risk that justified a two-tier design for
    doom-loop (2026-08-16: 19M tokens of correct work killed by an advisory
    that should have stayed advisory) does not apply here, because file edits
    and test runs are commonplace enough in real work that the window rarely
    closes on a converging attempt.
    """

    enabled: bool = True
    min_turns: int = 80
    window: int = 40
    _turns: int = 0
    _last_progress_turn: int = 0

    def tick(self) -> None:
        """Call once per turn (one deduped 'usage' event)."""
        self._turns += 1

    def mark_progress(self) -> None:
        """Call on a file-modifying tool_use or a test-runner Bash tool_use."""
        self._last_progress_turn = self._turns

    @property
    def non_converging_reason(self) -> str | None:
        """A reason iff past ``min_turns`` with no progress for ``window``
        consecutive turns, else None. Always None when ``enabled`` is False —
        the kill switch is checked here so every caller gets it for free."""
        if not self.enabled:
            return None
        if self._turns <= self.min_turns:
            return None
        since = self._turns - self._last_progress_turn
        if since < self.window:
            return None
        return (
            f"no file edit or test run in {since} turns (turn {self._turns}, "
            f"threshold {self.min_turns}, window {self.window})"
        )

    @staticmethod
    def from_config(
        worker_cfg: dict | None, *, cap: int | None = None,
    ) -> "ConvergenceTracker":
        """Config lives under ``worker.*``: ``abort_non_converging`` (the
        on/off switch, default True), ``convergence_check_after_turns``
        (``min_turns``) and ``convergence_window_turns`` (``window``).

        A malformed numeric value (an operator typo, a bad hand-edit of
        ``config.yaml``) falls back to the default rather than raising —
        this is read EAGERLY at the top of every attempt, so a bare ``int()``
        that raised on a typo would kill every attempt on the task with an
        unrelated crash, including with the feature turned OFF.

        ``cap``, when given, is the running attempt's REAL per-attempt turn
        budget (``Bounds.max_turns_per_attempt``, after any per-kind
        override) — ``min_turns`` is clamped to at most half of it. Review
        fix (round 2): ``_REPORT_KINDS`` tasks (investigation/design_doc) run
        with an 80-turn cap, and a live corpus showed their usage-tick count
        running to 86-103 UNDER that nominal 80 (subagent turns inflate the
        tick count past the SDK's own top-level ``max_turns``). The default
        ``min_turns=80`` sat almost exactly at that cap, so a report attempt
        that (correctly) writes its deliverable once near the very end
        tripped the check before ever getting credit for the write. Left
        unset (the normal 500-turn case), this is a no-op: ``min(80, 250)``
        is still 80.
        """
        w = worker_cfg or {}

        def _int(key: str, default: int) -> int:
            try:
                return int(w.get(key, default))
            except (TypeError, ValueError):
                return default

        min_turns = _int("convergence_check_after_turns", 80)
        window = _int("convergence_window_turns", 40)
        if cap is not None and cap > 0:
            min_turns = min(min_turns, cap // 2)
        return ConvergenceTracker(
            enabled=bool(w.get("abort_non_converging", True)),
            min_turns=min_turns,
            window=window,
        )


# The CLI states the reset time in prose: "resets 4:20am (Asia/Jerusalem)" or
# "resets Jul 24 at 6pm (Europe/London)". Matched loosely (optional ":MM",
# case-insensitive am/pm) with the IANA zone required in parentheses — a
# message with no zone cannot be resolved to an absolute time and must fall
# back (see `parse_quota_reset`).
_QUOTA_RESET_RE = re.compile(
    r"resets\s+"
    r"(?:(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})\s+at\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)"
    r"\s*\(\s*(?P<zone>[^)]+?)\s*\)",
    re.IGNORECASE,
)

_QUOTA_RESET_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Clamp bounds for a parsed reset time, named so `QuotaExhausted` and its
# tests share one definition of "too soon" / "too far" rather than
# re-deriving the numbers.
_QUOTA_RESET_MIN_WAIT_S = 300      # 5 minutes
_QUOTA_RESET_MAX_WAIT_S = 6 * 3600  # 6 hours


def parse_quota_reset(message: str, *, now: datetime) -> datetime | None:
    """Extract the wall's own reset time from a quota-exhaustion message.

    Recognises the CLI's two phrasings — ``"resets 4:20am (Asia/Jerusalem)"``
    and ``"resets Jul 24 at 6pm (Europe/London)"`` — resolves the zone with
    `zoneinfo`, and returns the next occurrence of that wall-clock time at or
    after ``now`` as an aware UTC datetime. Returns ``None`` when the message
    doesn't match, the zone is missing or unknown, or the result parses so far
    out that a wrong parse is more likely than a real "days from now" wall
    (see the upper clamp below).

    The result is clamped: below ``now + 5min`` it is raised to ``now + 5min``
    (a reset that is about to pass is still worth a short wait, not an
    immediate re-park); above ``now + 6h`` this returns ``None`` so the caller
    falls back to the fixed retry hour instead of trusting a parse that says
    "days".
    """
    if not message:
        return None
    m = _QUOTA_RESET_RE.search(message)
    if not m:
        return None
    try:
        zone = ZoneInfo(m.group("zone").strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None

    hour = int(m.group("hour"))
    minute = int(m.group("minute") or 0)
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return None
    ampm = m.group("ampm").lower()
    hour %= 12
    if ampm == "pm":
        hour += 12

    local_now = now.astimezone(zone)
    month_name = m.group("month")
    if month_name:
        month = _QUOTA_RESET_MONTHS.get(month_name[:3].lower())
        if month is None:
            return None
        try:
            candidate = local_now.replace(
                month=month, day=int(m.group("day")), hour=hour, minute=minute,
                second=0, microsecond=0)
        except ValueError:
            return None
        # No year in the message; a date+time more than a day in the past is
        # next year's occurrence, not today's.
        if candidate < local_now - timedelta(days=1):
            candidate = candidate.replace(year=candidate.year + 1)
    else:
        candidate = local_now.replace(hour=hour, minute=minute, second=0,
                                       microsecond=0)
        if candidate < local_now:
            candidate += timedelta(days=1)

    result = candidate.astimezone(timezone.utc)
    lower_bound = now + timedelta(seconds=_QUOTA_RESET_MIN_WAIT_S)
    if result < lower_bound:
        return lower_bound
    if result > now + timedelta(seconds=_QUOTA_RESET_MAX_WAIT_S):
        return None
    return result


# The CLI's own rejections are phrased "You've hit your <period> limit", and
# the period varies: two observed live are "monthly spend limit" and "weekly
# limit". Neither matched any literal below, so a hard billing wall was
# classified as a generic error and burned all 3 attempts against it instead of
# parking with a wake condition. Matching the SHAPE covers the periods we have
# not seen.
#
# The class includes both apostrophes so possessive periods match too —
# "hit your team's weekly limit" is the enterprise phrasing, and constraint #1
# makes enterprise profiles first-class. (A previous comment here claimed the
# class covered "the CLI's typographic apostrophe"; it did not — the
# apostrophe in "You've" sits BEFORE the match window, so that case passed by
# accident, not by design.)
# Bounded by WORDS, not characters. The CLI's period is always one or two
# words ("weekly", "monthly spend", "team's weekly"), while the English
# false positive a character bound admits — "hit your head on the limit
# switch" — needs three. Reachability is already low (only an ERRORED
# result's text reaches here), but a classifier deciding between parking
# and burning three attempts should not depend on its caller to be right.
_QUOTA_RE = re.compile(r"hit your (?:[\w'\u2019-]+ ){0,2}limit")

# EVERY term must contain a space or be a full API error type. `final_text`
# carries a TRACEBACK, so a bare substring matches FILE PATHS: the old literal
# "quota" fired on any traceback through a directory or module whose name
# contains it — and this codebase is full of quota-handling code, so
# `quota_park.py` in a stack trace was enough to park a healthy task on a
# billing wall it never hit. Paths do not contain spaces.
_QUOTA_TERMS = (
    "usage limit", "spend limit", "rate limit exceeded",
    "quota exceeded", "quota reached", "out of quota", "insufficient quota",
    "your quota", "rate_limit_error",
)


def quota_reason(text: str) -> str:
    """The CLI's own one-line explanation, for the park detail.

    Trimmed to the first non-empty line: `final_text` also carries a traceback,
    and the park detail is a human-facing summary, not a log.
    """
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:200]
    return "subscription quota exhausted"


def quota_signal(text: str) -> bool:
    """Is this failure a billing wall rather than a broken task?

    Decides between parking with a wake condition and burning all 3 attempts,
    so it must be wrong in neither direction. Only ever applied to text from an
    ERRORED result — see the is_error gate in claude_backend — which is what
    makes it safe to match more phrasings: a coder's own summary saying "added
    rate limit handling" no longer reaches here at all."""
    t = text.lower()
    return bool(_QUOTA_RE.search(t)) or any(s in t for s in _QUOTA_TERMS)


# The wall shapes `quota_signal` deliberately cannot see: it is scoped to the
# CLI's OWN billing prose, and a prose-less transport/outage failure (a raw
# HTTP 429/5xx, or the SDK's own `overloaded_error`/`api_error` subtype) is a
# different wall — infrastructure, not a subscription limit — that still must
# park the task rather than escalate a defect nobody found. Kept beside
# `quota_signal`/`_QUOTA_TERMS`, never merged into them, for the same reason
# `_infra_sdk_failure` (orchestrator.py) is kept separate: blurring what each
# classifier is proven against makes both harder to trust.
#
# Every term carries a space or is a full API error TYPE, same discipline as
# `_QUOTA_TERMS` above — `final_text`/an exception's `str()` carries a
# traceback, and a bare substring matches file paths.
_API_WALL_TERMS = (
    "overloaded_error", "api_error", "internal server error",
    "service unavailable", "bad gateway", "gateway timeout",
)
_API_WALL_STATUS_RE = re.compile(r"\bhttp\s*(429|5\d\d)\b", re.I)


def api_wall_reason(text: str) -> str | None:
    """The reason string iff `text` names an API wall `quota_signal` cannot
    see (a raw 429/5xx or an SDK overload/api_error subtype), else None.

    Same shape as `quota_reason`: the first non-empty line, trimmed to 200
    chars — `text` may carry a traceback, and the park detail is a
    human-facing summary, not a log.
    """
    t = (text or "").lower()
    if not (_API_WALL_STATUS_RE.search(t) or any(s in t for s in _API_WALL_TERMS)):
        return None
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:200]
    return "API unavailable"


class QuotaExhausted(Exception):
    """Raised when a subscription usage limit is hit mid-task.

    The orchestrator catches this and parks the task in ``paused_quota`` rather
    than failing it; the watcher resumes when quota refreshes. Carries an
    optional ISO timestamp for when quota is expected back.
    """

    # How long to wait before a parked task tries again, when the caller does
    # not know the real reset time (`parse_quota_reset` returned None) — no
    # zone, no match, or a parse outside the clamp window below.
    RETRY_AFTER_S = 3600

    def __init__(self, message: str = "subscription quota exhausted",
                 resets_at: str | None = None, *, infra: bool = False):
        super().__init__(message)
        # What KIND of wall this is. ``infra=True`` is the prose-less SDK /
        # transport death (`_infra_sdk_failure`): the TASK is parked the same
        # way — it did no work and must not be charged an attempt — but the
        # POOL must not be: a single dead session is not a billing wall, and
        # the fleet response to dead sessions is the 3-strike infra breaker,
        # not an hour-long pause. INCIDENT (2026-08-22, task c8d1a30d): one
        # SDK stream died on a >1 MB JSON line ("infrastructure, not work")
        # and the scheduler armed a 60-minute 'quota' pause on the whole pool
        # with 12 tasks queued and a free worker.
        self.infra = infra
        # DEFAULTED HERE, not at the call site, so no raise site can produce a
        # park that never wakes. With `resets_at=None`, TWO mechanisms silently
        # did nothing: the wake watcher resumes a PAUSED_QUOTA task only when
        # `wake_check_at` is set AND due, and the scheduler arms its pool-wide
        # cooldown only when that value parses. So nothing auto-resumed AND the
        # pool fed the next queued task into the same wall, parking it too —
        # one at a time. That is the observed incident: 4 tasks, 12 attempts,
        # one billing wall.
        #
        # The CLI phrases the wall's own reset time as "resets 2pm (<zone>)"
        # or "resets Jul 24 at 6pm (...)" — `parse_quota_reset` extracts it
        # when `resets_at` isn't already given. A FIXED HOUR is the FALLBACK,
        # not the default: getting a parse wrong is bad in both directions —
        # too far ahead stalls the whole pool for days, too near thrashes —
        # so the parse is clamped to [now+5min, now+6h] (`parse_quota_reset`)
        # and anything outside that window (unparseable message, missing/
        # unknown zone, or a result past the 6h ceiling) falls back to this
        # fixed hour, which is self-correcting: if the wall is still up the
        # task re-parks, which is cheap because the CLI rejects it before any
        # model call.
        now = datetime.now(timezone.utc)
        if resets_at is None:
            parsed = parse_quota_reset(message, now=now)
            resets_at = parsed.isoformat() if parsed is not None else None
        self.resets_at = resets_at or (
            now + timedelta(seconds=self.RETRY_AFTER_S)).isoformat()
