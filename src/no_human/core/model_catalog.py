"""Model catalog: which ids each agent role may run, and the two rules a
picker must never break. Part 1 of 3 (operator ticket, 2026-08-22).

**This module chooses nothing; it answers what is allowed.** Picking,
persisting and restarting the server belong to part 2 (the endpoint + CLI)
and part 3 (the Settings pane). It stays free of any FastAPI/click/web
symbol so both surfaces share one answer instead of re-deriving it.

**Rule 1 — the vendor pin is separate from the model tier, and must be
stated, not implied.** ``agent.backend.CLAUDE_PINNED_ROLES`` pins *which
backend* runs a role, not which model: ``resolve_backend_name``
(``agent/backend.py:259-267``) returns ``"claude"`` for every role except
``"coder"``, and each of the five roles reads its own model id from its own
``llm.*`` config key. **All five roles' models are user-changeable.** What a
pinned role is *not* offered is a non-Claude id — the coder's backend
(Claude/Codex/local) is a separate control and out of scope here.

**Rule 2 — the review gate needs two different models.** ``review_model``
may never equal ``primary_model``, in either direction: the README and
``docs/verification.md`` both promise "a different model" reviews the code,
and that promise is the product's headline gate.

**Why an unknown id is refused rather than accepted as free text.**
``core/pricing.py``'s ``_unknown_models`` / ``unknown_pricing_models()``
exists because an unpriced id silently pricing at the fallback once left a
per-attempt spend brake inert on 27 of 27 tasks. The pricer must still fall
back for historical rows it cannot refuse after the fact, but a *picker*
sits before the spend is ever made and can refuse outright — so it does. No
free-text field, no "custom model" escape hatch.

The operator's A/B that rejected ``claude-opus-5`` as the reviewer is
recorded next to ``review_model`` in ``config.py`` (around line 1151) —
see that comment for the numbers; they are not restated here.

These functions read nothing live. The five read sites bind the config
object the server loaded at start (``core/runtime.py``, ``review/reviewer.py``,
``core/orchestrator.py``), so changing a role's model needs a restart — that
restart semantics belongs to part 2's API, and is named here only so part 2
does not need to re-derive it.

Predicate ORDER in :func:`validate` is load-bearing (see its docstring) and
is part of this module's contract, not an implementation detail.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from ..agent.backend import CLAUDE_PINNED_ROLES
from .pricing import MODEL_PRICES_USD_PER_MTOK

__all__ = [
    "ROLES",
    "PINNED_ROLES",
    "VENDOR_PIN_NOTE",
    "CODER_BACKEND_REASON",
    "REVIEWER_COST_NOTE",
    "PriceInfo",
    "ModelOption",
    "options_for",
    "role_note",
    "validate",
    "defaults",
]

# The ONE role -> config-key pairing. Nothing else may re-spell this mapping;
# import ROLES instead of hard-coding "reviewer" -> "review_model" again.
ROLES = MappingProxyType(
    {
        "coder": "primary_model",
        "reviewer": "review_model",
        "planner": "planner_model",
        "supervisor": "supervisor_model",
        "utility": "utility_model",
    }
)

# CLAUDE_PINNED_ROLES also names "intake", which is not a picker role: intake
# rides utility_model (intake/evaluator.py:313-321, read at
# intake/evaluator.py:339/414/521/749 and intake/split_proposal.py:108) rather
# than having its own llm.* key, so the intersection with ROLES drops it here
# — by derivation, never by a second literal tuple. Consequence for part 3: a
# user who changes the "utility" role also changes intake's model — there is
# no separate intake control to show.
PINNED_ROLES = frozenset(ROLES) & set(CLAUDE_PINNED_ROLES)

VENDOR_PIN_NOTE = (
    "This role always runs on the Claude backend, so only Claude models are "
    "offered. That is a vendor pin, not a limit on the model tier — any "
    "Claude model listed here is available."
)

# The coder role is not vendor-pinned (see Rule 1 above), but a non-Claude id
# still cannot be written to llm.primary_model: no backend reads that key for
# any vendor other than Claude — the coder's actual backend (Claude/Codex/
# local) is chosen separately, by worker.backend, with its own model keys
# (llm.codex_model / llm.local_model). This message names that path so a
# refusal tells the operator where the id DOES belong, not just that it was
# rejected. Layered strictly after model_catalog.validate() in part 2's
# model_settings module — never folded into validate() itself, which stays a
# pure catalog/pricing/review-gate answer.
CODER_BACKEND_REASON = (
    "{model_id!r} cannot be set as llm.primary_model: only the Claude backend "
    "reads that key (agent/backend.py make_backend). Choose the backend with "
    "worker.backend, and set llm.codex_model / llm.local_model for those."
)

# Cost/quality honesty for the reviewer row — a SHORT, user-facing sentence.
# The full measured A/B evidence (recall/specificity scores, round-duration and
# cost multiples) lives where engineers look for it — config.py next to the
# `review_model` default, and docs/REVIEWER_RECALL_METHOD.md — NOT in the
# Settings UI, where a wall of internal benchmark numbers only confuses a user
# choosing a model. Reviewer-only: no other role carries a cost note.
REVIEWER_COST_NOTE = (
    "Kept on Opus 4.8 rather than Opus 5: on our review benchmark it caught "
    "more real issues, ran about 3× faster, and cost far less."
)

# Coarse price-class thresholds, on the INPUT rate ($/MTok). Named constants
# rather than relative min/max labelling: with four or more priced ids,
# relative positioning collapses everything toward "medium"; a fixed
# threshold re-labels a re-priced row correctly the day it changes.
HIGH_INPUT_RATE = 5.0
MEDIUM_INPUT_RATE = 2.0


def _price_class(input_rate: float) -> str:
    if input_rate >= HIGH_INPUT_RATE:
        return "high"
    if input_rate >= MEDIUM_INPUT_RATE:
        return "medium"
    return "low"


@dataclass(frozen=True, slots=True)
class PriceInfo:
    """The coarse label plus the raw rates, so a caller never has to parse
    either back out of the other. Rates always come straight from
    ``MODEL_PRICES_USD_PER_MTOK`` — no table row is ever copied here."""

    label: str
    input_rate: float
    output_rate: float


@dataclass(frozen=True, slots=True)
class ModelOption:
    """One selectable id for a role."""

    id: str
    price_class: PriceInfo
    is_default: bool
    note: str
    # True for a non-Claude id offered to the (unpinned) coder role: picking
    # it needs a backend switch (worker.backend) plus a matching
    # llm.codex_model / llm.local_model, since no backend reads a non-Claude
    # id from llm.primary_model. Always False for a pinned role's options —
    # those are all Claude ids by construction. See CODER_BACKEND_REASON.
    requires_backend: bool


def _known_roles_message() -> str:
    return f"unknown role {{!r}}; must be one of {sorted(ROLES)}"


def role_note(role: str) -> str:
    """The ROLE-level note a picker must render verbatim next to the row —
    :data:`VENDOR_PIN_NOTE` for every role in :data:`PINNED_ROLES`, ``""``
    for ``coder``. This is the same sentence :func:`options_for` stamps onto
    each *option*; a role dict (``model_settings.models_payload``) uses this
    helper directly instead of re-deriving the ``role in PINNED_ROLES``
    check a second time.
    """
    if not isinstance(role, str) or role not in ROLES:
        raise ValueError(_known_roles_message().format(role))
    return VENDOR_PIN_NOTE if role in PINNED_ROLES else ""


def options_for(role: str) -> list[ModelOption]:
    """The ids a picker may offer for ``role``, priced and defaulted.

    Only ids present in ``MODEL_PRICES_USD_PER_MTOK`` are offered — an
    unpriced id is never presented as a choice (see the module docstring's
    "why an unknown id is refused" section). Results are sorted descending
    by input rate, then by id, so the order is deterministic across runs and
    independent of the price table's insertion order.

    ``note`` carries :data:`VENDOR_PIN_NOTE`, verbatim, for every role in
    :data:`PINNED_ROLES` — the UI (part 3) must render that exact sentence
    rather than silently presenting a shortened list or inventing its own
    wording. For ``coder`` (not vendor-pinned) ``note`` is ``""``.
    """
    if not isinstance(role, str) or role not in ROLES:
        raise ValueError(_known_roles_message().format(role))

    default_id = defaults()[ROLES[role]]
    note = role_note(role)

    options = [
        ModelOption(
            id=model_id,
            price_class=PriceInfo(
                label=_price_class(rates[0]),
                input_rate=rates[0],
                output_rate=rates[1],
            ),
            is_default=(model_id == default_id),
            note=note,
            requires_backend=not _is_claude_id(model_id),
        )
        for model_id, rates in MODEL_PRICES_USD_PER_MTOK.items()
        if not _violates_vendor_pin(role, model_id)
    ]
    options.sort(key=lambda opt: (-opt.price_class.input_rate, opt.id))
    return options


def _is_claude_id(model_id: str) -> bool:
    """True when ``model_id`` names a Claude model. The one place this string
    test is spelled — both the vendor-pin rule and ``ModelOption.
    requires_backend`` reuse it, rather than re-deriving "starts with
    claude-" a second time."""
    return str(model_id or "").startswith("claude-")


def _violates_vendor_pin(role: str, model_id: str) -> bool:
    """True when ``role`` is vendor-pinned to Claude and ``model_id`` is not
    a Claude id. Checked FIRST in :func:`validate`: for a pinned role this is
    the actionable reason ("wrong vendor"), not a distraction about price."""
    return role in PINNED_ROLES and not _is_claude_id(model_id)


def _has_no_published_price(model_id: str) -> bool:
    """True when ``model_id`` has no row in ``MODEL_PRICES_USD_PER_MTOK`` —
    including ``None``, ``""``, and non-string ids, all coerced to a safe
    string first so a bad value is refused, never raised as a 500."""
    return str(model_id or "") not in MODEL_PRICES_USD_PER_MTOK


def _collides_with_the_review_gate(
    role: str, model_id: str, current: dict[str, str]
) -> bool:
    """True when picking ``model_id`` for ``role`` would make
    ``review_model == primary_model`` — checked symmetrically, since setting
    the coder to the reviewer's model is the same violation as the reverse.

    A missing or empty value on the OTHER side reads as "not set": the rule
    cannot be violated when only one side is configured, so a partial
    (in-progress) config update is never blocked by it.
    """
    if role not in ("reviewer", "coder"):
        return False
    other_key = "primary_model" if role == "reviewer" else "review_model"
    other_value = current.get(other_key) or ""
    return bool(other_value) and str(model_id or "") == other_value


def _reject_unrecognised_current_keys(current: dict[str, str]) -> None:
    """``current`` must be keyed by the ``llm.*`` config keys (``ROLES``'s
    values — e.g. ``"primary_model"``), never by role names (``"coder"``,
    ``"reviewer"``). A role-keyed dict has neither key
    :func:`_collides_with_the_review_gate` reads, so without this check it
    would silently no-op the reviewer-vs-coder gate instead of raising."""
    allowed = set(ROLES.values())
    unknown = set(current) - allowed
    if unknown:
        raise ValueError(
            f"current has unrecognised key(s) {sorted(unknown)!r}; "
            f"must be a subset of {sorted(allowed)!r} (config keys, not "
            "role names)"
        )


def validate(role: str, model_id: str, *, current: dict[str, str]) -> str | None:
    """``None`` when ``model_id`` is an allowed choice for ``role``, else a
    human-readable reason it is refused.

    ``current`` is keyword-only with NO default: it is the caller's current
    ``{config_key: model_id}`` state and is required so the reviewer-vs-coder
    collision gate (below) can never be silently skipped by omission. Its
    keys must be config keys (``ROLES``'s values), not role names — a
    role-keyed dict raises rather than no-opping the gate it feeds.

    ORDER IS LOAD-BEARING: unknown-role -> current-shape -> vendor pin ->
    price -> review gate. Vendor pin is checked before price so that
    offering a non-Claude id (e.g. ``gpt-5-codex``) to a pinned role reports
    the actionable vendor reason. For ``coder`` — not vendor-pinned — the
    very same id instead reports the "no published price" reason; that
    difference is intended and documented here so nobody "fixes" it into
    uniform wording later.
    """
    if not isinstance(role, str) or role not in ROLES:
        raise ValueError(_known_roles_message().format(role))

    _reject_unrecognised_current_keys(current)

    if _violates_vendor_pin(role, model_id):
        return (
            f"{model_id!r} is not a Claude model. {VENDOR_PIN_NOTE}"
        )

    if _has_no_published_price(model_id):
        return (
            f"{model_id!r} has no published price in "
            "core.pricing.MODEL_PRICES_USD_PER_MTOK, so it cannot be billed "
            "at a known rate."
        )

    if _collides_with_the_review_gate(role, model_id, current):
        return (
            "the reviewer must run on a different model than the coder — "
            "README.md and docs/verification.md both promise a different "
            "model reviews the code, and this is that gate."
        )

    return None


def defaults() -> dict[str, str]:
    """The five shipped ``llm.*`` defaults, keyed by config key (``ROLES``'s
    values), read straight from ``DEFAULT_CONFIG`` so "Reset to defaults" in
    part 3 has exactly one source of truth. Imports ``DEFAULT_CONFIG``
    locally, the same idiom as ``orchestrator._utility_model``, to keep this
    module free of any config import at module scope. Returns a fresh dict
    each call, so a caller mutating it cannot corrupt ``DEFAULT_CONFIG``.
    """
    from ..config import DEFAULT_CONFIG

    llm = DEFAULT_CONFIG["llm"]
    return {config_key: llm[config_key] for config_key in ROLES.values()}
