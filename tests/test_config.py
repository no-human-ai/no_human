"""Auth scrub + subscription-mode assertion — the load-bearing safety boundary."""

import os
import re

import pytest

from no_human import config
from no_human.config import (
    DEFAULT_CONFIG,
    AuthError,
    _atomic_write_text,
    assert_local_backend_mode,
    assert_subscription_mode,
    load_config,
    load_env_token,
    scrub_metered_auth,
)


def test_scrub_removes_all_metered_vars(monkeypatch):
    for var in config.METERED_AUTH_VARS:
        monkeypatch.setenv(var, "x")
    report = scrub_metered_auth()
    assert set(report.removed) == set(config.METERED_AUTH_VARS)
    assert report.api_key_present is True
    for var in config.METERED_AUTH_VARS:
        assert var not in os.environ


def test_scrub_ignores_absent_vars(monkeypatch):
    for var in config.METERED_AUTH_VARS:
        monkeypatch.delenv(var, raising=False)
    report = scrub_metered_auth()
    assert report.removed == []
    assert report.api_key_present is False


def test_assert_subscription_refuses_when_api_key_present(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    with pytest.raises(AuthError, match="ANTHROPIC_API_KEY"):
        assert_subscription_mode(env_path=tmp_path / "nope.env")
    # scrubbed even though it raised — cannot fall through to metered billing
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_assert_subscription_requires_token(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(AuthError, match="No subscription token"):
        assert_subscription_mode(env_path=tmp_path / "nope.env")


def test_assert_subscription_succeeds_with_token(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    report = assert_subscription_mode(env_path=tmp_path / "nope.env")
    assert report.api_key_present is False


def test_load_env_token_from_file(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text('# comment\nCLAUDE_CODE_OAUTH_TOKEN="file-token"\n')
    assert load_env_token(env) == "file-token"
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "file-token"


def test_load_config_generates_default(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg = load_config(cfg_path)
    assert cfg_path.exists()
    assert cfg.primary_model == "claude-sonnet-5"
    assert cfg.review_model == "claude-opus-4-8"
    assert cfg["approval"]["auto_merge_on_approval"] is False
    # the metered key must never appear anywhere in the generated config
    assert "ANTHROPIC_API_KEY" not in cfg_path.read_text()


def test_load_config_tolerates_deprecated_tracker_block(tmp_path):
    """An old config still carrying a ``tracker:`` section (the removed TRACKER
    integration) must load with a single deprecation warning and be ignored —
    never crash, and never leak the dead section into the resolved config."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "tracker:\n  enabled: true\n  boards: [SPRINT1]\n"
        "concurrency:\n  workers: 2\n"
    )
    with pytest.warns(DeprecationWarning, match="tracker"):
        cfg = load_config(cfg_path)
    # ignored — not carried into the resolved config
    assert "tracker" not in cfg.data
    # the rest of the user's config still applies
    assert cfg["concurrency"]["workers"] == 2


def test_load_config_has_integrations_defaults(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg["integrations"]["jira"]["enabled"] is False
    assert "project_key" in cfg["integrations"]["jira"]
    # CircleCI is a view over `ci.*`, like github_actions/gitlab/jenkins — it
    # has no `integrations.circleci` block. It used to, holding `enabled` +
    # `org_slug` + `project`, and NOTHING read any of the three: the block
    # rendered an on/off toggle and an onboarding form that governed nothing
    # while the panel claimed CircleCI was the active CI backend.
    assert "circleci" not in cfg["integrations"]
    # ...and the block it moved to exists, off by default, with the key the
    # CircleCI backend is built from.
    assert cfg["ci"]["enabled"] is False
    assert cfg["ci"]["project"] == ""


def test_approve_identity_ships_empty(tmp_path):
    # No person's name/email may ship as the default merge identity — it is
    # resolved from the repo's own git config at merge time instead (see
    # vcs/approve_merge.py::_resolve_approve_identity).
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.data["git"]["approve_identity"] == {"name": "", "email": ""}


def test_load_config_null_integrations_section_does_not_crash(tmp_path):
    # Deep-merge shadowing trap: `integrations:` set to null in the user file
    # replaces the whole default dict — loading must not crash, and the
    # registry must tolerate the null section.
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("integrations:\nllm:\n  auth_mode: subscription\n")
    cfg = load_config(cfg_path)
    assert cfg.data.get("integrations") is None  # shadowed to null, as documented
    from no_human.integrations import list_integrations
    assert len(list_integrations(cfg.data)) == 9  # registry tolerates it


def test_load_config_rejects_api_key(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm:\n  ANTHROPIC_API_KEY: sk-ant-leak\n")
    with pytest.raises(AuthError, match="ANTHROPIC_API_KEY"):
        load_config(cfg_path)


def test_load_config_rejects_decomposition_enabled(tmp_path):
    """The LeadAgent child-task path was removed 2026-08-12 (operator
    decision A1); re-enabling its gate must fail loudly at startup, not
    silently do nothing."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("decomposition:\n  enabled: true\n")
    with pytest.raises(config.ConfigError, match="decomposition was removed"):
        load_config(cfg_path)


def test_load_config_allows_decomposition_default(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("decomposition:\n  enabled: false\n")
    cfg = load_config(cfg_path)
    assert cfg.data["decomposition"]["enabled"] is False


def test_load_config_default_has_no_decomposition_enabled(tmp_path):
    """Nobody touches the key — the load-bearing default-off path."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm:\n  auth_mode: subscription\n")
    cfg = load_config(cfg_path)
    assert cfg.data["decomposition"]["enabled"] is False


def test_atomic_write_text_uses_os_replace(tmp_path, monkeypatch):
    """Guard: _atomic_write_text must go through os.replace, not direct write."""
    target = tmp_path / "config.yaml"
    replaced = []
    real_replace = os.replace
    def spy_replace(src, dst):
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", spy_replace)
    _atomic_write_text(target, "key: value\n")
    assert target.read_text() == "key: value\n"
    assert len(replaced) == 1
    assert replaced[0][1] == str(target)
    assert replaced[0][0].endswith(".yaml.tmp")


def test_atomic_write_text_no_partial_read(tmp_path):
    """A concurrent reader never sees a half-written file."""
    target = tmp_path / "config.yaml"
    target.write_text("original")
    _atomic_write_text(target, "replaced content")
    assert target.read_text() == "replaced content"
    assert not target.with_suffix(".yaml.tmp").exists()


def test_no_fake_auto_pr_switch():
    """`git.auto_pr` was a config key nothing read — it looked like a safety
    switch and was not one. A fake off-switch is worse than none."""
    from no_human.config import DEFAULT_CONFIG
    assert "auto_pr" not in DEFAULT_CONFIG["git"]


def test_supervisor_has_its_own_model_key():
    """The supervisor rode on review_model, so it silently ran at the reviewer's
    tier — an inherited choice nobody made. The supervisor is a sparse,
    single-turn course-corrector, so it has its own tier: Sonnet 5."""
    from no_human.config import DEFAULT_CONFIG
    llm = DEFAULT_CONFIG["llm"]
    assert llm["supervisor_model"] == "claude-sonnet-5"
    assert llm["primary_model"] == "claude-sonnet-5"
    assert llm["review_model"] == "claude-opus-4-8"
    assert llm["planner_model"] == "claude-opus-5"


def test_utility_tier_exists_and_does_not_touch_the_four_gates():
    """A fourth, advisory tier for summarize/classify/distill jobs, kept off
    the implement/plan/review/supervise path. It must never become a gate."""
    from no_human.config import DEFAULT_CONFIG
    llm = DEFAULT_CONFIG["llm"]
    assert llm["utility_model"] == "claude-haiku-4-5"
    for gate in ("primary_model", "planner_model", "review_model",
                 "supervisor_model"):
        assert llm[gate] != llm["utility_model"], (
            f"{gate} must not run on the utility tier"
        )


def test_utility_model_property_survives_a_config_that_predates_the_key():
    """~/.no_human/config.yaml is written once and deep-merged forever. A config
    frozen before the utility tier existed must still resolve the new default,
    not crash and not silently fall back to a gate model."""
    from pathlib import Path
    from no_human.config import Config, DEFAULT_CONFIG
    stale = Config(data={"llm": {"auth_mode": "subscription"}},
                   path=Path("/nonexistent/config.yaml"))
    assert stale.utility_model == DEFAULT_CONFIG["llm"]["utility_model"]


def test_ci_gate_block_ships_disabled_and_topology_free():
    """The CI_GATE gate ships disabled AND carries no deployment topology.

    A packaged build serves the effective config over ``/api/config``, so any
    project id, cluster name or job path left in these defaults is readable by
    whoever installs the app. The operator supplies them in
    ``~/.no_human/config.yaml`` on their own machine instead.
    """
    from no_human.config import DEFAULT_CONFIG
    s = DEFAULT_CONFIG["ci_gate"]
    assert s["enabled"] is False
    # Empty, not merely different: gate.py's eligibility check requires
    # enabled + repos + project_id, so these make the gate inert rather than
    # misconfigured.
    assert s["project_id"] is None
    assert s["hostname"] == ""
    assert s["repos"] == []
    assert s["variables"] == {}
    assert s["kubeconfig"] == ""
    assert s["enrich_job_url"] == ""
    assert s["jenkins_controller"] == ""
    assert s["registry_prefix"] == ""
    assert s["ref"] == "main"
    # Generic, and still carries the required interpolation point.
    assert s["namespace_template"] == "ci-gate-pr{pr_number}"
    assert "{pr_number}" in s["namespace_template"]
    # Bounded send-back loop cap lives with the other blocker bounds.
    assert DEFAULT_CONFIG["blockers"]["max_ci_gate_fix_rounds"] == 3


def test_per_edit_lint_is_on_by_default():
    """W1.3: SWE-agent's biggest ACI win — a non-parsing edit costs one hook
    round, not a failed attempt. Safe as default: no-op without a confirmed
    lint command, fail-open on linter timeout."""
    from no_human.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["hooks"]["per_edit_lint"] is True


def test_stuck_active_watchdog_default():
    """The stuck-active watchdog threshold ships in DEFAULT_CONFIG (40 min,
    above the 30-min test timeout) — discoverable and tunable."""
    from no_human.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["blockers"]["stuck_active_minutes"] == 40


def test_a_loaded_config_never_aliases_the_global_defaults(tmp_path):
    """A config.yaml that omits a section used to hand back DEFAULT_CONFIG's own
    nested dict: `_deep_merge` copied only the TOP level, so
    `merged["server"] is DEFAULT_CONFIG["server"]`. Any caller that then wrote
    into its own config — the nightly eval sets `server.port` for its isolated
    instance — silently re-pointed the DEFAULT for the whole process. Measured
    2026-08-10: DEFAULT_CONFIG["server"]["port"] 8420 -> 8431, which surfaced as
    a README-claims failure in an unrelated test."""
    from no_human.config import DEFAULT_CONFIG, load_config

    home = tmp_path / "home"
    home.mkdir()
    # No `server:` block — the shape that aliased.
    (home / "config.yaml").write_text("llm:\n  review_model: claude-opus-5\n")

    before = DEFAULT_CONFIG["server"]["port"]
    cfg = load_config(home / "config.yaml")

    assert cfg.data["server"] is not DEFAULT_CONFIG["server"], (
        "the loaded config aliases DEFAULT_CONFIG's nested dict")
    cfg.data["server"]["port"] = before + 11
    assert DEFAULT_CONFIG["server"]["port"] == before, (
        "writing to a loaded config mutated the global defaults")
    # ...and the user's own override still wins over the default.
    assert cfg.data["llm"]["review_model"] == "claude-opus-5"


# --------------------------------------------------------------------------- #
# Reviewer session windows (2026-08-11)                                        #
# --------------------------------------------------------------------------- #

def test_the_review_window_defaults_have_exactly_one_source_of_truth():
    """DEFAULT_CONFIG and the reviewer's own fallback constants must be the same
    numbers. Two spellings of a default is how a knob silently stops matching
    the code it configures; this is the guard that catches the drift. Break
    either side and this reddens."""
    from no_human.config import DEFAULT_CONFIG
    from no_human.review import reviewer as rv

    assert DEFAULT_CONFIG["llm"]["review_timeout_seconds"] == rv._REVIEW_TIMEOUT
    assert (DEFAULT_CONFIG["llm"]["code_review_timeout_seconds"]
            == rv._CODE_REVIEW_TIMEOUT)
    # The measured numbers themselves (see the constant's comment): above the
    # 1357s worst review round observed 2026-08-11, not the old 600s wall that
    # sat below the ~1078s mean.
    assert rv._REVIEW_TIMEOUT == 1500
    assert rv._CODE_REVIEW_TIMEOUT == 1800


def test_an_absurdly_small_review_window_is_clamped_to_the_floor():
    """A window under the floor is not a tuning choice, it is a typo: every
    review would time out twice and every task would escalate unreviewed. Clamp
    (loudly) rather than raise — a bad number in one knob must not make the
    whole install unloadable."""
    from no_human.config import (
        REVIEW_TIMEOUT_FLOOR_S,
        code_review_timeout_seconds,
        review_timeout_seconds,
    )

    assert review_timeout_seconds({"llm": {"review_timeout_seconds": 5}}) == (
        REVIEW_TIMEOUT_FLOOR_S)
    assert review_timeout_seconds({"llm": {"review_timeout_seconds": 0}}) == (
        REVIEW_TIMEOUT_FLOOR_S)
    assert review_timeout_seconds({"llm": {"review_timeout_seconds": -30}}) == (
        REVIEW_TIMEOUT_FLOOR_S)
    assert code_review_timeout_seconds(
        {"llm": {"code_review_timeout_seconds": 1}}) == REVIEW_TIMEOUT_FLOOR_S
    # At the floor exactly: honoured, not clamped away.
    assert review_timeout_seconds(
        {"llm": {"review_timeout_seconds": REVIEW_TIMEOUT_FLOOR_S}}
    ) == REVIEW_TIMEOUT_FLOOR_S


def test_a_nonnumeric_review_window_falls_back_to_the_default():
    """YAML hands back whatever was typed. A string, a null or a bool is not a
    number of seconds — take the measured default rather than crash or, worse,
    hand `asyncio.wait_for` something it will reject at review time."""
    from no_human.config import DEFAULT_CONFIG, review_timeout_seconds

    default = DEFAULT_CONFIG["llm"]["review_timeout_seconds"]
    for bad in ("25 minutes", None, True, [1500], {"s": 1500}):
        assert review_timeout_seconds({"llm": {"review_timeout_seconds": bad}}) == (
            default), bad
    # Absent key, absent section, empty dict — all the same default.
    assert review_timeout_seconds({"llm": {}}) == default
    assert review_timeout_seconds({}) == default


def test_config_exposes_the_review_windows_as_properties():
    """The Config object is what every production call site holds, so the knob
    has to be readable from it the way `review_model` is."""
    from no_human.config import Config, DEFAULT_CONFIG

    cfg = Config(data={"llm": {"review_timeout_seconds": 1200,
                               "code_review_timeout_seconds": 2400}},
                 path=None)
    assert cfg.review_timeout_seconds == 1200
    assert cfg.code_review_timeout_seconds == 2400

    empty = Config(data={"llm": {}}, path=None)
    assert empty.review_timeout_seconds == (
        DEFAULT_CONFIG["llm"]["review_timeout_seconds"])
    assert empty.code_review_timeout_seconds == (
        DEFAULT_CONFIG["llm"]["code_review_timeout_seconds"])


def test_a_nonfinite_review_window_is_rejected_rather_than_crashing_the_install(
        tmp_path, caplog):
    """`.inf`, `.nan` and `-.inf` are LEGAL YAML floats.

    Found in adversarial review of the config-driven window. They are real
    `float`s, so `isinstance(raw, (int, float))` waves them through; `nan < 60`
    is False so the floor branch does not catch them either; and `int(inf)` /
    `int(nan)` then raise OverflowError / ValueError — out of a property that
    every orchestrator construction reads (cli/commands.py `_build_orchestrator`)
    and out of `load_config` itself. One typo in one knob would make the install
    unloadable, which is the exact invariant `_timeout_knob` states twice and
    the reason it clamps instead of raising. Driven through `yaml.safe_load`
    and `load_config`, not through hand-built floats: the point is that YAML
    produces these from ordinary-looking text.
    """
    import logging
    import math

    import yaml as _yaml

    from no_human.config import DEFAULT_CONFIG, load_config, review_timeout_seconds

    default = DEFAULT_CONFIG["llm"]["review_timeout_seconds"]
    for literal in (".inf", ".nan", "-.inf"):
        data = _yaml.safe_load(f"llm:\n  review_timeout_seconds: {literal}\n")
        raw = data["llm"]["review_timeout_seconds"]
        # The instrument first: if YAML ever stopped producing a non-finite
        # float here, the rest of this test would be checking nothing.
        assert isinstance(raw, float) and not math.isfinite(raw), (literal, raw)

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="no_human.config"):
            assert review_timeout_seconds(data) == default, literal
        assert any("review_timeout_seconds" in r.getMessage()
                   for r in caplog.records), (literal, caplog.records)

    # End to end, the way the crash was reproduced: a config.yaml on disk ->
    # load_config -> the property every reviewer construction reads.
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "llm:\n  review_timeout_seconds: .inf\n  code_review_timeout_seconds: .nan\n")
    cfg = load_config(home / "config.yaml")
    assert cfg.review_timeout_seconds == default
    assert cfg.code_review_timeout_seconds == (
        DEFAULT_CONFIG["llm"]["code_review_timeout_seconds"])

    # ...and the reviewer that reads it still builds, which is what "unloadable"
    # actually meant.
    from no_human.review.reviewer import AdversarialReviewer

    class _B:
        model = "claude-opus-5"

    reviewer = AdversarialReviewer.from_config(cfg.data, backend=_B())
    assert reviewer._timeout == default


def test_the_config_floor_never_inverts_the_retry_window():
    """`_agent_review` halves a timed-out round's window but floors it at
    `_REVIEW_MIN_RETRY_TIMEOUT`. If the config floor sat BELOW that floor, a
    configured window in between would give round TWO a bigger window than
    round one — an inversion of the rule that a hang must escalate sooner, not
    later. Aligning the two removes the case rather than documenting it."""
    from no_human.config import REVIEW_TIMEOUT_FLOOR_S
    from no_human.review.reviewer import _REVIEW_MIN_RETRY_TIMEOUT

    assert REVIEW_TIMEOUT_FLOOR_S >= _REVIEW_MIN_RETRY_TIMEOUT, (
        f"a configured window between {_REVIEW_MIN_RETRY_TIMEOUT} and "
        f"{REVIEW_TIMEOUT_FLOOR_S} would grow on retry instead of shrinking")


# --------------------------------------------------------------------------- #
# A default-ON knob shipped undocumented (blockers.challenge, 2026-08-19). The
# suite was green because nothing asks whether a config key a user can set is
# described anywhere. This is that gate — differential, because a strict
# "document everything" would have failed on 62 pre-existing keys, and a gate
# that is red on arrival gets disabled rather than obeyed.
# --------------------------------------------------------------------------- #

#: The 72 keys undocumented when this gate was written — the honest
#: number under an ancestry-aware matcher. The first version of this gate
#: counted 62, because 18 keys were passing on a leaf name that belonged to
#: an unrelated section (`enabled:` under integrations.jira documenting
#: `telemetry.enabled`). It may only SHRINK: adding a
#: key here to make the gate pass is the failure mode it exists to prevent —
#: document the key instead. The size assert below catches simple GROWTH; what
#: it cannot catch is a SWAP (document one key, add another), so the rule is
#: stated here and in the failure message, and the list is short enough
#: to read in a review.
#: A HIGH-WATER MARK, not a ratchet, and the difference matters: it fails a
#: set that grows past 72, but it is never lowered when a key leaves, so
#: documenting one key and adding a different undocumented one keeps the size
#: at 72 and passes. Demonstrated in review. What it genuinely buys is a loud
#: diff — `-72 / +73` is unmissable where one more line inside a sorted
#: 72-entry frozenset is not. Real enforcement would derive the number from
#: history, outside the file the author is editing.
_BASELINE_SIZE_AT_WRITING = 71

_UNDOCUMENTED_AT_BASELINE = frozenset({
    "blockers.ignore_comment_authors",
    "blockers.max_ci_fix_rounds",
    "blockers.max_ci_gate_fix_rounds",
    "blockers.pr_ci_policy",
    "blockers.stuck_active_minutes",
    "bounds.attempt_tokens",
    "bounds.complex_multiplier",
    "bounds.lifetime_tokens",
    "bounds_investigation.max_attempts",
    "bounds_investigation.max_correction_rounds",
    "bounds_investigation.max_turns_per_attempt",
    "ci.base_url",
    "ci.cookie_auto_refresh",
    "ci.crumb_path",
    "ci.job",
    "ci.storage_state_path",
    "ci.wake_hint",
    "concurrency.poll_interval",
    "context.attempt_state_distill_enabled",
    "context.repo_map_enabled",
    "decomposition.enabled",
    "docs.auto_refresh",
    "docs.max_turns",
    "docs.refresh_interval_seconds",
    "eval.nightly_budget_tokens",
    "filter_user_skills",
    "git.github_hosts",
    "learning.auto_confirm_recurring",
    "llm.auth_profile",
    "llm.codex_cli_path",
    "llm.codex_reasoning_effort",
    "llm.moa_planning.criteria_threshold",
    "llm.moa_planning.description_threshold",
    "llm.moa_planning.enabled",
    "llm.moa_planning.min_signals",
    "llm.moa_planning.proposers",
    "onboarding.completed",
    "pipeline.trivial_tier.enabled",
    "planning.enabled",
    "planning.max_turns",
    "profile.auto_confirm_proven",
    "profile.auto_onboard",
    "repro_gate.mode",
    "reviewer.allow_advisory",
    "reviewer.feedback_rounds",
    "reviewer.passes",
    "supervisor.check_every",
    "supervisor.enabled",
    "supervisor.preflight",
    "tamper_adjudication.enabled",
    "team_brain.control_plane_url",
    "team_brain.enabled",
    "team_brain.max_stale_days",
    "telemetry.instance_id",
    "telemetry.posthog_publishable",
    "updates.enabled",
    "updates.interval_seconds",
})


def _leaf_config_keys(node, prefix=""):
    """Every settable key as a dotted path.

    An EMPTY dict is a leaf: `ci.variables` defaults to `{}` and is settable,
    and recursing into it yields nothing — so a future `{}`-defaulted knob
    (including a whole user-keyed mapping section) would be invisible to this
    gate. Found by review, mutation (f).
    """
    for key, value in (node or {}).items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            yield from _leaf_config_keys(value, path)
        else:
            yield path


#: `config.get("SECTION", {}).get("KEY")` and the defensive
#: `(config.get("SECTION") or {}).get("KEY")` — a key read straight from the user's
#: config with an inline default and no DEFAULT_CONFIG entry. Fully settable
#: (`_deep_merge` carries it through) and invisible to a defaults-only walk, so
#: the gate's universe has to include these or it is checking "keys we declare"
#: while claiming "keys a user can set". Found by review: `lint.command`
#: decides whether the lint gate exists at all and was documented nowhere.
_INLINE_READ = re.compile(
    r"""(?<![\w.])(?:self\.)?config(?:\.data)?\.get\(\s*["'](\w+)["']\s*"""
    r"""(?:,\s*\{\}\s*\)|\)\s*or\s*\{\}\s*\))\s*\.get\(\s*["'](\w+)["']""")


def _keys_read_without_a_default(src_root, declared_sections):
    """Settable keys the source reads but DEFAULT_CONFIG never declares.

    A section that IS declared (`llm.moa_planning`) is skipped: reading a
    subsection this way is ordinary, and its leaves are already walked.
    """
    found = set()
    for path in sorted(src_root.rglob("*.py")):
        for m in _INLINE_READ.finditer(path.read_text()):
            dotted = f"{m.group(1)}.{m.group(2)}"
            if dotted not in declared_sections:
                found.add(dotted)
    return found


def _documented_paths(docs: str):
    """Dotted paths the doc actually documents, by its own YAML nesting.

    Matching a LEAF name anywhere in the file is what the first version did,
    and it is blind to the class it was written for: `enabled` is the leaf of
    19 of the 166 keys, so a brand-new default-ON section passed because
    `enabled:` appears under `integrations.jira`. Review mutation (e) added
    four knobs including a default-ON boolean and the gate stayed green.

    So: rebuild each YAML key line's ancestry from its indentation, and let a
    doc line document a config path only when its ancestry is a SUFFIX of that
    path. `enabled:` nested under `concurrency:` documents
    `concurrency.enabled` and nothing else.
    """
    stack: list[tuple[int, str]] = []
    paths = set()
    for line in docs.splitlines():
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", line)
        if not m:
            continue
        indent = len(m.group(1).expandtabs(2))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, m.group(2)))
        paths.add(".".join(name for _, name in stack))
    return paths


def _is_documented(path: str, doc_paths: set, docs: str) -> bool:
    """Documented iff the doc's own nesting names this EXACT path, or the
    dotted path is written out literally (the "Settings at a glance" table
    documents keys that way).

    Exact, not suffix. A suffix match leaves the prefix unchecked, so a future
    `defaults.git.branch_prefix` or `overrides.ci.enabled` — documented
    nowhere — rode the `git.branch_prefix` / `ci.enabled` lines; the re-review
    demonstrated it, and anchoring on the CONFIG's own top-level names did not
    help, because the offending section is itself in the config. Measured
    before tightening: of the 99 documented keys, 84 match the full path and
    15 the literal table entry — **none** needed a suffix. So exact costs
    nothing and closes the hole.

    The literal hatch requires a dot: for a single-segment key the "dotted
    path" is one bare word, and any prose occurrence of `enabled` or
    `hostname` documented it.
    """
    if path in doc_paths:
        return True
    return "." in path and bool(
        re.search(rf"(?<![\w.]){re.escape(path)}(?![\w.])", docs))


def test_every_new_config_key_is_documented(tmp_path):
    """A key a user can set must be documented under a matching path.

    The matcher is ancestry-aware on purpose (see `_documented_paths`): the
    first version matched a bare leaf name anywhere in the file, which let a
    whole new default-ON section ride on an unrelated `enabled:`. It also
    accepts a literal dotted mention, because the summary table documents keys
    that way.

    Where it is still loose, stated in BOTH directions because the first
    version of this docstring named only the safe one:

    * false FAILURE — a key documented only in another file (e.g. a backend
      knob covered in docs/BACKENDS.md but never mentioned in
      docs/configuration.md) counts as undocumented here and sits in the
      baseline until this file also names it; so do keys whose doc line is
      inside a list item or an inconsistently-indented block, because the
      ancestry parser produces a longer path for those. (`worker.backend`
      used to be this example — it left the baseline once
      docs/configuration.md's local-backend section started mentioning it
      literally, which is the intended shrink-only behavior, not a bug.)
    * false PASS, four channels, none of them hypothetical:
      1. the parser cannot tell a fenced YAML block from a wrapped prose line
         that happens to start `word:`. Two such lines exist
         (docs/configuration.md's `size:` and `statement:` sentences), so a
         top-level key named `size` or `statement` would pass documented by an
         English sentence. No collision today — checked, the one-segment doc
         ancestries share no name with any leaf — which is luck, not design;
         fencing-aware parsing is the fix when it bites.
      2. the doc contains a fenced block for a DIFFERENT file's schema
         (`<repo>/.no_human.yml`), and its keys enter the ancestry set, so a
         global key named `test_commands`, `playbook_hints` or
         `forbidden_paths_extra` would pass on that block.
      3. the literal-path hatch accepts a dotted name anywhere in the file,
         including a "removed in vX" note. Every literal-only pass today is a
         real table row or a descriptive sentence — checked one by one — but
         the matcher cannot tell the difference.
      4. the UNIVERSE is regex-swept, so a settable key read in a shape
         `_INLINE_READ` does not match is invisible. Two forms are known and
         live: a section pulled into a local first
         (`sec = config.data.get("reanalysis", {})` then `sec.get("enabled")`
         in api/app.py — `reanalysis.enabled`, `interval_seconds`, `days`,
         `max_proposals`, `onboarding.extra_scan_roots`) and a single-segment
         top-level read (`config.get("max_thinking_tokens", 10_000)`). Those
         five-plus keys are settable, undocumented, and this gate does not see
         them. An AST walk of `config` reads is the fix; a wider regex is not,
         because the value flows through a variable.
    """
    from pathlib import Path

    from no_human.config import load_config

    cfg = load_config(tmp_path / "config.yaml")
    docs = (Path(__file__).resolve().parents[1] / "docs" / "configuration.md").read_text()
    doc_paths = _documented_paths(docs)
    declared = set(_leaf_config_keys(cfg.data))
    sections = {p.rsplit(".", 1)[0] for p in declared} | declared
    settable = declared | _keys_read_without_a_default(
        Path(__file__).resolve().parents[1] / "src", sections)
    undocumented = {
        path for path in settable
        if not _is_documented(path, doc_paths, docs)
    }
    # "May only shrink" was a comment and a failure message; a future author
    # could satisfy this gate by ADDING their key to the baseline, and nothing
    # objected (the re-review demonstrated it). Now something does.
    assert len(_UNDOCUMENTED_AT_BASELINE) <= _BASELINE_SIZE_AT_WRITING, (
        f"the baseline grew to {len(_UNDOCUMENTED_AT_BASELINE)}; it may only "
        f"shrink from {_BASELINE_SIZE_AT_WRITING}. Document the key instead.")

    new = undocumented - _UNDOCUMENTED_AT_BASELINE
    assert not new, (
        "config key(s) a user can set, documented nowhere in "
        f"docs/configuration.md: {sorted(new)}. Document them; do NOT add them "
        "to the baseline set, which may only shrink."
    )
    stale = _UNDOCUMENTED_AT_BASELINE - undocumented
    assert not stale, (
        f"these keys are documented now — drop them from the baseline: {sorted(stale)}")


def test_configuration_md_local_model_line_uses_a_real_yaml_comment():
    """The `llm:` sample's `local_model: null` line used an em dash
    (`—`) instead of `#` to introduce its trailing note, which is not a YAML
    comment marker at all — it would parse as part of the scalar value,
    making the shipped sample invalid to copy-paste. One character, but it
    breaks the sample."""
    from pathlib import Path

    docs = (Path(__file__).resolve().parents[1] / "docs" / "configuration.md"
            ).read_text(encoding="utf-8")
    line = next(
        ln for ln in docs.splitlines() if ln.strip().startswith("local_model:"))
    assert "#" in line, (
        f"docs/configuration.md's local_model sample line has no `#` "
        f"comment marker: {line!r}"
    )
    trailer = line.split("local_model:", 1)[1].split("null", 1)[1]
    assert "—" not in trailer, (
        f"docs/configuration.md's local_model line still uses an em dash "
        f"instead of `#` for its trailing comment: {line!r}"
    )


# --------------------------------------------------------------------------- #
# Local model backend (part 1: config keys + assert_local_backend_mode).      #
# --------------------------------------------------------------------------- #

def test_the_local_backend_keys_ship_with_inert_defaults():
    llm = DEFAULT_CONFIG["llm"]
    assert llm["local_model"] is None
    assert llm["local_base_url"] is None
    assert llm["local_cli_path"] is None
    assert llm["primary_model"] == "claude-sonnet-5"
    assert llm["review_model"] == "claude-opus-4-8"
    assert llm["planner_model"] == "claude-opus-5"
    assert llm["supervisor_model"] == "claude-sonnet-5"
    assert DEFAULT_CONFIG["worker"]["backend"] == "claude"


def test_a_config_that_sets_the_local_keys_parses_them(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "llm:\n"
        "  local_model: my-model\n"
        "  local_base_url: \"http://localhost:8000\"\n"
        "  local_cli_path: /opt/bin/cli\n"
    )
    cfg = load_config(cfg_path)
    assert cfg.data["llm"]["local_model"] == "my-model"
    assert cfg.data["llm"]["local_base_url"] == "http://localhost:8000"
    assert cfg.data["llm"]["local_cli_path"] == "/opt/bin/cli"


def test_a_config_that_omits_the_local_keys_still_resolves_them(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm:\n  auth_mode: subscription\n")
    cfg = load_config(cfg_path)
    assert cfg.data["llm"]["local_model"] is None
    assert cfg.data["llm"]["local_base_url"] is None
    assert cfg.data["llm"]["local_cli_path"] is None


def test_local_mode_refuses_a_missing_base_url():
    for bad in (None, ""):
        with pytest.raises(AuthError, match="local_base_url") as exc_info:
            assert_local_backend_mode(bad)
        assert "ANTHROPIC_BASE_URL" in str(exc_info.value)


@pytest.mark.parametrize("url", [
    "ftp://localhost",
    "file:///x",
    "ws://localhost:1234",
    "localhost:8000",
])
def test_local_mode_refuses_a_non_http_scheme(url):
    with pytest.raises(AuthError, match="scheme"):
        assert_local_backend_mode(url)


@pytest.mark.parametrize("url", [
    "http://my-llm.internal:8000",
    "http://localhost.evil.com:8000",
    "https://example.com",
])
def test_local_mode_refuses_a_dns_hostname(url):
    with pytest.raises(AuthError, match="DNS name"):
        assert_local_backend_mode(url)


@pytest.mark.parametrize("url", [
    "http://8.8.8.8:8000",
    "http://1.1.1.1",
    "http://[2001:4860:4860::8888]:8000",
])
def test_local_mode_refuses_a_public_ip(url):
    with pytest.raises(AuthError, match="public"):
        assert_local_backend_mode(url)


@pytest.mark.parametrize("url", [
    # IPv4 cloud IMDS (link-local 169.254.0.0/16) — the SSRF target. Python's
    # is_private admits it, so is_private alone was NOT a safe gate.
    "http://169.254.169.254",
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "http://169.254.170.2:80",              # ECS task-metadata endpoint
    # IPv6 cloud IMDS (fd00:ec2::254) lives in the fc00::/7 ULA block, which is
    # is_private but NOT is_link_local — the case the minimal fix could not see.
    "http://[fd00:ec2::254]:80",
    "http://[fe80::1]:8000",                # IPv6 link-local
    "http://0.0.0.0:8000",                  # unspecified / this-host
    "http://192.0.2.10:8000",               # TEST-NET-1 (documentation)
])
def test_local_mode_refuses_link_local_and_metadata_addresses(url):
    """SSRF regression: local mode must reject the cloud metadata endpoints and
    every non-RFC1918 private range, not just publicly-routable IPs."""
    with pytest.raises(AuthError, match="loopback/RFC1918"):
        assert_local_backend_mode(url)


@pytest.mark.parametrize("url", [
    "http://user:pass@localhost:8000",
    "http://tok@127.0.0.1:8000",
])
def test_local_mode_refuses_userinfo_in_the_url(url):
    with pytest.raises(AuthError) as exc_info:
        assert_local_backend_mode(url)
    msg = str(exc_info.value)
    assert "pass" not in msg
    assert "tok" not in msg


@pytest.mark.parametrize("url", [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://127.0.0.1:1234/v1",
    "https://localhost",
    "http://10.0.0.5:8000",
    "http://192.168.1.20:11434",
    "http://172.16.3.4:8000",
    "http://[::1]:8000",
])
def test_local_mode_accepts_the_loopback_forms(monkeypatch, url):
    for var in config.METERED_AUTH_VARS:
        monkeypatch.delenv(var, raising=False)
    report = assert_local_backend_mode(url)
    assert isinstance(report, config.ScrubReport)


def test_local_mode_reruns_the_scrub_with_keep_empty(monkeypatch):
    for var in config.METERED_AUTH_VARS:
        monkeypatch.setenv(var, "x")
    report = assert_local_backend_mode("http://localhost:8000")
    assert set(report.removed) == set(config.METERED_AUTH_VARS)
    assert report.api_key_present is True
    for var in config.METERED_AUTH_VARS:
        assert var not in os.environ


def test_the_local_backend_did_not_widen_the_anthropic_scrub_list():
    assert config.METERED_AUTH_VARS == (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLOUD_ML_REGION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AWS_BEARER_TOKEN_BEDROCK",
    )
    assert config.LOCAL_LLM_API_KEY_VAR not in config.METERED_AUTH_VARS


@pytest.mark.parametrize("url", [
    "http://user:pass@localhost:8000",
    "http://localhost:8000?api_key=sk-x",
    "http://localhost:8000?apikey=sk-x",
    "http://localhost:8000?token=sk-x",
    "http://localhost:8000?secret=sk-x",
    "http://localhost:8000?PASSWORD=sk-x",
])
def test_load_config_rejects_a_credential_embedded_in_local_base_url(tmp_path, url):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"llm:\n  local_base_url: \"{url}\"\n")
    with pytest.raises(AuthError) as exc_info:
        load_config(cfg_path)
    assert "sk-x" not in str(exc_info.value)
    assert "pass" not in str(exc_info.value)


def test_load_config_accepts_a_local_base_url_with_a_harmless_query_param(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text('llm:\n  local_base_url: "http://localhost:8000/v1?stream=true"\n')
    cfg = load_config(cfg_path)
    assert cfg.data["llm"]["local_base_url"] == "http://localhost:8000/v1?stream=true"


def test_set_local_backend_fields_writes_both_values_and_preserves_comments(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm:  # which subscription pays\n  auth_profile: default\n")
    resolved = config.set_local_backend_fields(
        {"local_model": "my-model", "local_base_url": "http://localhost:8000/v1"},
        cfg_path,
    )
    assert resolved == {
        "local_model": "my-model",
        "local_base_url": "http://localhost:8000/v1",
    }
    reloaded = load_config(cfg_path)
    assert reloaded.data["llm"]["local_model"] == "my-model"
    assert reloaded.data["llm"]["local_base_url"] == "http://localhost:8000/v1"
    # The inline comment on the header and the sibling scalar are untouched.
    text = cfg_path.read_text()
    assert "# which subscription pays" in text
    assert "auth_profile: default" in text


def test_set_local_backend_fields_refuses_a_public_url_leaving_the_file_untouched(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    original = "llm:\n  auth_profile: default\n"
    cfg_path.write_text(original)
    with pytest.raises(AuthError, match="public/routable"):
        config.set_local_backend_fields(
            {"local_base_url": "http://8.8.8.8:8000"}, cfg_path,
        )
    assert cfg_path.read_text() == original


def test_set_local_backend_fields_refuses_an_unknown_key(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm:\n  auth_profile: default\n")
    with pytest.raises(ValueError, match="unrecognised config key"):
        config.set_local_backend_fields({"api_key_model": "x"}, cfg_path)


def test_the_claude_pinned_roles_and_backend_resolution_are_untouched():
    from no_human.agent.backend import CLAUDE_PINNED_ROLES, resolve_backend_name
    assert CLAUDE_PINNED_ROLES == ("reviewer", "planner", "supervisor", "utility", "intake")
    assert resolve_backend_name(DEFAULT_CONFIG) == "claude"
    assert resolve_backend_name({"worker": {"backend": "local"}}) == "local"
    assert resolve_backend_name({"worker": {"backend": "local"}}, role="reviewer") == "claude"


# --------------------------------------------------------------------------- #
# 8. `llm.codex_auth_mode` — the api_key/subscription dispatcher               #
# --------------------------------------------------------------------------- #
#
# No real credential appears below. `session_check` is the seam
# `assert_codex_subscription_mode` exposes precisely so these tests never
# have to shell out to a real `codex` binary or touch `~/.codex/auth.json`.

class _FakeStatus:
    def __init__(self, present, via, detail=""):
        self.present = present
        self.via = via
        self.detail = detail


def test_codex_auth_mode_defaults_to_api_key():
    assert config.codex_auth_mode({}) == "api_key"
    assert config.codex_auth_mode({"llm": {}}) == "api_key"
    assert config.codex_auth_mode(None) == "api_key"


def test_codex_auth_mode_accepts_subscription():
    assert config.codex_auth_mode(
        {"llm": {"codex_auth_mode": "subscription"}}) == "subscription"
    # Case/whitespace are not a second way to spell the value wrong.
    assert config.codex_auth_mode(
        {"llm": {"codex_auth_mode": " Subscription "}}) == "subscription"


def test_codex_auth_mode_rejects_an_unrecognised_value_rather_than_defaulting():
    """A typo here would otherwise silently bill the metered API instead of
    the plan the operator thought they had selected — fail loud, never
    fall back to api_key."""
    with pytest.raises(AuthError) as exc:
        config.codex_auth_mode({"llm": {"codex_auth_mode": "chatgpt-plus"}})
    msg = str(exc.value)
    assert "codex_auth_mode" in msg
    assert "chatgpt-plus" in msg
    assert "api_key" in msg and "subscription" in msg


def test_assert_codex_subscription_mode_scrubs_the_openai_routes(monkeypatch):
    for var in config.CODEX_SUBSCRIPTION_SCRUB_VARS:
        monkeypatch.setenv(var, "placeholder")
    # Literal names, set independently of CODEX_SUBSCRIPTION_SCRUB_VARS: if a
    # var is deleted from that list, the loop above would stop setting it
    # too, and the vacuous "never set ⇒ never present" would let the
    # membership assertions below pass even though the var is no longer
    # scrubbed. Setting them here by name closes that hole.
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder")
    monkeypatch.setenv("CODEX_API_KEY", "placeholder")
    monkeypatch.setenv("OPENAI_BASE_URL", "placeholder")

    report = config.assert_codex_subscription_mode(
        session_check=lambda: _FakeStatus(True, "chatgpt"))
    assert set(report.removed) == set(config.CODEX_SUBSCRIPTION_SCRUB_VARS)
    for var in config.CODEX_SUBSCRIPTION_SCRUB_VARS:
        assert var not in os.environ
    # Literal-name pins: the loops above prove nothing if a var is deleted
    # from CODEX_SUBSCRIPTION_SCRUB_VARS itself — assert the actual scrubbed
    # names directly so a shrunk list still fails this test.
    assert "OPENAI_API_KEY" not in os.environ
    assert "CODEX_API_KEY" not in os.environ
    assert "OPENAI_BASE_URL" not in os.environ


def test_assert_codex_subscription_mode_refuses_when_no_session_is_found():
    with pytest.raises(AuthError) as exc:
        config.assert_codex_subscription_mode(
            session_check=lambda: _FakeStatus(False, "none"))
    msg = str(exc.value)
    assert "llm.codex_auth_mode" in msg and "subscription" in msg
    assert "codex login" in msg
    assert "api_key" in msg  # names the alternative, not just the rule
    # Names the file it explicitly does NOT read, but never reads it to get
    # here — there is no credential value in this message to echo.
    assert "sk-" not in msg


def test_assert_codex_subscription_mode_refuses_an_api_key_backed_session():
    """A `codex login status` that reports an API-key-backed session is not
    the ChatGPT plan this mode exists to use — refused the same as no
    session at all, not silently accepted as 'close enough'."""
    with pytest.raises(AuthError, match="codex login"):
        config.assert_codex_subscription_mode(
            session_check=lambda: _FakeStatus(True, "api_key"))


def test_assert_codex_subscription_mode_accepts_unrecognised_wording():
    """The CLI's own verdict wins on wording it hasn't seen before — a
    stricter parse here would refuse a legitimate session over a vendor
    wording change no_human doesn't recognise."""
    report = config.assert_codex_subscription_mode(
        session_check=lambda: _FakeStatus(True, "unknown"))
    assert isinstance(report, config.ScrubReport)


def test_assert_codex_mode_dispatches_subscription_to_the_session_check():
    calls = []

    def _check():
        calls.append(1)
        return _FakeStatus(True, "chatgpt")

    report = config.assert_codex_mode("subscription", session_check=_check)
    assert isinstance(report, config.ScrubReport)
    assert calls == [1]


def test_assert_codex_mode_dispatches_api_key_by_default(tmp_path, monkeypatch):
    """Any mode other than the literal string "subscription" — including the
    default — takes the api_key path. `codex_auth_mode` is what turns an
    unrecognised string into a fail-loud error; the dispatcher itself just
    routes the two legal values."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=placeholder-not-a-real-key\n")
    report = config.assert_codex_mode("api_key", env_path=env_file)
    assert isinstance(report, config.ScrubReport)
    assert os.environ["OPENAI_API_KEY"] == "placeholder-not-a-real-key"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
