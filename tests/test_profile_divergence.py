"""project.yml diverging from the confirmed DB profile: the DB row still
wins (no precedence inversion), but a once-per-task advisory event names the
differing keys, and `nh doctor` reports the same divergence."""

from types import SimpleNamespace


from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.doctor import diagnose
from no_human.notify.slack import SlackNotifier
from no_human.profile import ProjectProfile, profile_divergence


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover - not exercised here
        raise AssertionError("backend should not run in resolution tests")


def _orch(store, tmp_path, events):
    cfg = load_config(tmp_path / "config.yaml")
    orch = Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))
    orch._sink = events.append
    return orch


def _repo(path):
    return SimpleNamespace(path=path)


def _usable(repo_path):
    return ProjectProfile(
        repo_path=str(repo_path), ecosystem="node",
        install_cmd="npm ci", test_cmd="npm test",
        ci={"backend": "gitlab", "enabled": True, "project": "x/y"},
        derived_from=["package.json"], proven={"test_cmd": True}, confirmed=True,
    )


def _div(events):
    return [e for e in events if e["kind"] == "profile_divergence"]


async def test_diverging_yml_advises_and_db_still_wins(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    await store.upsert_profile(_usable(repo_path))
    diverged = _usable(repo_path)
    diverged.test_cmd = "npm run other"
    diverged.ui_evidence = dict(diverged.ui_evidence)
    diverged.ui_evidence["enabled"] = True
    diverged.save()

    events = []
    orch = _orch(store, tmp_path, events)
    prof = await orch._usable_profile(repo_path)

    assert len(_div(events)) == 1
    e = _div(events)[0]
    assert e["text"] == (
        "project.yml differs from the confirmed profile on: "
        "test_cmd, ui_evidence - the confirmed profile wins; "
        "re-onboard or use the API to update"
    )
    assert e["keys"] == ["test_cmd", "ui_evidence"]
    assert prof.test_cmd == "npm test"
    assert prof.ui_evidence["enabled"] is False


async def test_advisory_fires_once_per_orchestrator(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    await store.upsert_profile(_usable(repo_path))
    diverged = _usable(repo_path)
    diverged.test_cmd = "npm run other"
    diverged.save()

    events = []
    orch = _orch(store, tmp_path, events)
    await orch._usable_profile(repo_path)
    await orch._usable_profile(repo_path)
    await orch._usable_profile(repo_path)
    await orch._resolve_test_cmd(_repo(repo_path))

    assert len(_div(events)) == 1


async def test_identical_yml_is_silent(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    prof = _usable(repo_path)
    prof.save()
    await store.upsert_profile(prof)

    events = []
    orch = _orch(store, tmp_path, events)
    got = await orch._usable_profile(repo_path)

    assert _div(events) == []
    assert got is not None and got.test_cmd == "npm test"


async def test_missing_yml_is_silent(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    await store.upsert_profile(_usable(repo_path))

    events = []
    orch = _orch(store, tmp_path, events)
    got = await orch._usable_profile(repo_path)

    assert _div(events) == []
    assert got is not None and got.test_cmd == "npm test"


async def test_no_db_row_uses_yml_and_is_silent(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    _usable(repo_path).save()

    events = []
    orch = _orch(store, tmp_path, events)
    got = await orch._usable_profile(repo_path)

    assert got is not None and got.test_cmd == "npm test"
    assert _div(events) == []


async def test_doctor_reports_divergence_keys(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    await store.upsert_profile(_usable(repo_path))
    diverged = _usable(repo_path)
    diverged.test_cmd = "npm run other"
    diverged.ui_evidence = dict(diverged.ui_evidence)
    diverged.ui_evidence["enabled"] = True
    diverged.save()

    d = await diagnose(store)
    assert any(
        "PROFILE DIVERGENCE" in a and "test_cmd, ui_evidence" in a
        for a in d.advisories
    )
    assert d.healthy is True


async def test_doctor_silent_when_identical(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    prof = _usable(repo_path)
    prof.save()
    await store.upsert_profile(prof)

    d = await diagnose(store)
    assert not any("PROFILE DIVERGENCE" in a for a in d.advisories)


async def test_nothing_is_auto_synced(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    await store.upsert_profile(_usable(repo_path))
    diverged = _usable(repo_path)
    diverged.test_cmd = "npm run other"
    diverged.save()

    yml_path = diverged.yaml_path()
    yml_before = yml_path.read_text()
    db_before = (await store.get_profile(str(repo_path))).to_dict()

    events = []
    orch = _orch(store, tmp_path, events)
    await orch._usable_profile(repo_path)
    await diagnose(store)

    assert yml_path.read_text() == yml_before
    assert (await store.get_profile(str(repo_path))).to_dict() == db_before


def test_profile_divergence_ignores_repo_path():
    db_prof = _usable("/primary/repo")
    yml_prof = _usable("/some/worktree/repo")
    assert profile_divergence(db_prof, yml_prof) == []


async def test_wiki_refresh_does_not_trigger_advisory(store, tmp_path):
    """`docs_gen`'s wiki refresh (scheduler.py / `nh docs generate`) writes
    the new `wiki_commit` to the yml via `profile.save()` only — it never
    calls `store.upsert_profile`. That one-sided write must never read as a
    human having edited project.yml."""
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    await store.upsert_profile(_usable(repo_path))
    on_disk = _usable(repo_path)
    on_disk.wiki_commit = "deadbeef" * 5
    on_disk.save()

    events = []
    orch = _orch(store, tmp_path, events)
    got = await orch._usable_profile(repo_path)

    assert _div(events) == []
    assert got is not None and got.test_cmd == "npm test"

    d = await diagnose(store)
    assert not any("PROFILE DIVERGENCE" in a for a in d.advisories)


async def test_repo_config_budget_write_does_not_trigger_advisory(store, tmp_path):
    """`nh repo config` (SCRUM-26 per-repo budget overrides) calls
    `store.upsert_profile` only — it never calls `profile.save()`. That
    one-sided write must never read as a human having edited project.yml."""
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    base = _usable(repo_path)
    base.save()
    await store.upsert_profile(base)

    in_db = _usable(repo_path)
    in_db.default_attempt_tokens = 4_000_000
    in_db.default_lifetime_tokens = 20_000_000
    in_db.default_budget_unit = "weighted"
    await store.upsert_profile(in_db)

    events = []
    orch = _orch(store, tmp_path, events)
    got = await orch._usable_profile(repo_path)

    assert _div(events) == []
    assert got is not None and got.default_attempt_tokens == 4_000_000

    d = await diagnose(store)
    assert not any("PROFILE DIVERGENCE" in a for a in d.advisories)


def test_profile_divergence_excludes_machine_managed_fields():
    """Direct unit test on the helper: a difference confined to wiki_commit
    or the default_* budget fields must not be reported, even though those
    are real dict differences — they are the product's own one-sided writes,
    not a human edit of project.yml."""
    db_prof = _usable("/repo")
    db_prof.wiki_commit = "aaaa"
    db_prof.default_attempt_tokens = 4_000_000
    db_prof.default_lifetime_tokens = 20_000_000
    db_prof.default_budget_unit = "weighted"

    yml_prof = _usable("/repo")
    yml_prof.wiki_commit = "bbbb"
    # default_* fields left at the dataclass default (0 / "")

    assert profile_divergence(db_prof, yml_prof) == []

    # A REAL human-authored difference alongside the machine-managed noise
    # still surfaces — only the noise is suppressed.
    yml_prof.test_cmd = "npm run other"
    assert profile_divergence(db_prof, yml_prof) == ["test_cmd"]
