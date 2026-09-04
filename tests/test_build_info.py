"""The loaded-code snapshot, and the attempt row that carries it.

Motivating incident: task ecfe1789 escalated on a tamper-guard false positive
3h18m after the commit fixing that exact false positive had merged. The guard
on main was right; the process was stale. Nothing on the record could tell the
two apart, so the failure was attributed to the ticket.
"""

import importlib
import subprocess
import threading
import time

import pytest

from no_human.core.build_info import LoadedCode, _detect, staleness_note
from no_human.core.task import Task


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A real git checkout with a package dir inside it."""
    root = tmp_path / "repo"
    (root / "src" / "no_human").mkdir(parents=True)
    (root / "src" / "no_human" / "__init__.py").write_text("x = 1\n")
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


# --- provenance ------------------------------------------------------------


def test_clean_checkout_reports_its_sha(repo):
    info = _detect(repo / "src" / "no_human")
    assert info.sha == _git(repo, "rev-parse", "HEAD")
    assert info.dirty is False
    assert info.descriptor == f"git:{info.sha}"


def test_dirty_package_tree_is_marked_dirty(repo):
    """A sha alone would claim fidelity the working tree does not have."""
    (repo / "src" / "no_human" / "__init__.py").write_text("x = 2\n")
    info = _detect(repo / "src" / "no_human")
    assert info.dirty is True
    assert info.descriptor.endswith("+dirty")


def test_edits_outside_the_package_do_not_mark_it_dirty(repo):
    """Repo-wide dirt is not loaded-code dirt — otherwise the flag is noise
    in exactly the actively-developed repo it exists to describe."""
    (repo / "README.md").write_text("unrelated\n")
    assert _detect(repo / "src" / "no_human").dirty is False


def test_no_repo_at_all_reports_unknown(tmp_path, monkeypatch):
    """The easy half: nothing above us is a git repo, so there is no sha.

    Named for what it actually proves. Its predecessor was called
    "…not_a_borrowed_sha" while testing only this case — a directory with no
    repo above it cannot borrow a sha, so the test could never fail for the
    reason its name claimed. The borrowing case is `venv_inside_a_user_project`
    below, and it FAILED when this file first shipped.
    """
    monkeypatch.setattr("no_human.core.build_info._dist_version", lambda: None)
    outside = tmp_path / "site-packages" / "no_human"
    outside.mkdir(parents=True)
    info = _detect(outside)
    assert info.sha is None
    assert info.descriptor == "unknown"


@pytest.fixture
def venv_inside_a_user_project(tmp_path):
    """The documented PyPI install: our wheel under `.venv/` in the user's own
    git repo. `.venv/` is gitignored, which is what makes this dangerous —
    `rev-parse HEAD` succeeds and `status --porcelain` is empty, so the naive
    reading is "their commit, and clean"."""
    proj = tmp_path / "userproj"
    pkg = proj / ".venv" / "lib" / "python3.12" / "site-packages" / "no_human"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("# ours, installed\n")
    _git(tmp_path, "init", "-q", str(proj))
    _git(proj, "config", "user.email", "u@example.com")
    _git(proj, "config", "user.name", "u")
    (proj / ".gitignore").write_text(".venv/\n")
    (proj / "app.py").write_text("their code\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "the user's own project")
    return proj, pkg


def test_installed_wheel_never_borrows_the_users_project_sha(venv_inside_a_user_project):
    """The HIGH defect. Being inside a repo is not being tracked by it."""
    proj, pkg = venv_inside_a_user_project
    info = _detect(pkg)
    users_sha = _git(proj, "rev-parse", "HEAD")
    assert info.sha != users_sha
    assert info.sha is None, f"borrowed {users_sha} from the user's project"
    assert not info.descriptor.startswith("git:")
    # And it must not claim the borrowed tree was clean, either.
    assert info.dirty is None


def test_no_fabricated_staleness_when_the_user_commits_to_their_own_repo(
        venv_inside_a_user_project):
    """The compounding failure: with a borrowed sha, the user's next commit
    makes our snapshot a strict ancestor of THEIR HEAD, and we tell the
    operator to restart over a repo we have nothing to do with."""
    proj, pkg = venv_inside_a_user_project
    info = _detect(pkg)
    (proj / "feature.py").write_text("the user adds a feature\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "user moves on")
    assert staleness_note(info, package_root=pkg) is None


def test_untracked_checkout_of_our_own_layout_is_also_unknown(tmp_path):
    """Same rule, no venv involved: a package copied into a repo that does not
    track it gets no attribution. Guards the check itself rather than the
    scenario that motivated it."""
    root = tmp_path / "repo2"
    (root / "vendored" / "no_human").mkdir(parents=True)
    (root / "vendored" / "no_human" / "__init__.py").write_text("x\n")
    _git(tmp_path, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("only this is tracked\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "init")
    assert _detect(root / "vendored" / "no_human").sha is None


def test_no_checkout_falls_back_to_the_distribution_version(tmp_path, monkeypatch):
    monkeypatch.setattr("no_human.core.build_info._dist_version", lambda: "1.2.3")
    outside = tmp_path / "site-packages2" / "no_human"
    outside.mkdir(parents=True)
    info = _detect(outside)
    assert info.sha is None
    assert info.descriptor == "dist:1.2.3"
    # `dist:` must never be mistakable for a commit.
    assert not info.descriptor.startswith("git:")


def test_dirty_is_tri_state_not_collapsed_to_clean():
    """None means unknowable. Reporting it as False would present a wheel as
    a verified-clean checkout."""
    assert LoadedCode(dist_version="1.0").dirty is None


def test_the_tri_state_survives_into_the_descriptor():
    """The dataclass is not what anyone reads — `attempts.loaded_code_version`
    and the status line are, and both take the descriptor. Three states, three
    spellings, or the distinction dies exactly where it is needed."""
    sha = "a" * 40
    assert LoadedCode(sha=sha, dirty=False).descriptor == f"git:{sha}"
    assert LoadedCode(sha=sha, dirty=True).descriptor == f"git:{sha}+dirty"
    assert LoadedCode(sha=sha, dirty=None).descriptor == f"git:{sha}+dirty?"
    # The load-bearing assertion: unknown must not read as clean.
    assert (LoadedCode(sha=sha, dirty=None).descriptor
            != LoadedCode(sha=sha, dirty=False).descriptor)


def test_unreadable_index_attributes_nothing_at_all(repo):
    """A corrupt index takes the tracking check down with it, and tracking is
    a precondition for attribution — so this degrades all the way to unknown,
    not to a sha with unknown dirtiness. `rev-parse` still succeeds here; that
    is exactly the reading we must NOT act on."""
    assert _git(repo, "rev-parse", "HEAD")  # refs are fine
    (repo / ".git" / "index").write_bytes(b"corrupt")
    info = _detect(repo / "src" / "no_human")
    assert info.sha is None
    assert not info.descriptor.startswith("git:")


def test_unknowable_dirtiness_keeps_the_sha_but_says_so(repo, monkeypatch):
    """The `dirty=None` branch, reached the way it actually happens in
    production: tracking and HEAD resolve, and only `status` fails — it is the
    expensive call, and it is the one that hits the 10s timeout on a large
    repo under load. The sha is real; the cleanliness is not knowable."""
    import no_human.core.build_info as bi
    real = bi._git

    def only_status_fails(cwd, *args):
        return None if args[:1] == ("status",) else real(cwd, *args)

    monkeypatch.setattr(bi, "_git", only_status_fails)
    info = bi._detect(repo / "src" / "no_human")
    assert info.sha == real(repo, "rev-parse", "HEAD")
    assert info.dirty is None
    assert info.descriptor.endswith("+dirty?")


# --- the advisory staleness signal ----------------------------------------


def test_staleness_note_fires_when_loaded_sha_is_behind_head(repo):
    old = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("newer\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second")
    note = staleness_note(LoadedCode(sha=old, dirty=False), package_root=repo)
    assert note is not None and old[:8] in note


def test_staleness_note_silent_when_current(repo):
    head = _git(repo, "rev-parse", "HEAD")
    assert staleness_note(LoadedCode(sha=head, dirty=False), package_root=repo) is None


def test_staleness_note_silent_when_provenance_unknown(repo):
    assert staleness_note(LoadedCode(dist_version="1.0"), package_root=repo) is None


def test_staleness_note_silent_for_a_diverged_sha(repo):
    """A detached review checkout is not stale — it is elsewhere. A signal
    that fires on every worktree is one nobody reads."""
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text("s\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "side")
    side = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-")
    assert _git(repo, "rev-parse", "HEAD") == head
    assert staleness_note(LoadedCode(sha=side, dirty=False), package_root=repo) is None


def test_staleness_note_accepts_a_premeasured_head(repo, monkeypatch):
    """The status path must not run a second rev-parse after measuring HEAD."""
    loaded = "a" * 40
    head = "b" * 40

    def git_without_a_second_head_lookup(_cwd, *args):
        if args == ("rev-parse", "HEAD"):
            raise AssertionError("staleness_note re-measured HEAD")
        assert args == ("merge-base", "--is-ancestor", loaded, "HEAD")
        return ""

    monkeypatch.setattr("no_human.core.build_info._git", git_without_a_second_head_lookup)
    note = staleness_note(LoadedCode(sha=loaded, dirty=False), package_root=repo,
                          head=head)
    assert note is not None
    assert loaded[:8] in note
    assert head[:8] in note


# --- the record ------------------------------------------------------------


async def test_attempt_records_the_loaded_code_version(store):
    """The whole point: every attempt carries the sha of the code that
    produced its verdict, so an escalation can be re-judged afterwards."""
    from no_human.core.build_info import loaded_code

    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    attempt_id = await store.create_attempt(t.id, 1)

    rows = await store.list_attempts(t.id)
    assert [r["id"] for r in rows] == [attempt_id]
    assert rows[0]["loaded_code_version"] == loaded_code().descriptor
    assert rows[0]["loaded_code_version"]  # never blank — "unknown" at worst


def test_concurrent_staleness_checks_spawn_one_git_at_a_time(monkeypatch):
    """The degraded case is the dangerous one: git is slow exactly when the
    board is polling every ~2s, so a plain cache-miss check lets ~15 pollers
    each start their own git before the first answer lands."""
    # NOT `import no_human.api.app as api`: the api package's __init__
    # binds the name `app` to the FastAPI INSTANCE, which shadows the
    # submodule and silently hands back the wrong object.
    api = importlib.import_module("no_human.api.app")

    measurement_steps = []
    release = threading.Event()

    def slow_head():
        measurement_steps.append("head")
        release.wait(timeout=5)
        return "a" * 40

    def slow_note(_info, *, head):
        assert head == "a" * 40
        measurement_steps.append("note")
        return "behind"

    monkeypatch.setattr(api, "_stale_cache", None)
    monkeypatch.setattr("no_human.core.build_info.head_sha", slow_head)
    monkeypatch.setattr("no_human.core.build_info.staleness_note", slow_note)

    threads = [threading.Thread(target=api._loaded_code_stale) for _ in range(15)]
    for t in threads:
        t.start()
    # Give the losers time to take the fast path rather than queue behind it.
    time.sleep(0.2)
    assert measurement_steps == ["head"], (
        f"{len(measurement_steps)} concurrent git measurements")
    release.set()
    for t in threads:
        t.join(timeout=5)
    assert measurement_steps == ["head", "note"]


def test_a_loser_does_not_block_on_the_slow_measurement(monkeypatch):
    """Losers must return immediately. Parking them would swap a git herd for
    a thread-pool herd — these run under `asyncio.to_thread`, whose executor
    is small enough that 15 waiters would starve everything else."""
    # NOT `import no_human.api.app as api`: the api package's __init__
    # binds the name `app` to the FastAPI INSTANCE, which shadows the
    # submodule and silently hands back the wrong object.
    api = importlib.import_module("no_human.api.app")

    release = threading.Event()
    monkeypatch.setattr(api, "_stale_cache", None)
    monkeypatch.setattr("no_human.core.build_info.head_sha", lambda: "a" * 40)
    monkeypatch.setattr("no_human.core.build_info.staleness_note",
                        lambda *_args, **_kwargs: release.wait(timeout=5) or "behind")

    winner = threading.Thread(target=api._loaded_code_stale)
    winner.start()
    time.sleep(0.1)
    started = time.monotonic()
    got = api._loaded_code_stale()    # must not wait for the winner
    assert time.monotonic() - started < 0.5
    # WHAT it returns, not just how fast. With nothing cached yet a loser gets
    # None, which the board renders as no banner — indistinguishable from
    # "current". That is the intended trade: during the first cold miss the
    # honest choices are silence or a claim we have not finished checking, and
    # silence beats fabricating a staleness warning. Self-heals next poll.
    assert got is None
    release.set()
    winner.join(timeout=5)


def test_a_loser_serves_the_last_known_value_once_there_is_one(monkeypatch):
    """After the first measurement the trade has no downside: a loser returns
    the previous answer rather than silence."""
    # NOT `import no_human.api.app as api`: the api package's __init__
    # binds the name `app` to the FastAPI INSTANCE, which shadows the
    # submodule and hands back the wrong object.
    api = importlib.import_module("no_human.api.app")

    release = threading.Event()
    monkeypatch.setattr(api, "_stale_cache", ("deadbeef" * 5, "behind"))
    monkeypatch.setattr("no_human.core.build_info.head_sha", lambda: "a" * 40)
    monkeypatch.setattr("no_human.core.build_info.staleness_note",
                        lambda *_args, **_kwargs: release.wait(timeout=5) or "behind again")

    winner = threading.Thread(target=api._loaded_code_stale)
    winner.start()
    time.sleep(0.1)
    assert api._loaded_code_stale() == "behind"   # stale but not silent
    release.set()
    winner.join(timeout=5)


def test_the_cache_is_keyed_on_head_not_on_the_clock(monkeypatch):
    """A new HEAD invalidates a current answer without waiting for a TTL."""
    # NOT `import no_human.api.app as api`: the api package's __init__
    # binds the name `app` to the FastAPI INSTANCE, which shadows the
    # submodule and silently hands back the wrong object.
    api = importlib.import_module("no_human.api.app")

    old_head = "a" * 40
    new_head = "b" * 40
    monkeypatch.setattr(api, "_stale_cache", (old_head, None))
    monkeypatch.setattr("no_human.core.build_info.head_sha", lambda: new_head)
    monkeypatch.setattr("no_human.core.build_info.staleness_note",
                        lambda _info, *, head: "behind" if head == new_head else None)
    assert api._loaded_code_stale() == "behind"
    assert api._stale_cache == (new_head, "behind")


def test_an_unchanged_head_does_not_re_run_the_ancestry_check(monkeypatch):
    """The HEAD key retains the old TTL's protection from repeated merges."""
    api = importlib.import_module("no_human.api.app")
    head = "a" * 40
    monkeypatch.setattr(api, "_stale_cache", (head, "behind"))
    monkeypatch.setattr("no_human.core.build_info.head_sha", lambda: head)
    monkeypatch.setattr("no_human.core.build_info.staleness_note",
                        lambda *_args, **_kwargs: pytest.fail("ancestry re-ran"))
    assert api._loaded_code_stale() == "behind"


def test_an_unresolvable_head_reads_as_no_answer_not_as_current(monkeypatch):
    """A failed HEAD lookup stays silent and recomputes when git recovers."""
    api = importlib.import_module("no_human.api.app")
    recovered_head = "b" * 40
    heads = iter((None, recovered_head))
    notes = []

    monkeypatch.setattr(api, "_stale_cache", None)
    monkeypatch.setattr("no_human.core.build_info.head_sha", lambda: next(heads))
    monkeypatch.setattr("no_human.core.build_info.staleness_note",
                        lambda _info, *, head: notes.append(head) or "behind")
    assert api._loaded_code_stale() is None
    assert notes == []
    assert api._loaded_code_stale() == "behind"
    assert notes == [recovered_head]


def test_a_head_lookup_failure_does_not_erase_a_known_stale_verdict(monkeypatch):
    """A transient git hiccup (missing binary, non-repo, timeout) must not
    overwrite an established 'behind HEAD' verdict with (None, None) — that
    renders bit-for-bit identically to 'current' on a board that polls every
    10s. The prior answer must survive, in the return value AND the cache, so
    the next poll is not silent either."""
    api = importlib.import_module("no_human.api.app")
    seeded_head = "a" * 40
    seeded_note = (
        "loaded code deadbeef is behind HEAD aaaaaaaa — "
        "restart to pick up merged fixes"
    )
    monkeypatch.setattr(api, "_stale_cache", (seeded_head, seeded_note))
    monkeypatch.setattr("no_human.core.build_info.head_sha", lambda: None)
    monkeypatch.setattr(
        "no_human.core.build_info.staleness_note",
        lambda *a, **kw: pytest.fail("ancestry re-ran on an unmeasurable HEAD"))

    result = api._loaded_code_stale()

    assert result is not None
    assert result == seeded_note
    assert api._stale_cache == (seeded_head, seeded_note)


def test_the_retained_verdict_is_replaced_once_git_recovers(monkeypatch):
    """Retention is not sticky: once head_sha() succeeds again, the cache
    re-keys to the freshly measured HEAD and note."""
    api = importlib.import_module("no_human.api.app")
    seeded_head = "a" * 40
    recovered_head = "b" * 40
    heads = iter((None, recovered_head))

    monkeypatch.setattr(api, "_stale_cache", (seeded_head, "behind"))
    monkeypatch.setattr("no_human.core.build_info.head_sha", lambda: next(heads))
    monkeypatch.setattr(
        "no_human.core.build_info.staleness_note",
        lambda _info, *, head: "behind again" if head == recovered_head else None)

    assert api._loaded_code_stale() == "behind"
    assert api._stale_cache == (seeded_head, "behind")

    assert api._loaded_code_stale() == "behind again"
    assert api._stale_cache == (recovered_head, "behind again")


async def test_recorded_version_survives_later_updates(store):
    """update_attempt rewrites the mutable surface; the provenance stamp is
    written at creation and must not be lost by a later write."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    attempt_id = await store.create_attempt(t.id, 1)
    before = (await store.list_attempts(t.id))[0]["loaded_code_version"]
    await store.update_attempt(attempt_id, status="failed",
                               failure_reason="boom")
    assert (await store.list_attempts(t.id))[0]["loaded_code_version"] == before
