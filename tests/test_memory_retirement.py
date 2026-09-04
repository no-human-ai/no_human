"""Memory lifecycle C: retirement + flood control (research report 2026-08-12).

45-day auto-archive of unconfirmed proposals (reversible), superseded_by
pointers on confirm, SUGGEST-only retirement for active rows, and the
one-time flood-source triage.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from click.testing import CliRunner

from no_human.core.db import SOURCE_PROPOSED, Store
from no_human.learning import (
    LearningQueue,
    ORIGIN_REVIEW,
    ORIGIN_SUPERVISOR,
    TYPE_ANTI_PATTERN,
    TYPE_RULE,
    TYPE_SKILL,
)
from no_human.learning.retire import (
    find_superseded,
    is_templated_success_proposal,
    retirement_candidates,
    sweep_unconfirmed,
)


def _days_ago(n: int, *, iso: bool = False) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=n)
    if iso:
        return dt.isoformat()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def _set_created_at(store, mem_id, value):
    await store.db.execute(
        "UPDATE memories SET created_at = ? WHERE id = ?", (value, mem_id))
    await store.db.commit()


# --------------------------------------------------------------------------- #
# The 45-day sweep (AC1)                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sweep_archives_only_old_unconfirmed(store):
    old_unconfirmed = await store.add_memory(
        mem_type=TYPE_SKILL, title="old unconfirmed", content="x",
        confirmed=False)
    new_unconfirmed = await store.add_memory(
        mem_type=TYPE_SKILL, title="new unconfirmed", content="x",
        confirmed=False)
    old_confirmed = await store.add_memory(
        mem_type=TYPE_SKILL, title="old confirmed", content="x",
        confirmed=True)
    old_archived = await store.add_memory(
        mem_type=TYPE_SKILL, title="old archived", content="x",
        confirmed=False)

    old = _days_ago(60)
    for mid in (old_unconfirmed, old_confirmed, old_archived):
        await _set_created_at(store, mid, old)
    await store.archive_memory(old_archived, "pre-archived for the test")

    report = await sweep_unconfirmed(store, days=45)
    assert report.archived_ids == [old_unconfirmed]
    assert report.dry_run is False

    row = await store._fetchone(
        "SELECT archived FROM memories WHERE id = ?", (old_unconfirmed,))
    assert row["archived"] == 1
    for other in (new_unconfirmed, old_confirmed, old_archived):
        assert other not in report.archived_ids


@pytest.mark.asyncio
async def test_sweep_matches_both_created_at_formats(store):
    """Regression pin: the created_at comparison must run IN SQL. A Python-
    side ISO-string cutoff compare (`' ' < 'T'`) would mark every
    default-format row as expired regardless of its real age."""
    space_old = await store.add_memory(
        mem_type=TYPE_SKILL, title="space old", content="x", confirmed=False)
    iso_old = await store.add_memory(
        mem_type=TYPE_SKILL, title="iso old", content="x", confirmed=False)
    space_fresh = await store.add_memory(
        mem_type=TYPE_SKILL, title="space fresh", content="x", confirmed=False)

    await _set_created_at(store, space_old, _days_ago(60))
    await _set_created_at(store, iso_old, _days_ago(60, iso=True))
    # space_fresh keeps its natural just-inserted (SQLite default format,
    # space separator) created_at.

    report = await sweep_unconfirmed(store, days=45)
    assert set(report.archived_ids) == {space_old, iso_old}
    assert space_fresh not in report.archived_ids


@pytest.mark.asyncio
async def test_sweep_skips_null_created_at(store):
    """Fail closed: an unparseable/absent created_at is a refusal, not a
    sweep target — proven against a genuinely-old control row so the test
    cannot pass merely because nothing was old enough."""
    null_row = await store.add_memory(
        mem_type=TYPE_SKILL, title="null", content="x", confirmed=False)
    empty_row = await store.add_memory(
        mem_type=TYPE_SKILL, title="empty", content="x", confirmed=False)
    control_old = await store.add_memory(
        mem_type=TYPE_SKILL, title="control", content="x", confirmed=False)

    await store.db.execute(
        "UPDATE memories SET created_at = NULL WHERE id = ?", (null_row,))
    await store.db.execute(
        "UPDATE memories SET created_at = '' WHERE id = ?", (empty_row,))
    await store.db.commit()
    await _set_created_at(store, control_old, _days_ago(60))

    report = await sweep_unconfirmed(store, days=45)
    assert report.archived_ids == [control_old]


@pytest.mark.asyncio
async def test_sweep_never_archives_confirmed_rows(store):
    confirmed_id = await store.add_memory(
        mem_type=TYPE_SKILL, title="confirmed", content="x", confirmed=True)
    await _set_created_at(store, confirmed_id, _days_ago(400))

    report = await sweep_unconfirmed(store, days=45)
    assert confirmed_id not in report.archived_ids

    row = await store._fetchone(
        "SELECT archived, confirmed, last_used_at FROM memories WHERE id = ?",
        (confirmed_id,))
    assert row["archived"] == 0
    assert row["confirmed"] == 1
    assert row["last_used_at"] is None


@pytest.mark.asyncio
async def test_sweep_is_idempotent_and_reversible(store):
    dedupe_key = "sig:idempotent-test"
    mem_id = await store.add_memory(
        mem_type=TYPE_SKILL, title="x", content="y", confirmed=False,
        dedupe_key=dedupe_key)
    await _set_created_at(store, mem_id, _days_ago(60))

    count_before = (
        await store._fetchone("SELECT COUNT(*) AS n FROM memories"))["n"]

    r1 = await sweep_unconfirmed(store, days=45)
    assert r1.archived_ids == [mem_id]

    r2 = await sweep_unconfirmed(store, days=45)
    assert r2.archived_ids == []

    # The reversal the runbook documents.
    await store.db.execute(
        "UPDATE memories SET archived = 0 WHERE id = ?", (mem_id,))
    await store.db.commit()

    pending_ids = {m["id"] for m in await LearningQueue(store).pending()}
    assert mem_id in pending_ids
    assert await store.memory_dedupe_key_exists(dedupe_key) is True

    count_after = (
        await store._fetchone("SELECT COUNT(*) AS n FROM memories"))["n"]
    assert count_after == count_before


@pytest.mark.asyncio
async def test_sweep_respects_limit_and_source(store):
    ids = []
    for i in range(5):
        mid = await store.add_memory(
            mem_type=TYPE_SKILL, title=f"t{i}", content="x", confirmed=False)
        ids.append(mid)
    old = _days_ago(60)
    for mid in ids:
        await _set_created_at(store, mid, old)

    # A source="board" unconfirmed+old row — not producible through
    # `add_memory` (it refuses an unconfirmed non-proposed row by design), so
    # inserted directly, the same way the legacy stranded rows exist.
    board_id = uuid.uuid4().hex
    await store.db.execute(
        "INSERT INTO memories (id, type, title, content, source, confirmed, "
        "created_at) VALUES (?, ?, ?, ?, 'board', 0, ?)",
        (board_id, TYPE_SKILL, "board rule", "x", old))
    await store.db.commit()

    report = await sweep_unconfirmed(store, days=45, limit=2)
    assert len(report.archived_ids) == 2
    assert board_id not in report.archived_ids
    assert set(report.archived_ids) <= set(ids)

    row = await store._fetchone(
        "SELECT archived FROM memories WHERE id = ?", (board_id,))
    assert row["archived"] == 0


@pytest.mark.asyncio
async def test_sweep_rejects_nonpositive_days(store):
    with pytest.raises(ValueError):
        await sweep_unconfirmed(store, days=0)
    with pytest.raises(ValueError):
        await store.archive_unconfirmed_older_than(days=-1)


# --------------------------------------------------------------------------- #
# Supersede-on-confirm (AC3)                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_confirm_supersedes_near_duplicate(store):
    q = LearningQueue(store)
    old_id = await store.add_memory(
        mem_type=TYPE_RULE, title="Always run tests",
        content="run pytest before pushing", confirmed=True)
    new_id = await store.add_memory(
        mem_type=TYPE_RULE, title="Always run tests",
        content="run pytest before pushing", confirmed=False)

    assert await q.confirm(new_id) is True

    old_row = await store._fetchone(
        "SELECT archived, superseded_by FROM memories WHERE id = ?", (old_id,))
    assert old_row["archived"] == 1
    assert old_row["superseded_by"] == new_id

    new_row = await store._fetchone(
        "SELECT archived, superseded_by FROM memories WHERE id = ?", (new_id,))
    assert new_row["archived"] == 0
    assert new_row["superseded_by"] is None


@pytest.mark.asyncio
async def test_supersede_refuses_self_and_dead_targets(store):
    a = await store.add_memory(
        mem_type=TYPE_RULE, title="A", content="x", confirmed=True)
    b = await store.add_memory(
        mem_type=TYPE_RULE, title="B", content="x", confirmed=True)
    c = await store.add_memory(
        mem_type=TYPE_RULE, title="C", content="x", confirmed=True)

    assert await store.supersede_memory(a, a) is False

    await store.archive_memory(b, "pre-archived for the test")
    assert await store.supersede_memory(c, b) is False  # new_id (b) archived

    assert await store.supersede_memory(a, c) is True
    row = await store._fetchone(
        "SELECT superseded_by FROM memories WHERE id = ?", (a,))
    assert row["superseded_by"] == c

    d = await store.add_memory(
        mem_type=TYPE_RULE, title="D", content="x", confirmed=True)
    # `a` is already archived+superseded — a second supersede must refuse and
    # must not overwrite the existing pointer.
    assert await store.supersede_memory(a, d) is False
    row2 = await store._fetchone(
        "SELECT superseded_by FROM memories WHERE id = ?", (a,))
    assert row2["superseded_by"] == c


@pytest.mark.asyncio
async def test_supersede_respects_scope(store):
    global_id = await store.add_memory(
        mem_type=TYPE_RULE, title="Global rule", content="same text",
        confirmed=True)
    scoped_id = await store.add_memory(
        mem_type=TYPE_RULE, title="Global rule", content="same text",
        confirmed=True, project="/repo/a", project_scope="scope-a")

    global_row = await store.find_memory(global_id)
    dupes = await find_superseded(store, global_row)
    assert dupes == [], (
        "a global confirmed row must not supersede a project-scoped "
        "near-duplicate"
    )

    scoped_row = await store.find_memory(scoped_id)
    dupes2 = await find_superseded(store, scoped_row)
    assert dupes2 == []


# --------------------------------------------------------------------------- #
# SUGGEST-only retirement for active rows (AC2)                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_retirement_candidates_never_archive(store):
    confirmed_id = await store.add_memory(
        mem_type=TYPE_RULE, title="stale rule", content="x", confirmed=True)
    await store.db.execute(
        "UPDATE memories SET last_used_at = ? WHERE id = ?",
        (_days_ago(120, iso=True), confirmed_id))
    await store.db.commit()

    candidates = await retirement_candidates(store, days=90)
    ids = {c["id"] for c in candidates}
    assert confirmed_id in ids
    match = next(c for c in candidates if c["id"] == confirmed_id)
    assert match["never_used"] is False
    assert match["stale_days"] == 90

    row = await store._fetchone(
        "SELECT archived FROM memories WHERE id = ?", (confirmed_id,))
    assert row["archived"] == 0
    active_ids = {m["id"] for m in await LearningQueue(store).active()}
    assert confirmed_id in active_ids


# --------------------------------------------------------------------------- #
# Migration idempotence                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_superseded_by_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "migrate.db"
    s1 = await Store(db_path).connect()
    await s1.close()
    s2 = await Store(db_path).connect()
    try:
        cols = [r["name"] for r in await s2._fetchall(
            "PRAGMA table_info(memories)")]
        assert cols.count("superseded_by") == 1
    finally:
        await s2.close()


# --------------------------------------------------------------------------- #
# One-time flood-source triage (AC5)                                          #
# --------------------------------------------------------------------------- #


async def _seed_triage_fixture(store):
    template_ids = []
    for i in range(60):
        mid = await store.add_memory(
            mem_type=TYPE_SKILL,
            title=f"Approach that worked: task {i}"[:120],
            content=(f"Task 'task {i}' completed to a reviewable PR.\n"
                     "Consider capturing the successful approach as a "
                     "reusable skill."),
            source=SOURCE_PROPOSED, confirmed=False,
            dedupe_key=f"triage-tpl-{i}",
            evidence={"kind": "task_outcome", "task_id": f"t{i}",
                     "status": "awaiting_approval"},
        )
        assert mid is not None
        template_ids.append(mid)

    evidence_ids = []
    for i in range(8):
        mid = await store.add_memory(
            mem_type=TYPE_RULE if i % 2 == 0 else TYPE_ANTI_PATTERN,
            title=f"Repo rule {i}", content="always run the tests",
            source=SOURCE_PROPOSED, confirmed=False,
            origin=ORIGIN_REVIEW if i % 2 == 0 else ORIGIN_SUPERVISOR,
            dedupe_key=f"triage-ev-{i}",
            evidence={"kind": "review_finding", "task_id": f"r{i}"},
        )
        assert mid is not None
        evidence_ids.append(mid)

    confirmed_ids = []
    for i in range(5):
        mid = await store.add_memory(
            mem_type=TYPE_SKILL,
            title=f"Approach that worked: confirmed {i}"[:120],
            content="Consider capturing the successful approach as a "
                    "reusable skill.",
            source=SOURCE_PROPOSED, confirmed=True,
        )
        assert mid is not None
        confirmed_ids.append(mid)

    return template_ids, evidence_ids, confirmed_ids


@pytest.mark.asyncio
async def test_triage_selects_only_template_rows(store):
    """Predicate-level pin of AC5's counts (60 template + 8 evidence + 5
    confirmed): every templated row matches, none of the other 13 do."""
    template_ids, evidence_ids, confirmed_ids = await _seed_triage_fixture(store)

    pending = await LearningQueue(store).pending()
    matched = {m["id"] for m in pending if is_templated_success_proposal(m)}
    assert matched == set(template_ids)
    assert len(matched) == 60

    for eid in evidence_ids:
        row = await store.find_memory(eid)
        assert is_templated_success_proposal(row) is False
    for cid in confirmed_ids:
        row = await store.find_memory(cid)
        assert is_templated_success_proposal(row) is False


def _make_triage_cli_runner(db_path, home, monkeypatch):
    import no_human.cli.commands as cmd_mod
    import no_human.config as config_mod

    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        utility_model = "claude-haiku-4-5"
        data: dict = {}

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = db_path
    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    monkeypatch.setattr(config_mod, "NO_HUMAN_HOME", home)
    return CliRunner()


def test_triage_dry_run_then_apply_via_cli(tmp_path, monkeypatch):
    """AC5, end to end: dry run reports 60 and archives nothing; --apply
    archives exactly the 60, leaving the 8 evidence-bearing rows pending and
    all 5 confirmed rows untouched, with the triage marker on each archived
    row's content and a receipt listing the archived ids."""
    db_path = tmp_path / "triage.db"
    home = tmp_path / "home"

    async def _seed():
        async with Store(db_path) as s:
            return await _seed_triage_fixture(s)

    template_ids, evidence_ids, confirmed_ids = asyncio.run(_seed())

    runner = _make_triage_cli_runner(db_path, home, monkeypatch)
    from no_human.cli.commands import cli

    dry = runner.invoke(cli, ["learnings", "--triage-templated"],
                        catch_exceptions=False)
    assert dry.exit_code == 0
    assert "60" in dry.output

    async def _mid_counts():
        async with Store(db_path) as s:
            pending = await LearningQueue(s).pending()
            archived = await s._fetchall(
                "SELECT id FROM memories WHERE archived = 1")
            return pending, archived
    pending_after_dry, archived_after_dry = asyncio.run(_mid_counts())
    assert len(archived_after_dry) == 0, "dry run must archive nothing"
    assert len(pending_after_dry) == 68

    apply_result = runner.invoke(
        cli, ["learnings", "--triage-templated", "--apply"],
        catch_exceptions=False)
    assert apply_result.exit_code == 0

    async def _final():
        async with Store(db_path) as s:
            pending = await LearningQueue(s).pending()
            archived_rows = await s._fetchall(
                "SELECT id, content FROM memories WHERE archived = 1")
            confirmed_rows = await s._fetchall(
                "SELECT id, archived FROM memories WHERE confirmed = 1")
            return pending, archived_rows, confirmed_rows
    pending_final, archived_rows, confirmed_rows = asyncio.run(_final())

    assert {r["id"] for r in pending_final} == set(evidence_ids)
    assert len(pending_final) == 8
    assert {r["id"] for r in archived_rows} == set(template_ids)
    assert len(archived_rows) == 60
    assert all("[archived:" in r["content"] and "one-time triage" in r["content"]
              for r in archived_rows)
    assert len(confirmed_rows) == 5
    assert all(r["archived"] == 0 for r in confirmed_rows)
    assert {r["id"] for r in confirmed_rows} == set(confirmed_ids)

    receipts = list((home / "receipts").glob("learning-triage-*.json"))
    assert len(receipts) == 1
    payload = json.loads(receipts[0].read_text())
    assert set(payload["archived_ids"]) == set(template_ids)
    assert payload["before_pending"] == 68
    assert payload["after_pending"] == 8
