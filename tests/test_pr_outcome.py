"""PR-outcome telemetry (migration 0010): did the work actually LAND?

The metric this corrects counted a task as a success once it reached
AWAITING_APPROVAL/DONE. That predicate is a status and contains no forge query,
so "success" and "reached a reviewable state" were the same number wearing two
names — and, as `test_delivered_does_not_even_imply_a_pr_exists` pins below, it
does not even imply a PR was opened.

The properties these tests defend, in order of how badly each would hurt:

1. `unknown` is never a success and never overwrites a known outcome.
2. A CLOSED PR is never settled as `closed_unmerged` on a git probe that could
   not run — the failure mode that would land hardest on PRs that DID merge.
3. Offline (no `gh`, no network, no token) degrades to `unknown`, never crashes
   and never invents an outcome.
4. A zero denominator renders as "not measurable", never as 0%.
"""

from __future__ import annotations

import subprocess


from no_human.blockers.wake import WakeWatcher
from no_human.core import autonomy
from no_human.core.autonomy import (compute_pr_outcome_metrics,
                                    render_pr_outcome_lines)
from no_human.core.db import SETTLED_PR_OUTCOMES
from no_human.core.task import Task, TaskStatus
from no_human.vcs import pr_outcome as po
from no_human.vcs.pr_watcher import default_branch_shipped, refs_resolvable

GH_URL = "https://github.com/o/r/pull/86"
GH_URL2 = "https://github.com/o/r/pull/87"


def _git(repo_path, *args):
    subprocess.run(["git", "-C", str(repo_path), *args], check=True,
                   capture_output=True)


def _make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _shipped_repo(tmp_path, name="repo"):
    """Branch's content reaches main via a fresh squash-shaped commit."""
    repo = _make_repo(tmp_path, name)
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "feature")
    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "squash")
    return repo


def _unshipped_repo(tmp_path, name="repo"):
    repo = _make_repo(tmp_path, name)
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "feature")
    _git(repo, "checkout", "main")
    return repo


async def _task(store, *, status=TaskStatus.AWAITING_APPROVAL, repo_path="/tmp/x",
                url=GH_URL, branch="feature"):
    t = Task.new("outcome", repo_path=str(repo_path))
    t.context = {"pr_watch": url, "pr_branch": branch, "base_branch": "main"}
    await store.create_task(t)
    await store.set_status(t, status, validate=False)
    return t


# --------------------------------------------------------------------------- #
# 1. Classification vocabulary
# --------------------------------------------------------------------------- #

def test_forge_merged_is_the_only_unconditional_merge():
    assert po.classify_outcome("MERGED") == (po.MERGED, po.EVIDENCE_FORGE_MERGED)


def test_open_and_blank_are_distinguished():
    assert po.classify_outcome("OPEN")[0] == po.OPEN
    # "" is what default_pr_state returns for no-gh/unparseable/network-error.
    assert po.classify_outcome("")[0] == po.UNKNOWN
    assert po.classify_outcome(None)[0] == po.UNKNOWN


def test_an_unrecognised_forge_state_is_unknown_not_a_fate():
    """A state we cannot interpret is not evidence of anything."""
    for junk in ("DRAFT", "LOCKED", "banana", "MERGED_BY_MISTAKE"):
        assert po.classify_outcome(junk)[0] == po.UNKNOWN, junk


def test_closed_without_a_ship_probe_is_unknown_never_closed_unmerged():
    """THE CENTRAL ASYMMETRY.

    A local squash merge leaves the forge reporting CLOSED for a PR that
    shipped. Classifying CLOSED as `closed_unmerged` on its own would report
    ~zero merges on exactly the repos the operator actually uses.
    """
    outcome, evidence = po.classify_outcome("CLOSED", shipped=None)
    assert outcome == po.UNKNOWN
    assert evidence == po.EVIDENCE_CLOSED_SHIP_UNVERIFIED


def test_closed_with_content_on_base_is_merged():
    assert po.classify_outcome("CLOSED", shipped=True) == (
        po.MERGED, po.EVIDENCE_CONTENT_ON_BASE)


def test_closed_with_verified_absence_is_closed_unmerged():
    assert po.classify_outcome("CLOSED", shipped=False) == (
        po.CLOSED_UNMERGED, po.EVIDENCE_CLOSED_CONTENT_ABSENT)


def test_ci_empty_list_is_unknown_not_pass():
    """`default_pr_checks` returns [] for no-gh AND for no-CI. Neither is green."""
    assert po.classify_ci([]) == po.CI_UNKNOWN
    assert po.classify_ci(None) == po.CI_UNKNOWN
    assert po.classify_ci([{"status": "pass"}]) == po.CI_PASS
    assert po.classify_ci([{"status": "pass"}, {"status": "fail"}]) == po.CI_FAIL
    assert po.classify_ci([{"status": "pass"}, {"status": "pending"}]) == po.CI_PENDING
    # A vocabulary we do not recognise is not a pass.
    assert po.classify_ci([{"status": "weird"}]) == po.CI_UNKNOWN


# --------------------------------------------------------------------------- #
# 2. Unknown never counts as success
# --------------------------------------------------------------------------- #

def test_unknown_is_not_settled_and_not_merged():
    assert po.is_settled(po.UNKNOWN) is False
    assert po.is_settled(po.OPEN) is False
    assert po.is_settled(po.MERGED) is True
    assert po.is_settled(po.CLOSED_UNMERGED) is True


def test_merged_rate_counts_unknown_against_total_and_out_of_settled():
    merged, settled, total = po.merged_rate(
        [po.MERGED, po.UNKNOWN, po.UNKNOWN, po.CLOSED_UNMERGED])
    assert (merged, settled, total) == (1, 2, 4)


def test_rollup_any_unknown_makes_the_whole_task_unknown():
    """A multi-repo task with one merged and one unmeasurable PR has NOT landed."""
    assert po.rollup([po.MERGED, po.UNKNOWN]) == po.UNKNOWN
    assert po.rollup([po.MERGED, po.MERGED]) == po.MERGED
    assert po.rollup([po.MERGED, po.OPEN]) == po.OPEN
    assert po.rollup([po.CLOSED_UNMERGED, po.MERGED]) == po.CLOSED_UNMERGED
    assert po.rollup([]) == po.UNKNOWN
    # Junk from a future build is unknown, not silently dropped.
    assert po.rollup([po.MERGED, "teleported"]) == po.UNKNOWN


def test_counts_always_has_every_bucket_including_zero_merged():
    c = po.counts([po.UNKNOWN])
    assert c == {po.MERGED: 0, po.CLOSED_UNMERGED: 0, po.OPEN: 0, po.UNKNOWN: 1}


# --------------------------------------------------------------------------- #
# 3. The store: no-downgrade rule
# --------------------------------------------------------------------------- #

async def test_unknown_never_erases_a_recorded_merge(store):
    await store.record_pr_outcome(
        task_id="t1", pr_url=GH_URL, outcome=po.MERGED,
        outcome_evidence=po.EVIDENCE_FORGE_MERGED, forge="github",
        checked_at="2026-01-01")
    await store.record_pr_outcome(
        task_id="t1", pr_url=GH_URL, outcome=po.UNKNOWN,
        outcome_evidence=po.EVIDENCE_STATE_UNAVAILABLE, forge="github",
        checked_at="2026-02-02")
    row = (await store.list_pr_outcomes(task_id="t1"))[0]
    assert row["outcome"] == po.MERGED
    # The evidence and the timestamp must not drift away from the verdict they
    # justify: a kept `merged` wearing the rejected observation's evidence
    # would be unauditable.
    assert row["outcome_evidence"] == po.EVIDENCE_FORGE_MERGED
    assert row["checked_at"] == "2026-01-01"


async def test_open_also_cannot_overwrite_a_settled_outcome(store):
    """A merged PR does not re-open. An observation saying so is broken, not news."""
    await store.record_pr_outcome(task_id="t1", pr_url=GH_URL, outcome=po.MERGED,
                                  outcome_evidence=po.EVIDENCE_FORGE_MERGED)
    await store.record_pr_outcome(task_id="t1", pr_url=GH_URL, outcome=po.OPEN,
                                  outcome_evidence=po.EVIDENCE_FORGE_OPEN)
    row = (await store.list_pr_outcomes(task_id="t1"))[0]
    assert row["outcome"] == po.MERGED


async def test_an_unsettled_row_can_still_be_upgraded(store):
    """The guard must not freeze rows — that would stop the refresh working."""
    await store.record_pr_outcome(task_id="t1", pr_url=GH_URL, outcome=po.UNKNOWN,
                                  outcome_evidence=po.EVIDENCE_STATE_UNAVAILABLE)
    await store.record_pr_outcome(task_id="t1", pr_url=GH_URL, outcome=po.MERGED,
                                  outcome_evidence=po.EVIDENCE_FORGE_MERGED)
    row = (await store.list_pr_outcomes(task_id="t1"))[0]
    assert row["outcome"] == po.MERGED


async def test_settled_to_settled_correction_is_allowed(store):
    """A better probe correcting an earlier verdict is not a downgrade."""
    await store.record_pr_outcome(task_id="t1", pr_url=GH_URL,
                                  outcome=po.CLOSED_UNMERGED,
                                  outcome_evidence=po.EVIDENCE_CLOSED_CONTENT_ABSENT)
    await store.record_pr_outcome(task_id="t1", pr_url=GH_URL, outcome=po.MERGED,
                                  outcome_evidence=po.EVIDENCE_CONTENT_ON_BASE)
    row = (await store.list_pr_outcomes(task_id="t1"))[0]
    assert row["outcome"] == po.MERGED


async def test_opened_at_survives_a_refresh_and_ci_none_keeps_the_old_value(store):
    await store.record_pr_outcome(task_id="t1", pr_url=GH_URL, outcome=po.OPEN,
                                  outcome_evidence=po.EVIDENCE_OPENED_BY_AGENT,
                                  ci_status=po.CI_FAIL, opened_at="2026-01-01")
    # A state-only observation: it did not look at CI (None), so a measured
    # `fail` must survive it.
    await store.record_pr_outcome(task_id="t1", pr_url=GH_URL, outcome=po.OPEN,
                                  outcome_evidence=po.EVIDENCE_FORGE_OPEN,
                                  ci_status=None, checked_at="2026-02-02")
    row = (await store.list_pr_outcomes(task_id="t1"))[0]
    assert row["opened_at"] == "2026-01-01"
    assert row["ci_status"] == po.CI_FAIL


async def test_unsettled_only_selects_exactly_the_re_pollable_rows(store):
    for i, outcome in enumerate(
            [po.MERGED, po.CLOSED_UNMERGED, po.OPEN, po.UNKNOWN]):
        await store.record_pr_outcome(task_id=f"t{i}", pr_url=f"u{i}",
                                      outcome=outcome)
    rows = await store.list_pr_outcomes(unsettled_only=True)
    assert {r["outcome"] for r in rows} == {po.OPEN, po.UNKNOWN}


async def test_a_future_outcome_value_is_re_polled_not_treated_as_settled(store):
    """`unsettled_only` is spelled as a NEGATIVE so an unrecognised value is
    re-polled rather than silently skipped as if its fate were known."""
    await store.record_pr_outcome(task_id="t1", pr_url=GH_URL,
                                  outcome="reverted_after_merge")
    rows = await store.list_pr_outcomes(unsettled_only=True)
    assert [r["task_id"] for r in rows] == ["t1"]


def test_db_and_vcs_agree_on_which_outcomes_are_settled():
    """PINS THE DUPLICATION. `core.db` restates the settled vocabulary because
    `core` must not import `vcs`; this is what stops the two drifting."""
    assert set(SETTLED_PR_OUTCOMES) == {po.MERGED, po.CLOSED_UNMERGED}
    assert set(SETTLED_PR_OUTCOMES) == {o for o in po.OUTCOMES if po.is_settled(o)}


# --------------------------------------------------------------------------- #
# 4. The ship probe: tri-state, and why False alone is not evidence
# --------------------------------------------------------------------------- #

async def test_refs_resolvable_distinguishes_present_from_absent(tmp_path):
    repo = _unshipped_repo(tmp_path)
    assert await refs_resolvable(str(repo), "feature", "main") is True
    assert await refs_resolvable(str(repo), "no-such-branch", "main") is False
    assert await refs_resolvable(str(tmp_path / "nope"), "main") is False
    assert await refs_resolvable("", "main") is False


async def test_probe_shipped_returns_none_when_the_branch_is_gone(tmp_path):
    """THE DEFECT THIS PREVENTS.

    `default_branch_shipped` returns False for a deleted branch, exactly as it
    does for content that never landed. A branch is routinely deleted AFTER a
    successful merge, so trusting that False would file merged PRs as
    `closed_unmerged` — a settled outcome that is never re-polled.
    """
    repo = _shipped_repo(tmp_path)
    _git(repo, "branch", "-D", "feature")
    # The raw probe cannot tell this from "the content never landed"...
    assert await default_branch_shipped(str(repo), "feature", "main") is False
    # ...so the recorder's probe refuses to answer instead of guessing.
    assert await po.probe_shipped(str(repo), "feature", "main") is None


async def test_probe_shipped_still_answers_true_and_false_when_it_can(tmp_path):
    shipped = _shipped_repo(tmp_path, "s")
    unshipped = _unshipped_repo(tmp_path, "u")
    assert await po.probe_shipped(str(shipped), "feature", "main") is True
    assert await po.probe_shipped(str(unshipped), "feature", "main") is False


async def test_probe_shipped_is_none_for_missing_inputs(tmp_path):
    assert await po.probe_shipped(None, "feature") is None
    assert await po.probe_shipped(str(tmp_path), None) is None
    assert await po.probe_shipped(str(tmp_path / "gone"), "feature") is None


# --------------------------------------------------------------------------- #
# 5. Recording at PR-open time
# --------------------------------------------------------------------------- #

async def test_a_forge_pr_is_recorded_open_at_open_time(store):
    await po.record_pr_opened(store, "t1", GH_URL)
    row = (await store.list_pr_outcomes(task_id="t1"))[0]
    assert row["outcome"] == po.OPEN
    assert row["outcome_evidence"] == po.EVIDENCE_OPENED_BY_AGENT
    assert row["forge"] == "github"
    assert row["pr_number"] == 86
    assert row["opened_at"]


async def test_a_local_bare_repo_pr_is_unknown_forever_and_says_why(store):
    """The bench sandbox pushes to a local bare repo. There is no forge to ask,
    so no bench spec can ever produce a merge outcome — recording `open` here
    would rebuild the original defect one level up."""
    await po.record_pr_opened(store, "t1", "local-pr://remote.git/feature")
    row = (await store.list_pr_outcomes(task_id="t1"))[0]
    assert row["outcome"] == po.UNKNOWN
    assert row["outcome_evidence"] == po.EVIDENCE_NO_FORGE
    assert row["forge"] == "local"


async def test_record_pr_opened_never_raises_when_the_store_is_broken():
    """Telemetry that can fail a delivery is worse than no telemetry."""
    class Broken:
        async def record_pr_outcome(self, **kw):
            raise RuntimeError("disk on fire")

    await po.record_pr_opened(Broken(), "t1", GH_URL)  # must not raise


async def test_observe_pr_never_raises_and_returns_the_outcome():
    class Broken:
        async def record_pr_outcome(self, **kw):
            raise RuntimeError("nope")

    assert await po.observe_pr(Broken(), "t1", GH_URL,
                               forge_state="MERGED") == po.MERGED


# --------------------------------------------------------------------------- #
# 6. The refresh sweep — including fully offline
# --------------------------------------------------------------------------- #

async def test_refresh_offline_records_unknown_and_never_invents_an_outcome(store):
    """NO GH, NO TOKEN, NO NETWORK.

    `default_pr_state` returns "" in that situation. The sweep must record
    `unknown`, keep the row unsettled so a later online run can resolve it, and
    not raise.
    """
    await po.record_pr_opened(store, "t1", GH_URL)

    async def offline_state(url):
        return ""          # exactly what default_pr_state gives with no gh

    tally = await po.refresh_outcomes(store, pr_state=offline_state)
    assert tally[po.UNKNOWN] == 1
    row = (await store.list_pr_outcomes(task_id="t1"))[0]
    assert row["outcome"] == po.UNKNOWN
    assert row["outcome_evidence"] == po.EVIDENCE_STATE_UNAVAILABLE
    # Still unsettled → it will be asked about again.
    assert await store.list_pr_outcomes(unsettled_only=True)


async def test_refresh_survives_a_raising_forge_and_keeps_sweeping(store):
    await po.record_pr_opened(store, "t1", GH_URL)
    await po.record_pr_opened(store, "t2", GH_URL2)
    seen = []

    async def flaky(url):
        seen.append(url)
        if url == GH_URL:
            raise RuntimeError("network down")
        return "MERGED"

    tally = await po.refresh_outcomes(store, pr_state=flaky)
    assert len(seen) == 2, "one bad PR must not abort the sweep"
    assert tally[po.MERGED] == 1 and tally[po.UNKNOWN] == 1


async def test_refresh_leaves_local_remote_rows_exactly_as_recorded(store):
    """Re-polling a `local-pr://` marker would ask gh about a non-URL, get ""
    back, and replace a precise evidence token with a vague one."""
    await po.record_pr_opened(store, "t1", "local-pr://remote.git/feature")

    async def boom(url):
        raise AssertionError("must not poll a local marker")

    tally = await po.refresh_outcomes(store, pr_state=boom)
    row = (await store.list_pr_outcomes(task_id="t1"))[0]
    assert row["outcome_evidence"] == po.EVIDENCE_NO_FORGE
    assert tally[po.UNKNOWN] == 1


async def test_refresh_does_not_re_poll_settled_rows(store):
    await store.record_pr_outcome(task_id="t1", pr_url=GH_URL, outcome=po.MERGED,
                                  outcome_evidence=po.EVIDENCE_FORGE_MERGED,
                                  forge="github")

    async def boom(url):
        raise AssertionError("settled rows must not be re-polled")

    tally = await po.refresh_outcomes(store, pr_state=boom)
    assert sum(tally.values()) == 0


async def test_refresh_uses_the_ship_probe_only_for_closed(store):
    await po.record_pr_opened(store, "t1", GH_URL)
    calls = []

    async def state(url):
        return "CLOSED"

    async def shipped(pr_url, task_id):
        calls.append(pr_url)
        return True

    tally = await po.refresh_outcomes(store, pr_state=state, shipped_probe=shipped)
    assert calls == [GH_URL]
    assert tally[po.MERGED] == 1
    row = (await store.list_pr_outcomes(task_id="t1"))[0]
    assert row["outcome_evidence"] == po.EVIDENCE_CONTENT_ON_BASE


async def test_refresh_with_a_none_returning_ship_probe_stays_unknown(store):
    await po.record_pr_opened(store, "t1", GH_URL)

    async def state(url):
        return "CLOSED"

    async def shipped(pr_url, task_id):
        return None          # could not tell

    tally = await po.refresh_outcomes(store, pr_state=state, shipped_probe=shipped)
    assert tally[po.UNKNOWN] == 1
    row = (await store.list_pr_outcomes(task_id="t1"))[0]
    assert row["outcome"] == po.UNKNOWN
    assert row["outcome_evidence"] == po.EVIDENCE_CLOSED_SHIP_UNVERIFIED


# --------------------------------------------------------------------------- #
# 7. The wake watcher — the automatic refresh path
# --------------------------------------------------------------------------- #

async def test_wake_records_a_merge_when_the_forge_says_merged(store):
    t = await _task(store)

    async def pr_state(url):
        return "MERGED"

    out = await WakeWatcher(store, {}, pr_state=pr_state)._check_open_pr(t)
    assert out == "merged"
    row = (await store.list_pr_outcomes(task_id=t.id))[0]
    assert row["outcome"] == po.MERGED


async def test_wake_records_a_squash_merged_closed_pr_as_merged(tmp_path, store):
    repo = _shipped_repo(tmp_path)
    t = await _task(store, repo_path=repo)

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=default_branch_shipped)
    assert await w._check_open_pr(t) == "shipped_pr_closed"
    row = (await store.list_pr_outcomes(task_id=t.id))[0]
    assert row["outcome"] == po.MERGED
    assert row["outcome_evidence"] == po.EVIDENCE_CONTENT_ON_BASE


async def test_wake_never_settles_closed_unmerged_from_an_ambiguous_probe(
        tmp_path, store):
    """🔴 THE REGRESSION GUARD.

    `default_branch_shipped` returns False both for "not on base" and for "could
    not run". The watcher cannot tell them apart, so it must record `unknown`
    (unsettled, re-pollable) rather than `closed_unmerged` (settled, final).
    The task still ESCALATES exactly as before — this changes the RECORD only.
    """
    repo = _unshipped_repo(tmp_path)
    t = await _task(store, repo_path=repo)

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=default_branch_shipped)
    assert await w._check_open_pr(t) == "escalated_pr_closed"
    assert (await store.get_task(t.id)).status is TaskStatus.ESCALATED

    row = (await store.list_pr_outcomes(task_id=t.id))[0]
    assert row["outcome"] == po.UNKNOWN, (
        "a False from default_branch_shipped is 'cannot tell', not "
        "'content absent' — settling it would permanently mislabel merged PRs "
        "whose branch was deleted")
    assert row["outcome_evidence"] == po.EVIDENCE_CLOSED_SHIP_UNVERIFIED
    # Unsettled, so `nh pr-outcomes refresh` will resolve it properly later.
    assert await store.list_pr_outcomes(unsettled_only=True)


async def test_wake_with_no_ship_probe_records_unknown_not_closed(store):
    t = await _task(store)

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state)   # no pr_shipped hook at all
    assert await w._check_open_pr(t) == "escalated_pr_closed"
    row = (await store.list_pr_outcomes(task_id=t.id))[0]
    assert row["outcome"] == po.UNKNOWN


async def test_wake_offline_records_unknown_and_does_not_disturb_the_task(store):
    """No gh: `default_pr_state` yields "". The ladder must not act on it."""
    t = await _task(store)

    async def pr_state(url):
        return ""

    w = WakeWatcher(store, {}, pr_state=pr_state)
    await w._check_open_pr(t)
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL
    row = (await store.list_pr_outcomes(task_id=t.id))[0]
    assert row["outcome"] == po.UNKNOWN


async def test_wake_does_not_erase_a_known_merge_when_the_forge_goes_dark(store):
    """The end-to-end shape of the no-downgrade rule, through the live path."""
    t = await _task(store)
    await store.record_pr_outcome(task_id=t.id, pr_url=GH_URL, outcome=po.MERGED,
                                  outcome_evidence=po.EVIDENCE_FORGE_MERGED,
                                  forge="github")

    async def pr_state(url):
        return ""            # gh uninstalled / token expired / offline

    await WakeWatcher(store, {}, pr_state=pr_state)._check_open_pr(t)
    row = (await store.list_pr_outcomes(task_id=t.id))[0]
    assert row["outcome"] == po.MERGED


# --------------------------------------------------------------------------- #
# 8. The report: three figures, never one
# --------------------------------------------------------------------------- #

async def test_delivered_does_not_even_imply_a_pr_exists(store):
    """The old metric's real weakness, pinned.

    Two of the three orchestrator paths to AWAITING_APPROVAL open no PR at all
    (the "already satisfied" path and the code-review path). So `delivered`
    over-counts even "produced a pull request", let alone "merged".
    """
    await _task(store)                      # AWAITING_APPROVAL, no PR recorded
    rep = await compute_pr_outcome_metrics(store)
    assert rep.delivered_tasks == 1
    assert rep.tasks_with_recorded_pr == 0
    assert rep.tasks_without_recorded_pr == 1
    assert rep.merged_tasks == 0


async def test_unknown_never_inflates_the_merged_figure(store):
    t1 = await _task(store)
    t2 = await _task(store, url=GH_URL2)
    await store.record_pr_outcome(task_id=t1.id, pr_url=GH_URL, outcome=po.MERGED,
                                  outcome_evidence=po.EVIDENCE_FORGE_MERGED)
    await store.record_pr_outcome(task_id=t2.id, pr_url=GH_URL2,
                                  outcome=po.UNKNOWN,
                                  outcome_evidence=po.EVIDENCE_STATE_UNAVAILABLE)
    rep = await compute_pr_outcome_metrics(store)
    assert rep.delivered_tasks == 2
    assert rep.merged_tasks == 1
    assert rep.unknown_tasks == 1
    assert rep.settled_tasks == 1, "an unknown must not enter the settled denominator"


async def test_zero_settled_renders_as_not_measurable_never_as_zero_percent(store):
    """"We measured none of these" and "none of these landed" are opposite
    facts and must never render the same.

    🔴 FOUND BY RUNNING IT, not by reading it. The first version gated only the
    `merged/settled` line and happily printed `merged / delivered 0/94 = 0%`
    over a database where nothing had been measured at all — a confident 0%
    that reads as "no_human's work never lands". That is the day-one state of
    this table on any pre-existing database, so it is the state most readers
    meet it in. NEITHER line may show a percentage until something is settled.
    """
    for _ in range(3):
        await _task(store)
    rep = await compute_pr_outcome_metrics(store)
    text = "\n".join(render_pr_outcome_lines(rep))
    assert "This is NOT a 0% merge rate" in text
    assert "0%" not in text.replace("NOT a 0% merge rate", ""), (
        "no percentage may be printed when nothing has a known fate")
    assert text.count("NOT YET MEASURABLE") == 2, "both denominators must abstain"


async def test_a_percentage_appears_once_something_is_actually_settled(store):
    """The other half of the rule: abstaining forever would be useless."""
    t = await _task(store)
    await store.record_pr_outcome(task_id=t.id, pr_url=GH_URL, outcome=po.MERGED,
                                  outcome_evidence=po.EVIDENCE_FORGE_MERGED)
    text = "\n".join(render_pr_outcome_lines(
        await compute_pr_outcome_metrics(store)))
    assert "NOT YET MEASURABLE" not in text
    assert "merged / delivered   1/1  = 100%" in text
    assert "merged / settled     1/1  = 100%" in text


async def test_the_report_keeps_the_three_figures_separate(store):
    t1 = await _task(store)
    await store.record_pr_outcome(task_id=t1.id, pr_url=GH_URL, outcome=po.MERGED,
                                  outcome_evidence=po.EVIDENCE_FORGE_MERGED)
    rep = await compute_pr_outcome_metrics(store)
    text = "\n".join(render_pr_outcome_lines(rep))
    # Both denominators appear, each named — no single blended percentage.
    assert "merged / delivered" in text
    assert "merged / settled" in text
    assert "unknown (not measured)" in text


async def test_every_rendered_figure_carries_both_caveats(store):
    """The caveats live in ONE string with one owner so they cannot be present
    on `nh pr-outcomes show` and missing from `nh bench report`."""
    await _task(store)
    text = "\n".join(render_pr_outcome_lines(
        await compute_pr_outcome_metrics(store)))
    assert po.MERGED_CAVEAT in text
    assert po.UNKNOWN_CAVEAT in text


def test_the_merged_caveat_says_what_merged_does_not_establish():
    """The whole point of the exercise: do not let 'merged' be read as the
    north star's 'success at high quality and low cost'."""
    c = po.MERGED_CAVEAT
    assert "does NOT establish quality" in c
    assert "may have reviewed, corrected, rebased or" in c
    assert "cost" in c


async def test_a_multi_repo_task_rolls_up_conservatively(store):
    t = await _task(store)
    await store.record_pr_outcome(task_id=t.id, pr_url=GH_URL, outcome=po.MERGED,
                                  outcome_evidence=po.EVIDENCE_FORGE_MERGED)
    await store.record_pr_outcome(task_id=t.id, pr_url=GH_URL2,
                                  outcome=po.UNKNOWN,
                                  outcome_evidence=po.EVIDENCE_STATE_UNAVAILABLE)
    rep = await compute_pr_outcome_metrics(store)
    assert rep.total_prs == 2
    assert rep.tasks_with_recorded_pr == 1
    assert rep.merged_tasks == 0, "half-measured delivery is not a landed one"
    assert rep.unknown_tasks == 1


async def test_only_delivered_tasks_are_in_the_population(store):
    """The question is 'of the things we called successes, how many landed'."""
    await _task(store, status=TaskStatus.FAILED)
    await _task(store, status=TaskStatus.ESCALATED, url=GH_URL2)
    rep = await compute_pr_outcome_metrics(store)
    assert rep.delivered_tasks == 0


async def test_report_survives_a_store_without_the_table(store, monkeypatch):
    """Telemetry must degrade, not crash, if the read fails."""
    await _task(store)

    async def boom(**kw):
        raise RuntimeError("no such table")

    monkeypatch.setattr(store, "list_pr_outcomes", boom)
    rep = await compute_pr_outcome_metrics(store)
    assert rep.delivered_tasks == 1
    assert rep.tasks_without_recorded_pr == 1


# --------------------------------------------------------------------------- #
# 9. `nh bench report` surfaces it even when the CARD is refused
# --------------------------------------------------------------------------- #

def _cli_env(tmp_path, monkeypatch):
    """Point `bench report` at an empty results dir and a throwaway database."""
    import types

    import no_human.cli.commands as cmds
    import no_human.eval.northstar_card as nc

    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setattr(nc, "RESULTS_DIR", results)
    monkeypatch.setattr(nc, "REPORT_MD", tmp_path / "REPORT.md")
    cfg = types.SimpleNamespace(db_path=tmp_path / "nh.db")
    monkeypatch.setattr(cmds, "_bootstrap", lambda **kw: (cfg, None))
    return cfg


def test_bench_report_still_prints_pr_outcomes_when_the_card_is_refused(
        tmp_path, monkeypatch):
    """🔴 The block must NOT be gated behind a successful card render.

    It reads a different population (real tasks) from a different source (the
    `pr_outcomes` table) than the card, so the card's verdict says nothing
    about it. Every results file on disk when this was written was refused by
    the publish-refusal checks, so gating it there would have made the figure
    effectively unreachable — the honest number hidden behind an unrelated
    guard.
    """
    from click.testing import CliRunner

    from no_human.cli.commands import cli

    _cli_env(tmp_path, monkeypatch)          # no latest.json → refusal path
    res = CliRunner().invoke(cli, ["bench", "report"])

    assert res.exit_code == 1, res.output
    assert "no results yet" in res.output, "the refusal itself must survive"
    assert "PR OUTCOME" in res.output
    assert "NOT YET MEASURABLE" in res.output
    assert "not the bench corpus" in res.output, (
        "the two populations must be labelled as different")


def test_the_pr_outcome_block_never_fails_the_command(tmp_path, monkeypatch):
    """A telemetry block that can crash `bench report` is worse than no block."""
    import no_human.cli.commands as cmds

    monkeypatch.setattr(cmds, "_bootstrap",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("no config")))
    cmds._print_pr_outcome_block()           # must not raise


def test_autonomy_docstring_states_what_pr_reached_is_not():
    """A docstring's own sentence is a free test, and this one was wrong before:
    it claimed AWAITING_APPROVAL/DONE meant 'reached a reviewable PR (success)'."""
    # Whitespace-normalised: the sentence is line-wrapped in the source, and a
    # guard that breaks on re-wrapping is a guard that gets deleted.
    doc = " ".join((autonomy.__doc__ or "").split())
    assert "only one of them opens a pull request" in doc
    assert "contains no forge query" in doc


def test_open_with_content_on_base_is_merged_not_open():
    """Review finding E (2026-08-11). The wake watcher can now observe a PR
    that the forge still calls OPEN while its content is demonstrably on the
    base — the supervised local squash lands before the PR is closed, and
    `wake.py`'s CONFLICTING rung completes the task there.

    Dropping `shipped` for OPEN filed that landing as `open` FOREVER: the row
    is unsettled, but `refresh_outcomes` only probes containment for CLOSED
    PRs, so nothing would ever revisit it, and a task recorded DONE would sit
    behind an `open` outcome permanently. The same positive evidence that
    settles a CLOSED PR settles this one — it is the same probe, the same
    both-directions tree containment, and the same evidence token."""
    assert po.classify_outcome("OPEN", shipped=True) == (
        po.MERGED, po.EVIDENCE_CONTENT_ON_BASE)


def test_open_without_positive_containment_stays_open():
    """The other three answers must not move: only a POSITIVE containment
    reclassifies. `False` is the overloaded 'absent OR could not run' and an
    open PR is the normal state of affairs, not a failed merge."""
    assert po.classify_outcome("OPEN", shipped=False) == (
        po.OPEN, po.EVIDENCE_FORGE_OPEN)
    assert po.classify_outcome("OPEN", shipped=None) == (
        po.OPEN, po.EVIDENCE_FORGE_OPEN)
    assert po.classify_outcome("OPEN") == (po.OPEN, po.EVIDENCE_FORGE_OPEN)


def test_positive_containment_settles_even_when_the_forge_state_is_unreadable():
    """The `""` state (no gh / network error / unparseable ref) beside a
    POSITIVE containment probe. The probe did run and did answer; the forge is
    what failed. Recording `unknown` there would file a task the watcher is
    completing on that very evidence as unmeasured."""
    assert po.classify_outcome("", shipped=True) == (
        po.MERGED, po.EVIDENCE_CONTENT_ON_BASE)
    assert po.classify_outcome(None, shipped=True) == (
        po.MERGED, po.EVIDENCE_CONTENT_ON_BASE)


def test_a_negative_or_absent_probe_never_settles_a_non_closed_pr():
    """The asymmetry that keeps `False` from inventing failures: only a forge
    that independently says CLOSED lets a `False` settle anything."""
    for state in ("", None, "OPEN", "DRAFT"):
        for shipped in (False, None):
            outcome, _ = po.classify_outcome(state, shipped=shipped)
            assert outcome != po.CLOSED_UNMERGED, (state, shipped)
            assert outcome != po.MERGED, (state, shipped)


def test_the_forge_merged_flag_still_owns_its_own_evidence_token():
    """Provenance: a forge-merged PR reports `forge_merged` even when the
    content probe also says yes — the token must name what was observed."""
    assert po.classify_outcome("MERGED", shipped=True) == (
        po.MERGED, po.EVIDENCE_FORGE_MERGED)
