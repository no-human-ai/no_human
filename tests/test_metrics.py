"""The measurement spine: north-star numbers straight from the record."""

from __future__ import annotations

import time

import pytest

from no_human.agent.verification_receipts import VerificationReceipt
from no_human.core.metrics import (
    cache_read_share, compute_metrics, verification_receipt_rate,
)
from no_human.core.task import Task


def _ev(kind: str, **extra) -> dict:
    return {"source": "test", "kind": kind, "text": extra.pop("text", ""),
            "ts": time.time(), **extra}


async def test_empty_db_yields_zeros_not_crashes(store):
    m = await compute_metrics(store)
    assert m["prs_opened"] == 0 and m["prs_merged"] == 0
    assert m["attempts_per_pr"] is None and m["tokens_per_pr"] is None
    assert m["error_breakdown"] == {}


async def test_error_breakdown_groups_agent_errors_by_class(store):
    """0.2/0.3: /api/metrics surfaces WHERE attempts waste tokens — refusal
    (fail-fast) vs retryable rate_limited vs a genuine error; pre-0.3 events
    with no class fall into 'unclassified'."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("agent_error", error_class="refusal"),
        _ev("agent_error", error_class="refusal"),
        _ev("agent_error", error_class="rate_limited"),
        _ev("agent_error"),  # no class → unclassified
        _ev("review", passed=1),  # unrelated kind must not count
    ])
    eb = (await compute_metrics(store))["error_breakdown"]
    assert eb == {"refusal": 2, "rate_limited": 1, "unclassified": 1}


async def test_the_north_star_numbers_add_up(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    a1 = await store.create_attempt(t.id, attempt_number=1)
    a2 = await store.create_attempt(t.id, attempt_number=2)
    await store.update_attempt(a1, tokens_used=100, cache_read_tokens=1000,
                               auth_profile="personal")
    await store.update_attempt(a2, tokens_used=50, cache_read_tokens=500,
                               auth_profile="personal",
                               pr_url="https://forge/pr/1")
    await store.save_events(t.id, [
        _ev("review", passed=0), _ev("review", passed=1),
        _ev("merged", text="PR merged by a human"),
        _ev("repro_gate", verdict="waived"),
        _ev("attempt_failed", text="review failed: off-by-one in parser"),
    ])

    m = await compute_metrics(store)

    assert m["prs_opened"] == 1 and m["prs_merged"] == 1
    assert m["attempts_total"] == 2 and m["attempts_per_pr"] == 2.0
    assert m["tokens_per_pr"] == 1650  # (100+50 tokens) + (1000+500 cache)
    prof = {p["profile"]: p for p in m["by_auth_profile"]}
    assert prof["personal"]["attempts"] == 2
    assert m["review_pass"] == 1 and m["review_fail"] == 1
    assert m["repro_gate_verdicts"] == {"waived": 1}
    assert any("off-by-one" in r for r in m["recent_rejection_reasons"])


async def test_ci_gate_counts_come_from_the_events(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("ci_gate_trigger"), _ev("ci_gate_trigger"),
        _ev("ci_gate_pass"), _ev("ci_gate_fail"),
    ])
    m = await compute_metrics(store)
    assert m["ci_gate"] == {"triggered": 2, "passed": 1, "failed": 1}


async def test_ci_gate_block_is_zeroes_on_an_empty_db(store):
    m = await compute_metrics(store)
    assert m["ci_gate"] == {"triggered": 0, "passed": 0, "failed": 0}


async def test_cache_economics_expose_the_creation_share(store):
    """P0.3: a rising cache_creation share means the prompt prefix is being
    rebuilt (full price) instead of reused (~10% price) — the number every
    context change must be judged against."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    a1 = await store.create_attempt(t.id, attempt_number=1)
    a2 = await store.create_attempt(t.id, attempt_number=2)
    await store.update_attempt(a1, cache_creation_tokens=100_000,
                               cache_read_tokens=900_000)
    await store.update_attempt(a2, cache_creation_tokens=300_000,
                               cache_read_tokens=700_000)
    m = await compute_metrics(store)
    ce = m["cache_economics"]
    assert ce["attempts_measured"] == 2
    assert ce["cache_creation_total"] == 400_000
    assert ce["cache_read_total"] == 1_600_000
    assert ce["creation_per_attempt"] == 200_000
    assert ce["creation_share"] == 0.2


async def test_cache_economics_zero_state(store):
    m = await compute_metrics(store)
    assert m["cache_economics"]["attempts_measured"] == 0
    assert m["cache_economics"]["creation_share"] is None


async def test_metrics_expose_the_three_cost_buckets(store):
    """The UI cannot price a burn honestly from ``tokens_per_pr`` alone.

    ``tokens_per_pr`` is ``(tokens_used + cache_read) // prs_OPENED`` — it excludes
    cache-CREATION entirely and divides by opened, not merged. Pricing it as if it were
    fresh tokens (or splitting it by ``creation_share``, whose universe is
    ``creation + read``) is a category error: the Stats page ended up showing two blended
    rates 20% apart on the same screen. Emit the raw buckets so ONE cost function can
    serve every surface and they cannot drift apart.
    """
    t = Task.new("t", repo_path="/r")
    await store.create_task(t)
    a = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        a, tokens_used=150, cache_read_tokens=1500, cache_creation_tokens=90,
        pr_url="https://forge/pr/9",
    )

    m = await compute_metrics(store)

    assert m["tokens_used_total"] == 150          # in + out only
    assert m["cache_economics"]["cache_creation_total"] == 90
    assert m["cache_economics"]["cache_read_total"] == 1500
    assert m["prs_opened"] == 1 and m["prs_merged"] == 0


async def test_reviewer_tokens_are_persisted_and_priced(store):
    """The reviewer's burn was thrown away, so no cost surface could see it.

    ``_run_review`` returns a ReviewDecision carrying tokens_used / cache_read_tokens /
    cache_creation_tokens, and the orchestrator never wrote them down: the DB held the CODER's
    tokens only. The Stats tile is the first surface to make an absolute dollar claim, so it had
    to say "coder spend" — with 59 Opus-4-8 review runs over full diffs costing nothing on the
    record. Persist them in their OWN columns (coder attribution stays intact for by_tier) and
    surface the totals so the UI can price true spend.
    """
    t = Task.new("t", repo_path="/r")
    await store.create_task(t)
    a = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        a, tokens_used=100, cache_read_tokens=1000, cache_creation_tokens=50,
        review_tokens_used=30, review_cache_read_tokens=400, review_cache_creation_tokens=20,
        pr_url="https://forge/pr/1",
    )

    m = await compute_metrics(store)

    # The coder's buckets are untouched...
    assert m["tokens_used_total"] == 100
    assert m["cache_economics"]["cache_creation_total"] == 50
    assert m["cache_economics"]["cache_read_total"] == 1000
    # ...and the reviewer's are now on the record, separately.
    assert m["review_tokens_used_total"] == 30
    assert m["review_cache_creation_total"] == 20
    assert m["review_cache_read_total"] == 400


# --------------------------------------------------------------------------- #
# cache_read_share — the per-attempt reuse ratio (the earliest signal an
# attempt is heading for budget exhaustion). A SIBLING of cache_economics'
# fleet-wide creation_share, not a replacement for it.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("read, creation, expected", [
    (900_000, 100_000, 0.9),
    (0, 0, None),
    (None, None, None),
    (0, 100, 0.0),
    (100, 0, 1.0),
    (None, 100, 0.0),
    (100, None, 1.0),
])
def test_cache_read_share_helper(read, creation, expected):
    assert cache_read_share(read, creation) == expected


def test_cache_read_share_is_the_complement_of_creation_share():
    """cache_economics['creation_share'] and cache_read_share() are the SAME
    ratio's two ends over the SAME denominator (read + creation) — they must
    sum to 1.0, or a reader could believe they were computed over different
    universes."""
    read, creation = 700_000, 300_000
    creation_share = round(creation / (creation + read), 4)
    assert cache_read_share(read, creation) == round(1 - creation_share, 4)


def test_cache_read_share_has_no_second_definition():
    """The helper lives in core/metrics.py; every other consumer (api/models.py,
    eval/northstar_card.py) imports it rather than re-deriving the division,
    and no JS file re-implements the same read/(read+creation) arithmetic."""
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    py_defs = 0
    for py in (repo / "src").rglob("*.py"):
        if "def cache_read_share(" in py.read_text(encoding="utf-8"):
            py_defs += 1
    assert py_defs == 1, "cache_read_share must be defined exactly once"

    js_division_pattern = re.compile(
        r"cache_read.*/\s*\(.*cache_read.*\+.*cache_creation", re.IGNORECASE)
    for js in (repo / "web" / "src").glob("*.js"):
        assert not js_division_pattern.search(js.read_text(encoding="utf-8")), (
            f"{js} appears to re-derive the cache-read-share ratio locally — "
            f"it must consume the value the API already computed instead"
        )


async def test_cache_read_share_distribution_is_per_attempt(store):
    """Distinct from the fleet cache_economics['creation_share']: this is a
    DISTRIBUTION over per-attempt ratios (p50/p90), the earliest signal a
    single attempt — not the whole fleet — is heading for budget exhaustion."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    a1 = await store.create_attempt(t.id, attempt_number=1)
    a2 = await store.create_attempt(t.id, attempt_number=2)
    a3 = await store.create_attempt(t.id, attempt_number=3)
    # shares: 0.5, 0.8, 0.9
    await store.update_attempt(a1, cache_read_tokens=50, cache_creation_tokens=50)
    await store.update_attempt(a2, cache_read_tokens=80, cache_creation_tokens=20)
    await store.update_attempt(a3, cache_read_tokens=90, cache_creation_tokens=10)

    m = await compute_metrics(store)
    dist = m["cache_read_share_dist"]
    assert dist["attempts_measured"] == 3
    assert dist["p50"] == 0.8
    assert dist["p90"] == 0.9
    # The fleet ratio stays a SEPARATE key, unaffected in shape by this one.
    assert "creation_share" in m["cache_economics"]
    assert set(dist) == {"attempts_measured", "p50", "p90"}


async def test_cache_read_share_distribution_zero_state(store):
    m = await compute_metrics(store)
    assert m["cache_read_share_dist"] == {
        "attempts_measured": 0, "p50": None, "p90": None,
    }


async def test_cache_economics_keys_are_unchanged(store):
    """cache_economics itself must stay byte-identical — the new distribution
    is a SIBLING key on the metrics dict, never merged into this one."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    a1 = await store.create_attempt(t.id, attempt_number=1)
    await store.update_attempt(a1, cache_creation_tokens=100_000,
                               cache_read_tokens=900_000)
    m = await compute_metrics(store)
    assert set(m["cache_economics"]) == {
        "attempts_measured", "cache_creation_total", "cache_read_total",
        "creation_per_attempt", "read_per_attempt", "creation_share",
    }


# --------------------------------------------------------------------------- #
# verification_receipt_rate — per-attempt "did the coder run its checks
# before claiming done" rate (aggregated over verification_receipts rows).
# --------------------------------------------------------------------------- #

def _receipt(seq, kind="test", excerpt="1 passed"):
    return VerificationReceipt(
        kind=kind, command=f"pytest -q # {seq}", output_excerpt=excerpt,
        output_bytes=len(excerpt), truncated=False, seq=seq)


async def test_empty_db_yields_a_null_rate_not_a_crash(store):
    r = await verification_receipt_rate(store)
    assert r == {"attempts": 0, "ran_checks": 0, "rate": None, "by_kind": {}}


async def test_rate_counts_attempts_with_at_least_one_receipt(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    a1 = await store.create_attempt(t.id, attempt_number=1)
    a2 = await store.create_attempt(t.id, attempt_number=2)
    a3 = await store.create_attempt(t.id, attempt_number=3)
    await store.add_verification_receipt(a1, _receipt(1))
    await store.add_verification_receipt(a1, _receipt(2, kind="lint"))
    await store.add_verification_receipt(a2, _receipt(1, kind="lint"))
    # a3 gets no receipt at all — it claimed done without running a check.
    await store.update_attempt(a1, status="succeeded")
    await store.update_attempt(a2, status="failed", failure_reason="review failed")
    await store.update_attempt(a3, status="succeeded")

    r = await verification_receipt_rate(store)
    assert r["attempts"] == 3
    assert r["ran_checks"] == 2
    assert r["rate"] == round(2 / 3, 4)
    assert r["by_kind"] == {"test": 1, "lint": 2}


async def test_rate_excludes_in_progress_and_interrupted_attempts(store):
    """Only attempts that actually CONCLUDED (succeeded/failed) count as
    having "claimed done" — an attempt still running, or cut off by the
    harness before it could conclude, is neither."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    a1 = await store.create_attempt(t.id, attempt_number=1)
    a2 = await store.create_attempt(t.id, attempt_number=2)
    # a1 stays in_progress (no receipt, no status update).
    await store.update_attempt(a2, status="interrupted")

    r = await verification_receipt_rate(store)
    assert r == {"attempts": 0, "ran_checks": 0, "rate": None, "by_kind": {}}


async def test_rate_excludes_infra_and_mechanical_rows(store):
    """The same predicate the lifetime budget gates on
    (`Store._lifetime_included_sql`) keeps an infra-classified retry or a
    mechanical post-pass round — neither the coder's own submission — out of
    the denominator, exactly as it keeps them out of the budget count."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    a1 = await store.create_attempt(t.id, attempt_number=1)
    a2 = await store.create_attempt(t.id, attempt_number=2)
    await store.update_attempt(a1, status="failed", infra_failure=1)
    await store.update_attempt(a2, status="succeeded", mechanical_round=1)

    r = await verification_receipt_rate(store)
    assert r == {"attempts": 0, "ran_checks": 0, "rate": None, "by_kind": {}}


async def test_compute_metrics_surfaces_the_verification_receipt_rate(store):
    """`compute_metrics` itself does not carry this key — it is merged
    separately in `/api/metrics` (see `api/app.py`), same treatment as
    `by_playbook`. Assert the two stay independently importable and callable
    from the same store."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    a = await store.create_attempt(t.id, attempt_number=1)
    await store.add_verification_receipt(a, _receipt(1))
    await store.update_attempt(a, status="succeeded")

    m = await compute_metrics(store)
    assert "verification_receipts" not in m
    r = await verification_receipt_rate(store)
    assert r["attempts"] == 1 and r["ran_checks"] == 1 and r["rate"] == 1.0
