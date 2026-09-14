"""One attempt's USD cost, and one task's — the ONLY place either is computed.

The bug this file exists to fix: ``web/src/cost.js`` priced every attempt at
one flat Anthropic rate ($3/1K fresh, $0.3/1K cache-read), which was simply
wrong for a Codex/OpenAI attempt once ``core/pricing.py`` gained per-model
OpenAI rows (``gpt-5.3-codex`` is $1.75/$14, not $3/$15) — the board was
pricing an 8x cheaper model as if it were Sonnet, silently, for every Codex
task on the page. Fixing it in JS a third time (a second price table, or a
``?? 3.0`` default) would just move the drift somewhere else; the API is now
the one place a USD figure is computed, and the board only formats what it is
sent. See ``web/src/cost.js``'s header for the JS side of that split.

``attempt_cost`` prices ONE attempt row: it walks every role in
``db.USAGE_ROLES``, looks up that role's model from the ``attempts.models``
JSON column (keyed on role NAME — "coder", "reviewer", ... — not the column
prefix), and sums ``pricing.usd_cost`` per role at that role's own rate. A
row where two roles used different models (e.g. a Codex coder reviewed by
Claude) is real and common, so the per-attempt label is the shared model when
every priced role agrees, and ``"mixed"`` when they do not — collapsing to
one of them would misreport which model actually spent which dollar.

DEGENERATE INPUT, all handled without raising, because every caller here is a
read path the board renders directly and a 500 is worse than an honest
number:

  * ``models`` is ``NULL``, ``'{}'``, or not valid JSON (11 of this install's
    684 attempt rows recorded before the column existed) — every role prices
    at ``pricing.FALLBACK_PRICE_NAME``, never at 0.0.
  * A role's token columns are all ``NULL``/0 — that role contributes $0 and
    its (unpriced) model does not count toward the "mixed" decision, so an
    attempt with real spend in only one role is never mislabeled "mixed" by
    an idle one.
  * The attempt list passed to ``attempts_cost`` is ``None``/empty — returns
    ``(None, None)``, matching every sibling ``total_*`` aggregate in
    ``api/models.py`` (``_aux_totals``): "no attempts" and "attempts that
    spent nothing" are different facts and must not both render as 0.
  * ``ledger_rows_as_attempts``: a row's ``model`` is ``None``, or an id
    ``pricing`` does not recognise — prices at ``FALLBACK_PRICE_NAME``, same
    as an attempt row would. A row's ``site`` does not match any registered
    ``AUX_USAGE_TIERS`` prefix (no such row exists today) — priced as
    ``"utility_"`` rather than dropped or raised, because a wrong-but-priced
    dollar is a smaller error than a silently missing one. ``rows`` is
    ``None``/empty — returns ``[]``, so ``attempts_cost(attempts + [])``
    behaves exactly as it does today with no ledger rows at all.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .db import AUX_USAGE_TIERS, ORPHANED_SITE_PREFIX, USAGE_ROLES, usage_columns_for
from .pricing import FALLBACK_PRICE_NAME, input_price_usd_per_mtok, usd_cost


def _parse_models(raw: Any) -> dict[str, Any]:
    """``attempts.models`` as a role-name -> model-id dict, or ``{}``.

    Mirrors how it is written: ``Orchestrator._active_models()`` builds a
    plain dict and ``Store.update_attempt`` JSON-encodes it on the way in, so
    a healthy row holds a JSON object string. Anything else (NULL, ``''``,
    ``'{}'``, a non-JSON string, or JSON that decoded to something other than
    an object) is a row this pricer must still price, not reject — it prices
    at the fallback instead, exactly like an unpriced model id does.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def attempt_cost(row: Mapping[str, Any] | None) -> tuple[float, str]:
    """One attempt's USD cost and the model label it was priced at.

    Sums ``pricing.usd_cost`` per role in ``db.USAGE_ROLES``, each at its own
    recorded model. Returns ``(dollars, label)`` — ``label`` is the one model
    every role with nonzero spend agreed on, ``pricing.FALLBACK_PRICE_NAME``
    when the row spent nothing chargeable, or ``"mixed"`` when priced roles
    disagree. Never raises: see the module docstring's DEGENERATE INPUT list.
    """
    if not row:
        return 0.0, FALLBACK_PRICE_NAME
    models = _parse_models(row.get("models"))
    total = 0.0
    labels: set[str] = set()
    for prefix, role in USAGE_ROLES.items():
        tokens_used_col, cache_read_col, cache_creation_col = usage_columns_for(prefix)
        output_col = "output_tokens" if prefix == "" else f"{prefix}output_tokens"
        tokens_used = row.get(tokens_used_col) or 0
        cache_read = row.get(cache_read_col) or 0
        cache_creation = row.get(cache_creation_col) or 0
        if not (tokens_used or cache_read or cache_creation):
            # This role spent nothing on this attempt — no dollars, and its
            # (possibly unpriced) model must not affect the mixed/agree call
            # for roles that actually spent something.
            continue
        model = models.get(role)
        dollars = usd_cost(
            tokens_used=tokens_used,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            output_tokens=row.get(output_col),
            model=model,
        )
        total += dollars
        _, label = input_price_usd_per_mtok(model)
        labels.add(label)
    if not labels:
        return 0.0, FALLBACK_PRICE_NAME
    if len(labels) == 1:
        return round(total, 6), next(iter(labels))
    return round(total, 6), "mixed"


def attempts_cost(rows: list[Mapping[str, Any]] | None) -> tuple[float | None, str | None]:
    """The USD cost of a list of attempts, summed, and its shared model label.

    ``(None, None)`` when ``rows`` is ``None``/empty — "this task has no
    attempts yet" — never ``(0.0, ...)``, which would say "this task's
    attempts cost nothing" and is not the same fact. Once there is at least
    one attempt, the total is always a number (0.0 is legitimate there: it
    means the attempts recorded really did spend zero).
    """
    if not rows:
        return None, None
    total = 0.0
    labels: set[str] = set()
    for row in rows:
        dollars, label = attempt_cost(row)
        total += dollars
        if dollars:
            labels.add(label)
    total = round(total, 6)
    if not labels:
        return total, FALLBACK_PRICE_NAME
    if len(labels) == 1:
        return total, next(iter(labels))
    return total, "mixed"


def ledger_rows_as_attempts(rows: list[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Reshape OWNED ``unattributed_usage`` rows (``Store.
    usage_ledger_rows_by_task`` / ``task_usage_ledger_rows``, or a
    whole-ledger grouping) into attempt-shaped dicts, so ``attempt_cost`` /
    ``attempts_cost`` price them at their own recorded model instead of a
    second, drifting pricer being written for the ledger.

    Each input row is one ``(site, model)`` group carrying
    ``tokens_used``/``cache_read_tokens``/``cache_creation_tokens``. The role
    is derived from ``site``: ``orphaned_<tier>usage`` strips
    ``ORPHANED_SITE_PREFIX`` and the trailing ``"usage"`` to recover
    ``<tier>`` (e.g. ``"orphaned_plan_usage"`` -> ``"plan_"``), validated
    against ``AUX_USAGE_TIERS`` — the ledger writer today writes nothing
    else. An unrecognised ``site`` falls back to ``"utility_"`` (see the
    module docstring's DEGENERATE INPUT list).

    No ``{tier}output_tokens`` key is emitted: ``unattributed_usage`` has no
    such column (it is a coder/reviewer SDK-only split), so the output
    premium is legitimately absent here, not a bug — same note
    ``orchestrator.py``'s aux-flush carries — and ``attempt_cost`` already
    treats a missing output column as zero extra, exactly as it does for any
    attempt row recorded before that column existed.

    The output is ordinary attempt-shaped mappings with only one role's
    columns populated, so ``attempts_cost`` prices each row at its own model
    and applies the identical "shared model, else mixed" label rule used for
    real attempts — one pricer, one mixed rule, for both tables.
    """
    out: list[dict[str, Any]] = []
    for row in rows or []:
        site = str(row.get("site") or "")
        tier: str | None = None
        if site.startswith(ORPHANED_SITE_PREFIX) and site.endswith("usage"):
            candidate = site[len(ORPHANED_SITE_PREFIX):-len("usage")]
            if candidate in AUX_USAGE_TIERS:
                tier = candidate
        if tier is None:
            tier = "utility_"
        role = USAGE_ROLES[tier]
        tokens_col, read_col, creation_col = usage_columns_for(tier)
        out.append({
            "models": {role: row.get("model")},
            tokens_col: row.get("tokens_used") or 0,
            read_col: row.get("cache_read_tokens") or 0,
            creation_col: row.get("cache_creation_tokens") or 0,
        })
    return out
