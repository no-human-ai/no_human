"""Model picker part 2 of 3: the ONE code path the GET/PUT API and the
``nh config models`` CLI both call. Building this as a pure module (no
FastAPI/click import) is deliberate — see ``core/model_catalog.py``'s
module docstring for the reasoning; that module answers "is this id
allowed", this one answers "what does the running system look like now, and
what happens when an operator asks to change it".

Everything here is synchronous and does blocking file I/O
(``config.set_model_ids`` writes with plain ``pathlib``/``os.replace``); an
async caller (the FastAPI handler) is expected to run it under
``asyncio.to_thread``, exactly like ``save_integration_config`` already does
for its own write.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .. import config as _config
from ..blockers.taxonomy import human_event
from .model_catalog import (
    CODER_BACKEND_REASON,
    REVIEWER_COST_NOTE,
    ROLES,
    _is_claude_id,
    defaults,
    options_for,
    role_note,
    validate,
)
from .role_backend_settings import (
    ROLE_BACKEND_ROLES,
    RoleBackendError,
    apply_role_backend_change,
    effective_role_backend,
    validate_role_backend_entries,
)

__all__ = [
    "ALLOWED_KEYS",
    "ROLE_BY_KEY",
    "ModelSettingsError",
    "CONFIG_AUDIT_TASK_ID",
    "current_models",
    "models_payload",
    "apply_model_changes",
    "model_change_event",
]

#: The five ``llm.*`` config keys a PUT/CLI write may ever touch — the exact
#: allow-list, derived from ``model_catalog.ROLES`` rather than re-typed, so
#: a sixth role can never appear here without also appearing there.
ALLOWED_KEYS = frozenset(ROLES.values())

#: The reverse of ``ROLES``: config key -> role name, for turning a submitted
#: key back into the role ``model_catalog.validate`` wants.
ROLE_BY_KEY = {config_key: role for role, config_key in ROLES.items()}


class ModelSettingsError(ValueError):
    """A model-settings write was refused. The message is always safe to
    show verbatim to an operator: never a stack trace, and any value it
    quotes is exactly what the operator just typed into a model-id field.

    Also raised (re-wrapped from :class:`~.role_backend_settings.
    RoleBackendError`) for a refused ``role_backends`` entry inside the same
    PUT body — one exception type reaching ``/api/config/models``'s single
    ``except`` clause, never a second error shape the endpoint has to learn.
    """


#: ``task_events`` has no task row for a config change — there is no task.
#: ``task_id`` carries no foreign-key constraint (see migrations), so a
#: fixed sentinel id lets ``nh logs``/the activity feed find every model
#: change in one place without inventing a task that never existed.
CONFIG_AUDIT_TASK_ID = "__config__"


def current_models(cfg_data: dict[str, Any] | None) -> dict[str, str]:
    """The five ``llm.*`` values resolved from *cfg_data*, keyed by config
    key. Falls back to the shipped default for any key absent from
    *cfg_data* — so a key missing from a raw/partial dict compares equal to
    its default rather than to ``None``, the same resolution ``load_config``
    itself performs via its deep-merge with ``DEFAULT_CONFIG``.
    """
    llm = (cfg_data or {}).get("llm") or {}
    fallback = defaults()
    return {key: llm.get(key) or fallback[key] for key in ROLES.values()}


def _option_dict(opt: Any) -> dict[str, Any]:
    return {
        "id": opt.id,
        "price_class": {
            "label": opt.price_class.label,
            "input_rate": opt.price_class.input_rate,
            "output_rate": opt.price_class.output_rate,
        },
        "is_default": opt.is_default,
        "note": opt.note,
        "requires_backend": opt.requires_backend,
        # The exact sentence a PUT of this id would be refused with —
        # options_for() leaves the coder role's `note` blank, so without this
        # a requires_backend option would otherwise reach the browser with no
        # explanatory text at all. Empty string (never null) for an option a
        # PUT would accept.
        "disabled_reason": (
            CODER_BACKEND_REASON.format(model_id=opt.id) if opt.requires_backend else ""
        ),
    }


def models_payload(
    running_cfg_data: dict[str, Any], config_path: Path
) -> dict[str, Any]:
    """The GET /api/models (and ``nh config models``) payload.

    Options and defaults come ONLY from ``model_catalog`` (never guessed or
    re-derived here); ``current`` comes from *running_cfg_data* — the config
    object the caller already has bound (the running server's ``app.state.
    config.data`` for the API, or a freshly loaded one for the CLI, which has
    no running process to ask). ``current`` is the honest "what is the
    running process actually using" answer and never changes meaning — the
    Settings pane's restart banner depends on that (see B6 below).

    ``saved`` is the same key resolved from a FRESH read of *config_path* —
    i.e. what the NEXT process start will run, and what ``apply_model_changes``
    diffs a PUT against (``apply_model_changes`` below reads on-disk, not
    *running_cfg_data*, for exactly this reason). A picker must edit against
    ``saved``, not ``current``: right after a successful save, ``current``
    still holds the stale running value until a restart, so a client that
    diffs its next pick (or a "reset to defaults" click) against ``current``
    computes an already-applied-on-disk change as "different" or a
    still-non-default disk value as "already at default" — silently inert
    until restart. ``saved`` fixes that without changing what ``current``
    means.

    ``note`` is :func:`model_catalog.role_note` for the role — the pinned-
    role sentence a picker must render next to the row, previously only
    landing on each *option* (still true; unchanged) and therefore never
    rendered as row-level UI text.

    ``restart_required`` is a true file-vs-process comparison: the five
    values resolved from a FRESH read of *config_path* are compared against
    the five values resolved from *running_cfg_data*. They differ exactly
    when a write has landed on disk that the running process has not picked
    up — the same shape of check ``/api/auth/status``'s
    ``_auth_status_payload`` already performs for the auth profile.

    Constraint §6d: a role in :data:`role_backend_settings.ROLE_BACKEND_ROLES`
    (today, ``"reviewer"`` only) additionally carries a ``"backend"`` block —
    ``role_backend_settings.effective_role_backend``'s return value verbatim,
    never re-derived — so the Settings picker can show "default
    (claude-opus-4-8)" vs an explicit chosen backend/model. Every other role
    is untouched: no ``"backend"`` key at all, the exact shape it had before
    this constraint landed. ``restart_required`` folds in the same role's
    on-disk-vs-running comparison, since a ``role_backends`` write takes
    effect on the orchestrator's NEXT task exactly like a plain model write.

    B6: that ``"backend"`` block is built from *on-disk* config, not
    *running_cfg_data* — deliberately asymmetric with ``current`` above (the
    five plain ``llm.*_model`` scalars, which DO still read the running
    process, since those apply live). A role-backend write only takes full
    effect on the orchestrator's next task, so echoing the still-running
    value back right after a successful PUT would show the just-saved choice
    as "default" and leave its Settings-pane clear control dead until a
    restart, even though the write already landed on disk. Reading on-disk
    means the very next GET (no restart) already reflects a save, and a
    clear-to-default PUT is immediately visible as cleared too.
    """
    running = current_models(running_cfg_data)
    on_disk_cfg = _config.load_config(config_path).data
    on_disk = current_models(on_disk_cfg)

    roles = [
        {
            "role": role,
            "key": key,
            "current": running[key],
            "saved": on_disk[key],
            "default": defaults()[key],
            "options": [_option_dict(opt) for opt in options_for(role)],
            # The role-level pinned-role sentence (see the docstring's "note"
            # paragraph above) — distinct from each option's own `note`.
            "note": role_note(role),
            # The reviewer-only cost/quality note (REVIEWER_COST_NOTE); every
            # other role carries "" — the A/B evidence only exists for this
            # role's tier decision (see model_catalog.py / config.py).
            "cost_note": REVIEWER_COST_NOTE if role == "reviewer" else "",
            **(
                {"backend": effective_role_backend(on_disk_cfg, role)}
                if role in ROLE_BACKEND_ROLES
                else {}
            ),
        }
        for role, key in ROLES.items()
    ]
    restart_required = on_disk != running or any(
        effective_role_backend(on_disk_cfg, role)
        != effective_role_backend(running_cfg_data, role)
        for role in ROLE_BACKEND_ROLES
    )
    return {"roles": roles, "restart_required": restart_required}


def apply_model_changes(
    body: Any,
    *,
    running_cfg_data: dict[str, Any],
    config_path: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Validate and (if anything actually changed) write *body* — a
    ``{config_key: model_id}`` mapping — to *config_path*.

    Returns ``(payload, changes)``: *payload* is the refreshed
    ``models_payload`` (reflecting the write, if one happened); *changes* is
    ``{key: {"old": ..., "new": ...}}`` for every ``llm.*_model`` key whose
    value actually changed, PLUS — constraint §6d — a ``"role_backends"``
    entry (``{role: {"old": ..., "new": ...}}``, `role_backend_settings.
    apply_role_backend_change`'s own return shape verbatim) when the body
    carried a ``role_backends`` key and it changed anything. Empty when the
    request was a no-op repeat in every part — an idempotent PUT writes
    nothing and emits nothing.

    Raises :class:`ModelSettingsError` on: a non-dict body, a non-string
    value, an unrecognised key, any ``model_catalog.validate`` refusal
    (vendor pin, unpriced id, reviewer==coder collision in either
    direction), or — checked strictly AFTER ``validate`` returns ``None`` —
    a non-Claude id for the coder role, which no backend reads from
    ``llm.primary_model``. A refused ``role_backends`` entry raises the same
    :class:`ModelSettingsError`, re-wrapped from `role_backend_settings.
    RoleBackendError` — see that class's docstring — so this stays the ONE
    exception type ``/api/config/models`` has to catch.

    B5: the ENTIRE body — both the scalar keys above AND ``role_backends`` —
    is validated before either half writes anything. `role_backends` is
    checked (via `role_backend_settings.validate_role_backend_entries`, the
    same refusals as `apply_role_backend_change`) right after the scalar
    validation loop and before the scalar write, so a body carrying a valid
    scalar change alongside a refused `role_backends` entry lands NEITHER —
    the scalar half never gets a head start over the half that fails.
    Belt-and-braces beyond that ordering: the two on-disk writes below are
    also wrapped so that if the write step itself fails partway through
    (rather than validation refusing first), the config file is restored to
    its exact pre-request bytes rather than left half-applied.
    """
    if not isinstance(body, dict):
        raise ModelSettingsError("expected a JSON object of {config_key: model_id}")

    # Popped BEFORE the unknown-key check below: "role_backends" is a real,
    # documented key of this PUT body, just not one of the five ALLOWED_KEYS
    # (those are `llm.*_model` scalars; this is a nested per-role mapping) —
    # so it must never trip the "unrecognised config key" refusal.
    body = dict(body)
    role_backend_entries = body.pop("role_backends", None)

    stripped: dict[str, str] = {}
    for key, value in body.items():
        if not isinstance(value, str):
            raise ModelSettingsError(f"{key!r} value must be a string, not {value!r}")
        stripped[key] = value.strip()

    unknown = set(stripped) - ALLOWED_KEYS
    if unknown:
        raise ModelSettingsError(
            f"unrecognised config key(s) {sorted(unknown)!r}; must be a "
            f"subset of {sorted(ALLOWED_KEYS)!r}"
        )

    on_disk_cfg = _config.load_config(config_path).data
    on_disk = current_models(on_disk_cfg)
    # The full resolved five AFTER this write: on-disk values overlaid with
    # every submitted value — so a multi-key PUT (e.g. coder and reviewer
    # swapping models in one request) validates against where each key is
    # HEADED, not where it stood before the request.
    resolved_after = {**on_disk, **stripped}

    for key, value in stripped.items():
        role = ROLE_BY_KEY[key]
        reason = validate(role, value, current=resolved_after)
        if reason is not None:
            raise ModelSettingsError(reason)
        if role == "coder" and not _is_claude_id(value):
            raise ModelSettingsError(CODER_BACKEND_REASON.format(model_id=value))

    # B5: validate the role_backends half of the body BEFORE either write
    # below touches disk — delegated to the SAME validation the (future)
    # standalone role-backend picker will use, never a second, divergent
    # implementation of "is this backend/model choice allowed" folded in
    # here. A refusal here must leave the scalar half above unwritten too,
    # so this runs before the scalar write, not just before the
    # role_backends write.
    if role_backend_entries is not None:
        try:
            validate_role_backend_entries(role_backend_entries, on_disk_cfg=on_disk_cfg)
        except RoleBackendError as exc:
            raise ModelSettingsError(str(exc)) from exc

    changes: dict[str, Any] = {
        key: {"old": on_disk[key], "new": value}
        for key, value in stripped.items()
        if value != on_disk[key]
    }

    # Belt-and-braces beyond the validate-before-write ordering above: if the
    # write step itself fails partway through (rather than validation
    # refusing first — e.g. `set_role_backend`'s own splice/verify/restore
    # raising after the scalar write already landed), restore the file to
    # its exact pre-request bytes rather than leave it half-applied.
    before_bytes = config_path.read_bytes() if config_path.exists() else None
    try:
        if changes:
            _config.set_model_ids({key: c["new"] for key, c in changes.items()}, config_path)

        if role_backend_entries is not None:
            try:
                role_backend_changes, _effective = apply_role_backend_change(
                    role_backend_entries,
                    running_cfg_data=running_cfg_data,
                    config_path=config_path,
                )
            except RoleBackendError as exc:
                raise ModelSettingsError(str(exc)) from exc
            if role_backend_changes:
                changes = {**changes, "role_backends": role_backend_changes}
    except Exception:
        if before_bytes is not None:
            config_path.write_bytes(before_bytes)
        elif config_path.exists():
            config_path.unlink()
        raise

    if not changes:
        return models_payload(running_cfg_data, config_path), {}

    return models_payload(running_cfg_data, config_path), changes


def model_change_event(changes: dict[str, dict[str, str]]) -> dict[str, Any]:
    """The ``source=human`` task_event for a model-settings write, persisted
    against :data:`CONFIG_AUDIT_TASK_ID` via ``Store.save_events``. Built
    from the same ``human_event`` helper every other human-originated event
    uses (``blockers/taxonomy.py``), so a config change reads the same shape
    as any other human action in the activity feed.
    """
    return {
        **human_event("config_models_set", prior_status=""),
        "ts": time.time(),
        "changes": changes,
    }
