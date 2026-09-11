"""The output-token split: capture, persistence, pricing, and the migration.

WHY THIS FILE EXISTS. `_usage_quad` in `agent/claude_backend.py` has always
returned four numbers — (input, output, cache_read, cache_creation) — and the
backend then threw two of them away, adding `input_tokens + output_tokens` into
one `tokens_used` column before the orchestrator or the DB ever saw the split.
Output bills ~5x input, so every figure derived from that column understates
spend in the same direction: the stats page's dollars, the cost tiles, and the
lifetime-budget brake. `core/pricing.py` has documented this in prose since it
was written, including the remedy: "Closing that needs a schema change (a
fourth column), not a weight."

The tests below pin the four properties that make that column trustworthy:

  1. the backend now REPORTS the split (`AgentResult.output_tokens`);
  2. a NEW attempt PERSISTS it, and an attempt that never reported one
     persists NULL rather than 0;
  3. an OLD-schema database migrates without losing a row, and its historical
     rows read NULL — never 0. `0` would claim those attempts emitted no
     output tokens, which is false for every one of them;
  4. the raw token total does NOT move, because `output_tokens` is a SUBSET of
     `tokens_used`, not a fourth addend.

Property 3 is the one with a recorded scar behind it: a `0` written for an
unreported field once inflated sibling caps and made a per-attempt brake inert
on 27 of 27 tasks. So the assertions here go through SQL (`IS NULL`,
`= 0`) rather than through a Python round-trip that could coerce one into the
other, and they fail loudly on 0 rather than only on a crash.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from no_human.core.db import MIGRATIONS_DIR, Store
from no_human.core.pricing import (
    CACHE_CREATION_WEIGHT,
    CACHE_READ_WEIGHT,
    OUTPUT_EXTRA_WEIGHT,
    weighted_tokens,
)

# The four columns the split adds — one per model tier, because all four tiers
# (coder, reviewer, planner, utility) run through the same backend and all four
# bill output at the same premium.
OUTPUT_COLUMNS = (
    "output_tokens",
    "review_output_tokens",
    "plan_output_tokens",
    "utility_output_tokens",
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _build_old_schema_db(path: Path) -> None:
    """A database at the ORIGINAL schema — `migrations/*.sql` and nothing else.

    Deliberately NOT "a current database with the new columns dropped": the
    thing under test is the code path a real user's on-disk file takes, and
    that file was created by these same .sql scripts and then walked forward by
    `_ensure_task_columns`. Building it any other way tests the test.

    Every column `_ensure_task_columns` adds is therefore absent here,
    including the twelve usage columns — this is an honestly old file.
    """
    conn = sqlite3.connect(path)
    try:
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            conn.executescript(sql_file.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def _seed_historical_rows(path: Path) -> list[tuple[str, int]]:
    """Real pre-existing rows, written with ONLY the columns an old file has.

    Returns (attempt_id, tokens_used) so the caller can prove the values
    survived rather than merely that the rows did. The token counts are the
    shape this ledger actually holds: a large attempt, a small one, and one
    that recorded zero (interrupted before it metered anything) — that last is
    the interesting one, because it is the row where a `0` in the new column
    would be indistinguishable from a real measurement.
    """
    rows = [
        ("att-old-1", 1_204_337),
        ("att-old-2", 8_119),
        ("att-old-3", 0),
    ]
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, source, title, status) VALUES (?, ?, ?, ?)",
            ("task-old", "freeform", "a task from before the split", "done"),
        )
        for n, (att_id, tokens) in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO attempts (id, task_id, attempt_number, "
                "turns_used, tokens_used, status) VALUES (?, ?, ?, ?, ?, ?)",
                (att_id, "task-old", n, 7, tokens, "succeeded"),
            )
        conn.commit()
    finally:
        conn.close()
    return rows


def _sql(path: Path, query: str, params: tuple = ()) -> list[tuple]:
    """Query the file DIRECTLY, outside the Store.

    The point of the NULL assertions is what is ON DISK. Reading them back
    through the Store's row factory would test a Python value, and a Python
    value is exactly where a NULL can quietly become a 0.
    """
    conn = sqlite3.connect(path)
    try:
        return list(conn.execute(query, params))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 1. the backend reports the split
# --------------------------------------------------------------------------- #

def test_usage_quad_still_returns_four_values():
    """The producer was never the defect — pin it so the fix cannot "simplify"
    the one function that already had the number."""
    from no_human.agent.claude_backend import _usage_quad

    assert _usage_quad(
        {
            "input_tokens": 900,
            "output_tokens": 100,
            "cache_read_input_tokens": 50_000,
            "cache_creation_input_tokens": 2_000,
        }
    ) == (900, 100, 50_000, 2_000)


def test_agent_result_carries_the_output_share():
    """`tokens_used` keeps meaning input+output; `output_tokens` says how much
    of it was output. Input is `tokens_used - output_tokens`, so the two
    numbers cannot drift out of agreement about the total."""
    from no_human.agent.claude_backend import AgentResult

    r = AgentResult(
        final_text="",
        num_turns=1,
        is_error=False,
        tokens_used=1_000,
        session_id=None,
        stop_reason=None,
        output_tokens=100,
    )
    assert r.tokens_used == 1_000
    assert r.output_tokens == 100
    assert r.tokens_used - r.output_tokens == 900  # the input share


def test_agent_result_output_defaults_to_none_not_zero():
    """A result that never saw a usage block does not get to claim it emitted
    no output. The default is the honest one."""
    from no_human.agent.claude_backend import AgentResult

    r = AgentResult(
        final_text="",
        num_turns=1,
        is_error=False,
        tokens_used=0,
        session_id=None,
        stop_reason=None,
    )
    assert r.output_tokens is None


# --------------------------------------------------------------------------- #
# 2. pricing
# --------------------------------------------------------------------------- #

def test_weighted_tokens_unchanged_when_the_split_is_unknown():
    """Every historical row, and every caller that has not been taught the new
    keyword, must price EXACTLY as it did before this change. Unknown is
    priced at the old rate — not guessed at, and not inflated."""
    before = (
        1_000_000 * 1.0
        + 1_000_000 * CACHE_READ_WEIGHT
        + 1_000_000 * CACHE_CREATION_WEIGHT
    )
    assert weighted_tokens(
        tokens_used=1_000_000,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
    ) == int(before)
    # An explicit 0 and an omitted argument agree, because 0 output really is
    # "no output premium" — the ambiguity lives in the DB column, not here.
    assert weighted_tokens(tokens_used=1_000, output_tokens=0) == 1_000
    assert weighted_tokens(tokens_used=1_000, output_tokens=None) == 1_000


def test_output_tokens_add_the_premium_not_the_whole_rate():
    """`output_tokens` is a SUBSET of `tokens_used`, which already charged it
    once at the fresh rate. So the weight applied here is the DIFFERENCE. A
    fully-output million must price at 5x a fully-input million, not 6x."""
    all_input = weighted_tokens(tokens_used=1_000_000, output_tokens=0)
    all_output = weighted_tokens(tokens_used=1_000_000, output_tokens=1_000_000)
    assert all_input == 1_000_000
    assert all_output == 5_000_000
    assert all_output == 5 * all_input
    assert OUTPUT_EXTRA_WEIGHT == 4.0


def test_the_understatement_this_change_exists_to_fix():
    """The ticket's own arithmetic, pinned as a test: an attempt that is 20%
    output BY COUNT is ~55% output BY COST. Before this column existed the
    cost surfaces saw the first number and reported it as the second."""
    total, output = 1_000_000, 200_000
    priced = weighted_tokens(tokens_used=total, output_tokens=output)
    assert priced == 1_800_000
    output_cost = output * 5.0
    assert round(output_cost / priced, 2) == 0.56
    # And the old, split-blind figure understated the same attempt by 44%.
    assert weighted_tokens(tokens_used=total) == 1_000_000


# --------------------------------------------------------------------------- #
# 3. a NEW attempt persists the split
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_new_attempt_persists_input_and_output_separately(tmp_path):
    """The acceptance criterion: for an attempt written today, input and
    output are both readable."""
    db_path = tmp_path / "new.db"
    store = await Store(db_path).connect()
    try:
        await store.db.execute(
            "INSERT INTO tasks (id, source, title, status) VALUES (?,?,?,?)",
            ("t1", "freeform", "t", "done"),
        )
        await store.db.execute(
            "INSERT INTO attempts (id, task_id, attempt_number) VALUES (?,?,?)",
            ("a1", "t1", 1),
        )
        await store.db.commit()
        await store.update_attempt(
            "a1", tokens_used=1_000, output_tokens=250,
            cache_read_tokens=9, cache_creation_tokens=3,
        )
    finally:
        await store.close()

    (total, out), = _sql(
        db_path, "SELECT tokens_used, output_tokens FROM attempts WHERE id='a1'")
    assert (total, out) == (1_000, 250)
    assert total - out == 750  # the input share, derived not stored


@pytest.mark.asyncio
async def test_an_attempt_that_reported_no_split_writes_null(tmp_path):
    """A new-schema row is not automatically a row that KNOWS its split. When
    the backend reports None, NULL is what lands — the same honesty the
    historical rows get."""
    db_path = tmp_path / "partial.db"
    store = await Store(db_path).connect()
    try:
        await store.db.execute(
            "INSERT INTO tasks (id, source, title, status) VALUES (?,?,?,?)",
            ("t2", "freeform", "t", "done"),
        )
        await store.db.execute(
            "INSERT INTO attempts (id, task_id, attempt_number) VALUES (?,?,?)",
            ("a2", "t2", 1),
        )
        await store.db.commit()
        await store.update_attempt("a2", tokens_used=4_000, output_tokens=None)
    finally:
        await store.close()

    (is_null, is_zero), = _sql(
        db_path,
        "SELECT output_tokens IS NULL, output_tokens = 0 "
        "FROM attempts WHERE id='a2'",
    )
    assert is_null == 1, "an unreported split must be NULL"
    assert not is_zero, "0 would claim the attempt emitted no output tokens"


# --------------------------------------------------------------------------- #
# 4. the migration, against a REAL old-schema file
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_old_schema_db_migrates_without_losing_rows(tmp_path):
    """Open a genuinely old file and prove the rows are still there, with
    their values, after the new columns arrive."""
    db_path = tmp_path / "old.db"
    _build_old_schema_db(db_path)
    seeded = _seed_historical_rows(db_path)

    # Precondition: the column really is absent, or this test proves nothing.
    cols_before = {r[1] for r in _sql(db_path, "PRAGMA table_info(attempts)")}
    assert "output_tokens" not in cols_before
    assert "tokens_used" in cols_before

    store = await Store(db_path).connect()
    try:
        attempts = await store.list_attempts("task-old")
    finally:
        await store.close()

    assert len(attempts) == len(seeded)
    by_id = {a["id"]: a for a in attempts}
    for att_id, tokens in seeded:
        assert by_id[att_id]["tokens_used"] == tokens, "historical value lost"
        assert by_id[att_id]["turns_used"] == 7

    cols_after = {r[1] for r in _sql(db_path, "PRAGMA table_info(attempts)")}
    for col in OUTPUT_COLUMNS:
        assert col in cols_after, f"{col} was not added by the migration"


@pytest.mark.asyncio
async def test_historical_rows_read_null_and_never_zero(tmp_path):
    """THE test this change is really about.

    The split was discarded at capture. There is no backfill and there never
    can be one, so the only truthful value for a pre-existing row is "unknown".
    A migration that wrote 0 would pass a "did it crash?" test and then quietly
    price a million output tokens at nothing.

    Asserted in SQL, against the file, both ways round: every historical row
    IS NULL, and ZERO historical rows equal 0.
    """
    db_path = tmp_path / "old.db"
    _build_old_schema_db(db_path)
    seeded = _seed_historical_rows(db_path)

    store = await Store(db_path).connect()
    try:
        pass  # connecting is what migrates
    finally:
        await store.close()

    for col in OUTPUT_COLUMNS:
        (n_null,), = _sql(
            db_path, f"SELECT COUNT(*) FROM attempts WHERE {col} IS NULL")
        assert n_null == len(seeded), f"{col}: historical rows must read NULL"

        # The assertion that fails on a 0-filled migration. `= 0` is NULL-safe
        # in SQLite (NULL = 0 is NULL, never true), so this counts only rows
        # that really do hold the number zero.
        (n_zero,), = _sql(
            db_path, f"SELECT COUNT(*) FROM attempts WHERE {col} = 0")
        assert n_zero == 0, (
            f"{col}: {n_zero} historical row(s) were backfilled with 0. "
            "0 means 'this attempt emitted no output tokens', which is false "
            "for every row written before the split was captured; the "
            "unrecoverable value is NULL."
        )


@pytest.mark.asyncio
async def test_the_column_has_no_zero_default(tmp_path):
    """Belt and braces on the DDL itself.

    The twelve usage columns beside these are declared `INTEGER DEFAULT 0`,
    and copying that declaration is the single most likely way this fix gets
    undone by a later edit. `dflt_value` must stay NULL so that ADD COLUMN
    backfills NULL and so that a future INSERT that omits the column does not
    silently invent a zero.
    """
    db_path = tmp_path / "decl.db"
    store = await Store(db_path).connect()
    await store.close()

    info = {r[1]: r for r in _sql(db_path, "PRAGMA table_info(attempts)")}
    for col in OUTPUT_COLUMNS:
        assert info[col][2] == "INTEGER"
        assert info[col][4] is None, (
            f"{col} must not carry a DEFAULT — a default of 0 would make "
            "every unreported split read as 'no output tokens'."
        )
    # The neighbours it must NOT be copied from, pinned so the contrast is
    # visible at the point somebody edits this dict.
    assert info["tokens_used"][4] is None       # original column, always NULL-able
    assert info["review_tokens_used"][4] == "0"


# --------------------------------------------------------------------------- #
# 5. the subset invariant — no double counting
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_raw_lifetime_total_does_not_move(tmp_path):
    """`output_tokens` is a breakdown OF `tokens_used`, not a fourth bucket
    beside it. `lifetime_usage` is the RAW figure `nh`, the web surfaces and
    `eval/northstar.py` all print, and it must keep matching them token for
    token — so adding the column must not change it by a single token."""
    db_path = tmp_path / "raw.db"
    store = await Store(db_path).connect()
    try:
        await store.db.execute(
            "INSERT INTO tasks (id, source, title, status) VALUES (?,?,?,?)",
            ("t3", "freeform", "t", "done"),
        )
        await store.db.execute(
            "INSERT INTO attempts (id, task_id, attempt_number) VALUES (?,?,?)",
            ("a3", "t3", 1),
        )
        await store.db.commit()
        await store.update_attempt(
            "a3", tokens_used=1_000, output_tokens=400,
            cache_read_tokens=100, cache_creation_tokens=10,
        )
        n, raw = await store.lifetime_usage("t3")
        _, by_class, _ = await store.lifetime_usage_by_class("t3")
    finally:
        await store.close()

    assert n == 1
    # 1000 + 100 + 10 — the 400 is already inside the 1000.
    assert raw == 1_110
    assert by_class["output_tokens"] == 400
    # And the weighted figure, which is the one that moved, prices the premium.
    assert weighted_tokens(**by_class) == int(
        1_000 + 400 * OUTPUT_EXTRA_WEIGHT + 100 * CACHE_READ_WEIGHT
        + 10 * CACHE_CREATION_WEIGHT
    )


@pytest.mark.asyncio
async def test_unknown_splits_sum_to_no_premium(tmp_path):
    """A task whose attempts all predate the split prices exactly as it did
    before — NULL contributes nothing, rather than blocking the SUM or being
    read as a measured zero."""
    db_path = tmp_path / "mixed.db"
    _build_old_schema_db(db_path)
    _seed_historical_rows(db_path)

    store = await Store(db_path).connect()
    try:
        _, by_class, _ = await store.lifetime_usage_by_class("task-old")
        n, raw = await store.lifetime_usage("task-old")
    finally:
        await store.close()

    assert n == 3
    assert raw == 1_204_337 + 8_119
    assert by_class["output_tokens"] == 0
    assert weighted_tokens(**by_class) == 1_204_337 + 8_119
