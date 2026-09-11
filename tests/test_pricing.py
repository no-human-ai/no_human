"""The OpenAI (Codex backend) side of the price table — citations, retirement,
and the subscription-billing gap.

WHY THIS FILE EXISTS, separate from `test_pricing_per_model.py`. Every test
over there assumed a single-vendor, uniformly-5:1 table; the moment a second
backend's ids entered `MODEL_PRICES_USD_PER_MTOK` that assumption broke on
purpose (`gpt-5.3-codex` is 8:1). This file pins the NEW claims specifically:
every OpenAI row is sourced and citable, the retired ids this codebase still
defaults to (`gpt-5-codex`) get no row and no fabricated price, and a run
billed by a flat ChatGPT subscription is a documented gap, not a silent 0.0.
"""

from __future__ import annotations

import re

import pytest

from no_human.core import pricing
from no_human.core.pricing import (
    CLAUDE_ID_PREFIX,
    MODEL_PRICES_USD_PER_MTOK,
    OUTPUT_EXTRA_WEIGHT,
    _reset_unknown_pricing_models,
    class_breakdown,
    fallback_output_extra_weight,
    output_extra_weight,
    unknown_pricing_models,
    weighted_tokens,
)

# Ids `codex exec` actually resolved on this machine on 2026-08-22 (measured
# for ticket d35aa60e) and that this table now carries a sourced row for.
OPENAI_IDS = tuple(k for k in MODEL_PRICES_USD_PER_MTOK if not k.startswith("claude-"))

# Ids that returned "Model not found" on 2026-08-22 and do not appear on the
# pricing page read 2026-08-23 either — retired, not merely unpriced. The
# first is also `config.py`'s current `llm.codex_model` default, which is
# exactly why it must NOT get a row: pricing the product's own default at a
# guessed number would be the most-hit wrong number in the table.
RETIRED_CODEX_IDS = ("gpt-5-codex", "gpt-5.1-codex", "gpt-5.1-codex-max", "gpt-5.2-codex")


@pytest.fixture(autouse=True)
def _clean_unknown_counter():
    _reset_unknown_pricing_models()
    yield
    _reset_unknown_pricing_models()


# --------------------------------------------------------------------------- #
# sourcing
# --------------------------------------------------------------------------- #

def test_every_openai_row_has_a_positive_input_and_output_rate():
    assert OPENAI_IDS, "no OpenAI ids in the table — nothing was sourced"
    for model in OPENAI_IDS:
        price_in, price_out = MODEL_PRICES_USD_PER_MTOK[model]
        assert price_in > 0, model
        assert price_out > 0, model


def test_every_openai_row_carries_a_url_and_a_date_in_its_citation():
    """Every sourced row must be traceable to an official page and a read
    date — not a promise in prose, a checkable pattern in the source file.

    Positive control: the pre-existing Anthropic citation block, which this
    change touched only to add an explicit ``https://`` scheme, must match
    the same pattern — proving the regex finds a real citation and is not
    vacuously true. Negative control: the module's own top synopsis line
    ("A token is not a token...") must NOT match, proving the regex does not
    just match anywhere in the file.
    """
    assert OPENAI_IDS
    source = __import__("pathlib").Path(pricing.__file__).read_text(encoding="utf-8")
    url_and_date = re.compile(r"https://\S+.*read 2026-\d{2}-\d{2}")

    for model in OPENAI_IDS:
        # Every id's own citation line names it, a URL, and a read-date.
        pattern = re.compile(rf"{re.escape(model)}\s+\$[\d.]+ / \$[\d.]+\s+https://\S+.*read 2026-\d{{2}}-\d{{2}}")
        assert pattern.search(source), f"no cited https:// + read-date line for {model}"

    # Positive control — a real citation line elsewhere in the file.
    assert "https://platform.claude.com" in source
    assert re.search(r"cached 2026-06-24", source)

    # Negative control — prose that must not accidentally satisfy the pattern.
    assert not url_and_date.search("A token is not a token — Anthropic bills the classes")


# --------------------------------------------------------------------------- #
# retirement — no row, ever, for an id that cannot be billed again
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("model", RETIRED_CODEX_IDS)
def test_the_retired_codex_ids_have_no_row(model):
    assert model not in MODEL_PRICES_USD_PER_MTOK


def test_gpt_5_codex_specifically_is_absent():
    """Named explicitly because it is not just retired — it is `config.py`'s
    current `llm.codex_model` default, so it is the row a careless "price the
    configured model" implementation would be most likely to add."""
    assert "gpt-5-codex" not in MODEL_PRICES_USD_PER_MTOK


def test_a_retired_id_still_takes_the_visible_fallback():
    for model in RETIRED_CODEX_IDS:
        assert output_extra_weight(model) == OUTPUT_EXTRA_WEIGHT
        assert unknown_pricing_models().get(model) == 1


# --------------------------------------------------------------------------- #
# the priced ids report their own ratio, not the fallback
# --------------------------------------------------------------------------- #

def test_output_extra_weight_is_the_rows_own_ratio_not_the_fallback():
    for model in OPENAI_IDS:
        price_in, price_out = MODEL_PRICES_USD_PER_MTOK[model]
        expected = price_out / price_in - 1.0
        assert output_extra_weight(model) == pytest.approx(expected), model


def test_a_priced_openai_id_is_not_reported_as_unknown():
    for model in OPENAI_IDS:
        output_extra_weight(model)
    assert unknown_pricing_models() == {}


def test_a_genuinely_unknown_openai_id_still_hits_the_fallback():
    """`gpt-5-codex` is retired, not merely unpriced — a distinct id this
    table has simply never seen exercises the same visible-fallback path a
    brand-new Claude id would."""
    assert output_extra_weight("gpt-5-codex") == OUTPUT_EXTRA_WEIGHT
    assert unknown_pricing_models() == {"gpt-5-codex": 1}


def test_a_genuinely_unknown_id_still_falls_back_and_is_logged_once(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="no_human.core.pricing"):
        premium = output_extra_weight("claude-opus-6")
    assert premium == OUTPUT_EXTRA_WEIGHT
    assert unknown_pricing_models() == {"claude-opus-6": 1}


# --------------------------------------------------------------------------- #
# the subscription-billing decision is documented, not fabricated
# --------------------------------------------------------------------------- #

def test_the_subscription_decision_is_recorded_in_the_module_docstring():
    doc = pricing.__doc__ or ""
    assert "flat ChatGPT subscription" in doc
    assert "no per-token price" in doc or "no row" in doc
    assert "unknown_pricing_models" in doc
    # No row anywhere in the table prices anything at exactly 0.0 — the
    # sentinel value this whole decision exists to avoid.
    for model, (price_in, price_out) in MODEL_PRICES_USD_PER_MTOK.items():
        assert price_in != 0.0, model
        assert price_out != 0.0, model


# --------------------------------------------------------------------------- #
# nothing about the Claude side moved
# --------------------------------------------------------------------------- #

def test_the_anthropic_rows_are_exactly_the_seven_published_pairs():
    expected = {
        "claude-opus-5": (5.0, 25.0),
        "claude-opus-4-8": (5.0, 25.0),
        "claude-opus-4-7": (5.0, 25.0),
        "claude-opus-4-6": (5.0, 25.0),
        "claude-sonnet-5": (3.0, 15.0),
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5": (1.0, 5.0),
    }
    actual = {k: v for k, v in MODEL_PRICES_USD_PER_MTOK.items() if k.startswith("claude-")}
    assert actual == expected


#: The four sourced rows, HARDCODED on purpose: `OPENAI_IDS` above is derived
#: from the table, so a deleted row deletes the thing under test and the
#: derived tests stay green. This pin is what turns "a row vanished" into a
#: failure — the same shape `test_the_anthropic_rows_are_exactly_the_seven_published_pairs`
#: uses for the Claude side. Values are the published short-context rates read
#: 2026-08-23 (citations next to the table in pricing.py).
EXPECTED_OPENAI_ROWS = {
    "gpt-5.3-codex": (1.75, 14.00),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.5": (5.00, 30.00),
    "gpt-5.6-terra": (2.00, 12.00),
}


def test_the_openai_rows_are_exactly_the_four_sourced_pairs():
    """Deleting or editing any OpenAI row must fail here, not vanish from the
    derived `OPENAI_IDS` subject. Positive control for the derivation: the
    expected set is non-empty and every id in it is non-Anthropic."""
    assert EXPECTED_OPENAI_ROWS and all(not k.startswith("claude-") for k in EXPECTED_OPENAI_ROWS)
    actual = {k: v for k, v in MODEL_PRICES_USD_PER_MTOK.items() if not k.startswith("claude-")}
    assert actual == EXPECTED_OPENAI_ROWS, (
        f"OpenAI price rows drifted from the sourced set: {actual!r} != {EXPECTED_OPENAI_ROWS!r}")
    assert set(OPENAI_IDS) == set(EXPECTED_OPENAI_ROWS)


# --------------------------------------------------------------------------- #
# the backend-keyed fallback — an unrecorded model must never price below a
# known one, on ANY backend, not just the Claude family it was derived from.
# --------------------------------------------------------------------------- #

def test_an_unrecorded_model_on_the_codex_backend_takes_the_max_openai_premium():
    """The bug this whole change exists to close: before this fix, an
    unrecorded Codex model priced at the flat 4.0, which is BELOW several
    priced OpenAI rows (`gpt-5.3-codex` is 7.0) — "failing to record the
    model" bought budget headroom on the Codex path. The fallback must now be
    at least as expensive as every priced row it could be standing in for.
    """
    expected_max_premium = max(
        price_out / price_in - 1.0
        for model, (price_in, price_out) in MODEL_PRICES_USD_PER_MTOK.items()
        if not model.startswith(CLAUDE_ID_PREFIX)
    )
    assert expected_max_premium > OUTPUT_EXTRA_WEIGHT, (
        "test assumption broken: no OpenAI row exceeds the 4.0 fallback any more"
    )
    assert output_extra_weight(None, backend="codex") == pytest.approx(expected_max_premium)
    assert output_extra_weight("", backend="codex") == pytest.approx(expected_max_premium)
    assert output_extra_weight("a-genuinely-unknown-id", backend="codex") == pytest.approx(
        expected_max_premium
    )
    assert fallback_output_extra_weight(backend="codex") == pytest.approx(expected_max_premium)


@pytest.mark.parametrize("backend", ["claude", "local", "unknown-backend", None, "", "  "])
def test_the_claude_and_unknown_backends_keep_the_four_point_zero_fallback(backend):
    """Only `"codex"` gets the raised fallback. Every other backend — the
    Claude default, the local coding-backend, an unrecognized string, and the
    unset/blank cases every pre-existing caller passes — must be BIT-FOR-BIT
    unchanged: still the plain `OUTPUT_EXTRA_WEIGHT`, never the OpenAI max."""
    assert output_extra_weight(None, backend=backend) == OUTPUT_EXTRA_WEIGHT
    assert output_extra_weight("a-genuinely-unknown-id", backend=backend) == OUTPUT_EXTRA_WEIGHT
    assert fallback_output_extra_weight(backend=backend) == OUTPUT_EXTRA_WEIGHT


def test_a_priced_id_ignores_the_backend():
    """A row in the table is the actual billed rate — a backend hint can never
    override a known price, in either direction."""
    price_in, price_out = MODEL_PRICES_USD_PER_MTOK["gpt-5.3-codex"]
    expected = price_out / price_in - 1.0
    for backend in ("codex", "claude", "local", None, "bogus"):
        assert output_extra_weight("gpt-5.3-codex", backend=backend) == pytest.approx(expected)
    # Also true the other way: a Claude id priced while `backend="codex"` is
    # passed still reports its own 4.0, not the raised Codex fallback.
    assert output_extra_weight("claude-sonnet-5", backend="codex") == pytest.approx(4.0)


def test_the_codex_fallback_is_derived_from_the_table_not_a_literal():
    """`fallback_output_extra_weight` must recompute from
    `MODEL_PRICES_USD_PER_MTOK` at call time — never a copied number — so a
    newly sourced OpenAI row raises the fallback with it, the same derivation
    rule `test_the_openai_rows_are_exactly_the_four_sourced_pairs` and
    `OPENAI_IDS` already hold the table to elsewhere in this file."""
    before = fallback_output_extra_weight(backend="codex")
    saved = dict(MODEL_PRICES_USD_PER_MTOK)
    try:
        # A hypothetical row priced far above every existing OpenAI premium.
        MODEL_PRICES_USD_PER_MTOK["gpt-hypothetical-future"] = (1.0, 100.0)
        after = fallback_output_extra_weight(backend="codex")
        assert after == pytest.approx(99.0)
        assert after > before
    finally:
        MODEL_PRICES_USD_PER_MTOK.clear()
        MODEL_PRICES_USD_PER_MTOK.update(saved)
    # Restored exactly — no test-order leakage into any test that follows.
    assert fallback_output_extra_weight(backend="codex") == pytest.approx(before)


def test_the_codex_fallback_survives_a_table_with_no_openai_rows():
    """If every OpenAI row were ever removed, the Codex fallback must degrade
    to the plain 4.0 rather than crash on an empty `max()` — a genuinely
    unpriced model is still better served by the old conservative number than
    by an exception."""
    saved = dict(MODEL_PRICES_USD_PER_MTOK)
    try:
        for model in list(MODEL_PRICES_USD_PER_MTOK):
            if not model.startswith(CLAUDE_ID_PREFIX):
                del MODEL_PRICES_USD_PER_MTOK[model]
        assert fallback_output_extra_weight(backend="codex") == OUTPUT_EXTRA_WEIGHT
    finally:
        MODEL_PRICES_USD_PER_MTOK.clear()
        MODEL_PRICES_USD_PER_MTOK.update(saved)


def test_an_unknown_codex_id_is_still_surfaced_once_by_id():
    """The backend-keyed fallback must not regress the existing
    surfaced-not-silent contract: a genuinely unpriced Codex id is still
    counted by `unknown_pricing_models`, warned once, and never twice."""
    import logging

    logger = logging.getLogger("no_human.core.pricing")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        for _ in range(3):
            output_extra_weight("gpt-genuinely-new", backend="codex")
    finally:
        logger.removeHandler(handler)
    assert unknown_pricing_models() == {"gpt-genuinely-new": 3}
    hits = [r for r in records if "gpt-genuinely-new" in r.getMessage()]
    assert len(hits) == 1, hits


def test_weighted_tokens_and_class_breakdown_forward_the_backend():
    """`weighted_tokens`/`class_breakdown` must resolve the SAME fallback
    `output_extra_weight` does — the whole point of a keyword-only `backend`
    parameter is that every consumer of the class dict prices identically."""
    expected_premium = fallback_output_extra_weight(backend="codex")
    classes = dict(tokens_used=1_000_000, output_tokens=100_000)
    priced_codex = weighted_tokens(**classes, backend="codex")
    priced_default = weighted_tokens(**classes)
    expected_codex = int(1_000_000 + 100_000 * expected_premium)
    expected_default = int(1_000_000 + 100_000 * OUTPUT_EXTRA_WEIGHT)
    assert priced_codex == expected_codex
    assert priced_default == expected_default
    assert priced_codex > priced_default

    text = class_breakdown(tokens_used=1_000, output_tokens=400, backend="codex")
    assert f"(x{1.0 + expected_premium:g})" in text


# --------------------------------------------------------------------------- #
# AC4 — the subscription-default comment names the priced/entitled distinction
# --------------------------------------------------------------------------- #

def test_the_subscription_default_comment_documents_priced_vs_entitled():
    """`agent/backend.py`'s `DEFAULT_CODEX_MODEL_SUBSCRIPTION` comment must
    distinguish "entitled on this account" (what the operator measured) from
    "documented and priced" (has a sourced row in `MODEL_PRICES_USD_PER_MTOK`)
    — and must not claim a priced id is undocumented."""
    import pathlib

    from no_human.agent import backend as agent_backend

    source = pathlib.Path(agent_backend.__file__).read_text(encoding="utf-8")
    start = source.index("DEFAULT_CODEX_MODEL_SUBSCRIPTION")
    comment_block = source[:start].rsplit("\n\n", 1)[-1]
    lowered = comment_block.lower()
    assert "entitle" in lowered
    assert "priced" in lowered
    assert "not a documented" not in lowered
