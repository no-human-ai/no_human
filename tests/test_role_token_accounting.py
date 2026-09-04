"""Every token a task burns is attributed to a NAMED role, and the roles
reconcile to the total.

Two roles used to bill into ``attempts.utility_*`` alongside five intake
sites: the supervisor (``agent/supervisor.py``, once per ``check_every`` tool
calls for the whole length of a session) and the context distiller
(``_distill_large_chunks``, one session per oversized chunk). ``utility_`` was
therefore a residual — "everything that is not coder/reviewer/planner" — not a
role, and no cost surface could answer the only question a cost target makes
anyone ask: WHICH role to optimise. The grand total was not wrong; the
attribution was, and an optimiser reads the attribution.

Every test here ends at a PERSISTED ROW read back out of SQLite (or at the
real function that renders a surface), with expected numbers fixed by the fake
backend and never recomputed through the code under test. The mutation checks
at the bottom name which mutant each test kills.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from no_human.agent.claude_backend import AgentResult
from no_human.config import load_config
from no_human.core.db import (
    AUX_USAGE_TIERS, USAGE_ROLES, Store, usage_columns_for,
)
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier

# One call's bill. Distinctive per role so a row that holds a number cannot
# have gotten it from any other role's sink.
SUP = (1_101, 2_202, 303)
DIS = (4_404, 5_505, 606)


class _TokenBackend:
    """A ClaudeBackend stand-in reporting a known bill on every call."""

    def __init__(self, tokens, text="ok"):
        self._tokens = tokens
        self._text = text
        self.calls = 0

    def __call__(self, *a, **kw):        # constructed as ClaudeBackend(...)
        return self

    async def run(self, prompt, **kwargs):
        self.calls += 1
        io, cr, cc = self._tokens
        return AgentResult(
            final_text=self._text, num_turns=1, is_error=False, tokens_used=io,
            session_id="s", stop_reason="end_turn",
            cache_read_tokens=cr, cache_creation_tokens=cc,
        )


class _NoBackend:
    async def run(self, *a, **k):  # pragma: no cover — must never be reached
        raise AssertionError("the coder backend should not run in this test")


def _orch(store, tmp_path, sink=None):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    return Orchestrator(store, cfg.data, _NoBackend(), SlackNotifier(None),
                        event_sink=sink)


async def _task(store, repo="/r"):
    t = Task.new("do a thing", repo_path=repo)
    await store.create_task(t)
    return t


async def _drain_to_attempt(orch, store, task):
    """The production drain, verbatim: `_pop_aux_usage()` splatted into
    `update_attempt` is what every attempt-exit path in the orchestrator does.
    The assertion then reads the row back out of SQLite."""
    attempt_id = await store.create_attempt(task.id, 1)
    await store.update_attempt(attempt_id, **orch._pop_aux_usage())
    return (await store.list_attempts(task.id))[-1]


class _Chunk:
    def __init__(self, content, source="file", title="big.py"):
        self.content, self.source, self.title = content, source, title


# --------------------------------------------------------------------------- #
# (a) supervisor usage lands in the supervisor's column                        #
# --------------------------------------------------------------------------- #

async def test_supervisor_burn_lands_in_the_supervisor_column(
    store, tmp_path, monkeypatch,
):
    """Driven through the REAL `_build_supervisor` wiring — the closure the
    orchestrator hands to `SupervisorHook`, not a re-implementation of it."""
    be = _TokenBackend(SUP, text="CONTINUE\nlooks fine")
    monkeypatch.setattr("no_human.core.orchestrator.advisory_backend", be)
    orch = _orch(store, tmp_path)
    t = await _task(store)

    sup = orch._build_supervisor(t, work_dir=str(tmp_path))
    assert sup is not None
    await sup.preflight("a plan")

    assert be.calls == 1
    row = await _drain_to_attempt(orch, store, t)
    assert (row["supervisor_tokens_used"],
            row["supervisor_cache_read_tokens"],
            row["supervisor_cache_creation_tokens"]) == SUP
    # ...and NOT in the bucket it used to be hidden in.
    assert row["utility_tokens_used"] == 0
    assert row["utility_cache_read_tokens"] == 0


async def test_supervisor_burn_with_no_attempt_row_reaches_the_ledger(
    store, tmp_path, monkeypatch,
):
    """A task that never reaches an attempt (parked at the plan gate,
    escalated, decomposed) still spent this. `_flush_orphaned_aux_usage` books
    it under a site that names the ROLE, so the residual keeps the same
    resolution the attempt row has."""
    be = _TokenBackend(SUP, text="CONTINUE\nfine")
    monkeypatch.setattr("no_human.core.orchestrator.advisory_backend", be)
    orch = _orch(store, tmp_path)
    t = await _task(store)

    sup = orch._build_supervisor(t, work_dir=str(tmp_path))
    await sup.preflight("a plan")
    await orch._flush_orphaned_aux_usage(t)

    rows = [dict(r) for r in await (await store.db.execute(
        "SELECT site, task_id, tokens_used, cache_read_tokens, "
        "cache_creation_tokens FROM unattributed_usage")).fetchall()]
    assert [r["site"] for r in rows] == ["orphaned_supervisor_usage"]
    assert rows[0]["task_id"] == t.id
    assert (rows[0]["tokens_used"], rows[0]["cache_read_tokens"],
            rows[0]["cache_creation_tokens"]) == SUP


# --------------------------------------------------------------------------- #
# (b) distillation usage lands in the distiller's column                       #
# --------------------------------------------------------------------------- #

async def test_distillation_burn_lands_in_the_distill_column(
    store, tmp_path, monkeypatch,
):
    be = _TokenBackend(DIS, text="a short summary")
    monkeypatch.setattr("no_human.core.orchestrator.advisory_backend", be)
    orch = _orch(store, tmp_path)
    t = await _task(store, repo=str(tmp_path))
    chunk = _Chunk("x" * (orch._CHUNK_DISTILL_THRESHOLD + 1))

    await orch._distill_large_chunks([chunk], t)

    assert be.calls == 1
    assert chunk.content.startswith("[distilled]")   # it did its job, too
    row = await _drain_to_attempt(orch, store, t)
    assert (row["distill_tokens_used"],
            row["distill_cache_read_tokens"],
            row["distill_cache_creation_tokens"]) == DIS
    assert row["utility_tokens_used"] == 0


async def test_a_distilled_chunk_that_produced_nothing_is_still_billed(
    store, tmp_path, monkeypatch,
):
    """The summary is discarded when it is not shorter than the original — the
    tokens were spent anyway. A cost column that only records USEFUL spend
    understates exactly the case worth optimising away."""
    be = _TokenBackend(DIS, text="y" * 9_000)   # longer than the chunk
    monkeypatch.setattr("no_human.core.orchestrator.advisory_backend", be)
    orch = _orch(store, tmp_path)
    t = await _task(store, repo=str(tmp_path))
    chunk = _Chunk("x" * (orch._CHUNK_DISTILL_THRESHOLD + 1))

    await orch._distill_large_chunks([chunk], t)

    assert not chunk.content.startswith("[distilled]")   # discarded
    row = await _drain_to_attempt(orch, store, t)
    assert row["distill_tokens_used"] == DIS[0]          # ...and billed


async def test_the_two_roles_do_not_share_a_bucket(
    store, tmp_path, monkeypatch,
):
    """Both roles on ONE attempt, with different bills. Before the split these
    two numbers were added together into `utility_` and could never be told
    apart again — which is the whole defect, since they scale with completely
    different things."""
    orch = _orch(store, tmp_path)
    t = await _task(store, repo=str(tmp_path))

    be_d = _TokenBackend(DIS, text="a short summary")
    monkeypatch.setattr("no_human.core.orchestrator.advisory_backend", be_d)
    await orch._distill_large_chunks(
        [_Chunk("x" * (orch._CHUNK_DISTILL_THRESHOLD + 1))], t)

    be_s = _TokenBackend(SUP, text="CONTINUE\nfine")
    monkeypatch.setattr("no_human.core.orchestrator.advisory_backend", be_s)
    await orch._build_supervisor(t, work_dir=str(tmp_path)).preflight("plan")

    row = await _drain_to_attempt(orch, store, t)
    assert row["supervisor_tokens_used"] == SUP[0]
    assert row["distill_tokens_used"] == DIS[0]
    assert row["utility_tokens_used"] == 0
    # The total is unchanged by the split: both are still in the task's burn.
    _, total = await store.lifetime_usage(t.id)
    assert total == sum(SUP) + sum(DIS)


async def test_a_skipped_distillation_is_recorded_so_a_zero_is_falsifiable(
    store, tmp_path, monkeypatch,
):
    """`distill_* == 0` had two completely different causes and no way to tell
    them apart: the distiller ran and was free, or it never ran at all.

    Measured on the live DB 2026-08-10: 162 `context_distill` events, the last
    on 2026-07-28, and the `distill_*` columns landed 2026-08-03 — so the
    distiller and its own columns have never once coexisted, and every one of
    the 828 zeros is CORRECT. That was read as lost spend instead, because
    nothing anywhere recorded the decision NOT to distill. The threshold and
    the decision are unchanged here; only the record of it is added."""
    events = []
    be = _TokenBackend(DIS, text="must never be produced")
    monkeypatch.setattr("no_human.core.orchestrator.advisory_backend", be)
    orch = _orch(store, tmp_path, sink=events.append)
    t = await _task(store, repo=str(tmp_path))
    small = _Chunk("x" * (orch._CHUNK_DISTILL_THRESHOLD - 1))

    await orch._distill_large_chunks([small], t)

    assert be.calls == 0                                  # behaviour unchanged
    row = await _drain_to_attempt(orch, store, t)
    assert row["distill_tokens_used"] == 0
    skipped = [e for e in events if e["kind"] == "context_distill_skipped"]
    assert len(skipped) == 1
    # The three numbers that make the zero readable: how many chunks were
    # WEIGHED, how close the biggest came, and against what.
    assert skipped[0]["chunks"] == 1
    assert skipped[0]["largest"] == len(small.content)
    assert skipped[0]["threshold"] == orch._CHUNK_DISTILL_THRESHOLD
    assert skipped[0]["reason"] == "no_large_chunk"    # which skip this is
    # ...and no distillation was claimed. `nh doctor` counts this kind as
    # liveness evidence, so a skip must never inflate it.
    assert [e for e in events if e["kind"] == "context_distill"] == []


async def test_a_distillation_records_the_bytes_it_removed(
    store, tmp_path, monkeypatch,
):
    """The OTHER half of `_note_distill_usage`'s own claim: distillation "pays
    for itself — a smaller coder prompt in exchange for N summarizer sessions
    — and that trade cannot be measured while the two sides of it are not
    separately recorded." The spend side got a column; the saving side was
    recorded nowhere, so the trade stayed untestable even for the 162 runs that
    did happen. This is that side."""
    events = []
    be = _TokenBackend(DIS, text="a short summary")
    monkeypatch.setattr("no_human.core.orchestrator.advisory_backend", be)
    orch = _orch(store, tmp_path, sink=events.append)
    t = await _task(store, repo=str(tmp_path))
    chunk = _Chunk("x" * (orch._CHUNK_DISTILL_THRESHOLD + 1))
    before = len(chunk.content)

    await orch._distill_large_chunks([chunk], t)

    row = await _drain_to_attempt(orch, store, t)
    assert row["distill_tokens_used"] == DIS[0]           # spend side
    ev = [e for e in events if e["kind"] == "context_distill"]
    assert len(ev) == 1
    assert ev[0]["chars_before"] == before                # saving side
    assert ev[0]["chars_after"] == len(chunk.content)
    assert ev[0]["chars_after"] < before
    assert [e for e in events if e["kind"] == "context_distill_skipped"] == []


async def test_a_distillation_that_grows_the_chunk_is_recorded_as_a_growth(
    store, tmp_path, monkeypatch,
):
    """`chars_after` can EXCEED `chars_before`, and this pins the case so the
    pair cannot be read as "bytes saved". The guard compares the RAW summary
    against `before`, but the replacement prepends `"[distilled] "` — 12 chars
    — so a summary one char under the original leaves the chunk 11 chars
    BIGGER. The event records that honestly; nothing else may claim otherwise.

    The off-by-12 is pre-existing and deliberately NOT fixed here: narrowing
    the guard would change which chunks get distilled, which is a behaviour
    change this commit does not make."""
    events = []
    orch_threshold = Orchestrator._CHUNK_DISTILL_THRESHOLD
    before = orch_threshold + 100
    be = _TokenBackend(DIS, text="y" * (before - 1))   # one char under
    monkeypatch.setattr("no_human.core.orchestrator.advisory_backend", be)
    orch = _orch(store, tmp_path, sink=events.append)
    t = await _task(store, repo=str(tmp_path))
    chunk = _Chunk("x" * before)

    await orch._distill_large_chunks([chunk], t)

    ev = [e for e in events if e["kind"] == "context_distill"]
    assert len(ev) == 1
    assert ev[0]["chars_before"] == before
    assert ev[0]["chars_after"] == before + 11        # 12-char prefix, -1 char
    assert ev[0]["chars_after"] > ev[0]["chars_before"]
    assert len(chunk.content) == before + 11          # behaviour unchanged


async def test_a_distillation_that_raises_is_recorded_not_silent(
    store, tmp_path, monkeypatch,
):
    """The third outcome. `except Exception: pass` swallowed backend failures
    whole, so a gather with an oversized chunk and a dead backend (quota
    exhausted, credentials scrubbed, backend unavailable) emitted NOTHING:
    not `context_distill`, not `context_distill_skipped`, and
    `_note_distill_usage` was never reached, so `distill_*` stayed 0 too.

    Every counter then read exactly like a distiller that is never consulted,
    while it was being consulted and failing on every call — the same
    zero-with-two-causes this commit exists to end, one layer down."""
    events = []

    class _Boom:
        def __call__(self, *a, **kw):
            raise RuntimeError("Credit balance too low")

    monkeypatch.setattr("no_human.core.orchestrator.advisory_backend", _Boom())
    orch = _orch(store, tmp_path, sink=events.append)
    t = await _task(store, repo=str(tmp_path))
    chunk = _Chunk("x" * (orch._CHUNK_DISTILL_THRESHOLD + 1))
    before = len(chunk.content)

    await orch._distill_large_chunks([chunk], t)      # never raises

    assert chunk.content == "x" * before              # chunk preserved
    failed = [e for e in events if e["kind"] == "context_distill_failed"]
    assert len(failed) == 1
    assert failed[0]["error"] == "RuntimeError"
    assert failed[0]["chars_before"] == before
    # Not a firing and not a skip: those two count as "weighed and decided".
    assert [e for e in events if e["kind"] == "context_distill"] == []
    assert [e for e in events if e["kind"] == "context_distill_skipped"] == []


async def test_a_distillation_that_buys_nothing_is_recorded_too(
    store, tmp_path, monkeypatch,
):
    """The fourth outcome, and the other half of the same blind spot: the call
    SUCCEEDS and is billed, but the summary is empty or no shorter than the
    chunk, so the chunk is left alone and `context_distill` is not emitted.

    Without this record, `distill_*` is non-zero while all three event kinds
    are zero — spend with no trace of what it bought."""
    events = []
    be = _TokenBackend(DIS, text="z" * 9_000)         # longer than the chunk
    monkeypatch.setattr("no_human.core.orchestrator.advisory_backend", be)
    orch = _orch(store, tmp_path, sink=events.append)
    t = await _task(store, repo=str(tmp_path))
    chunk = _Chunk("x" * (orch._CHUNK_DISTILL_THRESHOLD + 1))
    before = len(chunk.content)

    await orch._distill_large_chunks([chunk], t)

    assert chunk.content == "x" * before              # behaviour unchanged
    row = await _drain_to_attempt(orch, store, t)
    assert row["distill_tokens_used"] == DIS[0]       # it WAS billed
    skipped = [e for e in events if e["kind"] == "context_distill_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "no_gain"
    assert skipped[0]["chars_before"] == before
    assert [e for e in events if e["kind"] == "context_distill"] == []


# --------------------------------------------------------------------------- #
# (c) reconciliation: the roles partition the total, exactly                   #
# --------------------------------------------------------------------------- #

def test_every_usage_column_belongs_to_exactly_one_role():
    """STRUCTURAL reconciliation WITHIN THE REGISTRY, and the one that does
    not need data: the per-role column sets must partition
    `_usage_columns()` — no registered column unclaimed (spend nobody's role
    owns), none claimed twice (spend billed to two roles). It catches a
    `usage_columns_for` that stops covering a registered prefix, or that
    returns overlapping families for two of them.

    What it CANNOT catch, because both sides of the comparison derive from
    `USAGE_ROLES` and therefore narrow together: a metered column added to
    the `attempts` SCHEMA under a prefix no role registers. That is
    `test_no_metered_column_in_the_schema_is_unclaimed`'s job — this one is
    blind to it and must not be read as covering it."""
    claimed: list[str] = []
    for tier in USAGE_ROLES:
        claimed.extend(usage_columns_for(tier))
    assert len(claimed) == len(set(claimed)), "a column is billed twice"
    assert set(claimed) == set(Store._usage_columns())


async def test_no_metered_column_in_the_schema_is_unclaimed(store):
    """The SCHEMA-anchored half, and the only guard here whose expectation
    does not come from `USAGE_ROLES`: every metered column that actually
    exists on the `attempts` table must be claimed by some registered role.

    The registry test above compares a registry-derived set against a
    registry-derived set, so a role added to `_migrate` and forgotten in
    `USAGE_ROLES` leaves it green while that role's burn is counted by no
    surface at all — the exact drift that hid planning burn for a release.
    Reading `PRAGMA table_info(attempts)` is what breaks the circle: the
    column families are discovered from the live table, not enumerated.

    `*_output_tokens` is excluded from the match on purpose — it is a SLICE
    of `*_tokens_used`, not an addend, and is deliberately outside
    `_usage_columns()` (see `Store._output_columns_by_class`).
    """
    cols = {r["name"] for r in await store._fetchall(
        "PRAGMA table_info(attempts)")}
    metered = {c for c in cols if c.endswith(
        ("tokens_used", "cache_read_tokens", "cache_creation_tokens"))}
    assert metered, "PRAGMA returned no metered columns — instrument broken"
    assert metered == set(Store._usage_columns())


def test_the_aux_roles_are_derived_from_the_registered_roles():
    """`_pop_aux_usage` and `_flush_orphaned_aux_usage` iterate
    `AUX_USAGE_TIERS`. A prefix there with no column family behind it writes
    `update_attempt(nonexistent_column=…)` and raises mid-attempt; a
    registered role MISSING from it never gets drained, and its burn is lost
    without raising anything at all.

    Asserted as `==` against the two exclusions restated here, not as `⊆`: a
    subset check passes on an empty tuple and on any dropped role, which is
    the direction that loses spend silently."""
    assert set(AUX_USAGE_TIERS) == set(USAGE_ROLES) - {"", "review_"}
    assert len(AUX_USAGE_TIERS) == len(set(AUX_USAGE_TIERS))
    assert "" not in AUX_USAGE_TIERS          # the coder is not drained
    assert "review_" not in AUX_USAGE_TIERS   # nor is the reviewer


async def test_roles_sum_to_the_recorded_total(store):
    """DATA reconciliation, on a row that carries a distinct number in every
    single usage column: the per-role breakdown must add up to
    `lifetime_usage`, with no residual and nothing counted twice."""
    t = await _task(store)
    attempt_id = await store.create_attempt(t.id, 1)
    # A distinct value per column, so any double-count or omission shows up as
    # a wrong total rather than cancelling out.
    values = {col: 10 * (i + 1)
              for i, col in enumerate(Store._usage_columns())}
    await store.update_attempt(attempt_id, **values)

    by_role = await store.lifetime_usage_by_role(t.id)
    _, total = await store.lifetime_usage(t.id)

    assert set(by_role) == set(USAGE_ROLES.values())
    assert sum(r["total"] for r in by_role.values()) == total
    assert total == sum(values.values())
    # And each role reports ITS columns, not somebody else's.
    for tier, role in USAGE_ROLES.items():
        assert by_role[role]["total"] == sum(
            values[c] for c in usage_columns_for(tier))


async def test_output_tokens_ride_along_without_inflating_the_total(store):
    """`*_output_tokens` is a SLICE of `*_tokens_used`, already inside it.
    It is reported per role because a caller pricing a role needs it, and it
    must NOT be added to `total` — doing so double-counts every output token
    and breaks the reconciliation above."""
    t = await _task(store)
    attempt_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        attempt_id, supervisor_tokens_used=1_000,
        supervisor_output_tokens=400, supervisor_cache_read_tokens=50)

    by_role = await store.lifetime_usage_by_role(t.id)
    assert by_role["supervisor"]["output_tokens"] == 400
    assert by_role["supervisor"]["total"] == 1_050       # not 1_450
    _, total = await store.lifetime_usage(t.id)
    assert sum(r["total"] for r in by_role.values()) == total


async def test_a_task_with_no_attempts_reports_every_role_at_zero(store):
    """Stable shape: a caller rendering a breakdown gets every role, so an
    operator can see that a role cost nothing rather than wondering whether it
    was measured."""
    t = await _task(store)
    by_role = await store.lifetime_usage_by_role(t.id)
    assert set(by_role) == set(USAGE_ROLES.values())
    assert all(r["total"] == 0 for r in by_role.values())


# --------------------------------------------------------------------------- #
# The surfaces: every one that reports cost includes the new roles             #
# --------------------------------------------------------------------------- #

def _row_with(**kw):
    row = {c: 0 for c in Store._usage_columns()}
    row.update(kw)
    return row


def test_cli_burn_includes_the_new_roles():
    """`nh logs` / `nh agents` print `_attempt_tokens`'s burn. It summed four
    literal groups; supervisor and distill spend would have been invisible on
    the surface an operator watches a runaway on."""
    from no_human.cli.commands import _attempt_tokens

    row = _row_with(tokens_used=100, supervisor_tokens_used=7,
                    distill_cache_read_tokens=9)
    spend, burn = _attempt_tokens(row)
    assert spend == 100          # coder-only, unchanged
    assert burn == 116           # 100 + 7 + 9


def test_cli_role_breakdown_partitions_the_burn():
    """The `roles:` line in `nh logs` is a DECOMPOSITION of `burn`, not a
    selection from it — the two must agree for every row."""
    from no_human.cli.commands import _attempt_role_burn, _attempt_tokens

    row = _row_with(tokens_used=100, cache_read_tokens=5,
                    review_tokens_used=11, plan_tokens_used=13,
                    utility_tokens_used=17, supervisor_tokens_used=19,
                    distill_cache_creation_tokens=23)
    roles = _attempt_role_burn(row)
    assert roles == {"coder": 105, "reviewer": 11, "planner": 13,
                     "utility": 17, "supervisor": 19, "distill": 23}
    assert sum(roles.values()) == _attempt_tokens(row)[1]


def test_bench_card_nh_tokens_includes_the_new_roles():
    """The north-star card's `nh_tokens` is the numerator of the 10%-of-cost
    claim. Driven through the REAL `_score`, not a re-computation of it."""
    import asyncio
    import types

    from no_human.core.task import TaskStatus
    from no_human.eval.northstar import NorthStarRunner

    r = NorthStarRunner.__new__(NorthStarRunner)
    r.goal_judge = None
    spec = types.SimpleNamespace(
        id="ns-1", title="t", original={}, expect_escalation=False,
        subset=None, project=None, repo={}, skip_reason=None,
        spec_repo_path="", holdout=None)
    outcome = types.SimpleNamespace(status=TaskStatus.DONE)
    attempts = [_row_with(
        tokens_used=100, review_tokens_used=10, plan_tokens_used=20,
        utility_tokens_used=30, supervisor_tokens_used=40,
        distill_tokens_used=50,
        cache_read_tokens=1, supervisor_cache_read_tokens=2,
        distill_cache_read_tokens=3,
        cache_creation_tokens=4, supervisor_cache_creation_tokens=5,
        distill_cache_creation_tokens=6)]

    score = asyncio.run(
        r._score(spec, outcome, None, "sha", attempts, 1.0))

    assert score.nh_tokens == 250        # 100+10+20+30+40+50
    assert score.nh_cache_tokens == 6    # 1+2+3
    assert score.nh_cache_creation_tokens == 15   # 4+5+6


def test_replay_score_burn_includes_the_new_roles():
    import asyncio
    import types

    from no_human.eval.replay import ReplayRunner

    r = ReplayRunner.__new__(ReplayRunner)
    r._tamper_free = lambda outcome, attempts: True
    r.judge = None
    golden = types.SimpleNamespace(
        id="g1", title="t", is_red_team=False, impossible=False,
        tempts_tamper=False, known_good_diff=None)
    outcome = types.SimpleNamespace(status=types.SimpleNamespace(value="done"))
    attempts = [_row_with(tokens_used=100, supervisor_tokens_used=7,
                          distill_cache_read_tokens=9)]

    score = asyncio.run(r._score(golden, outcome, None, "sha", attempts, 1.0))
    assert score.tokens == 116


def test_board_aux_total_includes_the_new_roles():
    """`_aux_totals` feeds both the board card and the approval drawer via
    `web/src/cost.js`. It named plan_ and utility_ explicitly, so the surface a
    human approves spend from would have been short by two whole roles."""
    from no_human.api.models import _aux_totals

    used, read, creation = _aux_totals([_row_with(
        tokens_used=999,                       # coder: never in the aux total
        review_tokens_used=888,                # reviewer: has its own keys
        plan_tokens_used=1, utility_tokens_used=2,
        supervisor_tokens_used=4, distill_tokens_used=8,
        supervisor_cache_read_tokens=16,
        distill_cache_creation_tokens=32)])
    assert used == 15          # 1+2+4+8
    assert read == 16
    assert creation == 32


@pytest.mark.asyncio
async def test_metrics_reports_per_role_and_reconciles(store):
    """/api/metrics gains a per-ROLE breakdown (`by_tier` beside it answers
    which MODEL ran, a different question). The aggregate `aux_*` keys must
    stay consistent with it."""
    from no_human.core.metrics import compute_metrics

    t = await _task(store)
    attempt_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(attempt_id, **_row_with(
        tokens_used=100, review_tokens_used=10, plan_tokens_used=20,
        utility_tokens_used=30, supervisor_tokens_used=40,
        distill_cache_read_tokens=50))

    m = await compute_metrics(store)
    assert set(m["by_role"]) == set(USAGE_ROLES.values())
    assert m["by_role"]["supervisor"]["total"] == 40
    assert m["by_role"]["distill"]["total"] == 50
    assert m["by_role"]["coder"]["total"] == 100
    # aux = everything but coder and reviewer, and it agrees with by_role.
    assert m["aux_tokens_used_total"] == 20 + 30 + 40
    assert m["aux_cache_read_total"] == 50
    assert sum(r["total"] for r in m["by_role"].values()) == 250


# --------------------------------------------------------------------------- #
# Mutation checks: WHICH mutant kills WHICH test                               #
# --------------------------------------------------------------------------- #

async def test_mutation_supervisor_sink_reverted_to_utility(
    store, tmp_path, monkeypatch,
):
    """MUTANT: `sv_llm_call` calls `_note_utility_usage` again (the pre-fix
    code). The spend still reaches the row — the TOTAL was never the bug — so
    only a test that reads the supervisor's own column can see it.

    Kills THREE tests, measured by applying the mutant to
    `orchestrator.py`'s supervisor call site and running this file:
    `test_supervisor_burn_lands_in_the_supervisor_column` (its
    `supervisor_tokens_used == SUP` assertion),
    `test_supervisor_burn_with_no_attempt_row_reaches_the_ledger` (the
    ledger row is booked under `orphaned_utility_usage`, not
    `orphaned_supervisor_usage`) and
    `test_the_two_roles_do_not_share_a_bucket`. Does NOT kill
    `test_roles_sum_to_the_recorded_total` — reconciliation is blind to which
    role a token lands in, which is exactly why the column assertions above
    are separate tests and not folded into it.
    """
    be = _TokenBackend(SUP, text="CONTINUE\nfine")
    monkeypatch.setattr("no_human.core.orchestrator.advisory_backend", be)
    orch = _orch(store, tmp_path)
    monkeypatch.setattr(
        Orchestrator, "_note_supervisor_usage",
        lambda self, result: self._note_utility_usage(result))
    t = await _task(store)

    await orch._build_supervisor(t, work_dir=str(tmp_path)).preflight("plan")

    row = await _drain_to_attempt(orch, store, t)
    assert row["supervisor_tokens_used"] == 0     # the mutant's damage
    assert row["utility_tokens_used"] == SUP[0]   # ...misfiled, not lost
    _, total = await store.lifetime_usage(t.id)
    assert total == sum(SUP)                      # total unmoved: see docstring


async def test_mutation_distill_sink_dropped_entirely(
    store, tmp_path, monkeypatch,
):
    """MUTANT: `_distill_large_chunks` stops calling its sink at all — the
    shape the supervisor and distiller were in before B2 #6, and the shape any
    future role starts in.

    Kills: `test_distillation_burn_lands_in_the_distill_column`,
    `test_a_distilled_chunk_that_produced_nothing_is_still_billed` and
    `test_the_two_roles_do_not_share_a_bucket`. This one DOES also move the
    total, so `lifetime_usage` drops by the whole distill bill.
    """
    be = _TokenBackend(DIS, text="a short summary")
    monkeypatch.setattr("no_human.core.orchestrator.advisory_backend", be)
    orch = _orch(store, tmp_path)
    monkeypatch.setattr(
        Orchestrator, "_note_distill_usage", lambda self, result: None)
    t = await _task(store, repo=str(tmp_path))

    await orch._distill_large_chunks(
        [_Chunk("x" * (orch._CHUNK_DISTILL_THRESHOLD + 1))], t)

    assert be.calls == 1                         # the tokens were really spent
    row = await _drain_to_attempt(orch, store, t)
    assert row["distill_tokens_used"] == 0       # ...and nothing recorded them
    _, total = await store.lifetime_usage(t.id)
    assert total == 0


def test_mutation_a_role_missing_from_the_registry_is_caught():
    """MUTANT: a column family exists in the schema but its prefix is dropped
    from `USAGE_ROLES` — the exact drift that let planning burn go unpriced
    for a release, since every surface derives its column list from the
    registry and would silently narrow together.

    Kills: `test_every_usage_column_belongs_to_exactly_one_role`, by leaving
    the six `distill_*` columns unclaimed. Asserted here by running the
    structural check's own predicate against the mutated registry, so the
    mutant is proven lethal rather than asserted to be.
    """
    mutated = {k: v for k, v in USAGE_ROLES.items() if k != "distill_"}
    claimed = [c for tier in mutated for c in usage_columns_for(tier)]
    assert set(claimed) != set(Store._usage_columns())
    missing = set(Store._usage_columns()) - set(claimed)
    assert missing == {"distill_tokens_used", "distill_cache_read_tokens",
                       "distill_cache_creation_tokens"}


def test_mutation_output_tokens_folded_in_as_a_fourth_addend():
    """MUTANT: `usage_columns_for` returns the output column as a fourth
    addend. Every output token is then counted twice and the role sum exceeds
    the recorded total.

    Kills: `test_output_tokens_ride_along_without_inflating_the_total` and
    `test_roles_sum_to_the_recorded_total`.
    """
    mutated = [c for tier in USAGE_ROLES
               for c in usage_columns_for(tier) + (
                   "output_tokens" if tier == "" else f"{tier}output_tokens",)]
    assert len(mutated) != len(Store._usage_columns())
    assert set(mutated) - set(Store._usage_columns()) == {
        "output_tokens", "review_output_tokens", "plan_output_tokens",
        "utility_output_tokens", "supervisor_output_tokens",
        "distill_output_tokens"}
