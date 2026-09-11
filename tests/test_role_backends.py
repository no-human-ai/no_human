"""Constraint amendment §6d — "Settings per-role model/backend choice must
take REAL effect for the reviewer" (operator, commit ``413d76f0d``).

This is the centerpiece module for the amendment: it proves, end to end,
that ``CLAUDE_PINNED_ROLES`` is a DEFAULT pin for the reviewer role, not an
absolute one, while staying byte-identical when no explicit choice is made.
Four seams, each with its own section below:

1. ``agent.backend`` — ``explicit_role_backend`` / ``resolve_backend_name`` /
   ``make_backend`` construct the chosen backend for the reviewer role, and
   ONLY the reviewer role (every other pinned role is untouched).
2. ``config`` — ``set_role_backend`` is the single writer of
   ``llm.role_backends``; ``_reject_invalid_role_backends`` (run from
   ``load_config``) rejects anything that reached the file any other way.
3. ``core.role_backend_settings`` — the validation + availability-refusal
   layer in front of the writer (the Settings PUT's real code path).
4. Disclosure — ``Orchestrator._emit_models`` / ``_reviewer_attribution``
   read the SAME resolver, never a second opinion.

Every test that inspects a real ``ClaudeBackend``/``CodexBackend``'s
``extra_env`` / ``auth_mode`` / ``cli_path`` needs the REAL class, not
``conftest.py``'s autouse hermetic stub — hence ``pytestmark =
pytest.mark.real_backend`` for the whole module (mirrors
``tests/test_local_backend.py``, ``tests/test_codex_backend.py``). Nothing
here spawns a real subprocess or makes a network call: every backend-
construction test stops at ``__init__``, with ONE deliberate exception — the
codex reviewer-path scrub tests below go one step further, into
``_child_env()``, since (unlike ``ClaudeBackend``'s local-mode scrub, which
is visible on ``extra_env`` right after ``__init__``) ``CodexBackend`` only
builds its child env lazily. Those tests reuse
``tests.test_codex_backend._stub_cli`` to stay just as hermetic: the CLI
probes are monkeypatched, so nothing real is spawned there either.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.real_backend

from no_human.agent.backend import (
    CLAUDE_PINNED_ROLES,
    LOCAL_CAPABILITIES,
    BackendUnavailable,
    explicit_role_backend,
    make_backend,
    resolve_backend_name,
)
from no_human.agent.claude_backend import CLAUDE_CAPABILITIES, ClaudeBackend
from no_human.agent.codex_backend import CodexBackend
from no_human.config import AuthError, codex_auth_mode, load_config, set_role_backend
from no_human.core.model_catalog import defaults as model_defaults
from no_human.core.role_backend_settings import (
    ROLE_BACKEND_ROLES,
    RoleBackendError,
    apply_role_backend_change,
    effective_role_backend,
    role_backend_change_event,
)
from tests.test_codex_backend import FAKE_ENV, _stub_cli

CODEX_CFG = {"llm": {"codex_auth_mode": "api_key"}}
LOCAL_CFG = {
    "llm": {
        "local_base_url": "http://localhost:8000",
        "local_model": "some-local-model",
    }
}


def _cfg_with_reviewer_backend(backend: str, model: str, base: dict | None = None) -> dict:
    cfg = {k: dict(v) for k, v in (base or {}).items()}
    cfg.setdefault("llm", {})
    cfg["llm"]["role_backends"] = {"reviewer": {"backend": backend, "model": model}}
    return cfg


def _write_config(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


# --------------------------------------------------------------------------- #
# AC1 — byte-identical default reviewer construction.                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cfg", [None, {}, {"llm": {}}, {"llm": {"role_backends": {}}}]
)
def test_default_reviewer_construction_is_byte_identical_to_today(cfg):
    """No config, an empty config, and an explicitly-empty `role_backends`
    all take the pre-existing pinned path: a `ClaudeBackend` on
    `claude-opus-4-8` (the review_model default), no injected env, no CLI
    override — exactly what a reviewer session got before this amendment."""
    backend = make_backend(model="claude-opus-4-8", config=cfg, role="reviewer", readonly=True)
    assert isinstance(backend, ClaudeBackend)
    assert backend.model == "claude-opus-4-8"
    assert backend.extra_env == {}
    assert backend.cli_path is None
    assert backend.capabilities is CLAUDE_CAPABILITIES
    assert resolve_backend_name(cfg, role="reviewer") == "claude"
    assert explicit_role_backend(cfg, "reviewer") is None


def test_a_caller_supplied_backend_kwarg_is_ignored_for_the_reviewer_role():
    """The pin is the FACTORY's, not the caller's: a stray `backend="codex"`
    kwarg on a reviewer call must NOT move the review off Claude — only an
    explicit `llm.role_backends.reviewer` Settings entry can."""
    backend = make_backend(
        model="claude-opus-4-8", config={}, role="reviewer", backend="codex", readonly=True,
    )
    assert isinstance(backend, ClaudeBackend)


def test_effective_role_backend_default_reads_review_model():
    effective = effective_role_backend({}, "reviewer")
    assert effective == {"backend": "claude", "model": "claude-opus-4-8", "is_default": True}
    assert model_defaults()["review_model"] == "claude-opus-4-8"


# --------------------------------------------------------------------------- #
# AC2 — an explicit Settings reviewer choice takes effect end-to-end.         #
# --------------------------------------------------------------------------- #


def test_explicit_claude_reviewer_choice_overrides_the_model_only():
    cfg = _cfg_with_reviewer_backend("claude", "claude-sonnet-5")
    backend = make_backend(model="claude-opus-4-8", config=cfg, role="reviewer", readonly=True)
    assert isinstance(backend, ClaudeBackend)
    assert backend.model == "claude-sonnet-5"
    assert backend.extra_env == {}
    assert resolve_backend_name(cfg, role="reviewer") == "claude"


def test_explicit_codex_reviewer_choice_constructs_a_codex_backend():
    cfg = _cfg_with_reviewer_backend("codex", "gpt-5-codex", CODEX_CFG)
    backend = make_backend(model="claude-opus-4-8", config=cfg, role="reviewer", readonly=True)
    assert isinstance(backend, CodexBackend)
    assert backend.model == "gpt-5-codex"
    assert resolve_backend_name(cfg, role="reviewer") == "codex"
    # One billing path: the SAME `codex_auth_mode` resolver a coder session
    # on this config would use — never a second, reviewer-only credential
    # path invented for this role.
    assert backend.auth_mode == codex_auth_mode(cfg)


def test_explicit_local_reviewer_choice_constructs_a_local_backend_with_the_scrub():
    cfg = _cfg_with_reviewer_backend("local", "my-local-model", LOCAL_CFG)
    backend = make_backend(model="claude-opus-4-8", config=cfg, role="reviewer", readonly=True)
    assert isinstance(backend, ClaudeBackend)
    assert backend.model == "my-local-model"
    assert backend.capabilities is LOCAL_CAPABILITIES
    # The exact three-entry scrub `_local_child_env` gives the coder role —
    # inherited, not reimplemented: the reviewer never carries the real
    # subscription/enterprise OAuth token to a third-party server.
    assert set(backend.extra_env) == {
        "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    }
    assert backend.extra_env["ANTHROPIC_BASE_URL"] == "http://localhost:8000"
    assert backend.extra_env["CLAUDE_CODE_OAUTH_TOKEN"] == ""


def test_explicit_local_reviewer_choice_never_leaks_a_real_oauth_token(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-should-not-leak")
    cfg = _cfg_with_reviewer_backend("local", "my-local-model", LOCAL_CFG)
    backend = make_backend(model="claude-opus-4-8", config=cfg, role="reviewer", readonly=True)
    assert backend.extra_env["CLAUDE_CODE_OAUTH_TOKEN"] == ""


def test_explicit_codex_reviewer_choice_scrubs_the_claude_credential(monkeypatch):
    """S1: the codex reviewer path gets the SAME scrub `_child_env` already
    gives the coder role — mirrors
    `test_explicit_local_reviewer_choice_constructs_a_local_backend_with_the_scrub`
    above. Before this test, `test_explicit_codex_reviewer_choice_constructs_
    a_codex_backend` only asserted `auth_mode`; nothing proved the reviewer's
    Codex subprocess actually drops the Claude credential."""
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_ENV["OPENAI_API_KEY"])
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "not-a-real-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key-either")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "not-a-real-token-either")
    _stub_cli(monkeypatch)  # api_key billing gate must not shell out to the real CLI
    cfg = _cfg_with_reviewer_backend("codex", "gpt-5-codex", CODEX_CFG)
    backend = make_backend(model="claude-opus-4-8", config=cfg, role="reviewer", readonly=True)
    assert isinstance(backend, CodexBackend)
    env = backend._child_env()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["OPENAI_API_KEY"] == FAKE_ENV["OPENAI_API_KEY"]


def test_explicit_codex_reviewer_choice_never_leaks_a_real_oauth_token(monkeypatch):
    """Positive control for the scrub above (mirrors
    `test_explicit_local_reviewer_choice_never_leaks_a_real_oauth_token`): a
    realistic-looking subscription token planted in the parent env must still
    not reach the codex child env."""
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_ENV["OPENAI_API_KEY"])
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-should-not-leak")
    _stub_cli(monkeypatch)
    cfg = _cfg_with_reviewer_backend("codex", "gpt-5-codex", CODEX_CFG)
    backend = make_backend(model="claude-opus-4-8", config=cfg, role="reviewer", readonly=True)
    env = backend._child_env()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_explicit_reviewer_choice_does_not_affect_the_coder_role():
    """The same config's coder role is untouched by a reviewer-only entry —
    `role_backends` is per-role, never global."""
    cfg = _cfg_with_reviewer_backend("codex", "gpt-5-codex", CODEX_CFG)
    assert resolve_backend_name(cfg, role="coder") == "claude"
    backend = make_backend(model="claude-sonnet-5", config=cfg, role="coder")
    assert isinstance(backend, ClaudeBackend)
    assert backend.model == "claude-sonnet-5"


@pytest.mark.parametrize(
    "role", [r for r in CLAUDE_PINNED_ROLES if r != "reviewer"]
)
def test_a_reviewer_only_entry_does_not_move_any_other_pinned_role(role):
    """Out of scope for this ticket: planner/supervisor/utility/intake stay
    on the default even when the SAME config carries a reviewer override —
    proves the seam is reviewer-only today, not accidentally role-generic."""
    cfg = _cfg_with_reviewer_backend("codex", "gpt-5-codex", CODEX_CFG)
    assert resolve_backend_name(cfg, role=role) == "claude"
    backend = make_backend(model="claude-opus-5", config=cfg, role=role, readonly=True)
    assert isinstance(backend, ClaudeBackend)
    assert backend.extra_env == {}


def test_make_backend_does_not_mutate_the_callers_config_for_codex():
    cfg = _cfg_with_reviewer_backend("codex", "gpt-5-codex", CODEX_CFG)
    before = {"llm": dict(cfg["llm"])}
    make_backend(model="claude-opus-4-8", config=cfg, role="reviewer", readonly=True)
    assert cfg["llm"] == before["llm"]
    assert "codex_model" not in cfg["llm"]


def test_make_backend_does_not_mutate_the_callers_config_for_local():
    cfg = _cfg_with_reviewer_backend("local", "my-local-model", LOCAL_CFG)
    before = {"llm": dict(cfg["llm"])}
    make_backend(model="claude-opus-4-8", config=cfg, role="reviewer", readonly=True)
    assert cfg["llm"] == before["llm"]
    assert "local_model" not in cfg["llm"] or cfg["llm"]["local_model"] == "some-local-model"


def test_availability_refusal_reaches_apply_role_backend_change(tmp_path, monkeypatch):
    """`apply_role_backend_change` refuses a backend this install cannot run
    right now, via the SAME `describe_backend` the coder-backend picker
    uses — never a second opinion invented for this picker."""
    from no_human.core import role_backend_settings as rbs_module

    def _fake_describe(name, config_data):
        return {"id": name, "available": False, "reason": f"{name} is not configured"}

    monkeypatch.setattr(rbs_module, "describe_backend", _fake_describe)
    config_path = tmp_path / "config.yaml"
    load_config(config_path)  # materialize a default file
    with pytest.raises(RoleBackendError, match="not configured"):
        apply_role_backend_change(
            {"reviewer": {"backend": "codex", "model": "gpt-5-codex"}},
            running_cfg_data={},
            config_path=config_path,
        )
    # Refused BEFORE any write reached disk.
    assert effective_role_backend(load_config(config_path).data, "reviewer")["is_default"]


def test_apply_role_backend_change_refuses_a_claude_model_not_offered_to_the_role(tmp_path):
    """`model_catalog._is_claude_id` recognises a Claude id by the
    ``"claude-"`` prefix alone (`core/model_catalog.py:205-210`) — the
    catalog check only fires for a string shaped like one. Use a
    "claude-"-prefixed id that is genuinely absent from
    ``MODEL_PRICES_USD_PER_MTOK`` so this exercises the real refusal path
    rather than a string the code never treats as a Claude id at all."""
    config_path = tmp_path / "config.yaml"
    load_config(config_path)
    with pytest.raises(RoleBackendError, match="not an offered model"):
        apply_role_backend_change(
            {"reviewer": {"backend": "claude", "model": "claude-not-a-real-model"}},
            running_cfg_data={},
            config_path=config_path,
        )


def test_apply_role_backend_change_writes_then_a_fresh_load_resolves_the_choice(tmp_path):
    config_path = tmp_path / "config.yaml"
    load_config(config_path)
    changes, effective = apply_role_backend_change(
        {"reviewer": {"backend": "claude", "model": "claude-sonnet-5"}},
        running_cfg_data={},
        config_path=config_path,
    )
    assert changes["reviewer"]["old"] is None
    assert changes["reviewer"]["new"] == {"backend": "claude", "model": "claude-sonnet-5"}
    assert effective["reviewer"] == {
        "backend": "claude", "model": "claude-sonnet-5", "is_default": False,
    }
    reloaded = load_config(config_path).data
    assert resolve_backend_name(reloaded, role="reviewer") == "claude"
    backend = make_backend(model="claude-opus-4-8", config=reloaded, role="reviewer", readonly=True)
    assert backend.model == "claude-sonnet-5"


def test_apply_role_backend_change_is_idempotent_on_a_repeat(tmp_path):
    config_path = tmp_path / "config.yaml"
    load_config(config_path)
    entry = {"reviewer": {"backend": "claude", "model": "claude-sonnet-5"}}
    apply_role_backend_change(entry, running_cfg_data={}, config_path=config_path)
    changes, _effective = apply_role_backend_change(entry, running_cfg_data={}, config_path=config_path)
    assert changes == {}


def test_apply_role_backend_change_clears_back_to_default(tmp_path):
    config_path = tmp_path / "config.yaml"
    load_config(config_path)
    apply_role_backend_change(
        {"reviewer": {"backend": "claude", "model": "claude-sonnet-5"}},
        running_cfg_data={}, config_path=config_path,
    )
    changes, effective = apply_role_backend_change(
        {"reviewer": None}, running_cfg_data={}, config_path=config_path,
    )
    assert changes["reviewer"]["new"] is None
    assert effective["reviewer"]["is_default"] is True
    reloaded = load_config(config_path).data
    assert explicit_role_backend(reloaded, "reviewer") is None


def test_role_backend_change_event_shape():
    event = role_backend_change_event({"reviewer": {"old": None, "new": {"backend": "codex", "model": "x"}}})
    # `human_event` (blockers/taxonomy.py) always prefixes the verb with
    # "human_" — the ONE task_events shape every human status/config verb
    # uses, never a bespoke kind invented for this one.
    assert event["kind"] == "human_config_role_backend_set"
    assert event["source"] == "human"
    assert event["changes"]["reviewer"]["new"]["backend"] == "codex"
    assert "ts" in event


# --------------------------------------------------------------------------- #
# AC3 — single write path + load-time rejection.                              #
# --------------------------------------------------------------------------- #


def test_set_role_backend_is_the_only_writer_of_role_backends():
    """Grep-pinned: nothing outside `config.py`/`role_backend_settings.py`
    ever calls `set_role_backend` — a second call site would be a second,
    unvalidated write path for `llm.role_backends`."""
    import pathlib
    import re

    repo_src = pathlib.Path(__file__).resolve().parent.parent / "src" / "no_human"
    hits = []
    for path in repo_src.rglob("*.py"):
        if path.name in ("config.py", "role_backend_settings.py"):
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"\.set_role_backend\(|(?<!def )\bset_role_backend\(", text):
            hits.append(str(path))
    assert hits == []


def test_set_role_backend_rejects_an_unknown_role(tmp_path):
    config_path = tmp_path / "config.yaml"
    load_config(config_path)
    with pytest.raises(ValueError, match="not supported"):
        set_role_backend("planner", "claude", "claude-opus-5", config_path=config_path)


def test_set_role_backend_rejects_an_unsupported_backend(tmp_path):
    config_path = tmp_path / "config.yaml"
    load_config(config_path)
    with pytest.raises(ValueError, match="not supported"):
        set_role_backend("reviewer", "gemini", "some-model", config_path=config_path)


@pytest.mark.parametrize(
    "bad_yaml",
    [
        'llm:\n  role_backends: "not-a-mapping"\n',
        "llm:\n  role_backends:\n    planner: {backend: claude, model: claude-opus-5}\n",
        "llm:\n  role_backends:\n    reviewer: not-a-mapping\n",
        "llm:\n  role_backends:\n    reviewer: {backend: claude, model: claude-sonnet-5, extra: 1}\n",
        "llm:\n  role_backends:\n    reviewer: {backend: gemini, model: claude-sonnet-5}\n",
        "llm:\n  role_backends:\n    reviewer: {backend: claude, model: 'not a bare id!!'}\n",
        "llm:\n  role_backends:\n    reviewer: {backend: claude}\n",
        # B7: a `backend: claude` claim is checked against the catalog
        # regardless of the model string's shape — `gpt-5-codex` is a
        # well-shaped bare id (passes `_MODEL_ID_SHAPE_RE`) but is not a
        # model `model_catalog.options_for("reviewer")` offers, so a
        # hand-edited config can't smuggle it in under the Claude backend.
        "llm:\n  role_backends:\n    reviewer: {backend: claude, model: gpt-5-codex}\n",
    ],
)
def test_load_config_rejects_a_hand_edited_role_backends_entry(tmp_path, bad_yaml):
    config_path = _write_config(tmp_path, bad_yaml)
    with pytest.raises(ValueError):
        load_config(config_path)


def test_load_config_accepts_a_well_shaped_role_backends_entry(tmp_path):
    config_path = _write_config(
        tmp_path,
        "llm:\n  role_backends:\n    reviewer: {backend: claude, model: claude-sonnet-5}\n",
    )
    cfg = load_config(config_path).data
    assert cfg["llm"]["role_backends"] == {
        "reviewer": {"backend": "claude", "model": "claude-sonnet-5"}
    }


def test_role_backend_roles_whitelist_is_reviewer_only():
    """§6d wires the reviewer only — planner/supervisor/utility/intake are
    future entries through the SAME seam, not silently already-on."""
    assert tuple(ROLE_BACKEND_ROLES) == ("reviewer",)


def test_task_config_cannot_set_a_role_backend():
    """`explicit_role_backend` reads ONLY `llm.role_backends` off the config
    mapping handed to it — a per-task `task.config["backend"]`-shaped value
    sitting elsewhere in the same dict must never be consulted."""
    cfg = {"llm": {}, "task": {"config": {"role_backends": {"reviewer": {
        "backend": "codex", "model": "gpt-5-codex"}}}}}
    assert explicit_role_backend(cfg, "reviewer") is None
    assert resolve_backend_name(cfg, role="reviewer") == "claude"


# --------------------------------------------------------------------------- #
# AC4 — disclosure: task-detail models line + PR body.                        #
# --------------------------------------------------------------------------- #


def test_reviewer_attribution_default_is_empty_string():
    from no_human.core.orchestrator import Orchestrator

    class _Stub:
        config = {}
        _reviewer_attribution = Orchestrator._reviewer_attribution

    assert _Stub()._reviewer_attribution() == ""


def test_reviewer_attribution_non_default_names_backend_and_model():
    from no_human.core.orchestrator import Orchestrator

    class _Stub:
        config = _cfg_with_reviewer_backend("codex", "gpt-5-codex", CODEX_CFG)
        _reviewer_attribution = Orchestrator._reviewer_attribution

    assert _Stub()._reviewer_attribution() == "codex `gpt-5-codex`"


def test_backend_agnostic_contract_reviewer_prompt_indifferent_to_backend():
    """The review prompt/verdict-parsing contract (out of scope to change)
    must not silently assume `ClaudeBackend` — a role-generic seam that
    happened to only work for Claude would defeat the whole amendment. This
    only proves construction succeeds identically shaped for both backends;
    prompt/verdict internals are untouched (OUT OF SCOPE)."""
    claude_backend = make_backend(
        model="claude-opus-4-8", config={}, role="reviewer", readonly=True)
    codex_backend = make_backend(
        model="claude-opus-4-8",
        config=_cfg_with_reviewer_backend("codex", "gpt-5-codex", CODEX_CFG),
        role="reviewer", readonly=True,
    )
    assert hasattr(claude_backend, "model") and hasattr(codex_backend, "model")
    assert claude_backend.readonly is True
    assert codex_backend.readonly is True


def test_unknown_reviewer_backend_name_is_refused_with_the_name():
    cfg = _cfg_with_reviewer_backend("claude", "claude-sonnet-5")
    cfg["llm"]["role_backends"]["reviewer"]["backend"] = "gemini"
    with pytest.raises(BackendUnavailable) as exc:
        make_backend(model="claude-opus-4-8", config=cfg, role="reviewer", readonly=True)
    assert "gemini" in str(exc.value)


# --------------------------------------------------------------------------- #
# AC4 (continued) — the task-detail models line (`Orchestrator._emit_models`) #
# --------------------------------------------------------------------------- #


class _StubBackend:
    model = "claude-sonnet-5"


async def _orchestrator(store, config_path, events):
    from no_human.core.orchestrator import Orchestrator
    from no_human.notify.slack import SlackNotifier

    return Orchestrator(
        store,
        load_config(config_path).data,
        _StubBackend(),
        SlackNotifier(None),
        event_sink=events.append,
    )


# A four-role production-shaped models dict, stamped with an auth profile
# (as every real run is) — long enough that an appended suffix (the old
# disclosure mechanism) could never render past the 110-char clip
# `web/src/summaries.js` applies to the "Models" fact.
_PROD_MODELS = {
    "coder": "claude-sonnet-5",
    "planner": "claude-opus-5",
    "reviewer": "claude-opus-4-8",
    "supervisor": "claude-sonnet-5",
}


async def test_emit_models_default_reviewer_adds_no_disclosure(tmp_path, monkeypatch):
    from no_human.core.db import Store

    monkeypatch.setattr(
        "no_human.core.orchestrator.active_auth_profile", lambda: "personal"
    )
    store = await Store(tmp_path / "t.db").connect()
    try:
        events = []
        orch = await _orchestrator(store, tmp_path / "config.yaml", events)
        orch._emit_models(_PROD_MODELS)
        (event,) = [e for e in events if e["kind"] == "models"]
        assert len(event["text"]) >= 110
        assert "role_backends" not in event
        assert "reviewer-backend=" not in event["text"]
    finally:
        await store.close()


async def test_emit_models_non_default_reviewer_discloses_backend_and_model(
    tmp_path, monkeypatch
):
    from no_human.core.db import Store

    monkeypatch.setattr(
        "no_human.core.orchestrator.active_auth_profile", lambda: "personal"
    )
    config_path = tmp_path / "config.yaml"
    load_config(config_path)  # materialize a real config.yaml first
    set_role_backend("reviewer", "codex", "gpt-5-codex", config_path=config_path)

    store = await Store(tmp_path / "t.db").connect()
    try:
        events = []
        orch = await _orchestrator(store, config_path, events)
        # The disclosed backend tracks what actually got CONSTRUCTED, not just
        # the resolver's opinion — stub the reviewer object the way
        # `AdversarialReviewer.from_config` would set it up.
        orch.reviewer = _StubBackend()
        orch.reviewer.backend_name = "codex"
        orch._emit_models(_PROD_MODELS)
        (event,) = [e for e in events if e["kind"] == "models"]
        assert len(event["text"]) >= 110
        assert "reviewer-backend=" not in event["text"]
        assert event["role_backends"] == {
            "reviewer": {"backend": "codex", "model": "gpt-5-codex"}
        }
    finally:
        await store.close()


async def test_emit_models_disclosed_backend_tracks_constructed_reviewer_not_resolver(
    tmp_path, monkeypatch
):
    """S4: `AdversarialReviewer.backend_name` — not just the config resolver
    — is the source of the disclosed backend (`_emit_models`'s `constructed
    or reviewer_effective["backend"]`). A stub reviewer built on a DIFFERENT
    backend than config resolves to must win, proving the field is actually
    CONSUMED rather than written-and-ignored.
    """
    from no_human.core.db import Store

    monkeypatch.setattr(
        "no_human.core.orchestrator.active_auth_profile", lambda: "personal"
    )
    config_path = tmp_path / "config.yaml"
    load_config(config_path)  # materialize a real config.yaml first
    # Config says "codex", but the reviewer that actually got CONSTRUCTED
    # (e.g. because it was injected directly, bypassing from_config) reports
    # itself as "local" — disclosure must reflect what actually ran.
    set_role_backend("reviewer", "codex", "gpt-5-codex", config_path=config_path)

    store = await Store(tmp_path / "t.db").connect()
    try:
        events = []
        orch = await _orchestrator(store, config_path, events)
        orch.reviewer = _StubBackend()
        orch.reviewer.backend_name = "local"
        orch._emit_models(_PROD_MODELS)
        (event,) = [e for e in events if e["kind"] == "models"]
        assert event["role_backends"]["reviewer"]["backend"] == "local"
    finally:
        await store.close()
