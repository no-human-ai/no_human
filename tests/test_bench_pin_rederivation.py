"""Tests for pin re-derivation on rewritten histories (north-star follow-up).

56 generated specs pin commits that no longer exist because `no_human`'s own
history was rewritten — `git cat-file -e <pin>^{commit}` fails for all of
them, and the publishability gate (<=20% unmeasured) can never pass while
they sit dead. `build_bench_tasks` (bench_task.py) is the ONLY place allowed
to repair this: given an existing spec whose recorded pin no longer resolves
in the resolved repo, it re-derives a pin from the spec's own recorded
`started` timestamp against the spec's own RECORDED branch — falling back to
the checkout's HEAD only when that branch is gone (reusing `_pin_for`'s
existing `branch or "HEAD"` rule; no new default-branch discovery, that is
explicitly human-gated). The real dead specs in this corpus record `master`
or `feat/phase1-cli-ui-a11y` as their branch, both long gone, so every one of
them takes the HEAD fallback in practice. A successful re-derivation records
`pin_rederived: true` and preserves the dead pin as `pin_original`. A spec
refuses by name (never guesses) when re-derivation cannot happen: unparsable
or missing `started` gets `PIN_START_UNPARSABLE`; a parsable `started` with
no commit before it gets `PIN_NOT_REDERIVABLE` — two distinct reasons so a
consumer can tell "this date is junk" from "this date is fine but nothing
existed yet". Every spec is rebuilt on every `build_bench_tasks` call, not
just ones the transcript loop can reach: a post-loop sweep (bench_task.py)
applies the same repair to specs the loop never visits, so a dead pin is
never silently skipped just because its transcript is gone. Nothing at RUN
time may ever rewrite a spec.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

from no_human.eval.bench_task import (
    PIN_NOT_REDERIVABLE,
    PIN_START_UNPARSABLE,
    build_bench_tasks,
    check_repo_map,
    load_bench_tasks,
    spec_pin_not_rederivable,
    spec_pin_rederived,
)

from no_human.history.extractor import Message, Transcript

from tests.test_bench_task import _commit, _git_repo, _transcript

_DEAD_PIN = "a" * 40  # never a real object in any repo this file creates


def _tid(t) -> str:
    import hashlib
    return f"ns-{hashlib.sha256(t.cascade_id.encode()).hexdigest()[:8]}"


def _write_existing_spec(out_dir: Path, tid: str, *, repo_path: str,
                          pin: str, branch: str = "main",
                          started: str = "") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{tid}.yaml"
    data = {
        "id": tid, "title": "t", "request": "Fix the login bug in auth.py",
        "source": ({"started": started} if started else {}),
        "repo": {"path": repo_path, "pin": pin, "branch": branch},
        "runnable": True, "skip_reason": "",
    }
    p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100))
    return p


def _rewritten_repo(tmp_path: Path) -> Path:
    """A repo shaped like a history rewrite victim: it HAS a commit before the
    fixture session's `started` (2026-07-01T10:00:00.000Z), but the pin that
    used to point at (or near) it is fabricated and matches no object here —
    exactly what `git cat-file -e <pin>^{commit}` sees after a rebase/
    filter-repo/force-push drops the commit the spec was originally cut
    against."""
    return _git_repo(tmp_path)  # base commit at 2026-06-01, before the session


# --------------------------- re-derivation --------------------------------- #

def test_unreachable_pin_is_rederived_by_date(tmp_path):
    repo = _rewritten_repo(tmp_path)
    t = _transcript(cwd=str(repo))
    out = tmp_path / "specs"
    tid = _tid(t)
    _write_existing_spec(out, tid, repo_path=str(repo), pin=_DEAD_PIN,
                         started=t.started)

    build_bench_tasks([t], out_dir=out)
    spec = load_bench_tasks(out)[0]

    assert spec.repo["pin_rederived"] is True
    assert spec.repo["pin_original"] == _DEAD_PIN
    assert spec.repo["pin"] != _DEAD_PIN
    assert spec_pin_rederived(spec) is True
    # The re-derived pin must actually resolve in the repo it was derived from.
    rc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{spec.repo['pin']}^{{commit}}"],
        capture_output=True).returncode
    assert rc == 0


def test_a_resolvable_pin_spec_is_byte_identical_after_rebuild(tmp_path):
    repo = _git_repo(tmp_path)
    t = _transcript(cwd=str(repo))
    out = tmp_path / "specs"

    (path,) = build_bench_tasks([t], out_dir=out)
    before = path.read_bytes()

    build_bench_tasks([t], out_dir=out)
    after = path.read_bytes()

    assert before == after


def test_a_changed_transcript_refreshes_the_original_block(tmp_path):
    """A spec's `original` block (tokens, corrections, wall-clock) is a
    GENERATED field, not frozen at first write — an edited/re-extracted
    transcript for the SAME session must refresh it on the next rebuild,
    not sit stale forever. This must fail on the append-only freeze."""
    repo = _git_repo(tmp_path)
    request = "Fix the login bug in auth.py"
    first_msgs = [
        Message(role="user", content=request, step_type="user"),
        Message(role="assistant", content="Done", step_type="assistant"),
    ]
    t1 = Transcript(
        cascade_id="cc:same-session", title=request[:80], created="2026-07-01",
        messages=first_msgs, usage={"input_tokens": 10, "output_tokens": 5,
                                    "cache_read_input_tokens": 0,
                                    "cache_creation_input_tokens": 0},
        started="2026-07-01T10:00:00.000Z", ended="2026-07-01T10:05:00.000Z",
        cwd=str(repo), git_branch="main", source="cc:personal", corrections=0,
    )
    out = tmp_path / "specs"
    build_bench_tasks([t1], out_dir=out)
    before = load_bench_tasks(out)[0]

    second_msgs = [
        Message(role="user", content=request, step_type="user"),
        Message(role="assistant", content="Done", step_type="assistant"),
        Message(role="user", content="also fix the session cookie", step_type="user"),
        Message(role="assistant", content="Done that too", step_type="assistant"),
    ]
    t2 = Transcript(
        cascade_id="cc:same-session", title=request[:80], created="2026-07-01",
        messages=second_msgs, usage={"input_tokens": 999999, "output_tokens": 50,
                                     "cache_read_input_tokens": 0,
                                     "cache_creation_input_tokens": 0},
        started="2026-07-01T10:00:00.000Z", ended="2026-07-01T11:00:00.000Z",
        cwd=str(repo), git_branch="main", source="cc:personal", corrections=1,
    )
    build_bench_tasks([t2], out_dir=out)
    after = load_bench_tasks(out)[0]

    assert before.original["tokens"]["input_tokens"] == 10
    assert after.original["tokens"]["input_tokens"] == 999999
    assert after.original["corrections"] == 1
    assert after.original["wall_clock_s"] != before.original["wall_clock_s"]


def test_rebuilding_a_rederived_spec_is_a_no_op(tmp_path):
    """Once a dead pin has been repaired, the NEXT build must converge: the
    freshly re-derived pin now resolves, so a second pass is a true no-op —
    never a second re-derivation, never a rewrite of the audit trail."""
    repo = _rewritten_repo(tmp_path)
    t = _transcript(cwd=str(repo))
    out = tmp_path / "specs"
    tid = _tid(t)
    _write_existing_spec(out, tid, repo_path=str(repo), pin=_DEAD_PIN,
                         started=t.started)

    build_bench_tasks([t], out_dir=out)
    p = out / f"{tid}.yaml"
    once = p.read_bytes()

    build_bench_tasks([t], out_dir=out)
    twice = p.read_bytes()

    assert once == twice


# ------------------------------ refusals ------------------------------------ #

def test_no_commit_before_started_skips_with_the_named_reason(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-b", "main"],
                 ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    # Every commit is AFTER the fixture session's started date (2026-07-01):
    # there is nothing `rev-list --before=<started>` can resolve.
    _commit(repo, "after", when="2026-08-01T00:00:00Z")

    t = _transcript(cwd=str(repo))
    out = tmp_path / "specs"
    tid = _tid(t)
    _write_existing_spec(out, tid, repo_path=str(repo), pin=_DEAD_PIN,
                         started=t.started)

    build_bench_tasks([t], out_dir=out)
    spec = load_bench_tasks(out)[0]

    assert spec.runnable is False
    assert spec.skip_reason == PIN_NOT_REDERIVABLE
    assert spec.repo["pin_original"] == _DEAD_PIN
    assert not spec.repo.get("pin_rederived")


def test_an_unparsable_started_is_refused_not_guessed(tmp_path):
    """`--before=<garbage>` silently reads as "now" in real git and would hand
    back today's tip disguised as a derived pin — this must be refused BEFORE
    ever reaching git, not guessed from whatever `--before=<garbage>` returns."""
    repo = _rewritten_repo(tmp_path)
    t = _transcript(cwd=str(repo))
    out = tmp_path / "specs"
    tid = _tid(t)
    _write_existing_spec(out, tid, repo_path=str(repo), pin=_DEAD_PIN,
                         started="not-a-date")

    build_bench_tasks([t], out_dir=out)
    spec = load_bench_tasks(out)[0]

    assert spec.runnable is False
    assert spec.skip_reason == PIN_START_UNPARSABLE
    assert not spec.repo.get("pin_rederived")
    # Refused, not guessed: the dead pin is preserved untouched, not replaced
    # by whatever `--before=not-a-date` would have resolved to.
    assert spec.repo["pin"] == _DEAD_PIN
    assert spec.repo["pin_original"] == _DEAD_PIN


def test_an_unverifiable_pin_probe_leaves_the_spec_untouched(tmp_path, monkeypatch):
    """A probe that cannot complete (no git, a cold-mount timeout) is
    FAIL-CLOSED: it must never be read as "unreachable" and trigger a rewrite
    of a pin that was never actually broken."""
    import no_human.eval.bench_task as bench_task_mod

    repo = _git_repo(tmp_path)
    t = _transcript(cwd=str(repo))
    out = tmp_path / "specs"
    tid = _tid(t)
    # A pin that WOULD resolve if actually probed — proves the untouched
    # outcome comes from the probe being unverifiable, not from the pin
    # genuinely being unreachable.
    real_pin = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()
    _write_existing_spec(out, tid, repo_path=str(repo), pin=real_pin,
                         started=t.started)

    monkeypatch.setattr(bench_task_mod, "_pin_reachable", lambda *a, **k: None)
    build_bench_tasks([t], out_dir=out)
    spec = load_bench_tasks(out)[0]

    # The pin decision itself is untouched — an unverifiable probe must
    # never be read as "unreachable" and trigger a rewrite. (Other generated
    # fields, e.g. `original`, legitimately refresh on every rebuild — that
    # is not what this test is pinning down.)
    assert spec.repo["pin"] == real_pin
    assert not spec.repo.get("pin_rederived")
    assert spec.runnable is True
    assert spec.skip_reason == ""


def test_a_branch_verify_timeout_leaves_the_spec_untouched(tmp_path, monkeypatch):
    """A transient failure verifying whether the spec's recorded branch still
    exists (cold mount, slow disk) must never be read as "branch confirmed
    missing" and silently re-derive against HEAD instead — the same
    fail-closed contract as the pin-reachability probe itself. Before the
    fix, `_rederive_pin` caught the timeout, treated it as rc=1 ("branch
    missing"), fell back to HEAD, and wrote a "successful" re-derivation."""
    import no_human.eval.bench_task as bench_task_mod

    repo = _rewritten_repo(tmp_path)
    t = _transcript(cwd=str(repo), branch="main")
    out = tmp_path / "specs"
    tid = _tid(t)
    _write_existing_spec(out, tid, repo_path=str(repo), pin=_DEAD_PIN,
                         branch="main", started=t.started)

    real_run = subprocess.run

    def flaky_run(cmd, *args, **kwargs):
        if "rev-parse" in cmd and "--verify" in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(bench_task_mod.subprocess, "run", flaky_run)
    build_bench_tasks([t], out_dir=out)
    spec = load_bench_tasks(out)[0]

    # The pin decision itself is untouched — a transient branch-verify
    # failure must never be read as "branch confirmed missing" and silently
    # re-derive against HEAD instead.
    assert spec.repo["pin"] == _DEAD_PIN
    assert not spec.repo.get("pin_rederived")


def test_the_two_refusal_reasons_are_distinct(tmp_path):
    """`PIN_NOT_REDERIVABLE` (a parsable `started` with nothing before it) and
    `PIN_START_UNPARSABLE` (no usable `started` at all) are two different
    named constants — a consumer that wants "was this refused for a pin
    reason at all" uses `spec_pin_not_rederivable`/`PIN_REFUSAL_REASONS`
    rather than enumerating both strings itself."""
    assert PIN_NOT_REDERIVABLE != PIN_START_UNPARSABLE
    from no_human.eval.bench_task import PIN_REFUSAL_REASONS
    assert PIN_REFUSAL_REASONS == frozenset({PIN_NOT_REDERIVABLE, PIN_START_UNPARSABLE})

    repo = _rewritten_repo(tmp_path)
    t = _transcript(cwd=str(repo))
    out = tmp_path / "specs"
    tid = _tid(t)
    _write_existing_spec(out, tid, repo_path=str(repo), pin=_DEAD_PIN,
                         started="not-a-date")

    build_bench_tasks([t], out_dir=out)
    spec = load_bench_tasks(out)[0]

    assert spec.skip_reason == PIN_START_UNPARSABLE
    assert spec.skip_reason != PIN_NOT_REDERIVABLE
    assert spec_pin_not_rederivable(spec) is True


def test_a_successful_rederivation_clears_a_stale_refusal_stamp(tmp_path):
    """A spec stamped refused in a PRIOR build must not stay stuck once its
    pin becomes re-derivable (e.g. the repo gained the commit that makes
    re-derivation possible) — the next build clears the stale stamp rather
    than leaving `runnable: false` behind forever."""
    repo = _rewritten_repo(tmp_path)
    t = _transcript(cwd=str(repo))
    out = tmp_path / "specs"
    tid = _tid(t)
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{tid}.yaml"
    p.write_text(yaml.safe_dump({
        "id": tid, "title": "t", "request": "Fix the login bug in auth.py",
        "source": {"started": t.started},
        "repo": {"path": str(repo), "pin": _DEAD_PIN, "branch": "main"},
        "runnable": False, "skip_reason": PIN_NOT_REDERIVABLE,
    }, sort_keys=False, allow_unicode=True, width=100))

    build_bench_tasks([t], out_dir=out)
    spec = load_bench_tasks(out)[0]

    assert spec.runnable is True
    assert spec.skip_reason == ""
    assert spec.repo["pin_rederived"] is True

    # An UNRELATED refusal reason (not a pin refusal at all) must never be
    # cleared just because the pin turns out to be REACHABLE — the "clear a
    # stale stamp" guard only fires for a stamp that was actually one of the
    # two named pin-refusal reasons.
    (tmp_path / "sub2").mkdir()
    repo2 = _git_repo(tmp_path / "sub2")
    t2 = _transcript(cascade_id="cc:unrelated", request="some other request entirely",
                     cwd=str(repo2))
    real_pin2 = subprocess.run(
        ["git", "-C", str(repo2), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()
    tid2 = _tid(t2)
    out.mkdir(parents=True, exist_ok=True)
    p2 = out / f"{tid2}.yaml"
    p2.write_text(yaml.safe_dump({
        "id": tid2, "title": "t", "request": "some other request entirely",
        "source": {"started": t2.started},
        "repo": {"path": str(repo2), "pin": real_pin2, "branch": "main"},
        "runnable": False, "skip_reason": "curator: flagged as flaky",
    }, sort_keys=False, allow_unicode=True, width=100))

    build_bench_tasks([t2], out_dir=out)
    specs_by_id = {s.id: s for s in load_bench_tasks(out)}
    assert specs_by_id[tid2].runnable is False
    assert specs_by_id[tid2].skip_reason == "curator: flagged as flaky"


# ------------------------------ provenance ---------------------------------- #

def test_the_builder_records_the_session_start(tmp_path):
    """The corpus is the record: once a repair happens, the spec's own
    `source.started` must be populated from the transcript if the existing
    spec did not already carry one — so the NEXT rebuild re-derives from a
    date recorded on the spec itself, never a re-extracted one that could
    drift out from under an audit trail."""
    repo = _rewritten_repo(tmp_path)
    t = _transcript(cwd=str(repo))
    out = tmp_path / "specs"
    tid = _tid(t)
    _write_existing_spec(out, tid, repo_path=str(repo), pin=_DEAD_PIN,
                         started="")  # no started recorded yet

    build_bench_tasks([t], out_dir=out)
    spec = load_bench_tasks(out)[0]

    assert spec.source.get("started") == t.started
    assert spec.repo["pin_rederived"] is True


# ------------------------------ run time ------------------------------------ #

def test_run_time_never_rewrites_a_spec(tmp_path):
    """`check_repo_map` is the run-time probe (mirrors `_pin_reachable`'s own
    `git cat-file -e` check). It must only ever REPORT a dead pin, never
    repair it — repair is `bench build`'s job alone."""
    repo = _git_repo(tmp_path)
    t = _transcript(cwd=str(repo))
    out = tmp_path / "specs"
    tid = _tid(t)
    p = _write_existing_spec(out, tid, repo_path=str(repo), pin=_DEAD_PIN,
                             started=t.started)
    before = p.read_bytes()

    specs = load_bench_tasks(out)
    problems = check_repo_map(specs)

    assert p.read_bytes() == before  # untouched by the run-time path
    assert any(_DEAD_PIN[:12] in problem for problem in problems)


# -------------------------------- sweep -------------------------------------- #
# A spec whose source transcript the loop above never visits (aged out of the
# history export, deleted, rotated) still sits on disk forever unless
# something revisits it — the post-loop sweep in `build_bench_tasks` is that
# something. These specs are written directly (no matching transcript passed
# to `build_bench_tasks`), simulating exactly that "dead spec, no source"
# shape.

def test_a_dead_spec_the_transcript_loop_never_visits_is_stamped(tmp_path):
    repo = _git_repo(tmp_path)
    # The transcript loop visits ONLY this one — the dead spec below has no
    # matching transcript at all.
    t = _transcript(cwd=str(repo))
    out = tmp_path / "specs"
    _write_existing_spec(out, _tid(t), repo_path=str(repo), pin="deadbeef" * 5,
                         started=t.started)

    (tmp_path / "sub1").mkdir()
    dead_repo = _rewritten_repo(tmp_path / "sub1")
    dead = _write_existing_spec(out, "ns-orphaned", repo_path=str(dead_repo),
                                pin=_DEAD_PIN, started="")  # no source.started

    build_bench_tasks([t], out_dir=out)
    loaded = yaml.safe_load(dead.read_text(encoding="utf-8"))

    assert loaded["runnable"] is False
    assert loaded["skip_reason"] == PIN_START_UNPARSABLE
    assert loaded["repo"]["pin_original"] == _DEAD_PIN
    assert loaded["repo"]["pin"] == _DEAD_PIN


def test_the_sweep_leaves_a_reachable_unvisited_spec_byte_identical(tmp_path):
    """Positive control: a spec the loop never visits, but whose recorded pin
    STILL resolves, must come out of the sweep byte-identical — proving the
    dead-spec case above is about the pin being unreachable, not merely
    unvisited."""
    repo = _git_repo(tmp_path)
    t = _transcript(cwd=str(repo))
    out = tmp_path / "specs"
    _write_existing_spec(out, _tid(t), repo_path=str(repo), pin="deadbeef" * 5,
                         started=t.started)

    (tmp_path / "sub1").mkdir()
    reachable_repo = _git_repo(tmp_path / "sub1")
    real_pin = subprocess.run(
        ["git", "-C", str(reachable_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()
    unvisited = _write_existing_spec(out, "ns-unvisited", repo_path=str(reachable_repo),
                                     pin=real_pin, started=t.started)
    before = unvisited.read_bytes()

    build_bench_tasks([t], out_dir=out)

    assert unvisited.read_bytes() == before


def test_the_sweep_leaves_an_unverifiable_probe_untouched(tmp_path, monkeypatch):
    import no_human.eval.bench_task as bench_task_mod

    repo = _git_repo(tmp_path)
    t = _transcript(cwd=str(repo))
    out = tmp_path / "specs"
    _write_existing_spec(out, _tid(t), repo_path=str(repo), pin="deadbeef" * 5,
                         started=t.started)

    (tmp_path / "sub1").mkdir()
    other_repo = _git_repo(tmp_path / "sub1")
    real_pin = subprocess.run(
        ["git", "-C", str(other_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()
    unvisited = _write_existing_spec(out, "ns-flaky-probe", repo_path=str(other_repo),
                                     pin=real_pin, started=t.started)
    before = unvisited.read_bytes()

    monkeypatch.setattr(bench_task_mod, "_pin_reachable", lambda *a, **k: None)
    build_bench_tasks([t], out_dir=out)

    assert unvisited.read_bytes() == before


def test_the_sweep_leaves_a_spec_whose_repo_is_gone_untouched(tmp_path):
    repo = _git_repo(tmp_path)
    t = _transcript(cwd=str(repo))
    out = tmp_path / "specs"
    _write_existing_spec(out, _tid(t), repo_path=str(repo), pin="deadbeef" * 5,
                         started=t.started)

    unvisited = _write_existing_spec(out, "ns-gone", repo_path="/nonexistent/gone",
                                     pin=_DEAD_PIN, started=t.started)
    before = unvisited.read_bytes()

    build_bench_tasks([t], out_dir=out)

    assert unvisited.read_bytes() == before


def test_the_sweep_is_idempotent(tmp_path):
    """A dead spec that stays refused (no repair possible) must sweep to the
    SAME bytes every time, not just once — no drift across repeated builds."""
    repo = _git_repo(tmp_path)
    t = _transcript(cwd=str(repo))
    out = tmp_path / "specs"
    _write_existing_spec(out, _tid(t), repo_path=str(repo), pin="deadbeef" * 5,
                         started=t.started)

    dead_repo = tmp_path / "dead_repo2"
    dead_repo.mkdir()
    for args in (["init", "-b", "main"],
                 ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=dead_repo, check=True, capture_output=True)
    (dead_repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=dead_repo, check=True, capture_output=True)
    # Every commit is AFTER the started date below — never re-derivable.
    _commit(dead_repo, "after", when="2026-08-01T00:00:00Z")
    dead = _write_existing_spec(out, "ns-orphaned2", repo_path=str(dead_repo),
                                pin=_DEAD_PIN, started="2026-07-01T10:00:00.000Z")

    build_bench_tasks([t], out_dir=out)
    once = dead.read_bytes()
    once_loaded = yaml.safe_load(once)
    assert once_loaded["skip_reason"] == PIN_NOT_REDERIVABLE

    build_bench_tasks([t], out_dir=out)
    twice = dead.read_bytes()

    assert once == twice


def test_bench_build_summary_reports_repaired_and_not_rederivable(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from no_human.cli.commands import cli
    from no_human.history import extractor

    def _no_ide(**_kw):
        raise extractor.IDENotRunningError("no IDE")
    monkeypatch.setattr("no_human.history.extractor.extract_transcripts", _no_ide)

    repo = _rewritten_repo(tmp_path)
    t = _transcript(cwd=str(repo))
    monkeypatch.setattr(
        "no_human.history.claude_code.extract_claude_code_transcripts",
        lambda **kw: [t])

    out = tmp_path / "specs"
    _write_existing_spec(out, _tid(t), repo_path=str(repo), pin=_DEAD_PIN,
                         started=t.started)  # re-derivable — will be repaired
    _write_existing_spec(out, "ns-not-rederivable", repo_path=str(repo),
                         pin=_DEAD_PIN, started="")  # unparsable — not repaired

    result = CliRunner().invoke(
        cli, ["bench", "build", "--out", str(out)])

    assert result.exit_code == 0, result.output
    assert re.search(r"\d+\s+pin\(s\) re-derived", result.output)
    assert re.search(r"\d+\s+not re-derivable", result.output)
