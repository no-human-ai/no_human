"""GET /api/bench/latest — the bench card, finally reachable from the UI.

The web app's north-star row reads /api/metrics, which carries TASK counters.
The bench card was never served, so a reader could not see the two things that
decide whether a headline means anything: how much of the corpus went
unmeasured, and whether the run was publishable at all.
"""

from __future__ import annotations

import json
import logging

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from no_human.api.app import app
from no_human.eval.northstar import BenchScore
from no_human.eval.northstar_card import NorthStarCard


def _score(task_id: str, *, nh_tokens: int = 20_000, status: str = "done",
           satisfied: bool = True, gated: bool = False) -> BenchScore:
    return BenchScore(
        task_id=task_id, title=task_id, outcome_status=status,
        goal_satisfied=satisfied, escalated_honestly=False, mergeable=None,
        nh_tokens=nh_tokens, nh_cache_tokens=0, nh_cache_creation_tokens=0,
        nh_turns=1, nh_wall_clock_s=1.0, orig_tokens=100_000,
        orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=1.0, orig_corrections=0,
        expected_escalation=gated,
    )


@pytest_asyncio.fixture
async def client(store_factory, tmp_path, monkeypatch):
    from no_human.config import load_config
    import no_human.eval.northstar_card as nc

    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setattr(nc, "RESULTS_DIR", results)
    store = await store_factory("test.db")
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        c.results = results        # type: ignore[attr-defined]
        yield c


@pytest.mark.asyncio
async def test_no_run_recorded_is_a_404_not_an_empty_card(client):
    """An all-zero card renders as a catastrophic result. "Nothing has run" and
    "everything failed" must not look the same to the UI."""
    r = await client.get("/api/bench/latest")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_it_serves_the_card_the_ui_cannot_otherwise_reach(client):
    scores = [_score(f"ns-{i}") for i in range(12)]
    NorthStarCard(scores=scores, label="v13",
                  created_at="2026-07-21T00:00:00+00:00").save(
        client.results / "latest.json")

    body = (await client.get("/api/bench/latest")).json()
    assert body["label"] == "v13"
    assert body["total"] == 12
    assert body["success_rate"] == 1.0
    assert body["refusals"] == []          # a healthy run is publishable


@pytest.mark.asyncio
async def test_honest_escalation_carries_its_denominator(client):
    """THE point of the field. 2/2 and 14/14 are both "100%"; the first is what
    a broken corpus produces once the other 12 fail to resolve and leave the
    denominator. The UI must be able to tell them apart."""
    scores = [_score(f"ok-{i}") for i in range(10)]
    scores += [_score(f"gate-{i}", gated=True, satisfied=True) for i in range(2)]
    NorthStarCard(scores=scores, label="narrow").save(
        client.results / "latest.json")

    body = (await client.get("/api/bench/latest")).json()
    assert body["honest_escalation_rate"] == 1.0
    assert body["honest_escalations"] == 2
    assert body["escalation_specs"] == 2


@pytest.mark.asyncio
async def test_a_partly_honest_run_reports_both_numbers(client):
    scores = [_score(f"ok-{i}") for i in range(10)]
    scores += [_score("g1", gated=True, satisfied=True),
               _score("g2", gated=True, satisfied=False)]
    NorthStarCard(scores=scores).save(client.results / "latest.json")

    body = (await client.get("/api/bench/latest")).json()
    assert body["honest_escalations"] == 1
    assert body["escalation_specs"] == 2
    assert body["honest_escalation_rate"] == 0.5


@pytest.mark.asyncio
async def test_unmeasured_specs_are_reportable(client):
    """skipped + dead_specs over total is what "N of M unmeasured" needs."""
    scores = [_score(f"ok-{i}") for i in range(8)]
    scores += [_score(f"sk-{i}", status="skipped", satisfied=False, nh_tokens=0)
               for i in range(3)]
    scores += [_score("dead-1", nh_tokens=0)]      # ran, burned nothing
    NorthStarCard(scores=scores).save(client.results / "latest.json")

    body = (await client.get("/api/bench/latest")).json()
    assert body["total"] == 12
    assert body["skipped"] == 3
    assert body["dead_specs"] == 1


@pytest.mark.asyncio
async def test_refusals_are_computed_fresh_and_verbatim(client):
    """Computed from the stored card on every read, so it cannot go stale
    against the rules that produce it. The v14 shape: one spec worked, then the
    quota saturated."""
    scores = [_score("ns-0")] + [
        _score(f"ns-{i}", nh_tokens=0, status="escalated", satisfied=False)
        for i in range(1, 40)]
    NorthStarCard(scores=scores, label="v14").save(
        client.results / "latest.json")

    body = (await client.get("/api/bench/latest")).json()
    assert body["refusals"], "a saturated run must not report as publishable"
    assert any("zero tokens" in r for r in body["refusals"])


@pytest.mark.asyncio
async def test_a_forced_publish_is_visible_to_the_ui(client):
    """`nh bench publish --force` is allowed, but a forced publish must never
    be able to look like a clean one. The reasons are stored on the card and
    the UI renders them as an alarm — which requires the endpoint to serve
    them at all."""
    card = NorthStarCard(
        scores=[_score(f"ns-{i}") for i in range(12)], label="forced",
        override_reasons=["only 11 of 55 corpus specs loaded"])
    card.save(client.results / "latest.json")

    body = (await client.get("/api/bench/latest")).json()
    assert body["override_reasons"] == ["only 11 of 55 corpus specs loaded"]
    # And the run is otherwise healthy, so the override list is the ONLY thing
    # marking it — a reader who does not get it sees a clean card.
    assert body["refusals"] == []


@pytest.mark.asyncio
async def test_an_unreadable_card_is_not_a_404(client):
    """"No bench run yet" and "the recorded run is corrupt" are opposite
    states: one is idle, one is a broken instrument. `NorthStarCard.load`
    returns None for both a missing file and an unparseable one, so routing
    both to 404 reported the broken instrument as an idle one."""
    (client.results / "latest.json").write_text("{ this is not json")

    r = await client.get("/api/bench/latest")
    assert r.status_code == 200, r.text        # not 404, and not a 500 either
    body = r.json()
    assert body["refusals"], "a corrupt card must not read as publishable"
    assert any("could not be read" in x for x in body["refusals"])
    # Every figure is zeroed rather than absent, so the UI cannot render a
    # stale or partial number next to the alarm.
    assert body["total"] == 0 and body["success_rate"] == 0.0


@pytest.mark.asyncio
async def test_a_card_whose_schema_drifted_is_also_reported_not_a_500(client):
    """Valid JSON, wrong shape. `load` subscripts each score hard, so this
    raised out of the endpoint as a 500 and blanked the panel with no reason."""
    (client.results / "latest.json").write_text(
        '{"label": "x", "scores": [{"nope": 1}]}')

    r = await client.get("/api/bench/latest")
    assert r.status_code == 200, r.text
    assert any("could not be read" in x for x in r.json()["refusals"])


@pytest.mark.asyncio
async def test_the_unreadable_payload_has_the_SAME_KEYS_as_a_healthy_one(
        client):
    """The error path must answer the same shape as the healthy path. A UI
    reading `body.dead_specs` would break on the corrupt card — i.e. the panel
    would fail in exactly the situation this branch exists to explain."""
    NorthStarCard(scores=[_score(f"ns-{i}") for i in range(12)]).save(
        client.results / "latest.json")
    healthy = set((await client.get("/api/bench/latest")).json())

    (client.results / "latest.json").write_text("not json at all")
    broken = set((await client.get("/api/bench/latest")).json())

    assert healthy == broken, healthy ^ broken
    # Guard against the degenerate pass where BOTH are tiny: the healthy
    # payload must actually carry the fields the UI reads.
    assert {"total", "skipped", "dead_specs", "success_rate", "refusals",
            "override_reasons", "corpus_available", "escalation_specs",
            "honest_escalations"} <= healthy


@pytest.mark.asyncio
async def test_the_escalation_rate_is_null_not_100_percent_when_none_are_gated(
        client):
    """With no gated specs the rate is a CONVENTION (1.0), not a measurement.
    Serving 1.0 puts a "100%" in the UI's honest-escalation slot that no spec
    earned — the same unreadable 100% the numerator/denominator pair exists to
    kill, one layer down. null renders as "—"."""
    NorthStarCard(scores=[_score(f"ns-{i}") for i in range(12)]).save(
        client.results / "latest.json")

    body = (await client.get("/api/bench/latest")).json()
    assert body["escalation_specs"] == 0, "fixture must have no gated specs"
    assert body["honest_escalation_rate"] is None
    # The card's own property still returns the 1.0 convention — this is a
    # presentation rule at the boundary, not a change to the measurement.
    assert NorthStarCard.load(
        client.results / "latest.json").honest_escalation_rate == 1.0


@pytest.mark.asyncio
async def test_corpus_available_reaches_the_wire_and_is_not_hardcoded(client):
    """The UI's under-corpus alarm (`corpus > 0 && total < corpus`) is the one
    that catches a run filtered down to the specs that happen to resolve —
    which reads as perfect coverage and is not.

    The endpoint used to re-supply this field by hand after `**agg`, overriding
    a value #197 guards with one nothing tested for a non-zero result. Pinning
    the real number here means the wire hop has its own mutant.
    """
    card = NorthStarCard(scores=[_score(f"ns-{i}") for i in range(11)])
    card.corpus_available = 55
    card.save(client.results / "latest.json")

    body = (await client.get("/api/bench/latest")).json()
    assert body["corpus_available"] == 55
    assert body["total"] == 11          # loaded < available: the alarm state


@pytest.mark.asyncio
async def test_corpus_available_is_zero_on_a_card_that_never_recorded_it(client):
    """0 means "unknown", which the UI renders as "unknown (older card)" —
    never as "the corpus has no specs"."""
    NorthStarCard(scores=[_score(f"ns-{i}") for i in range(12)]).save(
        client.results / "latest.json")
    body = (await client.get("/api/bench/latest")).json()
    assert body["corpus_available"] == 0


# ------------------------- SCRUM-25: published baseline -------------------- #

@pytest.mark.asyncio
async def test_no_published_baseline_file_keeps_todays_behavior(client):
    """No `published_baseline.json` at all (older results dir, or a repo that
    never had a clean publish) — the endpoint must answer exactly as it always
    did: no `published`/`latest_run` keys, `latest.json`'s own card at the top
    level, THE REGRESSION TEST for this task."""
    NorthStarCard(scores=[_score(f"ns-{i}") for i in range(12)], label="v13",
                  created_at="2026-07-20T00:00:00+00:00").save(
        client.results / "latest.json")

    body = (await client.get("/api/bench/latest")).json()
    assert body["label"] == "v13"
    assert body["total"] == 12
    assert "published" not in body
    assert "latest_run" not in body


@pytest.mark.asyncio
async def test_a_refused_probe_over_a_clean_baseline_headlines_the_baseline(
        client):
    """THE bug (live 2026-07-24): a 1-spec health probe force-published over
    the real baseline used to become the ONLY card the endpoint could serve.
    With `published_baseline.json` recording the last CLEAN publish, the
    endpoint must headline that baseline and demote the probe to `latest_run`
    — never the reverse."""
    baseline = NorthStarCard(
        scores=[_score(f"ns-{i}") for i in range(30)], label="v14",
        created_at="2026-07-20T00:00:00+00:00")
    baseline.save(client.results / "published_baseline.json")

    probe = NorthStarCard(
        scores=[_score("health-0", nh_tokens=0)], label="health-probe",
        created_at="2026-07-24T09:00:00+00:00",
        override_reasons=["only 1 spec(s) ran (minimum 10) — a probe or a "
                          "capped run is a slice, not the corpus"])
    probe.save(client.results / "latest.json")

    body = (await client.get("/api/bench/latest")).json()
    assert body["label"] == "v14", "the probe must never become the headline"
    assert body["total"] == 30
    assert body["published"] is True
    assert body["latest_run"]["label"] == "health-probe"
    assert body["latest_run"]["override_reasons"], \
        "the probe's own refusal reasons must reach the footnote"


@pytest.mark.asyncio
async def test_latest_run_is_omitted_when_it_IS_the_published_baseline(
        client):
    """A clean publish writes BOTH files with the same card — there is no
    newer/different run to report, so `latest_run` must not appear (it would
    otherwise render as a pointless footnote duplicating the headline)."""
    card = NorthStarCard(scores=[_score(f"ns-{i}") for i in range(12)],
                         label="v13", created_at="2026-07-20T00:00:00+00:00")
    card.save(client.results / "latest.json")
    card.save(client.results / "published_baseline.json")

    body = (await client.get("/api/bench/latest")).json()
    assert body["published"] is True
    assert "latest_run" not in body


@pytest.mark.asyncio
async def test_hand_deleted_latest_still_serves_the_published_baseline(client):
    """Review 2026-07-25 residue: latest.json hand-deleted while
    published_baseline.json is intact must NOT 404 — the clean baseline is a
    recorded run and the UI must keep headlining it."""
    baseline = NorthStarCard(
        scores=[_score(f"ns-{i}") for i in range(30)], label="v14",
        created_at="2026-07-20T00:00:00+00:00")
    baseline.save(client.results / "published_baseline.json")

    r = await client.get("/api/bench/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == "v14"
    assert body["published"] is True
    assert "latest_run" not in body
