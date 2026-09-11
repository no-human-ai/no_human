"""Fixture-DB tests for `scripts/memory_triage_runbook.py` (refile of
e6cf94c4, rescoped). Predecessor e6cf94c4 died writing an uncalled helper;
`test_every_public_helper_is_reached_from_main` makes that checklist item
mechanical rather than a review-time reminder.

The script is loaded by path (it lives in `scripts/`, not the package),
exactly as `tests/test_review_round_value.py` loads its sibling script.

Every test that runs `--execute` or `--dry-run` goes through the `work_db`
fixture: a fresh copy of the committed `testdata/memory_triage_fixture.db`
in `tmp_path`, re-stamped by `stamp_ages()` so its age-sensitive rows read
relative to `now` regardless of when the suite runs (see
`tests/_memory_triage_fixture.py` for the time-bomb this avoids). The
committed fixture file itself is never opened for write by any test.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

from no_human.core.db import Store
from no_human.learning.retire import (
    DEFAULT_ARCHIVE_UNCONFIRMED_DAYS,
    is_templated_success_proposal,
)
from tests._memory_triage_fixture import (
    DEFAULT_FIXTURE_PATH as FIXTURE_PATH,
    FRESH_COUNT,
    STALE_COUNT,
    T_CONFIRMED_COUNT,
    T_ORGANIC_COUNT,
    T_TEMPLATED_COUNT,
    TITLE_FRESH_PREFIX,
    TITLE_NON_PROPOSED_SOURCE,
    TITLE_STALE_PREFIX,
    TITLE_SUPERSEDED_OLD,
    TITLE_T_CONFIRMED_PREFIX,
    TITLE_T_ORGANIC_PREFIX,
    TITLE_T_TEMPLATED_PREFIX,
    TITLE_UNPARSEABLE,
    build_templated_only_fixture,
    default_ages,
    stamp_ages,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "memory_triage_runbook.py"

EXPECTED_CLASSES = {
    "total",
    "confirmed_active",
    "unconfirmed_pending",
    "unconfirmed_stale_over_window",
    "templated_success_proposal",
    "archived",
    "superseded",
}


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "_nh_memory_triage_runbook", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


triage = _load_script()


# --------------------------------------------------------------------------- #
# Helpers -- direct SQL / sqlite3, deliberately independent of the script's
# own class_counts() so the "expected" side of a test can never be
# tautological.
# --------------------------------------------------------------------------- #


@pytest.fixture
def work_db(tmp_path, monkeypatch):
    dest = tmp_path / "work.db"
    shutil.copy(FIXTURE_PATH, dest)
    stamp_ages(dest, default_ages())
    monkeypatch.chdir(tmp_path)
    return dest


@pytest.fixture
def templated_db(tmp_path, monkeypatch):
    dest = tmp_path / "templated.db"
    asyncio.run(build_templated_only_fixture(dest))
    monkeypatch.chdir(tmp_path)
    return dest


def _title_ids(db_path: Path, titles: list[str]) -> list[str]:
    con = sqlite3.connect(db_path)
    try:
        out = []
        for title in titles:
            row = con.execute(
                "SELECT id FROM memories WHERE title = ?", (title,)
            ).fetchone()
            assert row is not None, f"fixture missing row titled {title!r}"
            out.append(row[0])
        return out
    finally:
        con.close()


def _snapshot(db_path: Path):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT id, archived, superseded_by, content FROM memories "
            "ORDER BY id"
        ).fetchall()
    finally:
        con.close()


def _direct_counts(db_path: Path, days: int) -> dict:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        counts = {
            "total": con.execute(
                "SELECT COUNT(*) AS n FROM memories").fetchone()["n"],
            "confirmed_active": con.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE confirmed = 1 "
                "AND (archived IS NULL OR archived = 0)").fetchone()["n"],
            "unconfirmed_pending": con.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE confirmed = 0 "
                "AND source = 'proposed' "
                "AND (archived IS NULL OR archived = 0)").fetchone()["n"],
            "unconfirmed_stale_over_window": con.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE confirmed = 0 "
                "AND source = 'proposed' "
                "AND (archived IS NULL OR archived = 0) "
                "AND created_at IS NOT NULL AND created_at != '' "
                "AND datetime(created_at) IS NOT NULL "
                "AND datetime(created_at) <= datetime('now', ?)",
                (f"-{days} days",)).fetchone()["n"],
            "archived": con.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE archived = 1"
            ).fetchone()["n"],
            "superseded": con.execute(
                "SELECT COUNT(*) AS n FROM memories "
                "WHERE superseded_by IS NOT NULL").fetchone()["n"],
        }
        unconfirmed_rows = con.execute(
            "SELECT * FROM memories WHERE confirmed = 0 "
            "AND (archived IS NULL OR archived = 0) "
            "AND (quarantined IS NULL OR quarantined = 0)").fetchall()
        counts["templated_success_proposal"] = sum(
            1 for row in unconfirmed_rows
            if is_templated_success_proposal(dict(row)))
        return counts
    finally:
        con.close()


def _read_event(cwd: Path) -> dict:
    return json.loads((cwd / "repair_event.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# --dry-run                                                                    #
# --------------------------------------------------------------------------- #


def test_dry_run_prints_per_class_counts_and_targets(work_db, capsys):
    expected = _direct_counts(work_db, DEFAULT_ARCHIVE_UNCONFIRMED_DAYS)
    stale_ids = _title_ids(
        work_db, [f"{TITLE_STALE_PREFIX}{i}" for i in range(1, STALE_COUNT + 1)])

    rc = triage.main(["--dry-run", str(work_db)])

    assert rc == 0
    out = capsys.readouterr().out
    for cls, count in expected.items():
        assert f"{cls}: {count}" in out, (cls, count, out)
    for mem_id in stale_ids:
        assert mem_id[:8] in out


def test_dry_run_writes_nothing(work_db):
    before_hash = hashlib.sha256(work_db.read_bytes()).hexdigest()
    before_stat = work_db.stat()
    before_snapshot = _snapshot(work_db)

    rc = triage.main(["--dry-run", str(work_db)])

    assert rc == 0
    after_stat = work_db.stat()
    assert hashlib.sha256(work_db.read_bytes()).hexdigest() == before_hash
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_size == before_stat.st_size
    assert _snapshot(work_db) == before_snapshot

    sidecars = [
        p for p in work_db.parent.iterdir()
        if p.name != work_db.name
        and (p.name.endswith("-wal") or p.name.endswith("-shm")
             or p.name.endswith(".tmp"))
    ]
    assert sidecars == []
    assert not (Path.cwd() / "repair_event.json").exists()


# --------------------------------------------------------------------------- #
# CLI usage / guard                                                            #
# --------------------------------------------------------------------------- #


def test_execute_requires_db_path(work_db):
    with pytest.raises(SystemExit) as exc:
        triage.main(["--execute"])
    assert exc.value.code == 2

    with pytest.raises(SystemExit) as exc:
        triage.main([])
    assert exc.value.code == 2

    with pytest.raises(SystemExit) as exc:
        triage.main(["--dry-run", "--execute", str(work_db)])
    assert exc.value.code == 2


def test_execute_refuses_live_no_human_home(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("NO_HUMAN_HOME", str(fake_home))
    live_db = fake_home / "no_human.db"
    shutil.copy(FIXTURE_PATH, live_db)
    before_hash = hashlib.sha256(live_db.read_bytes()).hexdigest()

    rc = triage.main(["--execute", str(live_db)])

    assert rc != 0
    err = capsys.readouterr().err
    assert str(live_db.resolve()) in err
    assert hashlib.sha256(live_db.read_bytes()).hexdigest() == before_hash

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    link = outside_dir / "via_symlink.db"
    link.symlink_to(live_db)

    rc2 = triage.main(["--execute", str(link)])

    assert rc2 != 0
    assert hashlib.sha256(live_db.read_bytes()).hexdigest() == before_hash


# --------------------------------------------------------------------------- #
# --execute                                                                    #
# --------------------------------------------------------------------------- #


def test_execute_archives_only_stale_unconfirmed_proposals(work_db):
    stale_ids = set(_title_ids(
        work_db, [f"{TITLE_STALE_PREFIX}{i}" for i in range(1, STALE_COUNT + 1)]))
    fresh_ids = _title_ids(
        work_db, [f"{TITLE_FRESH_PREFIX}{i}" for i in range(1, FRESH_COUNT + 1)])
    non_proposed_id = _title_ids(work_db, [TITLE_NON_PROPOSED_SOURCE])[0]
    unparseable_id = _title_ids(work_db, [TITLE_UNPARSEABLE])[0]

    rc = triage.main(["--execute", str(work_db)])
    assert rc == 0

    event = _read_event(Path.cwd())
    assert set(event["archived_ids"]) == stale_ids

    con = sqlite3.connect(work_db)
    con.row_factory = sqlite3.Row
    try:
        for mem_id in [*fresh_ids, non_proposed_id, unparseable_id]:
            row = con.execute(
                "SELECT archived FROM memories WHERE id = ?", (mem_id,)
            ).fetchone()
            assert row["archived"] == 0, mem_id
    finally:
        con.close()


def test_execute_archives_no_confirmed_rows(work_db):
    con = sqlite3.connect(work_db)
    con.row_factory = sqlite3.Row
    try:
        before_confirmed = {
            r["id"]: r["archived"] for r in con.execute(
                "SELECT id, archived FROM memories WHERE confirmed = 1")}
    finally:
        con.close()

    rc = triage.main(["--execute", str(work_db)])
    assert rc == 0

    con = sqlite3.connect(work_db)
    con.row_factory = sqlite3.Row
    try:
        after_confirmed = {
            r["id"]: r["archived"] for r in con.execute(
                "SELECT id, archived FROM memories WHERE confirmed = 1")}
    finally:
        con.close()

    assert after_confirmed == before_confirmed

    event = _read_event(Path.cwd())
    assert (event["after_counts"]["confirmed_active"]
            == event["before_counts"]["confirmed_active"])


def test_execute_is_reversible_and_deletes_nothing(work_db):
    # NOT an async test: `triage.main()` below calls `asyncio.run()`
    # internally (it is a synchronous CLI entry point), which raises if
    # called from inside a running event loop -- so this test stays
    # synchronous and opens its own loop only for the `Store` reversal
    # calls at the end, via `asyncio.run()`.
    con = sqlite3.connect(work_db)
    try:
        before_count = con.execute(
            "SELECT COUNT(*) FROM memories").fetchone()[0]
        before_ids = {r[0] for r in con.execute("SELECT id FROM memories")}
    finally:
        con.close()

    superseded_old_id = _title_ids(work_db, [TITLE_SUPERSEDED_OLD])[0]
    con = sqlite3.connect(work_db)
    try:
        prior_superseded_by = con.execute(
            "SELECT superseded_by FROM memories WHERE id = ?",
            (superseded_old_id,)).fetchone()[0]
    finally:
        con.close()
    assert prior_superseded_by is not None

    rc = triage.main(["--execute", str(work_db)])
    assert rc == 0

    con = sqlite3.connect(work_db)
    try:
        after_count = con.execute(
            "SELECT COUNT(*) FROM memories").fetchone()[0]
        after_ids = {r[0] for r in con.execute("SELECT id FROM memories")}
    finally:
        con.close()
    assert after_count == before_count
    assert after_ids == before_ids

    event = _read_event(Path.cwd())
    archived_ids = event["archived_ids"]
    assert archived_ids

    con = sqlite3.connect(work_db)
    con.row_factory = sqlite3.Row
    try:
        for mem_id in archived_ids:
            row = con.execute(
                "SELECT archived FROM memories WHERE id = ?",
                (mem_id,)).fetchone()
            assert row["archived"] == 1
        unchanged = con.execute(
            "SELECT superseded_by FROM memories WHERE id = ?",
            (superseded_old_id,)).fetchone()["superseded_by"]
    finally:
        con.close()
    assert unchanged == prior_superseded_by

    async def _unarchive_all():
        store = await Store(work_db).connect()
        try:
            for mem_id in archived_ids:
                ok = await store.unarchive_memory(mem_id)
                assert ok, mem_id
        finally:
            await store.close()

    asyncio.run(_unarchive_all())

    con = sqlite3.connect(work_db)
    con.row_factory = sqlite3.Row
    try:
        for mem_id in archived_ids:
            row = con.execute(
                "SELECT archived FROM memories WHERE id = ?",
                (mem_id,)).fetchone()
            assert row["archived"] == 0
    finally:
        con.close()


def test_repair_event_json_schema(work_db):
    rc = triage.main(["--execute", str(work_db)])
    assert rc == 0

    event_path = Path.cwd() / "repair_event.json"
    assert event_path.exists()
    doc = json.loads(event_path.read_text(encoding="utf-8"))

    assert set(doc) >= {"before_counts", "after_counts", "timestamps", "policy"}
    assert doc["policy"] == "LANDED-lifecycle-C-45day"

    before_keys = set(doc["before_counts"])
    after_keys = set(doc["after_counts"])
    assert before_keys == after_keys == EXPECTED_CLASSES

    for value in [*doc["before_counts"].values(), *doc["after_counts"].values()]:
        assert isinstance(value, int)

    start = datetime.fromisoformat(doc["timestamps"]["start"])
    end = datetime.fromisoformat(doc["timestamps"]["end"])
    assert start <= end

    assert doc["after_counts"]["total"] == doc["before_counts"]["total"]
    assert (doc["after_counts"]["archived"] - doc["before_counts"]["archived"]
            == len(doc["archived_ids"]))


def test_docstring_documents_modes_and_policy():
    doc = triage.__doc__
    assert doc
    assert "--dry-run" in doc
    assert "--execute" in doc
    assert "LANDED-lifecycle-C-45day" in doc
    assert "reversible" in doc.lower() or "reversal" in doc.lower()


def test_every_public_helper_is_reached_from_main():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    functions = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def names_referenced(node):
        names = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                names.add(sub.attr)
        return names

    reachable = set()
    frontier = ["main"]
    while frontier:
        name = frontier.pop()
        if name in reachable or name not in functions:
            continue
        reachable.add(name)
        for referenced in names_referenced(functions[name]):
            if referenced in functions and referenced not in reachable:
                frontier.append(referenced)

    declared = {name for name in functions if not name.startswith("_test")}
    unreached = declared - reachable
    assert not unreached, f"unreachable helper(s) from main(): {sorted(unreached)}"


def test_uses_landed_window_constant():
    parser = triage.build_parser()
    assert parser.get_default("days") == DEFAULT_ARCHIVE_UNCONFIRMED_DAYS


def test_execute_twice_is_a_noop_second_time(work_db):
    rc1 = triage.main(["--execute", str(work_db)])
    assert rc1 == 0
    event1 = _read_event(Path.cwd())
    assert event1["archived_ids"]

    rc2 = triage.main(["--execute", str(work_db)])
    assert rc2 == 0
    event2 = _read_event(Path.cwd())
    assert event2["archived_ids"] == []
    assert event2["before_counts"] == event2["after_counts"]


# --------------------------------------------------------------------------- #
# --templated-only                                                             #
# --------------------------------------------------------------------------- #


def test_templated_only_execute_archives_exactly_the_class(templated_db):
    templated_ids = set(_title_ids(
        templated_db,
        [f"{TITLE_T_TEMPLATED_PREFIX}{i}"
         for i in range(1, T_TEMPLATED_COUNT + 1)]))

    rc = triage.main(["--templated-only", "--execute", str(templated_db)])
    assert rc == 0

    con = sqlite3.connect(templated_db)
    try:
        archived_count = con.execute(
            "SELECT COUNT(*) FROM memories WHERE archived = 1"
        ).fetchone()[0]
        archived_ids_direct = {
            r[0] for r in con.execute(
                "SELECT id FROM memories WHERE archived = 1")}
    finally:
        con.close()
    assert archived_count == T_TEMPLATED_COUNT
    assert archived_ids_direct == templated_ids

    event = _read_event(Path.cwd())
    assert set(event["archived_ids"]) == templated_ids
    assert event["refused_ids"] == []


def test_templated_only_leaves_organic_proposals_untouched(templated_db):
    organic_ids = set(_title_ids(
        templated_db,
        [f"{TITLE_T_ORGANIC_PREFIX}{i}" for i in range(1, T_ORGANIC_COUNT + 1)]))
    before = {row[0]: row for row in _snapshot(templated_db)
              if row[0] in organic_ids}
    assert len(before) == T_ORGANIC_COUNT

    rc = triage.main(["--templated-only", "--execute", str(templated_db)])
    assert rc == 0

    after = {row[0]: row for row in _snapshot(templated_db)
              if row[0] in organic_ids}
    assert after == before


def test_templated_only_leaves_confirmed_rules_untouched(templated_db):
    confirmed_ids = set(_title_ids(
        templated_db,
        [f"{TITLE_T_CONFIRMED_PREFIX}{i}"
         for i in range(1, T_CONFIRMED_COUNT + 1)]))
    before = {row[0]: row for row in _snapshot(templated_db)
              if row[0] in confirmed_ids}
    assert len(before) == T_CONFIRMED_COUNT

    rc = triage.main(["--templated-only", "--execute", str(templated_db)])
    assert rc == 0

    after = {row[0]: row for row in _snapshot(templated_db)
              if row[0] in confirmed_ids}
    assert after == before

    event = _read_event(Path.cwd())
    assert (event["after_counts"]["confirmed_active"]
            == event["before_counts"]["confirmed_active"]
            == T_CONFIRMED_COUNT)


def test_templated_only_reversal_sql_restores_state(templated_db):
    before = _snapshot(templated_db)

    rc = triage.main(["--templated-only", "--execute", str(templated_db)])
    assert rc == 0

    event = _read_event(Path.cwd())
    assert event["archived_ids"]

    con = sqlite3.connect(templated_db)
    try:
        con.executescript(event["reversal_sql"])
        con.commit()
    finally:
        con.close()

    after = _snapshot(templated_db)
    assert len(after) == len(before)
    assert after == before


def test_templated_only_dry_run_reports_count(templated_db, capsys):
    rc = triage.main(["--templated-only", "--dry-run", str(templated_db)])
    assert rc == 0

    out = capsys.readouterr().out
    assert (f"Identified {T_TEMPLATED_COUNT} templated success proposals "
            "ready for archival" in out)


def test_templated_only_dry_run_writes_nothing(templated_db):
    before_hash = hashlib.sha256(templated_db.read_bytes()).hexdigest()
    before_stat = templated_db.stat()
    before_snapshot = _snapshot(templated_db)

    rc = triage.main(["--templated-only", "--dry-run", str(templated_db)])

    assert rc == 0
    after_stat = templated_db.stat()
    assert hashlib.sha256(templated_db.read_bytes()).hexdigest() == before_hash
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_size == before_stat.st_size
    assert _snapshot(templated_db) == before_snapshot

    sidecars = [
        p for p in templated_db.parent.iterdir()
        if p.name != templated_db.name
        and (p.name.endswith("-wal") or p.name.endswith("-shm")
             or p.name.endswith(".tmp"))
    ]
    assert sidecars == []
    assert not (Path.cwd() / "repair_event.json").exists()


def test_templated_only_respects_limit(templated_db, capsys):
    rc = triage.main(
        ["--templated-only", "--execute", str(templated_db),
         "--limit", "4"])
    assert rc == 0

    con = sqlite3.connect(templated_db)
    try:
        archived_count = con.execute(
            "SELECT COUNT(*) FROM memories WHERE archived = 1"
        ).fetchone()[0]
    finally:
        con.close()
    assert archived_count == 4

    event = _read_event(Path.cwd())
    assert len(event["archived_ids"]) == 4
    assert event["truncated"] is True

    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "--limit=4" in out


def test_templated_only_refuses_live_no_human_home(tmp_path, monkeypatch):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("NO_HUMAN_HOME", str(fake_home))
    live_db = fake_home / "no_human.db"
    asyncio.run(build_templated_only_fixture(live_db))
    before_hash = hashlib.sha256(live_db.read_bytes()).hexdigest()

    rc_dry = triage.main(["--templated-only", "--dry-run", str(live_db)])
    assert rc_dry != 0
    assert hashlib.sha256(live_db.read_bytes()).hexdigest() == before_hash

    rc_exec = triage.main(["--templated-only", "--execute", str(live_db)])
    assert rc_exec != 0
    assert hashlib.sha256(live_db.read_bytes()).hexdigest() == before_hash
