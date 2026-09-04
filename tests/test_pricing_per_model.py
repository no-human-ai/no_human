"""Per-model output pricing: the rate table, the fallback, and what it costs.

WHY THIS FILE EXISTS. `core/pricing.py` charged one output premium — 4.0 —
across all four model tiers, and its own docstring called that out as an
approximation: "The 5x ratio is Opus 5's. Sonnet and Haiku bill at their own
in/out ratios, and this table is model-blind." The remedy is a rate table keyed
on the model id already recorded in `attempts.models` (673 of this install's
684 rows carry one).

THE FINDING THAT SHAPES EVERY TEST BELOW. That docstring implied the ratios
DIFFER between tiers. They do not. Every Claude model Anthropic publishes a
price for bills output at exactly 5x input — $5/$25 Opus, $3/$15 Sonnet,
$1/$5 Haiku — so a correct per-model premium table is uniformly 4.0 and the
priced total does not move by one token on any input. That is not a reason to
skip the table: keying on the recorded id is what makes a future model which
breaks 5:1 price correctly the day its id first appears, and it is what stops
historical `claude-opus-4-8` rows being silently re-priced as `claude-opus-5`.
But it does mean the tests worth writing are about the KEYING and the FALLBACK,
not about a number that changed. `test_the_published_ratios_are_all_five_to_one`
pins the fact itself, so the day it stops being true a test says so.

The fallback is the sharp edge. An unpriced id must never price at nothing: a
`0` written for an unreported field is what left a per-attempt brake inert on
27 of 27 tasks once already, and the same shape of bug is available here to
anyone who writes `PRICES.get(model, 0)`.
"""

from __future__ import annotations

import logging

import pytest

from no_human.core.db import Store
from no_human.core.pricing import (
    FRESH_WEIGHT,
    MODEL_PRICES_USD_PER_MTOK,
    OUTPUT_EXTRA_WEIGHT,
    _reset_unknown_pricing_models,
    class_breakdown,
    output_extra_weight,
    unknown_pricing_models,
    weighted_tokens,
)
from no_human.core.task import Task

# Recorded in `attempts.models` on 441 of this install's 684 attempt rows, and
# NOT the model any tier is configured to use today (the Opus tier moved to
# `claude-opus-5` on 2026-07-26). Any test that wants to prove "keyed on the
# record, not on config" has to use an id with exactly this property.
HISTORICAL_OPUS = "claude-opus-4-8"
CONFIGURED_OPUS = "claude-opus-5"


@pytest.fixture(autouse=True)
def _clean_unknown_counter():
    """Module-level counter — leaking it between tests would let one test's
    unknown id satisfy another test's assertion."""
    _reset_unknown_pricing_models()
    yield
    _reset_unknown_pricing_models()


# --------------------------------------------------------------------------- #
# the table itself
# --------------------------------------------------------------------------- #

def test_the_published_ratios_are_all_five_to_one():
    """The load-bearing fact behind "this change moves no number" — for the
    Claude family only. The OpenAI rows added for the Codex backend do NOT
    hold this ratio (`gpt-5.3-codex` is 8:1); that break is covered by
    `tests/test_pricing.py`, not here. Scoping this test to `claude-*` keeps
    it pinning the fact it was written to pin, rather than failing the moment
    a differently-shaped model is added to the table.

    If Anthropic ever ships a Claude model at a different in:out ratio, this
    test fails and whoever adds it is forced to notice that the premium is no
    longer uniform — which is the moment the per-model keying starts earning
    its keep and the moment the brake-shift question has to be re-asked.
    """
    claude_ids = [m for m in MODEL_PRICES_USD_PER_MTOK if m.startswith("claude-")]
    assert claude_ids, "no claude- ids in the table — nothing was checked"
    for model in claude_ids:
        price_in, price_out = MODEL_PRICES_USD_PER_MTOK[model]
        assert price_out == pytest.approx(price_in * 5.0), model
        assert output_extra_weight(model) == pytest.approx(4.0), model


def test_the_fallback_is_never_cheaper_than_any_priced_model():
    """The conservatism property, stated as an invariant rather than a comment
    — scoped correctly now that it is no longer universally true.

    Within the Claude family it still holds trivially (everything is 4.0), so
    that half stays here as a regression pin. It does NOT hold globally any
    more: the OpenAI rows added for the Codex backend include premiums ABOVE
    the fallback (`gpt-5.3-codex` is 7.0 > 4.0), by design — see
    `core/pricing.py`'s module docstring ("THE OPENAI SIDE"). What must still
    hold, and is asserted here instead, is that every priced non-Claude row's
    premium is strictly positive (never free output) and that an id genuinely
    absent from the table — priced or not, Claude or not — still takes the
    fallback exactly.
    """
    for model in MODEL_PRICES_USD_PER_MTOK:
        if model.startswith("claude-"):
            assert output_extra_weight(model) <= OUTPUT_EXTRA_WEIGHT, model
        else:
            assert output_extra_weight(model) > 0.0, model
    assert output_extra_weight("a-model-id-not-in-the-table") == OUTPUT_EXTRA_WEIGHT


def test_prices_are_positive_so_no_row_can_price_output_at_nothing():
    for model, (price_in, price_out) in MODEL_PRICES_USD_PER_MTOK.items():
        assert price_in > 0 and price_out > 0, model


@pytest.mark.parametrize("backend", ["claude", "codex", "local", "unknown-backend", None])
def test_the_fallback_is_never_cheaper_than_any_priced_model_of_its_own_backend(backend):
    """The invariant restored, per backend. `test_the_fallback_is_never_cheaper_than_any_priced_model`
    above already pins that this stopped holding GLOBALLY once the OpenAI rows
    arrived (by design — `gpt-5.3-codex` is 7.0 > 4.0). What must hold instead,
    for every backend, is the narrower claim: that backend's own fallback is
    never cheaper than any of ITS OWN priced rows — i.e. "failing to record
    the model" never buys headroom on any backend's own price table.

    Claude/local/unknown/None all price OpenAI rows too when a Claude-tier
    caller happens to see one (todo: a mixed fleet), so this only asserts each
    backend's fallback against the rows that backend can actually route to:
    Claude ids for everything except "codex", and every non-Claude id for
    "codex" — the same split `core.pricing._rows_for_backend` makes.
    """
    from no_human.core.pricing import CLAUDE_ID_PREFIX, fallback_output_extra_weight

    fallback = fallback_output_extra_weight(backend=backend)
    if backend == "codex":
        own_rows = {
            m: v for m, v in MODEL_PRICES_USD_PER_MTOK.items()
            if not m.startswith(CLAUDE_ID_PREFIX)
        }
    else:
        own_rows = {
            m: v for m, v in MODEL_PRICES_USD_PER_MTOK.items()
            if m.startswith(CLAUDE_ID_PREFIX)
        }
    assert own_rows, f"no rows to check for backend={backend!r}"
    for model, (price_in, price_out) in own_rows.items():
        premium = price_out / price_in - 1.0
        assert fallback >= premium, (
            f"backend={backend!r} fallback {fallback} is cheaper than "
            f"{model}'s own premium {premium} — an unrecorded model would "
            f"be priced below a known one"
        )


# --------------------------------------------------------------------------- #
# RISK 3 — keyed on the recorded id, never on live config
# --------------------------------------------------------------------------- #

def test_the_premium_is_keyed_on_the_recorded_id_not_the_configured_model():
    """A historical row says `claude-opus-4-8`; the Opus tier is configured to
    `claude-opus-5`. Both must price, independently, from the string that was
    recorded — a lookup that resolved through `config.llm.*` would give the
    same answer for both and silently re-price 441 rows of history."""
    assert HISTORICAL_OPUS != CONFIGURED_OPUS
    assert HISTORICAL_OPUS in MODEL_PRICES_USD_PER_MTOK
    assert CONFIGURED_OPUS in MODEL_PRICES_USD_PER_MTOK
    # Priced from the recorded string, with no config loaded in this process.
    assert output_extra_weight(HISTORICAL_OPUS) == pytest.approx(4.0)


def test_the_pricer_never_reads_config():
    """Structural, not behavioural: today every id happens to price the same,
    so a config-reading implementation would pass every numeric assertion above
    while being exactly the defect this change exists to prevent. Pin the
    absence of the import instead."""
    import no_human.core.pricing as pricing

    source = __import__("pathlib").Path(pricing.__file__).read_text()
    assert "import config" not in source
    assert "from .config" not in source
    assert "from no_human.core.config" not in source
    assert not hasattr(pricing, "config")


# --------------------------------------------------------------------------- #
# RISK 2 + 4 — unknown must not be free, and must be visible
# --------------------------------------------------------------------------- #

def test_an_unknown_id_hits_the_fallback_and_is_surfaced(caplog):
    """A model id that appears in the data before it appears in the table —
    `claude-opus-6` is the concrete future case — must price at the fallback
    AND leave a trace a human can find. Absorbing it silently is the same class
    of bug as pricing it at zero: nobody learns the table is stale."""
    with caplog.at_level(logging.WARNING, logger="no_human.core.pricing"):
        premium = output_extra_weight("claude-opus-6")

    assert premium == OUTPUT_EXTRA_WEIGHT
    assert unknown_pricing_models() == {"claude-opus-6": 1}
    assert any(
        "claude-opus-6" in r.getMessage() for r in caplog.records
    ), caplog.records


def test_the_unknown_warning_fires_once_per_id_not_once_per_call(caplog):
    """This runs on the brake path. A warning per call would emit thousands of
    lines per task and get muted, which is how a real signal dies — but the
    COUNT must still track every sighting."""
    with caplog.at_level(logging.WARNING, logger="no_human.core.pricing"):
        for _ in range(50):
            output_extra_weight("claude-opus-6")

    warnings = [r for r in caplog.records if "claude-opus-6" in r.getMessage()]
    assert len(warnings) == 1, f"{len(warnings)} warnings"
    assert unknown_pricing_models() == {"claude-opus-6": 50}


def test_a_known_id_is_never_reported_as_unknown():
    for model in MODEL_PRICES_USD_PER_MTOK:
        output_extra_weight(model)
    assert unknown_pricing_models() == {}


def test_an_unknown_id_does_not_price_output_as_free():
    """The defect in its purest form. `MODEL_PRICES.get(model, 0)` would pass
    every keying test above and make 100% of an unknown tier's output free."""
    priced = weighted_tokens(
        tokens_used=1_000_000, output_tokens=1_000_000, model="claude-opus-6")
    assert priced == 5_000_000
    assert priced > weighted_tokens(tokens_used=1_000_000)


@pytest.mark.parametrize("model", [None, ""])
def test_an_unrecorded_model_does_not_price_output_as_free(model):
    """`models = '{}'` on 11 of 684 attempt rows, and the utility tier on all
    684 — the empty and None cases are not hypothetical, they are the majority
    of what the pricer will actually see."""
    priced = weighted_tokens(
        tokens_used=1_000_000, output_tokens=1_000_000, model=model)
    assert priced == 5_000_000


def test_an_unrecorded_model_is_not_logged_as_an_anomaly(caplog):
    """`None` is the documented "this caller does not know the tier" path, and
    it is the DEFAULT on every existing call site. Warning on it would fire on
    every priced task forever and drown the real signal from a genuinely
    unknown id."""
    with caplog.at_level(logging.WARNING, logger="no_human.core.pricing"):
        output_extra_weight(None)
        output_extra_weight("")
    assert unknown_pricing_models() == {}
    assert [r.getMessage() for r in caplog.records] == []


# --------------------------------------------------------------------------- #
# compatibility — the number the brake sees must not move
# --------------------------------------------------------------------------- #

def test_omitting_the_model_reproduces_the_old_arithmetic_exactly():
    """Every existing caller splats a four-key class dict and passes no model.
    Each one must get the number it got before this parameter existed, or a
    live spend brake changed behaviour as a side effect of a pricing refactor.
    """
    classes = dict(tokens_used=1_204_337, cache_read_tokens=3_060_000,
                   cache_creation_tokens=173_190, output_tokens=8_119)
    expected = int(
        1_204_337 * FRESH_WEIGHT
        + 8_119 * 4.0
        + 3_060_000 * 0.1
        + 173_190 * 1.25
    )
    assert weighted_tokens(**classes) == expected


def test_every_recorded_model_prices_identically_to_the_flat_table():
    """The three ids this ledger actually contains, plus the configured
    fourth tier, priced one at a time. All four equal the old flat number —
    stated as a test so the claim in the report is checkable, not asserted."""
    classes = dict(tokens_used=1_000_000, output_tokens=400_000,
                   cache_read_tokens=1_000_000, cache_creation_tokens=1_000_000)
    flat = weighted_tokens(**classes)
    for model in ("claude-sonnet-5", "claude-opus-4-8", "claude-opus-5",
                  "claude-haiku-4-5"):
        assert weighted_tokens(**classes, model=model) == flat, model


# --------------------------------------------------------------------------- #
# the human-readable breakdown must print the rate that was charged
# --------------------------------------------------------------------------- #

def test_the_breakdown_prints_the_multiplier_the_gate_actually_charged():
    """This string is what a human reads to reconcile a parked task against a
    bill. Printing a rate the gate did not use would make it worse than
    nothing, so it resolves the model the same way `weighted_tokens` does."""
    text = class_breakdown(
        tokens_used=1_000, output_tokens=400, model="claude-haiku-4-5")
    assert "of which 400 output (x5)" in text


def test_the_breakdown_prices_an_unknown_model_at_the_fallback_too():
    text = class_breakdown(
        tokens_used=1_000, output_tokens=400, model="claude-opus-6")
    assert "of which 400 output (x5)" in text
    assert unknown_pricing_models() == {"claude-opus-6": 1}


def test_the_breakdown_still_omits_an_unknown_split_entirely():
    """A task with no recorded split must read exactly as it did before the
    column existed — not gain a misleading "0 output"."""
    assert "output" not in class_breakdown(tokens_used=1_000)
    assert "output" not in class_breakdown(tokens_used=1_000, output_tokens=0)


# --------------------------------------------------------------------------- #
# the raw figure the brake's blocker prints
# --------------------------------------------------------------------------- #

async def test_the_raw_total_excludes_the_output_slice(store):
    """`lifetime_budget` printed `sum(by_class.values())` as its RAW figure.

    `by_class` carries four keys, and the fourth — `output_tokens` — is a slice
    of `tokens_used`, not a bucket beside it; `Store.lifetime_usage`'s docstring
    names that exact sum as the mistake. Summing all four double-counts every
    output token and inflates the one number in the blocker a human is supposed
    to be able to reconcile against `nh logs`. Latent only while every
    historical row's split is NULL; live the moment one is recorded.
    """
    t = Task.new("raw total", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        aid, tokens_used=1_000, output_tokens=400,
        cache_read_tokens=100, cache_creation_tokens=10)

    _, by_class, _ = await store.lifetime_usage_by_class(t.id)
    _, raw = await store.lifetime_usage(t.id)

    assert raw == 1_110
    # The expression the blocker now uses.
    assert sum(by_class[n] for n in Store._usage_columns_by_class()) == raw
    # The expression it used to use, kept here so the bug cannot quietly return.
    assert sum(by_class.values()) == raw + 400


async def test_the_lifetime_budget_event_reports_the_undoubled_raw_total(store):
    """The same defect, pinned at the surface it actually reaches: the
    `lifetime_budget` event's `tokens_used`, which the docstring beside it
    promises stays "the RAW total ... what `nh`, the web burn meters and
    eval/northstar.py all mean by that name". With an output split recorded,
    the old `sum(by_class.values())` reported 1,510 for 1,110 raw tokens."""
    from no_human.core.bounds import Bounds
    from no_human.core.orchestrator import Orchestrator

    t = Task.new("raw total at the surface", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        aid, tokens_used=1_000, output_tokens=400,
        cache_read_tokens=100, cache_creation_tokens=10)

    events: list[dict] = []
    o = Orchestrator.__new__(Orchestrator)
    o.store, o.bounds, o._sink = store, Bounds.from_config({}), events.append

    assert await o._check_lifetime_budget(t) is None
    ev = next(e for e in events if e["kind"] == "lifetime_budget")
    _, raw = await store.lifetime_usage(t.id)
    assert ev["tokens_used"] == raw == 1_110
    assert ev["raw_fresh"] + ev["raw_cache_read"] + ev["raw_cache_creation"] == (
        ev["tokens_used"])
