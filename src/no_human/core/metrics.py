"""The measurement spine (M4): the north-star numbers, straight from the DB.

North star: quality merged PRs at lower cost. Everything here is a read-only
SQL aggregate over ``attempts`` / ``tasks`` / ``task_events`` — no derived
state, no caching, so the numbers cannot drift from the record. Served by
``/api/metrics``; the M3 cost work is judged against ``tokens_per_pr`` and
``coder_cache_read_per_attempt``.
"""

from __future__ import annotations

import math
from typing import Any

from .cost import attempts_cost
from .db import USAGE_ROLES, Store, usage_columns_for


def cache_read_share(read: int | None, creation: int | None) -> float | None:
    """Share of an attempt's cache tokens that were READ (reuse) rather than
    re-CREATED. None when the attempt recorded no cache tokens at all — an
    unmeasured attempt is not a 0.0 one. Rounded to 4dp, matching
    cache_economics['creation_share']; note this is its COMPLEMENT over the
    same universe (read+creation), so the two can never be read as different
    denominators. The ONLY definition of this arithmetic — api/models.py and
    eval/northstar_card.py both import it rather than re-deriving it, and no
    JS twin exists (the frontend only ever displays a value this function, or
    the API row it populated, already computed)."""
    try:
        r = int(read) if read is not None else 0
        c = int(creation) if creation is not None else 0
    except (TypeError, ValueError):
        return None
    total = r + c
    if total <= 0:
        return None
    return round(r / total, 4)


def _percentile(vals: list[float], q: float) -> float:
    """Nearest-rank percentile. Not statistics.quantiles, which raises for
    n < 2 — a single-attempt session must still produce a p50/p90 (both equal
    to that one observation), not a crash."""
    idx = min(len(vals) - 1, math.ceil(q * len(vals)) - 1)
    return vals[idx]


async def compute_metrics(store: Store) -> dict[str, Any]:
    # `store.query`/`query_one`, never `store.db`. Aliasing the raw connection
    # (`db = store.db`) is how this module opened twelve cursors OUTSIDE the
    # connection's critical section while the pool wrote through it — see
    # `Store._fetchone` and `tests/test_db_concurrency.py`. /api/metrics runs
    # this on the board's live store, so the cursors were concurrent with the
    # pool's writes by construction.

    async def one(sql: str, *args) -> Any:
        row = await store.query_one(sql, args)
        return row[0] if row else None

    prs_opened = await one(
        "SELECT COUNT(DISTINCT pr_url) FROM attempts WHERE pr_url IS NOT NULL")
    prs_merged = await one(
        "SELECT COUNT(*) FROM task_events "
        "WHERE json_extract(data, '$.kind') = 'merged'")
    attempts_total = await one("SELECT COUNT(*) FROM attempts")

    # Per auth profile: attempts and token burn. The profile is stamped on the
    # attempt row from what the process actually exported, not from config.
    rows = await store.query(
        """SELECT COALESCE(auth_profile, 'unknown') AS profile,
                  COUNT(*) AS attempts,
                  COALESCE(SUM(COALESCE(tokens_used, 0)), 0) AS tokens,
                  COALESCE(SUM(COALESCE(cache_read_tokens, 0)), 0) AS cache_read
           FROM attempts GROUP BY profile ORDER BY attempts DESC""")
    by_profile = [
        {"profile": r[0], "attempts": r[1], "tokens": r[2], "cache_read": r[3]}
        for r in rows
    ]

    # Per complexity tier (C1.5): cost AND quality, so a cheaper setting
    # is kept only where quality holds — measured, never assumed.
    rows = await store.query(
        """SELECT COALESCE(json_extract(t.context, '$.complexity_tier'),
                           'unclassified') AS tier,
                  COUNT(*) AS attempts,
                  COALESCE(SUM(COALESCE(a.tokens_used, 0)), 0) AS tokens,
                  COALESCE(SUM(COALESCE(a.cache_read_tokens, 0)), 0) AS cache_read,
                  SUM(CASE WHEN a.status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded
           FROM attempts a JOIN tasks t ON t.id = a.task_id
           GROUP BY tier ORDER BY attempts DESC""")
    by_tier = [
        {"tier": r[0], "attempts": r[1], "tokens": r[2], "cache_read": r[3],
         "succeeded": r[4] or 0}
        for r in rows
    ]

    # Gate outcomes: review verdicts and what blocked. The rejection reasons
    # are the review-fail event texts — the operator's "why is it failing"
    # list, most recent first.
    review_pass = await one(
        "SELECT COUNT(*) FROM task_events WHERE "
        "json_extract(data, '$.kind') = 'review' "
        "AND json_extract(data, '$.passed') = 1")
    review_fail = await one(
        "SELECT COUNT(*) FROM task_events WHERE "
        "json_extract(data, '$.kind') = 'review' "
        "AND json_extract(data, '$.passed') = 0")
    rows = await store.query(
        """SELECT substr(COALESCE(json_extract(data, '$.text'), ''), 1, 200)
           FROM task_events
           WHERE json_extract(data, '$.kind') = 'attempt_failed'
           ORDER BY ts DESC LIMIT 10""")
    rejection_reasons = [r[0] for r in rows if r[0]]

    # Cache economics (P0.3): cache_creation is full-price input; cache_read
    # is ~10% price. A rising creation share means the prompt prefix is being
    # rebuilt instead of reused — the failure mode all context work must
    # avoid (93% of lifetime burn is coder cache-reads).
    rows = await store.query(
        """SELECT COUNT(*),
                  COALESCE(SUM(COALESCE(cache_creation_tokens, 0)), 0),
                  COALESCE(SUM(COALESCE(cache_read_tokens, 0)), 0)
           FROM attempts WHERE COALESCE(cache_read_tokens, 0) > 0
              OR COALESCE(cache_creation_tokens, 0) > 0""")
    n_attempts, sum_creation, sum_read = rows[0]
    cache_economics = {
        "attempts_measured": n_attempts,
        "cache_creation_total": sum_creation,
        "cache_read_total": sum_read,
        "creation_per_attempt": sum_creation // n_attempts if n_attempts else 0,
        "read_per_attempt": sum_read // n_attempts if n_attempts else 0,
        "creation_share": round(sum_creation / (sum_creation + sum_read), 4)
        if (sum_creation + sum_read) else None,
    }

    # Per-attempt cache-read-share distribution — a SIBLING of cache_economics
    # above, not a member of it: cache_economics pools every attempt's tokens
    # into one FLEET ratio, while this is a distribution OVER per-attempt
    # ratios (the earliest signal a single attempt is heading for budget
    # exhaustion, not a total). Keeping the two dicts structurally separate
    # means a reader can never mistake a pooled total for a per-attempt
    # spread. Same population/filter as cache_economics (no time window) so
    # the two stay comparable.
    rows = await store.query(
        """SELECT COALESCE(cache_read_tokens, 0), COALESCE(cache_creation_tokens, 0)
           FROM attempts
           WHERE COALESCE(cache_read_tokens, 0) > 0
              OR COALESCE(cache_creation_tokens, 0) > 0""")
    _shares = sorted(
        s for s in (cache_read_share(r, c) for r, c in rows) if s is not None)
    cache_read_share_dist = {
        "attempts_measured": len(_shares),
        "p50": _percentile(_shares, 0.5) if _shares else None,
        "p90": _percentile(_shares, 0.9) if _shares else None,
    }

    # CI_GATE integration gate (M6): runs started / passed / failed.
    rows = await store.query(
        """SELECT json_extract(data, '$.kind'), COUNT(*)
           FROM task_events
           WHERE json_extract(data, '$.kind')
                 IN ('ci_gate_trigger', 'ci_gate_pass', 'ci_gate_fail')
           GROUP BY 1""")
    ci_gate_raw = {r[0]: r[1] for r in rows}
    ci_gate = {
        "triggered": ci_gate_raw.get("ci_gate_trigger", 0),
        "passed": ci_gate_raw.get("ci_gate_pass", 0),
        "failed": ci_gate_raw.get("ci_gate_fail", 0),
    }

    # Repro-gate verdict split (advisory data — decides when "required" ships).
    rows = await store.query(
        """SELECT COALESCE(json_extract(data, '$.verdict'), '?'), COUNT(*)
           FROM task_events WHERE json_extract(data, '$.kind') = 'repro_gate'
           GROUP BY 1""")
    repro = {r[0]: r[1] for r in rows}

    # Error-class breakdown (0.2/0.3): how terminal agent errors split, so the
    # wasted-attempt causes are visible — a refusal (fail-fast, needs a human)
    # vs a retryable rate-limit/infra vs a genuine error. Populated by
    # _classify_error; agent_error events from before it group as 'unclassified'.
    rows = await store.query(
        """SELECT COALESCE(json_extract(data, '$.error_class'), 'unclassified'),
                  COUNT(*)
           FROM task_events WHERE json_extract(data, '$.kind') = 'agent_error'
           GROUP BY 1""")
    error_breakdown = {r[0]: r[1] for r in rows}

    # Intake-grill answering pass: how often it actually answers. The pass is
    # advisory by contract (every branch is wrapped in `except`), so before this
    # split existed a 0% answer rate and a 100% answer rate produced identical,
    # fully-green signals. Keys are evaluator.GRILL_ANSWERING_OUTCOMES; the
    # health number is parsed_* over the total.
    rows = await store.query(
        """SELECT COALESCE(json_extract(data, '$.outcome'), 'unclassified'),
                  COUNT(*)
           FROM task_events
           WHERE json_extract(data, '$.kind') = ?
           GROUP BY 1""", ("grill_answering",))
    grill_answering = {r[0]: r[1] for r in rows}

    # The split above is a PARSE rate, not an ANSWER rate — and the live
    # symptom the instrumentation exists for is a pass that PARSES and applies
    # ZERO answers ("all N answerable question(s) left unanswered",
    # 2026-08-06). That records as `parsed_first_try` and, until this key
    # existed, was indistinguishable at /api/metrics from a healthy pass:
    # `answers_applied` was written on every row and read by nothing.
    #
    # So: of the passes that PARSED, how many answers did they actually apply.
    # Restricted to the parsed outcomes because a pass that never parsed has
    # no answers to apply — counting its zero would blame the wrong stage.
    # The subset is a DECLARED constant next to the enum. It used to be
    # computed here as `o.startswith("parsed")`, described as "derived from the
    # enum, so an outcome added later cannot silently fall out": that was
    # overstated — a prefix is a NAMING CONVENTION, not a property of the enum,
    # and a future parsing outcome spelled any other way would have dropped out
    # exactly as silently. The declaration cannot force a new member into
    # itself either, so both of its failure modes are pinned by
    # test_the_parsed_subset_is_declared_and_agrees_with_the_enum instead.
    from ..intake.evaluator import GRILL_ANSWERING_PARSED_OUTCOMES as parsed
    # No empty-subset guard here, and the `or [""]` that used to stand in for
    # one is gone: SQLite ACCEPTS `IN ()` — an SQLite extension most other
    # engines reject — and matches nothing, so an emptied subset reports zeros
    # rather than raising. Checked on sqlite 3.40.1 and 3.51.0 and through
    # compute_metrics itself, which returns the same all-zeros payload with the
    # guard present and absent. The comment that used to sit here called it a
    # syntax error and a 500; it is neither, and the guard it justified was
    # inert.
    # `answers_applied IS NULL` is a row written before the field existed. It
    # is EXCLUDED from the zero-applied count rather than COALESCEd to 0: an
    # unrecorded number is not a zero, and folding the two would have this key
    # invent exactly the failure it was added to detect.
    row = await store.query_one(
        f"""SELECT COUNT(*),
                   COALESCE(SUM(json_extract(data, '$.answers_applied')
                                IS NOT NULL), 0),
                   COALESCE(SUM(COALESCE(
                       json_extract(data, '$.answers_applied'), 0)), 0),
                   COALESCE(SUM(COALESCE(
                       json_extract(data, '$.answerable'), 0)), 0),
                   COALESCE(SUM(CASE
                       WHEN json_extract(data, '$.answers_applied') = 0
                       THEN 1 ELSE 0 END), 0)
            FROM task_events
            WHERE json_extract(data, '$.kind') = ?
              AND json_extract(data, '$.outcome')
                  IN ({','.join('?' * len(parsed))})""",
        ("grill_answering", *parsed))
    n_parsed, measured, applied, answerable, zero_applied = (
        row or (0, 0, 0, 0, 0))
    grill_answers = {
        "parsed_passes": n_parsed,
        "measured_passes": measured,
        "answers_applied": applied,
        "answerable": answerable,
        # The number this key exists for: passes that parsed and answered
        # NOTHING. Nonzero here with a healthy-looking outcome split is the
        # failure that used to be invisible.
        "parsed_but_zero_applied": zero_applied,
        "answer_rate": round(applied / answerable, 4) if answerable else None,
    }

    # The QUESTIONS pass, same shape. It ran uninstrumented until 2026-08-07:
    # a malformed block returned None, `grill_spec` returned None, and the
    # orchestrator's `if not qa: return` fired before its own advisory — the
    # whole grill vanished with ZERO events of any kind.
    rows = await store.query(
        """SELECT COALESCE(json_extract(data, '$.outcome'), 'unclassified'),
                  COUNT(*)
           FROM task_events
           WHERE json_extract(data, '$.kind') = ?
           GROUP BY 1""", ("grill_questions",))
    grill_questions = {r[0]: r[1] for r in rows}

    total_cache_read = sum(p["cache_read"] for p in by_profile)
    total_tokens = sum(p["tokens"] for p in by_profile)

    # The reviewer's burn, kept apart from the coder's so per-tier/per-profile attribution stays
    # honest — but surfaced, so the UI can finally price the whole run instead of the coder half.
    rows = await store.query(
        """SELECT COALESCE(SUM(COALESCE(review_tokens_used, 0)), 0),
                  COALESCE(SUM(COALESCE(review_cache_creation_tokens, 0)), 0),
                  COALESCE(SUM(COALESCE(review_cache_read_tokens, 0)), 0)
           FROM attempts""")
    rev_used, rev_creation, rev_read = rows[0]
    # B2 #5/#6 (review #2): the roles that are neither the coder nor the
    # reviewer ran on separate backends and have their own columns. Surface
    # them here too, or /api/metrics under-counts by whole roles while the
    # bench counts them — the surfaces-disagree class this cost work exists to
    # kill. A5: derived from `USAGE_ROLES` rather than naming plan_/utility_,
    # because the last two roles to be added (supervisor, distill) would
    # otherwise have been invisible on this endpoint on the day they landed.
    aux_tiers = [t for t in USAGE_ROLES if t not in ("", "review_")]
    aux_cols = [usage_columns_for(t) for t in aux_tiers]
    rows = await store.query(
        "SELECT " + ", ".join(
            "COALESCE(SUM({}), 0)".format(
                " + ".join(f"COALESCE({c[i]}, 0)" for c in aux_cols))
            for i in (0, 1, 2))
        + " FROM attempts")
    aux_used, aux_read, aux_creation = rows[0]
    # Per-ROLE cost, whole-install. The three aggregate keys above collapse
    # four roles into one "aux" number, which is enough to price a run and
    # useless for deciding which role to optimise — the question this
    # endpoint is read to answer. One row per registered role, always
    # present, zeros included, so a consumer can render a stable breakdown
    # and can see that a role cost nothing rather than guessing whether it
    # was measured at all.
    role_cols = {role: usage_columns_for(tier)
                 for tier, role in USAGE_ROLES.items()}
    rows = await store.query(
        "SELECT " + ", ".join(
            f"COALESCE(SUM(COALESCE({c}, 0)), 0)"
            for cols in role_cols.values() for c in cols)
        + " FROM attempts")
    flat = list(rows[0]) if rows else [0] * (3 * len(role_cols))
    by_role = {}
    for idx, role in enumerate(role_cols):
        used, read, creation = (int(v or 0) for v in flat[idx * 3:idx * 3 + 3])
        by_role[role] = {
            "tokens_used": used, "cache_read": read,
            "cache_creation": creation, "total": used + read + creation,
        }
    # Whole-install USD burn, priced by core.cost.attempts_cost — the SAME
    # per-model function AttemptOut/TaskOut/TaskSummaryOut use, so this
    # lifetime figure can never disagree with a per-task one about how a
    # dollar is priced. One row per attempt, only the columns attempt_cost
    # actually reads (not `SELECT *`: full_final_text etc. would be dragged
    # along for nothing on an install with thousands of attempts).
    cost_cols = ["models"]
    for prefix in USAGE_ROLES:
        tokens_col, read_col, creation_col = usage_columns_for(prefix)
        output_col = "output_tokens" if prefix == "" else f"{prefix}output_tokens"
        cost_cols += [tokens_col, read_col, creation_col, output_col]
    rows = await store.query(f"SELECT {', '.join(cost_cols)} FROM attempts")
    cost_usd_total, cost_model_total = attempts_cost(
        [dict(zip(cost_cols, r)) for r in rows])

    return {
        "prs_opened": prs_opened or 0,
        "prs_merged": prs_merged or 0,
        "attempts_total": attempts_total or 0,
        "attempts_per_pr": round(attempts_total / prs_opened, 1) if prs_opened else None,
        "tokens_per_pr": (total_tokens + total_cache_read) // prs_opened if prs_opened else None,
        # The raw in+out total. `tokens_per_pr` folds it together with cache-read AND
        # divides by prs_OPENED, so it cannot be priced honestly on its own: cache-read is
        # a tenth of the price, cache-CREATION is not in it at all, and a "per merged PR"
        # figure needs prs_merged. Emitting the buckets lets one cost function serve the
        # per-PR tile, the lifetime tile and the task table, so they cannot disagree.
        "tokens_used_total": total_tokens,
        "review_tokens_used_total": rev_used or 0,
        "review_cache_creation_total": rev_creation or 0,
        "review_cache_read_total": rev_read or 0,
        "aux_tokens_used_total": aux_used or 0,
        "aux_cache_read_total": aux_read or 0,
        "aux_cache_creation_total": aux_creation or 0,
        # web/src/cost.js's lifetimeCost used to compute this itself at one
        # flat Anthropic rate; the board now only formats what this key sends.
        # None (not 0.0) when the install has no attempts yet — same "no
        # attempts" vs "attempts spent $0" distinction as TaskOut.cost_usd.
        "cost_usd_total": cost_usd_total,
        "cost_model_total": cost_model_total,
        # The token-basis sibling of cost_usd_total: the SAME nine buckets
        # (coder/reviewer/aux x used+cache_creation+cache_read) attempts_cost
        # prices, summed instead of priced. Subscription-mode surfaces read
        # this instead of a dollar estimate (a flat-fee plan pays nothing per
        # token, so a $ figure there is a rate estimate, not real spend). An
        # int, always — 0 (not None) for an install with no attempts, unlike
        # cost_usd_total: a token COUNT of zero is honest, a $0 estimate is not.
        "tokens_total": (
            total_tokens + total_cache_read + sum_creation
            + rev_used + rev_creation + rev_read
            + aux_used + aux_creation + aux_read
        ),
        # Per-role burn across the whole install. `by_tier` beside it answers
        # a different question (which MODEL ran, from `attempts.models`); this
        # one answers which ROLE spent, which is what a cost target is set
        # against.
        "by_role": by_role,
        "by_auth_profile": by_profile,
        "by_tier": by_tier,
        "review_pass": review_pass or 0,
        "review_fail": review_fail or 0,
        "recent_rejection_reasons": rejection_reasons,
        "repro_gate_verdicts": repro,
        "ci_gate": ci_gate,
        "cache_economics": cache_economics,
        "cache_read_share_dist": cache_read_share_dist,
        "error_breakdown": error_breakdown,
        "grill_answering_outcomes": grill_answering,
        "grill_answering_answers": grill_answers,
        "grill_questions_outcomes": grill_questions,
    }


async def verification_receipt_rate(store: Store) -> dict[str, Any]:
    """Per-attempt rate: did the coder submit at least one command the
    receipt observer RECOGNISES as a verification check
    (`agent.verification_receipts.classify`) before the attempt reached a
    terminal, coder-concluded status?

    POPULATION is `status IN ('succeeded', 'failed')` — an attempt still
    `in_progress` has not claimed anything yet, and one left `interrupted`
    was cut off by the harness, not concluded by the coder — AND
    `Store._lifetime_included_sql()`, the SAME predicate the lifetime budget
    gates on, so an infra-classified retry or a dead zero-work interrupted
    row (never the coder's own submission) cannot drag the rate down.

    A receipt existing for an attempt is sufficient: `add_verification_receipt`
    is only ever called from the PostToolUse hook WHILE the attempt runs
    (`VerificationReceiptHook.hook`), so any stored row necessarily precedes
    the row's own terminal `status` update — there is no later code path that
    backfills a receipt after the fact. `by_kind` breaks the same population
    down by which check kind ran, so "ran SOMETHING" and "ran the unit test
    suite specifically" can be told apart.
    """
    included = Store._lifetime_included_sql()
    row = await store.query_one(
        f"""
        SELECT COUNT(*),
               SUM(CASE WHEN EXISTS (
                   SELECT 1 FROM verification_receipts vr
                   WHERE vr.attempt_id = a.id
               ) THEN 1 ELSE 0 END)
        FROM attempts a
        WHERE a.status IN ('succeeded', 'failed') AND {included}
        """
    )
    total, ran = (row or (0, 0))
    total = int(total or 0)
    ran = int(ran or 0)
    rows = await store.query(
        f"""
        SELECT vr.kind, COUNT(DISTINCT vr.attempt_id)
        FROM verification_receipts vr
        JOIN attempts a ON a.id = vr.attempt_id
        WHERE a.status IN ('succeeded', 'failed') AND {included}
        GROUP BY vr.kind
        """
    )
    by_kind = {r[0]: r[1] for r in rows}
    return {
        "attempts": total,
        "ran_checks": ran,
        "rate": round(ran / total, 4) if total else None,
        "by_kind": by_kind,
    }


WINDOW_HOURS_DEFAULT = 24


async def window_spend(store: Store, *, hours: float = 24.0, now: str | None = None) -> dict[str, Any]:
    """Spend that OCCURRED in the trailing window — attempt-attributed, not
    task-attributed.

    The board's "last 24h" banner used to filter TASKS by `updated_at` and sum
    each survivor's LIFETIME `cost_usd`. Closing or cancelling an old task
    bumps `updated_at` with no new spend, so its entire historical cost swept
    into "last 24h" (measured ~3.5x inflation: a stale $18.68 task closed
    overnight alone accounted for most of the gap). `tasks` is not queried at
    all here — that is the whole fix. An attempt counts when ITS OWN activity
    falls in the window: it started in the window, it ended in the window (a
    long attempt that began earlier), or it is still open (`in_progress` with
    no `completed_at` yet).

    Timestamps are mixed-format: `started_at` is SQLite's `datetime('now')`
    default ("YYYY-MM-DD HH:MM:SS"), `completed_at` is Python `db._now()`
    (ISO-T, e.g. "...+00:00"). `julianday()` parses both alike; a string `>=`
    would not (see `core/health.py:_median_attempt_seconds`, same columns).
    `julianday(NULL)` is NULL, so a NULL side of an OR is simply false.
    """
    if now is not None:
        cutoff_expr = "julianday(?) - ?"
        cutoff_args: tuple[Any, ...] = (now, hours / 24.0)
    else:
        cutoff_expr = "julianday('now') - ?"
        cutoff_args = (hours / 24.0,)

    # `since` (ISO, response-only) — derived from the SAME cutoff expression
    # so a caller never sees a figure that disagrees with the filter below.
    since_row = await store.query_one(f"SELECT datetime({cutoff_expr})", cutoff_args)
    since = since_row[0] if since_row else None

    where = (
        f"WHERE julianday(started_at) >= ({cutoff_expr}) "
        f"OR julianday(completed_at) >= ({cutoff_expr}) "
        f"OR (completed_at IS NULL AND status = 'in_progress')"
    )
    cost_cols = ["models"]
    for prefix in USAGE_ROLES:
        tokens_col, read_col, creation_col = usage_columns_for(prefix)
        output_col = "output_tokens" if prefix == "" else f"{prefix}output_tokens"
        cost_cols += [tokens_col, read_col, creation_col, output_col]
    rows = await store.query(
        f"SELECT {', '.join(cost_cols)} FROM attempts {where}",
        cutoff_args + cutoff_args)
    attempt_dicts = [dict(zip(cost_cols, r)) for r in rows]
    cost_usd, cost_model = attempts_cost(attempt_dicts)

    tokens = 0
    for prefix in USAGE_ROLES:
        tokens_col, read_col, creation_col = usage_columns_for(prefix)
        for d in attempt_dicts:
            tokens += int(d.get(tokens_col) or 0)
            tokens += int(d.get(read_col) or 0)
            tokens += int(d.get(creation_col) or 0)

    return {
        "hours": hours,
        "since": since,
        "cost_usd": cost_usd,
        "cost_model": cost_model,
        "tokens": tokens,
        "attempts": len(attempt_dicts),
    }


async def playbook_outcomes(store) -> list[dict]:
    """D2 #5 (agent-a June-2026): which playbooks actually PAY?

    Joins the playbook_accessed event to each task's outcome and burn. A
    playbook that correlates with escalations and high spend is a liability,
    not an asset — the mined-playbook set can finally be pruned on evidence
    instead of vibes. Pure SQL over what is already recorded.

    An operator cancel is stored as `failed` plus a `cancel_reason` in context
    (`nh task cancel`, and the board's cancel button). It is a WITHDRAWAL, not
    a verdict on the playbook, so it is counted as `cancelled` rather than
    `escalated_or_failed` — the same `status == FAILED and cancel_reason` test
    every read site applies. Without this, a playbook is charged for every task
    a human chose to stop: on the author's own store that was 6 of one
    playbook's 31 recorded "failures".

    THE RULE LIVES IN TWO PLACES, AND IT HAS TO. Python callers can share one
    helper; the predicate below is inside a SQL string, so no Python helper can
    reach it. A refactor that centralises the Python sites therefore leaves the
    SQL ones spelling the pair out by hand — and looks, from the Python side,
    like the rule is now defined once. It is not. When the Python definition
    moves, grep for `cancel_reason` and confirm every SQL predicate still
    agrees with it; this file and `core/db.py` are the ones that cannot follow
    a rename. Three separate defects in this codebase have been one consumer
    applying a judgement its sibling did not.
    """
    rows = await store.query(
        """
        WITH used AS (
          SELECT DISTINCT
                 e.task_id AS task_id,
                 TRIM(REPLACE(json_extract(e.data, '$.text'),
                              'applying playbook: ', '')) AS playbook
          FROM task_events e
          WHERE json_extract(e.data, '$.kind') = 'playbook_accessed'
        )
        SELECT u.playbook                                       AS playbook,
               COUNT(DISTINCT t.id)                             AS tasks,
               SUM(CASE WHEN t.status IN ('awaiting_approval','done')
                        THEN 1 ELSE 0 END)                      AS reached_gate,
               SUM(CASE WHEN t.status IN ('escalated','failed')
                         AND json_extract(t.context, '$.cancel_reason') IS NULL
                        THEN 1 ELSE 0 END)                      AS escalated_or_failed,
               SUM(CASE WHEN t.status = 'failed'
                         AND json_extract(t.context, '$.cancel_reason') IS NOT NULL
                        THEN 1 ELSE 0 END)                      AS cancelled,
               COALESCE(SUM(a.tokens_used + a.cache_read_tokens), 0) AS tokens,
               COUNT(a.id)                                      AS attempts
        FROM used u
        JOIN tasks t     ON t.id = u.task_id
        LEFT JOIN attempts a ON a.task_id = t.id
        GROUP BY u.playbook
        ORDER BY tasks DESC
        """
    )
    rows = [dict(r) for r in rows]
    for r in rows:
        tasks = r["tasks"] or 0
        r["gate_rate"] = round((r["reached_gate"] or 0) / tasks, 3) if tasks else 0.0
        r["tokens_per_task"] = int((r["tokens"] or 0) / tasks) if tasks else 0
    return rows
