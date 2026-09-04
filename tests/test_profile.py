"""ProjectProfile: YAML round-trip, readiness gate, and SQLite mirror."""


from no_human.profile import PROFILE_RELPATH, ProjectProfile, apply_default_task_config


def _sample(repo_path):
    return ProjectProfile(
        repo_path=str(repo_path),
        ecosystem="python-pytest",
        install_cmd="uv sync",
        test_cmd="uv run pytest -q",
        lint_cmd="uv run ruff check",
        ci={"backend": "gitlab", "project": "ci_gate/subgroup/metrics-core"},
        human_gated_steps=["build image on Jenkins"],
        derived_from=["pyproject.toml", ".gitlab-ci.yml"],
        proven={"test_cmd": True, "install_cmd": True},
        confirmed=True,
        notes="derived by nh onboard",
    )


def test_yaml_round_trip(tmp_path):
    prof = _sample(tmp_path)
    path = prof.save()
    assert path == tmp_path / PROFILE_RELPATH
    assert path.exists()
    # repo_path is implied by location, not written into the file.
    assert "repo_path" not in path.read_text()

    loaded = ProjectProfile.load(tmp_path)
    assert loaded is not None
    assert loaded.repo_path == str(tmp_path)
    assert loaded.test_cmd == "uv run pytest -q"
    assert loaded.ci == {"backend": "gitlab", "project": "ci_gate/subgroup/metrics-core"}
    assert loaded.human_gated_steps == ["build image on Jenkins"]
    assert loaded.proven == {"test_cmd": True, "install_cmd": True}
    assert loaded.confirmed is True


def test_load_missing_returns_none(tmp_path):
    assert ProjectProfile.load(tmp_path) is None


def test_from_dict_ignores_unknown_keys():
    prof = ProjectProfile.from_dict(
        {"repo_path": "/r", "test_cmd": "pytest", "bogus": 1, "legacy_field": "x"}
    )
    assert prof.repo_path == "/r"
    assert prof.test_cmd == "pytest"


def test_is_usable_gate():
    # confirmed + test_cmd present + test_cmd proven -> usable
    assert _sample("/r").is_usable is True
    # not confirmed -> not usable even if proven
    p = _sample("/r")
    p.confirmed = False
    assert p.is_usable is False
    # confirmed but test never proven -> not usable (trust requires proof)
    p2 = _sample("/r")
    p2.proven = {}
    assert p2.is_usable is False
    # confirmed, no test command -> not usable
    p3 = _sample("/r")
    p3.test_cmd = ""
    assert p3.is_usable is False


def test_usable_under_policy():
    # confirmed profile is usable regardless of the flag
    assert _sample("/r").usable_under_policy(auto_confirm_proven=False) is True
    # proven-but-unconfirmed: usable ONLY when the flag is on (the ca23ce68 fix)
    p = _sample("/r")
    p.confirmed = False
    assert p.usable_under_policy(auto_confirm_proven=False) is False
    assert p.usable_under_policy(auto_confirm_proven=True) is True
    # the flag never bypasses PROOF — an unproven profile stays unusable
    p2 = _sample("/r")
    p2.confirmed = False
    p2.proven = {}
    assert p2.usable_under_policy(auto_confirm_proven=True) is False


async def test_store_upsert_and_get(store, tmp_path):
    prof = _sample(tmp_path)
    await store.upsert_profile(prof)
    got = await store.get_profile(str(tmp_path))
    assert got is not None
    assert got.ecosystem == "python-pytest"
    assert got.ci["project"] == "ci_gate/subgroup/metrics-core"
    assert got.confirmed is True
    assert got.proven["test_cmd"] is True


async def test_store_get_missing_returns_none(store):
    assert await store.get_profile("/nope") is None


async def test_store_upsert_updates_existing(store, tmp_path):
    prof = _sample(tmp_path)
    prof.confirmed = False
    await store.upsert_profile(prof)
    prof.confirmed = True
    prof.test_cmd = "uv run pytest -q tests/"
    await store.upsert_profile(prof)              # second upsert = update, not dup
    got = await store.get_profile(str(tmp_path))
    assert got.confirmed is True
    assert got.test_cmd == "uv run pytest -q tests/"
    # exactly one row for this repo
    cur = await store.db.execute(
        "SELECT COUNT(*) c FROM project_profiles WHERE repo_path = ?", (str(tmp_path),)
    )
    assert (await cur.fetchone())["c"] == 1


# --------------------------------------------------------------------------- #
# SCRUM-26: repo-level default token budgets                                  #
# --------------------------------------------------------------------------- #

def test_default_token_fields_default_to_unset():
    prof = ProjectProfile(repo_path="/r")
    assert prof.default_attempt_tokens == 0
    assert prof.default_lifetime_tokens == 0


def test_yaml_round_trip_carries_token_defaults(tmp_path):
    prof = _sample(tmp_path)
    prof.default_attempt_tokens = 6_000_000
    prof.default_lifetime_tokens = 16_000_000
    prof.save()

    loaded = ProjectProfile.load(tmp_path)
    assert loaded.default_attempt_tokens == 6_000_000
    assert loaded.default_lifetime_tokens == 16_000_000


async def test_store_round_trip_carries_token_defaults(store, tmp_path):
    prof = _sample(tmp_path)
    prof.default_attempt_tokens = 6_000_000
    prof.default_lifetime_tokens = 16_000_000
    await store.upsert_profile(prof)

    got = await store.get_profile(str(tmp_path))
    assert got.default_attempt_tokens == 6_000_000
    assert got.default_lifetime_tokens == 16_000_000


def test_apply_default_task_config_no_profile_is_noop():
    assert apply_default_task_config(None, {"backend": "claude"}) == {"backend": "claude"}


def test_apply_default_task_config_no_defaults_set_is_noop():
    prof = ProjectProfile(repo_path="/r")  # defaults unset (0)
    assert apply_default_task_config(prof, {}) == {}


def test_apply_default_task_config_copies_when_absent():
    prof = ProjectProfile(repo_path="/r", default_attempt_tokens=6_000_000,
                           default_lifetime_tokens=16_000_000)
    merged = apply_default_task_config(prof, {})
    assert merged == {"attempt_tokens": 6_000_000, "lifetime_tokens": 16_000_000}


def test_apply_default_task_config_explicit_override_wins():
    prof = ProjectProfile(repo_path="/r", default_attempt_tokens=6_000_000,
                           default_lifetime_tokens=16_000_000)
    merged = apply_default_task_config(prof, {"attempt_tokens": 1_234})
    # explicit attempt_tokens kept; lifetime_tokens still copied from the profile
    assert merged == {"attempt_tokens": 1_234, "lifetime_tokens": 16_000_000}
    # the original dict passed in is never mutated in place
    original = {"attempt_tokens": 1_234}
    apply_default_task_config(prof, original)
    assert original == {"attempt_tokens": 1_234}


# --------------------------------------------------------------------------- #
# R1: unit provenance on the repo-profile defaults.
#
# The August funnel died because a profile value written before the 2026-07-31
# cutover was indistinguishable from one written after it. `default_budget_unit`
# is that distinction, and it is stamped by the only write path there is.
# --------------------------------------------------------------------------- #

def test_default_budget_unit_is_unset_on_an_old_profile():
    """Absence is what every profile written before this change carries, and it
    has to keep meaning 'raw, convert it' — fail-closed, same as task.config."""
    assert ProjectProfile(repo_path="/r").default_budget_unit == ""


def test_yaml_and_store_round_trip_carry_the_unit(tmp_path):
    from no_human.core.pricing import WEIGHTED_UNIT

    prof = _sample(tmp_path)
    prof.default_lifetime_tokens = 4_000_000
    prof.default_budget_unit = WEIGHTED_UNIT
    prof.save()
    assert ProjectProfile.load(tmp_path).default_budget_unit == WEIGHTED_UNIT


def test_a_weighted_profile_stamps_the_task_config_it_writes():
    from no_human.core.pricing import BUDGET_UNIT_KEY, WEIGHTED_UNIT

    prof = ProjectProfile(repo_path="/r", default_attempt_tokens=2_000_000,
                          default_lifetime_tokens=4_000_000,
                          default_budget_unit=WEIGHTED_UNIT)
    assert apply_default_task_config(prof, {}) == {
        "attempt_tokens": 2_000_000,
        "lifetime_tokens": 4_000_000,
        BUDGET_UNIT_KEY: WEIGHTED_UNIT,
    }


def test_an_unstamped_profile_still_writes_no_marker():
    """Backward compatibility is the whole point: an existing profile's value
    must keep being read as raw, or this change silently 5x's every install."""
    from no_human.core.pricing import BUDGET_UNIT_KEY

    prof = ProjectProfile(repo_path="/r", default_lifetime_tokens=16_000_000)
    assert BUDGET_UNIT_KEY not in apply_default_task_config(prof, {})


def test_mixed_units_copy_nothing_rather_than_copying_unmarked():
    """The marker describes the WHOLE dict, so a stamped profile cannot
    describe a cap the caller brought — and withholding the marker while
    copying the profile's values anyway is WORSE than doing nothing: a
    weighted 4,000,000 then reads as pre-cutover raw and converts to 794,000,
    a 5x cut of a number the operator typed correctly.

    So: copy nothing. The caller's own cap stands, and the key it did not set
    falls back to the ungranted default, which is never worse than a value
    read in the wrong unit."""
    from no_human.core.pricing import BUDGET_UNIT_KEY, WEIGHTED_UNIT

    prof = ProjectProfile(repo_path="/r", default_attempt_tokens=2_000_000,
                          default_lifetime_tokens=4_000_000,
                          default_budget_unit=WEIGHTED_UNIT)
    merged = apply_default_task_config(prof, {"attempt_tokens": 6_000_000})
    assert merged == {"attempt_tokens": 6_000_000}, (
        "the profile's weighted lifetime default was copied where it would "
        f"have been read as raw; merged={merged}")
    assert BUDGET_UNIT_KEY not in merged, "mixed units — no dict-wide claim"
    assert "lifetime_tokens" not in merged, "no unmarked weighted value copied"

    # An UNSTAMPED profile is the pre-existing contract and is untouched: its
    # values are raw, the caller's cap is raw, one unit, so copying is safe.
    old = ProjectProfile(repo_path="/r", default_attempt_tokens=6_000_000,
                         default_lifetime_tokens=16_000_000)
    assert apply_default_task_config(old, {"attempt_tokens": 1_234}) == {
        "attempt_tokens": 1_234, "lifetime_tokens": 16_000_000}


def test_mixed_units_are_refused_in_the_OTHER_direction_too():
    """D6. The mirror of the case above, and the one the first cure missed:
    an UNSTAMPED (raw) profile writing into a caller dict that already
    DECLARES weighted. The raw value lands under the weighted marker and
    `_stored_token_cap` takes it at face value — 20,200,000 enforced where
    4,009,700 is the honest reading, 5.04x, fail-open and unwarned.

    Latent while no creation surface stamps at birth; guarded anyway, because
    the comment on this function claims both directions and because the
    mirror case was cured at exactly this latency bar."""
    from no_human.core.bounds import Bounds
    from no_human.core.orchestrator import Orchestrator
    from no_human.core.pricing import BUDGET_UNIT_KEY, WEIGHTED_UNIT

    raw_profile = ProjectProfile(repo_path="/r",
                                 default_lifetime_tokens=20_200_000)
    caller = {"attempt_tokens": 2_000_000, BUDGET_UNIT_KEY: WEIGHTED_UNIT}

    merged = apply_default_task_config(raw_profile, caller)
    assert "lifetime_tokens" not in merged, (
        "a RAW profile default landed under the caller's WEIGHTED marker; "
        f"merged={merged}")
    assert merged == caller

    # End to end: what the gate would enforce is the ungranted default, not
    # the raw number read at face value.
    assert Orchestrator._stored_token_cap(
        merged, "lifetime_tokens", Bounds().lifetime_tokens) == 4_000_000

    # The marker ALONE is enough to poison a copy — there need not already be
    # a token cap in the dict. (A guard written as
    # `... and TOKEN_CAP_KEYS & set(merged)` passes the case above and still
    # fails this one.)
    marker_only = apply_default_task_config(
        raw_profile, {BUDGET_UNIT_KEY: WEIGHTED_UNIT})
    assert marker_only == {BUDGET_UNIT_KEY: WEIGHTED_UNIT}, (
        f"copied a raw default under a bare weighted marker; {marker_only}")

    # Control, so this does not over-fire: same unit on both sides copies.
    both_weighted = ProjectProfile(repo_path="/r",
                                   default_lifetime_tokens=8_000_000,
                                   default_budget_unit=WEIGHTED_UNIT)
    assert apply_default_task_config(
        both_weighted, {BUDGET_UNIT_KEY: WEIGHTED_UNIT}) == {
            "lifetime_tokens": 8_000_000, BUDGET_UNIT_KEY: WEIGHTED_UNIT}


# --------------------------------------------------------------------------- #
# no-human-67: ui_evidence                                                    #
# --------------------------------------------------------------------------- #

def test_ui_evidence_defaults_are_off_and_documented():
    prof = ProjectProfile(repo_path="/r")
    assert prof.ui_evidence["enabled"] is False
    assert prof.ui_evidence["start_cmd"] == ""
    assert prof.ui_evidence["base_url"] == ""
    assert prof.ui_evidence["ready_path"] == "/"
    assert prof.ui_evidence["ready_timeout_s"] == 60
    assert prof.ui_evidence["ui_paths"] == [
        "web/**", "src/**/*.jsx", "src/**/*.tsx", "**/*.html", "**/*.css",
    ]


def test_ui_evidence_defaults_are_independent_per_instance():
    """The dict default is a `default_factory` — two instances must not
    share (and mutate) the same underlying dict."""
    a = ProjectProfile(repo_path="/a")
    b = ProjectProfile(repo_path="/b")
    a.ui_evidence["enabled"] = True
    assert b.ui_evidence["enabled"] is False


def test_ui_evidence_round_trips_via_to_dict_from_dict():
    prof = ProjectProfile(
        repo_path="/r",
        ui_evidence={
            "enabled": True,
            "start_cmd": "npm run dev",
            "base_url": "http://127.0.0.1:5173",
            "ready_path": "/healthz",
            "ready_timeout_s": 30,
            "ui_paths": ["web/**"],
            "publish": False,
        },
    )
    restored = ProjectProfile.from_dict(prof.to_dict())
    assert restored.ui_evidence == prof.ui_evidence


def test_ui_evidence_round_trips_via_yaml(tmp_path):
    prof = _sample(tmp_path)
    prof.ui_evidence = {
        "enabled": True,
        "start_cmd": "uv run uvicorn app:app",
        "base_url": "http://localhost:8000",
        "ready_path": "/",
        "ready_timeout_s": 45,
        "ui_paths": ["web/**", "**/*.html"],
    }
    prof.save()
    loaded = ProjectProfile.load(tmp_path)
    assert loaded is not None
    assert loaded.ui_evidence == prof.ui_evidence


async def test_ui_evidence_round_trips_via_store(store, tmp_path):
    prof = _sample(tmp_path)
    prof.ui_evidence["enabled"] = True
    prof.ui_evidence["start_cmd"] = "python -m http.server"
    await store.upsert_profile(prof)
    got = await store.get_profile(str(tmp_path))
    assert got is not None
    assert got.ui_evidence["enabled"] is True
    assert got.ui_evidence["start_cmd"] == "python -m http.server"


async def test_ui_evidence_is_in_data_json_not_a_mirror_column(store, tmp_path):
    """no-human-67's explicit constraint: `ui_evidence` must live in the
    `data` JSON blob only, never a dedicated `project_profiles` column —
    the mirror columns (repo_path/ecosystem/install_cmd/test_cmd/lint_cmd/
    confirmed) are a fixed, deliberately small set (`get_profile.data-json-
    is-the-record` lesson: a partial write to a mirror column changes
    nothing the reader sees)."""
    prof = _sample(tmp_path)
    prof.ui_evidence["enabled"] = True
    await store.upsert_profile(prof)
    cols = await store.db.execute("PRAGMA table_info(project_profiles)")
    names = {r["name"] for r in await cols.fetchall()}
    assert "ui_evidence" not in names
    row = await store._fetchone(
        "SELECT data FROM project_profiles WHERE repo_path = ?", (str(tmp_path),)
    )
    import json as _json
    assert _json.loads(row["data"])["ui_evidence"]["enabled"] is True


def test_from_dict_defaults_ui_evidence_for_an_old_profile_with_no_key():
    """A profile persisted before this field existed has no `ui_evidence`
    key in its stored dict — `from_dict` must fall back to the disabled
    default rather than raise or leave the field unset."""
    prof = ProjectProfile.from_dict({"repo_path": "/r", "test_cmd": "pytest"})
    assert prof.ui_evidence["enabled"] is False
