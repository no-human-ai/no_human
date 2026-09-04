"""Structured evidence + the controlled tag vocabulary (B3).

B1/B2 narrated a lesson's provenance into prose (`content`) and stored
whatever tags happened to reach the write — the utility model's TAGS line
verbatim, words mined from finding labels or the correction gist. B3 makes
both structural:

  * every proposal carries a machine-readable `evidence` record — what
    happened, in which task, citing the review/correction event — beside the
    prose that narrates it for the confirming human;
  * only tags from the reviewed vocabulary (`learning/vocab.py`) are stored;
    free tags are mapped to their canonical form or dropped, and the trigger
    surface widens to the alias family so the specific word still fires.

What must stay true:
  * evidence round-trips through the store and names the citable event
    (task id + attempt/round for a review, task id + ts for a correction),
  * evidence is INSIDE the PII gate, and so are the raw pre-vocabulary tags —
    sanitation must never double as redaction,
  * a row written before the columns existed (NULL evidence, NULL scope)
    still loads everywhere, and a pre-B3 database file migrates on connect,
  * the vocabulary is unambiguous by construction and extension is loud.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.learning import LearningQueue, ORIGIN_SUPERVISOR, TYPE_RULE
from no_human.learning.corrections import CorrectionRecord, cluster_corrections
from no_human.learning.triggers import filter_triggered, memory_is_triggered
from no_human.learning.vocab import (
    PROVENANCE_TAGS,
    TAG_VOCABULARY,
    sanitize_tags,
    trigger_terms,
)

REPO = "/tmp/repo-b3"

_LESSON = (
    "TYPE: rule\n"
    "TITLE: Run the suite with the venv interpreter\n"
    "LESSON: This repo's tests need the .venv interpreter; the bare `python` "
    "on PATH is an older build and its failures are not regressions.\n"
    "TAGS: pytest, venv, interpreter\n"
)

_MSG_A = (
    "The agent stopped at a failing `python` invocation without checking the "
    "venv interpreter. Run the suite with .venv/bin/python first."
)
_MSG_B = (
    "The agent stopped again at a failing `python` invocation. Use the venv "
    "interpreter (.venv/bin/python) and re-run the suite first."
)


def _cluster():
    clusters = cluster_corrections([
        CorrectionRecord(task_id="aaaaaaaabbbb", project=REPO,
                         message=_MSG_A, ts=11.0),
        CorrectionRecord(task_id="ccccccccdddd", project=REPO,
                         message=_MSG_B, ts=22.0),
    ])
    assert len(clusters) == 1
    return clusters[0]


def _distiller(reply=_LESSON):
    async def _fn(_prompt):
        return reply
    return _fn


async def _task(store, title="Pin the CI images"):
    t = Task.new(title, repo_path=REPO)
    await store.create_task(t)
    return t


_FINDING = {
    "label": "image pinning",
    "evidence": "ci/images.yaml:12 pins `latest`, not a digest",
    "comment": "pin the digest",
    "file": "ci/images.yaml",
    "line": 12,
}


# ── evidence round-trips and cites the event ──────────────────────────────── #

@pytest.mark.asyncio
async def test_review_proposal_carries_structured_evidence(store):
    q = LearningQueue(store)
    t = await _task(store)
    await q.propose_from_review(
        t, findings=[_FINDING], attempt=2, review_round=1,
        distill=_distiller())

    row = (await q.pending())[0]
    ev = json.loads(row["evidence"])
    assert ev["kind"] == "review_finding"
    # In which task, at which point — the citable coordinates of the event.
    assert ev["task_id"] == t.id
    assert ev["attempt"] == 2
    assert ev["review_round"] == 1
    # What happened, with the finding itself as fields rather than prose.
    assert ev["findings"] == [{
        "label": "image pinning",
        "file": "ci/images.yaml",
        "line": 12,
        "evidence": "ci/images.yaml:12 pins `latest`, not a digest",
    }]


@pytest.mark.asyncio
async def test_correction_proposal_evidence_cites_the_correction_events(store):
    q = LearningQueue(store)
    await q.propose_from_corrections(_cluster(), distill=_distiller())

    row = (await q.pending())[0]
    ev = json.loads(row["evidence"])
    assert ev["kind"] == "supervisor_correction"
    assert ev["count"] == 2
    assert ev["task_ids"] == ["aaaaaaaabbbb", "ccccccccdddd"]
    # (task_id, ts) is exactly the key that finds each `supervisor_decision`
    # row in `task_events` again — the citation, not a paraphrase of one.
    assert {(c["task_id"], c["ts"]) for c in ev["corrections"]} == {
        ("aaaaaaaabbbb", 11.0), ("ccccccccdddd", 22.0)}
    assert all(".venv/bin/python" in c["message"] for c in ev["corrections"])


@pytest.mark.asyncio
async def test_outcome_proposal_carries_structured_evidence(store):
    # Memory lifecycle C gates the per-success templated proposal behind
    # `propose_on_success` (default off) — opt back in here since this test's
    # claim is about the evidence shape, not about the flood-control default.
    q = LearningQueue(store, propose_on_success=True)
    t = await _task(store, title="Ship the exporter")
    await q.propose_from_outcome(t, status=TaskStatus.AWAITING_APPROVAL)

    row = (await q.pending())[0]
    ev = json.loads(row["evidence"])
    assert ev["kind"] == "task_outcome"
    assert ev["task_id"] == t.id
    assert ev["status"] == TaskStatus.AWAITING_APPROVAL.value


# ── evidence is inside the PII gate ───────────────────────────────────────── #

@pytest.mark.asyncio
async def test_the_evidence_field_is_inside_the_pii_gate(store, monkeypatch):
    """The gate must read the WHOLE row it guards. Evidence quotes the
    corrections verbatim, so it goes through `contains_pii` with everything
    else — asserted on the gate's actual arguments, because a field the gate
    never sees cannot be protected by tests that only vary the other fields."""
    import no_human.learning.queue as queue_mod

    seen: list[str] = []
    real = queue_mod.contains_pii

    def _spy(*parts):
        seen.extend(p for p in parts if p)
        return real(*parts)

    monkeypatch.setattr(queue_mod, "contains_pii", _spy)
    q = LearningQueue(store)
    assert await q.propose_from_corrections(
        _cluster(), distill=_distiller()) is not None
    # The evidence's own summary line reaches the gate — a string that exists
    # ONLY in the structured evidence, never in title/content/tags.
    assert any("issued the same correction" in s for s in seen), seen


# ── old rows still load (the migration proof) ─────────────────────────────── #

@pytest.mark.asyncio
async def test_a_row_without_evidence_or_scope_still_loads(store):
    """Rows written before B3/B4 carry NULL in both new columns — exactly what
    ADD COLUMN backfills — and must keep flowing through the queue, the
    project-scoped recall, and the confirm round-trip."""
    await store.add_memory(
        mem_type=TYPE_RULE, title="legacy", content="pre-B3 row",
        project=REPO, source="proposed", confirmed=False)

    row = (await LearningQueue(store).pending())[0]
    assert row["evidence"] is None
    assert row["project_scope"] is None
    # Scoped recall still surfaces it by path (B4's legacy matching).
    scoped = await store.list_memories(
        confirmed=False, project=REPO, scope="prj:" + "0" * 64)
    assert [m["title"] for m in scoped] == ["legacy"]
    assert await store.confirm_memory(row["id"])


@pytest.mark.asyncio
async def test_a_pre_b3_database_file_migrates_on_connect(tmp_path):
    """A database created before the columns existed gains them on the next
    connect, and the rows written under the OLD schema still load. Simulated
    by dropping the columns a fresh Store created — the resulting table IS the
    pre-B3 shape — then reconnecting."""
    db_path = tmp_path / "old.db"
    s = await Store(db_path).connect()
    for col in ("evidence", "project_scope"):
        await s.db.execute(f"ALTER TABLE memories DROP COLUMN {col}")
    await s.db.execute(
        "INSERT INTO memories (id, type, title, content, project, source, "
        "confirmed) VALUES ('m1', 'rule', 'old row', 'written pre-B3', "
        "?, 'proposed', 0)", (REPO,))
    await s.db.commit()
    await s.close()

    s = await Store(db_path).connect()  # ← the migration under test
    try:
        rows = await s.list_memories(confirmed=False)
        assert [r["title"] for r in rows] == ["old row"]
        assert rows[0]["evidence"] is None
        assert rows[0]["project_scope"] is None
        # And the migrated table accepts new-shape writes beside the old row.
        assert await s.add_memory(
            mem_type="rule", title="new row", content="post-B3",
            project=REPO, evidence={"kind": "task_outcome", "what": "x",
                                    "task_id": "t"},
            project_scope="prj:" + "a" * 64) is not None
        assert len(await s.list_memories(confirmed=False)) == 2
    finally:
        await s.close()


# ── the human confirm surface shows what changed ──────────────────────────── #

def _cli_runner(db_path, monkeypatch):
    """`nh learnings` against a seeded store — the same patching idiom as
    tests/test_cli_commands.py: config and the auth assertion are stubbed at
    the point of USE, nothing touches a real credential."""
    import no_human.cli.commands as cmd_mod
    from click.testing import CliRunner

    class _Cfg:
        data: dict = {}

        def get(self, key, default=None):
            return self.data.get(key, default)

    _Cfg.db_path = db_path
    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    return CliRunner()


def test_nh_learnings_renders_evidence_and_scope(tmp_path, monkeypatch):
    """The confirm queue shows the B3 evidence line and the B4 scope identity
    on the same screen as the confirm command — the human deciding sees what
    happened and how far the rule reaches."""
    import asyncio

    from no_human.cli.commands import cli

    db = tmp_path / "cli.db"

    async def _seed():
        async with Store(db) as s:
            q = LearningQueue(s)
            await q.propose_from_corrections(_cluster(), distill=_distiller())
            # Stamp a scope by hand: the fixture repo path has no real remote.
            await s.stamp_project_scope(REPO, "prj:" + "ab" * 32)

    asyncio.run(_seed())
    result = _cli_runner(db, monkeypatch).invoke(cli, ["learnings"])
    assert result.exit_code == 0, result.output
    assert "evidence:" in result.output
    assert "supervisor correction x2" in result.output
    assert "aaaaaaaa" in result.output          # the cited task
    assert "prj:abababab" in result.output      # the truncated scope identity
    assert REPO in result.output                # the readable checkout path


def test_nh_learnings_still_renders_a_row_without_evidence(tmp_path, monkeypatch):
    """Old rows print no evidence line rather than a guessed one — and still
    confirm. The B3 wiring must not break the queue for pre-B3 data."""
    import asyncio

    from no_human.cli.commands import cli

    db = tmp_path / "cli-legacy.db"

    async def _seed():
        async with Store(db) as s:
            await s.add_memory(
                mem_type=TYPE_RULE, title="legacy proposal",
                content="written before B3", project=REPO,
                source="proposed", confirmed=False)

    asyncio.run(_seed())
    runner = _cli_runner(db, monkeypatch)
    listed = runner.invoke(cli, ["learnings"])
    assert listed.exit_code == 0, listed.output
    assert "legacy proposal" in listed.output
    assert "evidence:" not in listed.output


# ── the controlled vocabulary ─────────────────────────────────────────────── #

def test_free_tags_map_to_the_vocabulary_or_drop():
    assert sanitize_tags(["pytest", "venv", "interpreter"]) == [
        "test", "environment"]
    assert sanitize_tags(["images", "ci", "digest"]) == [
        "container", "pipeline"]
    # A tag that maps to nothing is dropped, not stored — an unmappable tag
    # is either noise or a request to extend the reviewed constant.
    assert sanitize_tags(["blorptastic", "qzx"]) == []
    # Canonical tags and provenance tags pass through unchanged.
    assert sanitize_tags(["test", "review", "supervisor"]) == [
        "test", "review", "supervisor"]


def test_the_vocabulary_is_unambiguous_by_construction():
    """An alias under two canonical tags would silently re-map stored data;
    the import-time index refuses it. Exercised by rebuilding the index over a
    deliberately broken vocabulary — non-vacuity for the check itself."""
    import no_human.learning.vocab as vocab_mod

    assert vocab_mod._ALIAS_TO_TAG  # the real one built without complaint
    broken = dict(TAG_VOCABULARY)
    broken["shadow"] = frozenset({"pytest"})  # already an alias of "test"
    original = vocab_mod.TAG_VOCABULARY
    try:
        vocab_mod.TAG_VOCABULARY = broken
        with pytest.raises(ValueError, match="ambiguous"):
            vocab_mod._alias_index()
    finally:
        vocab_mod.TAG_VOCABULARY = original


def test_provenance_tags_are_in_the_vocabulary_but_alias_nothing():
    for tag in PROVENANCE_TAGS:
        assert sanitize_tags([tag]) == [tag]
        assert tag not in TAG_VOCABULARY


# ── canonical tags trigger on their alias family ──────────────────────────── #

def test_a_canonical_tag_triggers_on_the_specific_word():
    """Storing `environment` instead of `venv` must not lose the trigger match
    on a task that says "venv" — the vocabulary widened the trigger surface,
    it did not narrow it."""
    mem = {"tags": json.dumps(["environment"])}
    assert memory_is_triggered(mem, "the tests fail unless run in the venv")
    assert memory_is_triggered(mem, "fix the ENVIRONMENT bootstrap")
    assert not memory_is_triggered(mem, "update the readme badges")


def test_a_tag_outside_the_vocabulary_still_triggers_literally():
    """Pre-B3 rows (and the outcome path's enum tags) keep their behaviour:
    a literal substring match on the tag's own value."""
    assert "blorptastic" not in {a for s in TAG_VOCABULARY.values() for a in s}
    mem = {"tags": json.dumps(["blorptastic"])}
    assert memory_is_triggered(mem, "the blorptastic module again")
    assert not memory_is_triggered(mem, "the tests fail in the venv")


def test_trigger_terms_expand_only_canonical_tags():
    assert "venv" in trigger_terms("environment")
    assert trigger_terms("blorptastic") == frozenset({"blorptastic"})


@pytest.mark.asyncio
async def test_end_to_end_a_stored_lesson_fires_for_the_aliased_task_text(store):
    """The whole chain: free distiller tags → canonical stored tags → a future
    task whose text uses the SPECIFIC word still receives the lesson."""
    q = LearningQueue(store)
    await q.propose_from_corrections(_cluster(), distill=_distiller())
    row = (await q.pending())[0]
    assert await q.confirm(row["id"])
    active = await q.active()
    hit = filter_triggered(active, "the suite must run under the venv on CI")
    assert [m["id"] for m in hit] == [row["id"]]
    miss = filter_triggered(active, "rewrite the readme intro")
    assert miss == []


def test_generic_aliases_still_sanitize_but_never_trigger():
    # TAG_VOCABULARY serves two mechanisms with different risk profiles:
    # sanitize matches raw tags EXACTLY (an LLM tag "env" is unambiguous),
    # triggers match task text by SUBSTRING (where "env"/"path"/"json"/
    # "request" ride in unrelated tasks). The generic aliases must keep
    # mapping on the sanitize side and vanish from the trigger side.
    from no_human.learning.vocab import sanitize_tags, trigger_terms

    assert sanitize_tags(["env"]) == ["environment"]
    assert sanitize_tags(["path"]) == ["environment"]
    assert sanitize_tags(["request"]) == ["api"]
    assert sanitize_tags(["json"]) == ["api"]
    for generic in ("env", "path"):
        assert generic not in trigger_terms("environment")
    for generic in ("json", "request", "requests"):
        assert generic not in trigger_terms("api")
    assert "venv" in trigger_terms("environment")
    assert "endpoint" in trigger_terms("api")
    # Provenance tags contribute no trigger surface at all.
    assert trigger_terms("review") == frozenset()
    assert trigger_terms("supervisor") == frozenset()
