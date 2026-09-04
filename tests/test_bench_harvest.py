"""Failure-harvested bench candidates (gap-close W6, eval/harvest.py).

Every terminal non-success becomes a spec CANDIDATE for operator curation —
never a scored corpus row. These tests pin the safety half hardest: a
candidate can never run or score as harvested (runnable:false, subset
"candidate", written outside the corpus), the request is the initial ask
verbatim (the bench's no-cheat rule), and re-harvest never clobbers a
curated file.
"""

import yaml

from no_human.core.task import Task, TaskStatus
from no_human.eval.harvest import (HARVEST_STATUSES, candidate_from_task,
                                   harvest)


def _escalated_task():
    t = Task.new("fix the flaky retry", repo_path="/repo/x",
                 description="it fails on CI only")
    t.status = TaskStatus.ESCALATED
    t.blocker = {"category": "MISSING_ACCESS",
                 "question": "Which CI credentials should it use?",
                 "root_cause_hypothesis": "no CI token available locally"}
    t.context = {"attempt_log": ["attempt 1: tests failed: auth"]}
    t.acceptance_criteria = ["retry passes on CI"]
    return t


def test_candidate_shape_is_a_curatable_spec():
    cand = candidate_from_task(_escalated_task())
    assert cand["id"].startswith("hv-")
    # the bench no-cheat rule: initial ask verbatim, nothing the run learned
    assert cand["request"] == "fix the flaky retry\n\nit fails on CI only"
    assert cand["acceptance_criteria"] == ["retry passes on CI"]
    # the safety half — harvested candidates can never run or score
    assert cand["runnable"] is False
    assert cand["subset"] == "candidate"
    assert "operator" in cand["skip_reason"]
    # expect_escalation is the JUDGMENT BEING REQUESTED, never pre-answered
    assert cand["expect_escalation"] is False
    assert cand["harvest"]["blocker_category"] == "MISSING_ACCESS"
    assert cand["harvest"]["outcome"] == "escalated"
    assert cand["harvest"]["attempt_log"] == ["attempt 1: tests failed: auth"]


def test_successes_and_empty_titles_are_not_candidates():
    done = Task.new("shipped", repo_path="/r")
    done.status = TaskStatus.DONE
    assert candidate_from_task(done) is None
    empty = Task.new("  ", repo_path="/r")
    empty.status = TaskStatus.ESCALATED
    assert candidate_from_task(empty) is None
    # the harvestable set itself is pinned — successes can never join it
    assert TaskStatus.DONE not in HARVEST_STATUSES
    assert TaskStatus.AWAITING_APPROVAL not in HARVEST_STATUSES


async def test_harvest_writes_yaml_and_never_overwrites(store, tmp_path):
    t = _escalated_task()
    await store.create_task(t)
    out = tmp_path / "harvest"

    written = await harvest(store, out_dir=out)
    assert len(written) == 1
    data = yaml.safe_load(written[0].read_text())
    assert data["id"] == f"hv-{t.id[:8]}"
    assert data["runnable"] is False

    # the operator curates the file in place; a re-harvest must not clobber it
    written[0].write_text(written[0].read_text().replace(
        "runnable: false", "runnable: true"))
    again = await harvest(store, out_dir=out)
    assert again == []
    assert "runnable: true" in written[0].read_text()


def test_the_default_output_dir_is_outside_the_scored_corpus():
    """"Written OUTSIDE the corpus" is the safety claim this command rests on:
    an un-reviewed candidate that lands in `eval/northstar_tasks/` joins the
    set behind a PUBLISHED trust number. Every other test passes an explicit
    `out_dir`, so nothing pinned the default — setting it to the corpus path
    left all of them green."""
    from pathlib import Path

    from no_human.eval import harvest as h

    from no_human.eval.bench_task import NORTHSTAR_DIR

    default = Path(h._DEFAULT_OUT).resolve()
    # The REAL corpus, from the constant the loader uses — computing it here as
    # `Path(h.__file__).parent / "northstar_tasks"` named a directory that does
    # not exist, so the assertion below could never fire: with `_DEFAULT_OUT`
    # set to the literal scored corpus it still passed. Import the constant.
    # The corpus directory itself is not asserted to exist HERE: it is
    # `drop`-classified (EXPORT_CLASSIFICATION.txt `drop 54 eval/northstar_tasks/`),
    # so in the public export this test would fail on the tree, not on harvest —
    # which is exactly what the public CI did. The non-vacuity guard ("the
    # constant still names a real directory") lives with the other corpus-
    # dependent assertions in tests/test_northstar_corpus.py (private-only).
    corpus = Path(NORTHSTAR_DIR).resolve()
    assert corpus not in default.parents and default != corpus, (
        f"harvest would write candidates into the scored corpus: {default}")
    # and it is under the operator's own state dir, not the repo
    assert Path.home().resolve() in default.parents
