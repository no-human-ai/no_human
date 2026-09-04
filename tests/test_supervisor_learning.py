"""Supervisor corrections become learning proposals (B2).

The supervisor fires every `check_every` tool calls and its `correct` verdicts
are persisted to `task_events` — 257 of them on the measured store, across 69
tasks, teaching the product nothing. They were injected into ONE session's
context and discarded with it. B2 routes them into the SAME human-gated
proposals path B1 built for reviewer findings.

What must stay true after the wiring:
  * a correction seen ONCE is noise and is never proposed; seen twice, it is,
  * 257 corrections do not become 257 proposals — clustering is the feature,
  * re-harvesting the same store queues nothing new (the gist is the key),
  * a correction carrying personal data never reaches the memories table,
  * the queue says WHICH signal proposed each row (origin round-trips),
  * a supervisor-sourced proposal can NEVER reach a running agent until a
    human confirms it — the same gate independence B1 has, on the new source.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from no_human.core.db import Store
from no_human.core.task import Task
from no_human.learning import (
    ORIGIN_REVIEW,
    ORIGIN_SUPERVISOR,
    TYPE_ANTI_PATTERN,
    TYPE_RULE,
    LearningQueue,
)
from no_human.learning.corrections import (
    CorrectionRecord,
    build_correction_distill_prompt,
    cluster_corrections,
    is_project_scoped,
    normalize_gist,
)

REPO = "/tmp/repo-b2"
OTHER_REPO = "/tmp/repo-b2-other"

_LESSON = (
    "TYPE: rule\n"
    "TITLE: Run the suite with the venv interpreter\n"
    "LESSON: This repo's tests need the .venv interpreter; the bare `python` "
    "on PATH is an older build and its failures are not regressions.\n"
    "TAGS: pytest, venv, interpreter\n"
)

# The SAME recurring correction, re-issued — which is the shape the real store
# holds: the supervisor's repeats come from a deterministic detector or from a
# model re-stating the same complaint, not from free paraphrase. Neutral
# placeholder content only; no real paths, hosts or people.
_MSG_A = (
    "The agent stopped at a failing `python` invocation without checking the "
    "venv interpreter. Run the suite with .venv/bin/python before declaring "
    "anything done."
)
_MSG_B = (
    "The agent stopped again at a failing `python` invocation. Use the venv "
    "interpreter (.venv/bin/python) and re-run the suite before claiming the "
    "work is done."
)
# The same complaint in genuinely different words. It does NOT cluster — see
# test_a_paraphrase_of_the_same_correction_does_not_cluster.
_MSG_PARAPHRASE = (
    "Running `python` picked up an older build. Use the venv interpreter "
    "(.venv/bin/python) and re-run the suite; that failure is not evidence."
)
_MSG_UNRELATED = (
    "The changelog entry was never added. Add it to the release notes file "
    "before opening a pull request."
)


def _rec(message, *, project=REPO, task_id="task-1", ts=1.0):
    return CorrectionRecord(
        task_id=task_id, project=project, message=message, ts=ts)


def _cluster(messages, *, project=REPO, task_ids=None):
    """A cluster built through the REAL clustering function, so no test can
    hand-assemble a shape `cluster_corrections` would never produce."""
    ids = task_ids or [f"task-{i}" for i in range(len(messages))]
    clusters = cluster_corrections(
        [_rec(m, project=project, task_id=t, ts=float(i))
         for i, (m, t) in enumerate(zip(messages, ids))],
        min_occurrences=1,
    )
    assert len(clusters) == 1, [c.gist for c in clusters]
    return clusters[0]


def _distiller(reply=_LESSON, *, calls=None, boom=False):
    async def _fn(prompt):
        if calls is not None:
            calls.append(prompt)
        if boom:
            raise RuntimeError("utility tier exploded")
        return reply
    return _fn


async def _seed_corrections(store, messages, *, repo=REPO, task_id=None,
                            title="Wire the exporter"):
    """Persist supervisor `correct` decisions the way production does: a task
    row, then `supervisor_decision` events whose `text` is the action and whose
    `message` is the correction (Orchestrator.emit's shape)."""
    t = Task.new(title, repo_path=repo)  # repo=None → a repo-less task
    if task_id:
        t.id = task_id
    await store.create_task(t)
    await store.save_events(t.id, [
        {"source": "orchestrator", "kind": "supervisor_decision",
         "text": "correct", "message": m, "ts": float(i + 1), "task_id": t.id}
        for i, m in enumerate(messages)
    ])
    return t


# ── the >=2 rule: repetition, not isolation ───────────────────────────────── #

def test_a_single_correction_is_not_a_cluster():
    assert cluster_corrections([_rec(_MSG_A)]) == []


def test_the_same_correction_twice_is_a_cluster():
    clusters = cluster_corrections([_rec(_MSG_A, task_id="t1"),
                                    _rec(_MSG_B, task_id="t2")])
    assert len(clusters) == 1
    assert clusters[0].count == 2
    assert clusters[0].task_ids == ["t1", "t2"]


def test_a_recurring_and_a_one_off_correction_together_yield_one_cluster():
    """The discriminating case: noise alongside signal must not ride along."""
    clusters = cluster_corrections(
        [_rec(_MSG_A), _rec(_MSG_B), _rec(_MSG_UNRELATED)])
    assert len(clusters) == 1
    assert clusters[0].count == 2


def test_the_same_correction_in_two_projects_does_not_cluster():
    """A lesson is repo-scoped, exactly as every other proposal is. Two repos
    each seeing it once is two one-offs, not one recurrence."""
    assert cluster_corrections([_rec(_MSG_A, project=REPO),
                                _rec(_MSG_A, project=OTHER_REPO)]) == []


def test_gist_ignores_word_order_and_noise_but_not_subject_matter():
    assert normalize_gist(_MSG_A) == normalize_gist(_MSG_B)
    assert normalize_gist(_MSG_A) != normalize_gist(_MSG_UNRELATED)
    # The selected words are sorted, so the same leading vocabulary in a
    # different arrangement is one key.
    assert (normalize_gist("Rerun the export job now")
            == normalize_gist("The export job — rerun it"))
    # Line numbers, pids and paths must not be what makes two corrections
    # differ — otherwise nothing ever clusters.
    assert (normalize_gist("Retry the export at line:14 (pid 8123)")
            == normalize_gist("Retry the export at line:9902 (pid 4)"))


def test_a_paraphrase_of_the_same_correction_does_not_cluster():
    """THE LIMIT OF THE DESIGN, pinned so it is a documented trade rather than
    a bug someone finds later. The gist is a POSITIONAL key — the first N
    significant words — which is what makes it a pure function of one message
    and therefore stable under re-run (see corrections.py). The price is that a
    correction re-worded from scratch reads as a different correction. Three
    corpus-independent alternatives were measured; none grouped paraphrases,
    and all grouped less overall."""
    assert normalize_gist(_MSG_A) != normalize_gist(_MSG_PARAPHRASE)
    assert cluster_corrections([_rec(_MSG_A), _rec(_MSG_PARAPHRASE)]) == []


def test_a_correction_with_no_significant_words_is_dropped():
    """An empty gist would collapse every such correction in a project into
    one meaningless cluster."""
    assert normalize_gist("it is not. do it!") == ""
    assert cluster_corrections([_rec("ok"), _rec("no."), _rec("do it")]) == []


# ── one cluster → one unconfirmed, evidence-bearing proposal ──────────────── #

@pytest.mark.asyncio
async def test_a_cluster_becomes_one_unconfirmed_proposal(store):
    q = LearningQueue(store)
    calls = []
    mem_id = await q.propose_from_corrections(
        _cluster([_MSG_A, _MSG_B], task_ids=["aaaaaaaabbbb", "ccccccccdddd"]),
        distill=_distiller(calls=calls))

    assert mem_id is not None
    pending = await q.pending()
    assert len(pending) == 1
    row = pending[0]
    assert row["confirmed"] == 0
    assert row["source"] == "proposed"        # the queue-visibility contract
    assert row["origin"] == ORIGIN_SUPERVISOR  # …and the provenance, separately
    assert row["project"] == REPO
    assert row["type"] == TYPE_RULE            # the distiller said "rule"
    # Evidence a human can check: how often, across how many tasks, and the
    # corrections verbatim.
    assert "2x across 2 task(s)" in row["content"]
    assert "aaaaaaaa" in row["content"] and "cccccccc" in row["content"]
    assert ".venv/bin/python" in row["content"]
    assert "older build" in row["content"]     # the distilled lesson survived
    assert len(calls) == 1                     # exactly one utility call


@pytest.mark.asyncio
async def test_the_proposal_carries_trigger_tags(store):
    q = LearningQueue(store)
    await q.propose_from_corrections(
        _cluster([_MSG_A, _MSG_B]), distill=_distiller())
    tags = (await q.pending())[0]["tags"]
    assert "supervisor" in tags               # filterable by where it came from
    # B3: the model's free TAGS line ("pytest, venv, interpreter") is reduced
    # to the reviewed vocabulary before it becomes stored data — free tags rot
    # (learning/vocab.py). Triggering on the specific words is not lost:
    # `triggers.py` matches a canonical tag on its whole alias family.
    assert "test" in tags and "environment" in tags
    assert "pytest" not in tags and "venv" not in tags


@pytest.mark.asyncio
async def test_an_undistillable_reply_still_proposes_the_corrections(store):
    """The utility tier is advisory: a reply in the wrong shape degrades the
    lesson, it never loses the cluster."""
    q = LearningQueue(store)
    await q.propose_from_corrections(
        _cluster([_MSG_A, _MSG_B]), distill=_distiller("I could not do that."))
    row = (await q.pending())[0]
    assert row["type"] == TYPE_ANTI_PATTERN
    assert "(not distilled)" in row["content"]
    assert ".venv/bin/python" in row["content"]


@pytest.mark.asyncio
async def test_no_distiller_at_all_still_proposes(store):
    """This layer never reaches for a backend itself (B1's rule): with nothing
    injected it still proposes, from the corrections alone."""
    q = LearningQueue(store)
    assert await q.propose_from_corrections(_cluster([_MSG_A, _MSG_B])) is not None
    assert "(not distilled)" in (await q.pending())[0]["content"]


@pytest.mark.asyncio
async def test_a_hand_built_single_occurrence_cluster_is_still_refused(store):
    """Defence in depth. `cluster_corrections` enforces the >=2 rule, but a
    caller assembling a cluster directly must not be the way round the rule
    that keeps 257 corrections from becoming 257 proposals."""
    q = LearningQueue(store)
    lone = cluster_corrections([_rec(_MSG_A)], min_occurrences=1)[0]
    assert lone.count == 1
    assert await q.propose_from_corrections(lone, distill=_distiller()) is None
    assert await q.pending() == []


# ── personal data never reaches the table ─────────────────────────────────── #

_PII_MSG_A = (
    "Stop hardcoding the customer record in the fixture: it carries the "
    "shipping address 12 Maple Street, Springfield IL. Build it from a factory."
)
_PII_MSG_B = (
    "Stop hardcoding the customer record — the fixture still carries the "
    "shipping address 12 Maple Street, Springfield IL. Use a factory."
)


@pytest.mark.asyncio
async def test_a_correction_carrying_personal_data_is_never_proposed(store):
    """A correction quotes whatever the agent was looking at, verbatim — the
    same door B1's cited evidence opened, so it needs the same gate."""
    q = LearningQueue(store)
    notes = []
    mem_id = await q.propose_from_corrections(
        _cluster([_PII_MSG_A, _PII_MSG_B]),
        distill=_distiller(), note=notes.append)

    assert mem_id is None
    assert await q.pending() == [], "PII reached the memories table"
    # Refusal is not silent, and it names the KIND, never the value.
    assert any("personal data" in n for n in notes), notes
    assert any("street_address" in n for n in notes), notes
    assert not any("Maple" in n for n in notes), notes


@pytest.mark.asyncio
async def test_the_pii_gate_is_not_swallowing_ordinary_corrections(store):
    """Non-vacuity for the test above: the same shape of cluster with
    engineering content IS proposed. Without this, a gate that refused
    everything would pass the assertion above."""
    q = LearningQueue(store)
    assert await q.propose_from_corrections(
        _cluster([_MSG_A, _MSG_B]), distill=_distiller()) is not None
    assert len(await q.pending()) == 1


# ── dedupe / idempotence ──────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_reharvesting_the_same_store_queues_nothing_new(store):
    q = LearningQueue(store)
    await _seed_corrections(store, [_MSG_A, _MSG_B, _MSG_UNRELATED])

    first = await q.harvest_supervisor_corrections(distill=_distiller())
    assert len(first) == 1, "the recurring cluster proposed; the one-off did not"

    notes = []
    second = await q.harvest_supervisor_corrections(
        distill=_distiller(), note=notes.append)

    assert second == [], "a re-run duplicated the lesson"
    assert len(await q.pending()) == 1
    # The recurrence is announced rather than swallowed into a bare None.
    assert any("deduped" in n for n in notes), notes
    assert any("learn:" in n for n in notes), notes


@pytest.mark.asyncio
async def test_a_reharvest_spends_no_utility_call_on_a_known_cluster(store):
    """The harvest re-reads the WHOLE correction history every run, so after
    the first run most clusters are already queued. Discovering that inside
    `add_memory` would mean paying for a distillation per known lesson and
    writing nothing — a cost nobody would see, because the outcome (no new
    rows) looks identical either way."""
    q = LearningQueue(store)
    await _seed_corrections(store, [_MSG_A, _MSG_B])
    first_calls = []
    assert len(await q.harvest_supervisor_corrections(
        distill=_distiller(calls=first_calls))) == 1
    assert len(first_calls) == 1

    second_calls = []
    assert await q.harvest_supervisor_corrections(
        distill=_distiller(calls=second_calls)) == []
    assert second_calls == [], "paid the utility tier for an already-queued lesson"


@pytest.mark.asyncio
async def test_a_grown_cluster_still_dedupes_onto_the_same_row(store):
    """The reason the key is the GIST and not the cluster's contents: the same
    correction arriving a third time must collapse onto the existing row, not
    mint a second one because the cluster changed shape."""
    q = LearningQueue(store)
    await _seed_corrections(store, [_MSG_A, _MSG_B], task_id="1" * 32)
    assert len(await q.harvest_supervisor_corrections(distill=_distiller())) == 1

    await _seed_corrections(store, [_MSG_A], task_id="2" * 32,
                            title="Wire the importer")
    assert await q.harvest_supervisor_corrections(distill=_distiller()) == []
    assert len(await q.pending()) == 1


@pytest.mark.asyncio
async def test_a_different_recurring_correction_gets_its_own_entry(store):
    """Non-vacuity for the dedupe tests: dedupe must not be "nothing is ever
    proposed twice"."""
    q = LearningQueue(store)
    await _seed_corrections(store, [_MSG_A, _MSG_B])
    assert len(await q.harvest_supervisor_corrections(distill=_distiller())) == 1

    await _seed_corrections(store, [_MSG_UNRELATED, _MSG_UNRELATED],
                            title="Cut the release")
    assert len(await q.harvest_supervisor_corrections(distill=_distiller())) == 1
    assert len(await q.pending()) == 2


@pytest.mark.asyncio
async def test_harvest_aggregates_rather_than_proposing_every_correction(store):
    """THE milestone property, on the shape the real store has: many raw
    corrections, a handful of recurring patterns, one proposal each."""
    q = LearningQueue(store)
    one_offs = [
        f"{noun.capitalize()} handling is untested; add a case."
        for noun in (
            "ledger tariff harbour lantern quarry meadow beacon cobalt "
            "trellis pumice orchard glacier saffron thicket zephyr").split()
    ]
    assert len({normalize_gist(m) for m in one_offs}) == len(one_offs)
    await _seed_corrections(
        store, [_MSG_A] * 12 + [_MSG_B] * 9 + [_MSG_UNRELATED] + one_offs)

    written = await q.harvest_supervisor_corrections(distill=_distiller())
    assert len(written) == 1, "37 corrections became more than one proposal"
    assert "21x across 1 task(s)" in (await q.pending())[0]["content"]


@pytest.mark.asyncio
async def test_harvest_can_be_scoped_to_one_project(store):
    q = LearningQueue(store)
    await _seed_corrections(store, [_MSG_A, _MSG_B], repo=REPO)
    await _seed_corrections(store, [_MSG_UNRELATED, _MSG_UNRELATED],
                            repo=OTHER_REPO, title="Other work")

    assert len(await q.harvest_supervisor_corrections(
        project=OTHER_REPO, distill=_distiller())) == 1
    assert [m["project"] for m in await q.pending()] == [OTHER_REPO]


# ── a human's "no" sticks against the next harvest ────────────────────────── #

@pytest.mark.asyncio
async def test_a_rejected_proposal_is_not_re_queued_by_the_next_harvest(store):
    """THE REJECT TREADMILL. `reject` used to delete the row — and the row
    carries the dedupe key, so the next harvest re-read the same history,
    re-distilled the same cluster and re-queued the exact lesson the human had
    just turned down. The queue's two "no" verbs behaved oppositely: archive
    stuck, reject did not."""
    q = LearningQueue(store)
    await _seed_corrections(store, [_MSG_A, _MSG_B])
    assert len(await q.harvest_supervisor_corrections(distill=_distiller())) == 1
    proposal = (await q.pending())[0]

    assert await q.reject(proposal["id"])
    assert await q.pending() == [], "a rejected proposal must leave the queue"

    calls = []
    assert await q.harvest_supervisor_corrections(
        distill=_distiller(calls=calls)) == []
    assert calls == [], "paid the utility tier to re-distil a rejected lesson"
    assert await q.pending() == []


@pytest.mark.asyncio
async def test_rejecting_archives_the_row_rather_than_destroying_it(store):
    """What makes the "no" stick: the row survives with confirmed=0 and
    archived=1, so it is out of the queue but its dedupe key is still there.
    Recoverable, and auditable — the curator's invariant, applied to the one
    path that has a re-proposing batch behind it."""
    q = LearningQueue(store)
    await q.propose_from_corrections(
        _cluster([_MSG_A, _MSG_B]), distill=_distiller())
    proposal = (await q.pending())[0]
    await q.reject(proposal["id"])

    archived = await store.list_memories(include_archived=True)
    assert [m["id"] for m in archived] == [proposal["id"]]
    assert archived[0]["archived"] == 1
    assert archived[0]["confirmed"] == 0
    assert "rejected" in archived[0]["content"]
    assert await store.memory_dedupe_key_exists(archived[0]["file_path"])


@pytest.mark.asyncio
async def test_rejecting_a_review_proposal_still_deletes_it(store):
    """B1's semantics are UNCHANGED, and the asymmetry is deliberate:
    `propose_from_review` fires on a FAIL round, so a deleted proposal only
    returns when the reviewer raises that finding again — new evidence, worth
    re-asking about. Only B2 has a batch behind it that re-reads all history."""
    q = LearningQueue(store)
    t = Task.new("Pin the images", repo_path=REPO)
    await store.create_task(t)
    await q.propose_from_review(t, findings=[{
        "label": "image pinning",
        "evidence": "ci/images.yaml:12 pins `latest`, not a digest"}],
        attempt=1, review_round=1)
    proposal = (await q.pending())[0]
    assert proposal["origin"] == ORIGIN_REVIEW

    assert await q.reject(proposal["id"])
    assert await store.list_memories(include_archived=True) == []


# ── repo-less corrections never become rules for every project ────────────── #

@pytest.mark.asyncio
async def test_corrections_from_a_repo_less_task_are_never_proposed(store):
    """A task with no `repo_path` yields corrections with project=None, and a
    memory with project=NULL is GLOBAL: `list_memories` matches it with
    `(project = ? OR project IS NULL)`, so confirming one injects it into
    EVERY project's rules. Worse, two unrelated repo-less tasks would merge
    under the same `(None, gist)` key and invent a recurrence that happened in
    no single repo — something B1 structurally cannot do."""
    q = LearningQueue(store)
    await _seed_corrections(store, [_MSG_A, _MSG_B], repo=None)

    notes = []
    assert await q.harvest_supervisor_corrections(
        distill=_distiller(), note=notes.append) == []
    assert await q.pending() == []
    # Counted and said out loud, not silently capped.
    assert any("2 supervisor correction(s) skipped" in n for n in notes), notes
    assert any("repo_path" in n for n in notes), notes


def test_two_unrelated_repo_less_tasks_do_not_merge_into_one_cluster():
    assert is_project_scoped(_rec(_MSG_A, project=REPO)) is True
    assert is_project_scoped(_rec(_MSG_A, project=None)) is False
    assert is_project_scoped(_rec(_MSG_A, project="   ")) is False
    assert cluster_corrections([_rec(_MSG_A, project=None, task_id="t1"),
                                _rec(_MSG_B, project=None, task_id="t2")]) == []


@pytest.mark.asyncio
async def test_a_repo_less_task_does_not_suppress_the_repo_scoped_ones(store):
    """Non-vacuity: the exclusion drops exactly the unattributable records and
    leaves the rest of the harvest working."""
    q = LearningQueue(store)
    await _seed_corrections(store, [_MSG_A, _MSG_B], repo=None)
    await _seed_corrections(store, [_MSG_A, _MSG_B], repo=REPO,
                            title="Wire the importer")

    assert len(await q.harvest_supervisor_corrections(distill=_distiller())) == 1
    assert [m["project"] for m in await q.pending()] == [REPO]


def test_the_listing_shows_scope_and_flags_a_global_row(tmp_path, monkeypatch):
    """The confirming human sees the blast radius on the same screen as the
    confirm command. A project=NULL row is GLOBAL — `list_memories` matches it
    with `(project = ? OR project IS NULL)` — and it used to render exactly
    like a repo-scoped one.

    Points `load_config` at a tmp_path DB the same way the other CLI tests do
    (`tests/test_rules_skills.py::_make_runner`); this must never read the
    operator's real store.
    """
    import asyncio

    from click.testing import CliRunner

    import no_human.cli.commands as cmd_mod
    from no_human.cli.commands import cli

    db = tmp_path / "learn.db"

    async def _seed():
        s = await Store(db).connect()
        await s.add_memory(mem_type=TYPE_RULE, title="scoped rule",
                           content="x", project=REPO)
        await s.add_memory(mem_type=TYPE_RULE, title="global rule", content="y")
        await s.close()

    asyncio.run(_seed())

    class _Cfg:
        db_path = db
        data: dict = {}

        def get(self, key, default=None):
            return self.data.get(key, default)

    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)

    result = CliRunner().invoke(cli, ["learnings"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "scoped rule" in result.output and "global rule" in result.output
    assert f"scope: {REPO}" in result.output
    assert "GLOBAL — applies to every project" in result.output


# ── the tags field is inside the gate, not beside it ──────────────────────── #

# A distiller reply whose TITLE and LESSON are clean engineering prose and
# whose TAGS carry a consumer-mailbox email. The tags come verbatim from the
# utility model, which read the raw corrections — so this is the shape the gap
# had.
#
# The local part is DELIBERATELY not `first.last`: this file ships, and
# `tests/test_identity_scrub_guard.py`'s `personal-email` shape rule fails the
# build on a name-shaped address anywhere on the export surface — including one
# written as a fixture for a test about dropping personal data. A separator-free
# placeholder still trips `learning/pii.py`'s consumer-domain detector, which is
# the thing under test here.
_PII_IN_TAGS = (
    "TYPE: rule\n"
    "TITLE: Build fixtures from a factory\n"
    "LESSON: Fixtures in this repo are built from a factory, never hardcoded.\n"
    "TAGS: fixtures, factory, placeholder99@gmail.com\n"
)


@pytest.mark.asyncio
async def test_personal_data_confined_to_the_tags_drops_the_proposal(store):
    q = LearningQueue(store)
    notes = []
    mem_id = await q.propose_from_corrections(
        _cluster([_MSG_A, _MSG_B]),
        distill=_distiller(_PII_IN_TAGS), note=notes.append)

    assert mem_id is None
    assert await q.pending() == [], "PII reached the memories table via tags"
    assert any("personal_email" in n for n in notes), notes
    assert not any("gmail" in n for n in notes), notes


@pytest.mark.asyncio
async def test_the_same_tags_hole_is_closed_on_the_review_path(store):
    """Inherited from B1, and fixed there too: the identical one-line guard
    protects the identical field on the identical table. Fixing only B2 would
    leave the hole open next door."""
    q = LearningQueue(store)
    t = Task.new("Pin the images", repo_path=REPO)
    await store.create_task(t)
    notes = []

    async def _distill(_prompt):
        return _PII_IN_TAGS

    mem_id = await q.propose_from_review(
        t, findings=[{"label": "hardcoded fixture",
                      "evidence": "tests/fixtures/order.json:4 hardcodes it"}],
        attempt=1, review_round=1, distill=_distill, note=notes.append)
    assert mem_id is None
    assert await q.pending() == []
    assert any("personal_email" in n for n in notes), notes
    assert not any("gmail" in n for n in notes), notes


@pytest.mark.asyncio
async def test_clean_tags_are_not_swallowed_by_the_widened_gate(store):
    """Non-vacuity for both tests above: ordinary keyword tags still pass."""
    q = LearningQueue(store)
    assert await q.propose_from_corrections(
        _cluster([_MSG_A, _MSG_B]), distill=_distiller()) is not None
    # "venv" arrives free from the distiller and is stored as its canonical
    # vocabulary tag (B3).
    assert "environment" in (await q.pending())[0]["tags"]


# ── origin round-trips, and distinguishes the two producers ───────────────── #

@pytest.mark.asyncio
async def test_the_queue_says_which_signal_proposed_each_row(store):
    """B1 and B2 write to the same table through the same call; a human
    confirming the queue must be able to tell a reviewer's blocking finding
    from a supervisor correction that fired N times."""
    q = LearningQueue(store)
    t = Task.new("Pin the images", repo_path=REPO)
    await store.create_task(t)

    await q.propose_from_review(t, findings=[{
        "label": "image pinning",
        "evidence": "ci/images.yaml:12 pins `latest`, not a digest",
        "file": "ci/images.yaml", "line": 12,
    }], attempt=1, review_round=1)
    await q.propose_from_corrections(
        _cluster([_MSG_A, _MSG_B]), distill=_distiller())

    by_origin = {m["origin"]: m for m in await q.pending()}
    assert set(by_origin) == {ORIGIN_REVIEW, ORIGIN_SUPERVISOR}
    assert "image pinning" in by_origin[ORIGIN_REVIEW]["content"]
    assert ".venv/bin/python" in by_origin[ORIGIN_SUPERVISOR]["content"]
    # …and it survives the confirm round-trip into the active set.
    assert await q.confirm(by_origin[ORIGIN_SUPERVISOR]["id"])
    active = await q.active()
    assert [m["origin"] for m in active] == [ORIGIN_SUPERVISOR]


@pytest.mark.asyncio
async def test_a_row_written_without_an_origin_reads_as_unknown(store):
    """NULL, not a guessed-at value: rows written before the column genuinely
    do not record which signal produced them, and inventing one is how a
    provenance surface starts lying."""
    q = LearningQueue(store)
    await store.add_memory(mem_type=TYPE_RULE, title="legacy", content="x",
                           project=REPO)
    assert (await q.pending())[0]["origin"] is None


# ── gate independence, extended to the new source ─────────────────────────── #

@pytest.mark.asyncio
async def test_a_supervisor_proposal_never_reaches_a_running_agent(store):
    """B1's gate-independence test, on the B2 source. Drives the REAL assembly
    the orchestrator uses for `confirmed_rules`: list_memories(confirmed=True)
    → filter_triggered → build_memories_block → the reviewer prompt.

    It matters MORE here than for B1: the supervisor's corrections are already
    injected into the coder's context at runtime, so a correction that became
    an active rule with no human in between would be the agent authoring its
    own standing instructions from its own supervisor's opinions.
    """
    from no_human.core.orchestrator import Orchestrator
    from no_human.core.prompt_blocks import build_memories_block
    from no_human.learning.triggers import filter_triggered
    from no_human.review.reviewer import _build_review_prompt

    q = LearningQueue(store)
    t = Task.new("Wire the exporter", repo_path=REPO)
    await store.create_task(t)
    await q.propose_from_corrections(
        _cluster([_MSG_A, _MSG_B]), distill=_distiller())
    proposal = (await q.pending())[0]
    assert proposal["confirmed"] == 0
    assert proposal["origin"] == ORIGIN_SUPERVISOR

    haystack = f"{t.title} pytest venv interpreter supervisor"

    confirmed = await store.list_memories(confirmed=True, project=REPO)
    assert confirmed == [], "an unconfirmed proposal must not be in the active set"

    block = build_memories_block(
        filter_triggered(confirmed, haystack),
        Orchestrator._RULES_CRITICAL_CAP, Orchestrator._RULES_RELEVANT_CAP,
    )
    prompt = _build_review_prompt(t, "diff", "tests", "", confirmed_rules=block)
    assert "older build" not in prompt
    assert proposal["title"] not in prompt

    # …and the human gate is what unlocks it: confirm, and only then does the
    # same assembly carry it (proving the assertions above are not vacuous).
    assert await q.confirm(proposal["id"])
    confirmed = await store.list_memories(confirmed=True, project=REPO)
    block = build_memories_block(
        filter_triggered(confirmed, haystack),
        Orchestrator._RULES_CRITICAL_CAP, Orchestrator._RULES_RELEVANT_CAP,
    )
    assert proposal["title"] in _build_review_prompt(
        t, "diff", "tests", "", confirmed_rules=block)


@pytest.mark.asyncio
async def test_harvested_proposals_are_absent_from_the_active_set(store):
    """The same independence at the store's own API — whatever a caller asks
    for, an unharvested-then-unconfirmed lesson is not in `active()`."""
    q = LearningQueue(store)
    await _seed_corrections(store, [_MSG_A, _MSG_B])
    assert len(await q.harvest_supervisor_corrections(distill=_distiller())) == 1
    assert await q.active() == []
    assert len(await q.pending()) == 1


# ── the persisted event shape this whole flow reads ───────────────────────── #

@pytest.mark.asyncio
async def test_only_correct_decisions_are_read_and_the_project_is_joined(store):
    """`continue`, `budget_nudge` and `stop` are not corrections. On the
    measured store they are 3,422 of the 3,679 supervisor decisions — reading
    them would be reading mostly "carry on"."""
    t = Task.new("Wire the exporter", repo_path=REPO)
    await store.create_task(t)
    await store.save_events(t.id, [
        {"kind": "supervisor_decision", "text": "continue", "message": "",
         "ts": 1.0, "task_id": t.id},
        {"kind": "supervisor_decision", "text": "budget_nudge",
         "message": "spend", "ts": 2.0, "task_id": t.id},
        {"kind": "supervisor_decision", "text": "stop", "message": "doomed",
         "ts": 3.0, "task_id": t.id},
        {"kind": "supervisor_decision", "text": "correct", "message": _MSG_A,
         "ts": 4.0, "task_id": t.id},
        {"kind": "review", "text": "correct", "message": "not a supervisor",
         "ts": 5.0, "task_id": t.id},
    ])

    rows = await store.list_supervisor_corrections()
    assert [r["message"] for r in rows] == [_MSG_A]
    assert rows[0]["project"] == REPO
    assert rows[0]["task_id"] == t.id


# ── the prompt is bounded and carries the evidence ────────────────────────── #

def test_the_distill_prompt_is_bounded_and_carries_the_recurrence():
    prompt = build_correction_distill_prompt(_cluster([_MSG_A, _MSG_B]))
    assert "2 times across 2 separate task(s)" in prompt
    assert ".venv/bin/python" in prompt
    assert "under 600 characters" in prompt
    assert len(prompt) < 2000


def test_the_prompt_quotes_at_most_three_corrections():
    """A cluster of 40 must not build a 40-quote prompt."""
    cluster = _cluster([f"{_MSG_A} variant {i}" for i in range(40)])
    assert cluster.count == 40
    assert len(cluster.examples()) == 3
