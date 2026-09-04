"""Model picker part 2 of 3: GET /api/models + PUT /api/config/models.

Both endpoints are thin wrappers over `core.model_settings` (see that
module's docstring) — these tests exercise the wire contract: shapes, the
five 422 refusal rules (including the new coder-backend rule), the true
file-vs-process `restart_required` flag, the single `source=human`
task_event per write, and that a successful write does NOT reload
`app.state.config` (constraint: the running process only picks a model
change up on its next server restart).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import no_human.config as nh_config
from no_human.api.app import app
from no_human.core import model_catalog as mc

pytestmark = pytest.mark.usefixtures("isolated_env_file")

#: A hand-written config.yaml with real structure — a header comment, an
#: inline comment, a comment inside the `llm:` block, and unrelated
#: `server:`/`git:` sections — mirroring
#: tests/test_auth_profiles.py::test_set_auth_profile_preserves_comments_and_round_trips.
#: `test_put_preserves_config_yaml_structure` above seeds via a comment-free
#: `yaml.safe_dump(DEFAULT_CONFIG)`, so it cannot tell a comment-preserving
#: splice from a `yaml.safe_load`/`yaml.safe_dump` round-trip — neither has
#: any comments to lose. This seed does.
_COMMENTED_CONFIG = (
    "# no_human config.yaml — hand-edited, keep these comments.\n"
    "server:\n"
    "  host: 127.0.0.1\n"
    "  port: 8420\n"
    "llm:\n"
    "  # Billing mode: subscription bills CLAUDE_CODE_OAUTH_TOKEN.\n"
    "  auth_mode: subscription\n"
    "  primary_model: claude-sonnet-5\n"
    "  review_model: claude-opus-4-8  # reviewer tier — do not change lightly\n"
    "  planner_model: claude-opus-5\n"
    "  supervisor_model: claude-sonnet-5\n"
    "  utility_model: claude-haiku-4-5\n"
    "git:\n"
    "  branch_prefix: no-human/\n"
)


@pytest_asyncio.fixture
async def client(store, tmp_path, monkeypatch):
    # HARD GUARD: every config read/write in these tests must land under
    # tmp_path, never the operator's real ~/.no_human/config.yaml.
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")
    assert nh_config.CONFIG_PATH == tmp_path / "config.yaml"
    assert str(nh_config.CONFIG_PATH).startswith(str(tmp_path))

    app.state.store = store
    app.state.config = nh_config.load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    # A real browser always sends Origin on a write; the PUT route requires
    # it (writing=True), so the fixture mirrors the legitimate Settings UI
    # for every test — GET tests simply never rely on it.
    async with AsyncClient(transport=transport, base_url="http://localhost",
                           headers={"Origin": "http://127.0.0.1:8420"}) as c:
        yield c


# --------------------------------------------------------------------------- #
# GET /api/models                                                             #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_get_models_returns_all_five_roles_from_the_catalog(client):
    r = await client.get("/api/models")
    assert r.status_code == 200
    body = r.json()
    roles = {row["role"]: row for row in body["roles"]}
    assert set(roles) == set(mc.ROLES)
    for role, key in mc.ROLES.items():
        assert roles[role]["key"] == key
        assert roles[role]["options"], f"{role} must offer at least one option"
        # Options come ONLY from the catalog — never re-derived here.
        catalog_ids = {opt.id for opt in mc.options_for(role)}
        assert {opt["id"] for opt in roles[role]["options"]} == catalog_ids


@pytest.mark.asyncio
async def test_get_models_current_matches_the_shipped_defaults_on_a_fresh_install(client):
    r = await client.get("/api/models")
    body = r.json()
    defaults = mc.defaults()
    for row in body["roles"]:
        assert row["current"] == defaults[row["key"]]
        assert row["default"] == defaults[row["key"]]


@pytest.mark.asyncio
async def test_get_models_restart_required_false_when_disk_matches_running(client):
    r = await client.get("/api/models")
    assert r.json()["restart_required"] is False


@pytest.mark.asyncio
async def test_get_models_restart_required_true_when_disk_diverges_from_running(
        client, tmp_path):
    """A write lands on disk (e.g. from another `nh` process, or the CLI)
    that the already-running server has not picked up — the GET must say so
    rather than silently reporting the stale in-process value as current."""
    from no_human.config import set_model_ids

    set_model_ids({"utility_model": "claude-opus-5"}, tmp_path / "config.yaml")
    r = await client.get("/api/models")
    body = r.json()
    assert body["restart_required"] is True
    # `current` still reflects the RUNNING process, not the new disk value —
    # that is the whole point of the flag existing.
    util = next(row for row in body["roles"] if row["role"] == "utility")
    assert util["current"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_get_models_disabled_reason_matches_requires_backend_exactly(client):
    """Model picker part 3 (Settings pane) renders an option `disabled` iff
    the server marked it `requires_backend`, using `disabled_reason` as the
    reason text — so the wire contract must hold both directions: every
    `requires_backend: true` option carries a non-empty reason naming
    `worker.backend` (the knob the pane's note tells the user to use
    instead), and every `requires_backend: false` option carries `""`, never
    a stale leftover reason."""
    r = await client.get("/api/models")
    body = r.json()
    for row in body["roles"]:
        for opt in row["options"]:
            if opt["requires_backend"]:
                assert opt["disabled_reason"] != "", (row["role"], opt["id"])
                assert "worker.backend" in opt["disabled_reason"]
                assert opt["id"] in opt["disabled_reason"]
            else:
                assert opt["disabled_reason"] == "", (row["role"], opt["id"])


@pytest.mark.asyncio
async def test_get_models_cost_note_is_reviewer_only_and_matches_the_catalog(client):
    """Only the reviewer row may carry a `cost_note` (the A/B-revert evidence
    ModelsPanel.jsx renders under that row) — every other role's is `""`, and
    the reviewer's is the catalog constant verbatim, not a re-derived or
    truncated copy."""
    r = await client.get("/api/models")
    body = r.json()
    for row in body["roles"]:
        if row["role"] == "reviewer":
            assert row["cost_note"] == mc.REVIEWER_COST_NOTE
        else:
            assert row["cost_note"] == ""


# --------------------------------------------------------------------------- #
# PUT /api/config/models — refusals                                          #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_put_unknown_key_is_422(client):
    r = await client.put("/api/config/models", json={"not_a_real_key": "claude-opus-5"})
    assert r.status_code == 422
    assert "not_a_real_key" in r.text


@pytest.mark.asyncio
async def test_put_non_string_value_is_422(client):
    r = await client.put("/api/config/models", json={"utility_model": 5})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_non_object_body_is_422(client):
    r = await client.put("/api/config/models", json=["not", "an", "object"])
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_vendor_pin_refusal_is_422(client):
    """A pinned role (reviewer) offered a non-Claude id — even a priced one —
    must be refused with the vendor-pin reason, not silently accepted."""
    r = await client.put("/api/config/models", json={"review_model": "gpt-5.4"})
    assert r.status_code == 422
    assert "not a Claude model" in r.text


@pytest.mark.asyncio
async def test_put_unpriced_id_refusal_is_422(client):
    r = await client.put(
        "/api/config/models", json={"primary_model": "totally-made-up-model"})
    assert r.status_code == 422
    assert "no published price" in r.text


@pytest.mark.asyncio
async def test_put_review_gate_collision_is_422_coder_to_reviewer(client):
    """Setting the coder to the reviewer's CURRENT model is refused —
    README/verification.md promise a different model reviews the code."""
    defaults = mc.defaults()
    r = await client.put(
        "/api/config/models", json={"primary_model": defaults["review_model"]})
    assert r.status_code == 422
    assert "different model" in r.text


@pytest.mark.asyncio
async def test_put_review_gate_collision_is_422_reviewer_to_coder(client):
    defaults = mc.defaults()
    r = await client.put(
        "/api/config/models", json={"review_model": defaults["primary_model"]})
    assert r.status_code == 422
    assert "different model" in r.text


@pytest.mark.asyncio
async def test_put_coder_backend_refusal_is_422_for_a_priced_non_claude_id(client):
    """gpt-5.4 IS priced (passes model_catalog.validate cleanly) but is not a
    Claude id — no backend reads a non-Claude id from llm.primary_model, so
    this must be refused by the NEW coder-backend rule, layered strictly
    after validate(), naming worker.backend / llm.codex_model / llm.local_model."""
    r = await client.put("/api/config/models", json={"primary_model": "gpt-5.4"})
    assert r.status_code == 422
    assert "worker.backend" in r.text
    assert "llm.codex_model" in r.text
    assert "llm.local_model" in r.text
    # And this is NOT the price-refusal reason — the two-layer ordering
    # matters: validate() must have returned None first.
    assert "no published price" not in r.text


@pytest.mark.asyncio
async def test_put_refusal_does_not_write_to_disk(client, tmp_path):
    before = (tmp_path / "config.yaml").read_text()
    r = await client.put("/api/config/models", json={"primary_model": "gpt-5.4"})
    assert r.status_code == 422
    after = (tmp_path / "config.yaml").read_text()
    assert before == after


@pytest.mark.asyncio
async def test_put_requires_local_origin(store, tmp_path, monkeypatch):
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")
    app.state.store = store
    app.state.config = nh_config.load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        r = await c.put("/api/config/models", json={"utility_model": "claude-opus-5"})
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# PUT /api/config/models — success                                           #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_put_success_writes_to_disk_and_returns_refreshed_payload(client, tmp_path):
    r = await client.put("/api/config/models", json={"utility_model": "claude-opus-5"})
    assert r.status_code == 200
    body = r.json()
    # The response is the refreshed GET payload — computed the same way GET
    # computes it, from the (untouched) running process. `current` therefore
    # still shows the OLD value here; that is what makes restart_required
    # honest below. The actual write is verified straight off disk.
    util = next(row for row in body["roles"] if row["role"] == "utility")
    assert util["current"] == "claude-haiku-4-5"
    assert body["restart_required"] is True
    on_disk = nh_config.load_config(tmp_path / "config.yaml")
    assert on_disk.data["llm"]["utility_model"] == "claude-opus-5"


@pytest.mark.asyncio
async def test_put_strips_whitespace_before_validating(client, tmp_path):
    r = await client.put(
        "/api/config/models", json={"utility_model": "  claude-opus-5  "})
    assert r.status_code == 200
    on_disk = nh_config.load_config(tmp_path / "config.yaml")
    assert on_disk.data["llm"]["utility_model"] == "claude-opus-5"


@pytest.mark.asyncio
async def test_put_does_not_reload_running_app_state_config(client):
    """The running process's bound config must be untouched by a successful
    write — restart_required stays honest until an actual restart, and a
    task started right after this PUT still runs on the OLD model."""
    before = app.state.config.data.get("llm", {}).get("utility_model")
    r = await client.put("/api/config/models", json={"utility_model": "claude-opus-5"})
    assert r.status_code == 200
    assert app.state.config.data.get("llm", {}).get("utility_model") == before


@pytest.mark.asyncio
async def test_put_idempotent_no_op_emits_no_event(client, store):
    defaults = mc.defaults()
    r = await client.put(
        "/api/config/models", json={"utility_model": defaults["utility_model"]})
    assert r.status_code == 200
    events = await store.list_events("__config__")
    assert events == []


@pytest.mark.asyncio
async def test_put_persists_exactly_one_source_human_event_with_old_and_new(
        client, store):
    r = await client.put("/api/config/models", json={"utility_model": "claude-opus-5"})
    assert r.status_code == 200
    events = await store.list_events("__config__")
    assert len(events) == 1
    ev = events[0]
    assert ev["source"] == "human"
    assert ev["kind"] == "human_config_models_set"
    assert ev["changes"] == {
        "utility_model": {"old": "claude-haiku-4-5", "new": "claude-opus-5"}
    }


@pytest.mark.asyncio
async def test_put_multi_key_change_emits_one_event_with_both_changes(client, store):
    r = await client.put(
        "/api/config/models",
        json={"utility_model": "claude-opus-5", "supervisor_model": "claude-opus-5"},
    )
    assert r.status_code == 200
    events = await store.list_events("__config__")
    assert len(events) == 1
    assert set(events[0]["changes"]) == {"utility_model", "supervisor_model"}


@pytest.mark.asyncio
async def test_put_preserves_config_yaml_structure(client, tmp_path):
    """A comment-preserving splice, not a yaml dump — the rest of the file
    (every other top-level section) must be byte-identical apart from the
    one changed scalar."""
    path = tmp_path / "config.yaml"
    before_lines = path.read_text().splitlines()
    r = await client.put("/api/config/models", json={"utility_model": "claude-opus-5"})
    assert r.status_code == 200
    after_lines = path.read_text().splitlines()
    changed = [
        (b, a) for b, a in zip(before_lines, after_lines) if b != a
    ]
    assert len(after_lines) == len(before_lines)
    assert len(changed) == 1
    assert "utility_model" in changed[0][1]
    assert "claude-opus-5" in changed[0][1]


@pytest.mark.asyncio
async def test_put_preserves_hand_written_comments_and_unrelated_sections(
        store, tmp_path, monkeypatch):
    """The test above cannot distinguish the real comment-preserving splice
    from a mutant that swaps it for `yaml.safe_load`+`yaml.safe_dump` — its
    seed config has zero comment lines, so both implementations produce a
    file with the same line count and exactly one changed line. Seed a
    config with REAL structure instead (built manually, not via the shared
    `client` fixture, so it exists on disk before `load_config` ever runs —
    the same reason `test_put_requires_local_origin` above builds its own
    client) and assert every comment and unrelated section survives
    byte-for-byte. A round-trip mutant would delete all of them."""
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(nh_config, "CONFIG_PATH", path)
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")
    path.write_text(_COMMENTED_CONFIG)

    app.state.store = store
    app.state.config = nh_config.load_config(path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost",
                           headers={"Origin": "http://127.0.0.1:8420"}) as c:
        r = await c.put("/api/config/models", json={"utility_model": "claude-opus-5"})
    assert r.status_code == 200

    before_lines = _COMMENTED_CONFIG.splitlines()
    after_lines = path.read_text().splitlines()
    assert len(after_lines) == len(before_lines)
    changed = [(b, a) for b, a in zip(before_lines, after_lines) if b != a]
    assert len(changed) == 1
    assert changed[0] == (
        "  utility_model: claude-haiku-4-5",
        "  utility_model: claude-opus-5",
    )
    # Every comment and every unrelated section, byte-for-byte — exactly
    # what a safe_load/safe_dump round-trip would silently destroy.
    assert "# no_human config.yaml — hand-edited, keep these comments." in after_lines
    assert "  # Billing mode: subscription bills CLAUDE_CODE_OAUTH_TOKEN." in after_lines
    assert (
        "  review_model: claude-opus-4-8  # reviewer tier — do not change lightly"
        in after_lines
    )
    assert "server:" in after_lines
    assert "  host: 127.0.0.1" in after_lines
    assert "git:" in after_lines
    assert "  branch_prefix: no-human/" in after_lines


@pytest.mark.asyncio
async def test_api_key_guard_runs_on_written_file(client, tmp_path, monkeypatch):
    """`set_model_ids` reloads the file it just wrote via `load_config`,
    which runs `_reject_api_key_in_config` on every read — including the
    read of the write it just performed, not merely the original on-disk
    state. Prove the guard is wired into THAT reload (and not just the
    harmless pre-write reads) by making it succeed twice — the
    `apply_model_changes` on-disk read and `set_model_ids`'s own
    materialize-check read — then fail from the third call on: the
    post-write reload inside `set_model_ids`'s try/except. The write must
    then be rolled back to its exact original bytes, and the request must
    422 rather than silently succeed with a rejected value on disk."""
    from no_human import config as config_module

    real_guard = config_module._reject_api_key_in_config
    calls = {"n": 0}

    def _guard(data):
        calls["n"] += 1
        real_guard(data)
        if calls["n"] >= 3:
            raise config_module.AuthError(
                "simulated: a credential-looking key was found in the written file"
            )

    monkeypatch.setattr(config_module, "_reject_api_key_in_config", _guard)

    path = tmp_path / "config.yaml"
    before = path.read_text()
    r = await client.put("/api/config/models", json={"utility_model": "claude-opus-5"})
    assert r.status_code == 422
    assert calls["n"] >= 3
    after = path.read_text()
    assert after == before


@pytest.mark.asyncio
async def test_put_persists_correct_kind_for_role_backends_only_event(client, store):
    r = await client.put(
        "/api/config/models",
        json={"role_backends": {"reviewer": {"backend": "claude", "model": "claude-sonnet-5"}}},
    )
    assert r.status_code == 200
    events = await store.list_events("__config__")
    assert len(events) == 1
    ev = events[0]
    assert ev["source"] == "human"
    assert ev["kind"] == "human_config_models_set"
    assert ev["changes"] == {
        "role_backends": {
            "reviewer": {
                "old": None,
                "new": {"backend": "claude", "model": "claude-sonnet-5"},
            }
        }
    }


# --------------------------------------------------------------------------- #
# PUT /api/config/models — role_backends (constraint amendment §6d)          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_models_reviewer_row_carries_default_backend_block(client):
    """The reviewer row (only) carries `effective_role_backend`'s default
    shape verbatim on a fresh install — every other role has no `backend`
    key at all."""
    r = await client.get("/api/models")
    body = r.json()
    rows = {row["role"]: row for row in body["roles"]}
    assert rows["reviewer"]["backend"] == {
        "backend": "claude", "model": "claude-opus-4-8", "is_default": True,
    }
    for role, row in rows.items():
        if role != "reviewer":
            assert "backend" not in row


@pytest.mark.asyncio
async def test_put_role_backends_unknown_role_is_422(client):
    r = await client.put(
        "/api/config/models",
        json={"role_backends": {"planner": {"backend": "claude", "model": "claude-opus-5"}}},
    )
    assert r.status_code == 422
    assert "planner" in r.text


@pytest.mark.asyncio
async def test_put_role_backends_takes_effect_end_to_end_and_discloses_via_get(
        client, tmp_path):
    r = await client.put(
        "/api/config/models",
        json={"role_backends": {"reviewer": {"backend": "claude", "model": "claude-sonnet-5"}}},
    )
    assert r.status_code == 200
    on_disk = nh_config.load_config(tmp_path / "config.yaml")
    assert on_disk.data["llm"]["role_backends"] == {
        "reviewer": {"backend": "claude", "model": "claude-sonnet-5"}
    }
    # restart_required now also folds in the role_backends divergence — the
    # running process is still bound to the OLD (default) reviewer entry.
    r2 = await client.get("/api/models")
    body2 = r2.json()
    assert body2["restart_required"] is True
    rows = {row["role"]: row for row in body2["roles"]}
    # B6: the `backend` block reports the SAVED (on-disk) choice, not the
    # still-running process's default — otherwise the Settings pane would
    # show the just-saved override as "default" and its clear control would
    # stay dead until a restart, even though the write already landed.
    assert rows["reviewer"]["backend"]["is_default"] is False
    assert rows["reviewer"]["backend"]["backend"] == "claude"
    assert rows["reviewer"]["backend"]["model"] == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_put_role_backends_idempotent_no_op_emits_no_event(client, store, tmp_path):
    r1 = await client.put(
        "/api/config/models",
        json={"role_backends": {"reviewer": {"backend": "claude", "model": "claude-sonnet-5"}}},
    )
    assert r1.status_code == 200
    r2 = await client.put(
        "/api/config/models",
        json={"role_backends": {"reviewer": {"backend": "claude", "model": "claude-sonnet-5"}}},
    )
    assert r2.status_code == 200
    events = await store.list_events("__config__")
    assert len(events) == 1  # only the first write emitted an event


@pytest.mark.asyncio
async def test_put_role_backends_null_clears_back_to_default(client, tmp_path):
    await client.put(
        "/api/config/models",
        json={"role_backends": {"reviewer": {"backend": "claude", "model": "claude-sonnet-5"}}},
    )
    r = await client.put("/api/config/models", json={"role_backends": {"reviewer": None}})
    assert r.status_code == 200
    on_disk = nh_config.load_config(tmp_path / "config.yaml")
    assert on_disk.data["llm"].get("role_backends") in (None, {})


@pytest.mark.asyncio
async def test_put_role_backends_alongside_a_plain_model_key_in_one_request(
        client, tmp_path):
    """A single PUT may carry both a plain `llm.*_model` key and a
    `role_backends` entry — the two write paths are independent but must
    both land from one request."""
    r = await client.put(
        "/api/config/models",
        json={
            "utility_model": "claude-opus-5",
            "role_backends": {"reviewer": {"backend": "claude", "model": "claude-sonnet-5"}},
        },
    )
    assert r.status_code == 200
    on_disk = nh_config.load_config(tmp_path / "config.yaml")
    assert on_disk.data["llm"]["utility_model"] == "claude-opus-5"
    assert on_disk.data["llm"]["role_backends"] == {
        "reviewer": {"backend": "claude", "model": "claude-sonnet-5"}
    }


@pytest.mark.asyncio
async def test_put_role_backends_refusal_does_not_write_to_disk(client, tmp_path):
    before = (tmp_path / "config.yaml").read_text()
    r = await client.put(
        "/api/config/models",
        json={"role_backends": {"reviewer": {"backend": "gemini", "model": "x"}}},
    )
    assert r.status_code == 422
    after = (tmp_path / "config.yaml").read_text()
    assert before == after


@pytest.mark.asyncio
async def test_put_claude_backend_with_a_non_catalog_model_is_422(client, tmp_path):
    """B7: `backend: "claude"` is checked against the catalog regardless of
    whether the model string is even Claude-shaped — `gpt-5-codex` is not,
    so the old `_is_claude_id(model)`-gated check would have waved this
    through; the refusal must name the model, and nothing may land on disk."""
    before = (tmp_path / "config.yaml").read_text()
    r = await client.put(
        "/api/config/models",
        json={"role_backends": {"reviewer": {"backend": "claude", "model": "gpt-5-codex"}}},
    )
    assert r.status_code == 422
    assert "gpt-5-codex" in r.text
    after = (tmp_path / "config.yaml").read_text()
    assert after == before


@pytest.mark.asyncio
async def test_a_refused_put_writes_nothing_at_all(client, tmp_path):
    """B5: a request that carries BOTH a valid scalar change and a refused
    role_backends entry must leave every config scalar byte-identical — the
    whole body is validated before either write, so the scalar half never
    lands ahead of the role_backends refusal."""
    before = (tmp_path / "config.yaml").read_text()
    r = await client.put(
        "/api/config/models",
        json={
            "utility_model": "claude-opus-5",
            "role_backends": {"reviewer": {"backend": "gemini", "model": "x"}},
        },
    )
    assert r.status_code == 422
    after = (tmp_path / "config.yaml").read_text()
    assert after == before
    on_disk = nh_config.load_config(tmp_path / "config.yaml")
    assert on_disk.data["llm"]["utility_model"] == "claude-haiku-4-5"  # unchanged default


@pytest.mark.asyncio
async def test_a_refused_put_with_an_unknown_role_writes_no_scalar(client, tmp_path):
    """Same B5 guarantee, exercised via the unknown-role refusal path (a
    different `RoleBackendError` raised earlier in
    `validate_role_backend_entries`) rather than the unsupported-backend one
    above — both must refuse before touching disk."""
    before = (tmp_path / "config.yaml").read_text()
    r = await client.put(
        "/api/config/models",
        json={
            "utility_model": "claude-opus-5",
            "role_backends": {"planner": {"backend": "claude", "model": "claude-opus-5"}},
        },
    )
    assert r.status_code == 422
    after = (tmp_path / "config.yaml").read_text()
    assert after == before


@pytest.mark.asyncio
async def test_put_role_backends_null_clears_in_session_without_a_restart(client, tmp_path):
    """B6, the clear direction: after saving a non-default reviewer choice
    and then clearing it back to null, the VERY NEXT GET (same process, no
    restart) must already report `is_default: True` — the payload reads the
    on-disk file, which the clear PUT already rewrote, not the still-running
    process's stale in-memory config.

    A PUT never reloads `app.state.config` (an unrelated, deliberate rule —
    see `test_put_does_not_reload_running_app_state_config`), so the running
    process's in-memory copy is realistically stale relative to whatever the
    file on disk says. Force that staleness explicitly here: after the clear
    PUT rewrites the file back to default, poke the in-memory config back to
    the non-default entry the PUT just erased. A payload built from
    `running_cfg_data` (the B6 bug) would echo this stale non-default entry
    back as `is_default: False`; only a payload that reads the file the
    clear PUT actually wrote reports `True` regardless of what is sitting in
    memory. Without this poke, the running config's untouched default state
    happens to already match the correct post-clear answer, so the assertion
    below would pass even against the bug — this line is what makes the test
    discriminate it."""
    await client.put(
        "/api/config/models",
        json={"role_backends": {"reviewer": {"backend": "claude", "model": "claude-sonnet-5"}}},
    )
    r = await client.put("/api/config/models", json={"role_backends": {"reviewer": None}})
    assert r.status_code == 200

    app.state.config.data.setdefault("llm", {})["role_backends"] = {
        "reviewer": {"backend": "claude", "model": "claude-sonnet-5"}
    }

    r2 = await client.get("/api/models")
    body2 = r2.json()
    rows = {row["role"]: row for row in body2["roles"]}
    assert rows["reviewer"]["backend"] == {
        "backend": "claude", "model": "claude-opus-4-8", "is_default": True,
    }


@pytest.mark.asyncio
async def test_a_failed_role_backend_write_rolls_back_the_scalar_write(
        client, tmp_path, monkeypatch):
    """B5's defensive half: even though `validate_role_backend_entries` runs
    the ENTIRE body's validation before either write, `apply_model_changes`
    still wraps both writes in a byte-level rollback for the rarer case of a
    failure inside the write step itself (e.g. `set_role_backend`'s own
    splice/verify/restore raising after the scalar write already landed).
    Simulate that by making the on-disk writer itself blow up, and confirm
    the scalar half that already landed is rolled back to the pre-request
    bytes rather than left half-applied."""
    def _boom(*a, **k):
        raise RuntimeError("simulated: set_role_backend's own write failed")

    monkeypatch.setattr(nh_config, "set_role_backend", _boom)
    before = (tmp_path / "config.yaml").read_text()
    with pytest.raises(RuntimeError):
        await client.put(
            "/api/config/models",
            json={
                "utility_model": "claude-opus-5",
                "role_backends": {"reviewer": {"backend": "claude", "model": "claude-sonnet-5"}},
            },
        )
    after = (tmp_path / "config.yaml").read_text()
    assert after == before


@pytest.mark.asyncio
async def test_put_multi_key_swap_validates_against_the_resolved_after_state(
        client, tmp_path):
    """`apply_model_changes` validates each changed key against
    `resolved_after` — the on-disk five overlaid with EVERY submitted
    change — not against stale current-on-disk values. Prove this with the
    scenario the module's own comment names: coder and reviewer swapping
    models in one request. Either half alone 422s as a review-gate collision
    (see the two collision tests above); submitted together, the write must
    succeed, because by the time each key is checked the OTHER key has
    already moved out of the way."""
    defaults = mc.defaults()
    r = await client.put(
        "/api/config/models",
        json={
            "primary_model": defaults["review_model"],
            "review_model": defaults["primary_model"],
        },
    )
    assert r.status_code == 200
    on_disk = nh_config.load_config(tmp_path / "config.yaml")
    assert on_disk.data["llm"]["primary_model"] == defaults["review_model"]
    assert on_disk.data["llm"]["review_model"] == defaults["primary_model"]
