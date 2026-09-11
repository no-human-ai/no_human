"""The nightly funnel corpus (Phase C, task C2) and its non-vacuity controls.

Two controls, the same pair `eval/parcelo_corpus/build.py` enforces on the
bench corpus, because a held-out test with neither measures nothing:

  known-NEGATIVE  every tier's holdout must FAIL on the pristine repo. That is
                  asserted here, per tier, by RUNNING it. A holdout that is
                  already green at base makes the tier a free pass forever and
                  nobody would notice.
  suite-green     every tier's own shipped suite must PASS at base, or a red
                  night says nothing about the agent.

The known-POSITIVE control (a reference solution turning the holdout green)
belongs to the real run, not here: these tiers are driven by the agent, and a
reference solution checked in beside the ticket is a solution the agent could
read.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from no_human.eval.funnel_corpus import CORPUS_DIR, load_corpus

EXPECTED = {
    "t1_docs_oneliner": (400_000, 900),
    "t2_small_fix": (1_500_000, 1_800),
    "t3_small_feature": (3_000_000, 2_700),
    "t4_cross_file": (4_000_000, 3_600),
    "t5_test_first": (2_000_000, 2_400),
}


def test_the_corpus_is_the_five_agreed_tiers_at_the_agreed_ceilings():
    tasks = load_corpus()
    assert [t.name for t in tasks] == sorted(EXPECTED)
    for t in tasks:
        tokens, wall = EXPECTED[t.name]
        assert (t.criteria.max_weighted_tokens, t.criteria.max_wall_seconds) \
            == (tokens, wall), t.name


def test_every_ticket_is_inline_complete():
    """A task sees a fresh checkout and the ticket text — nothing else. A
    ticket that gestures at context the agent cannot reach is the single
    biggest predictor of a doom-loop, so each one must name its file and its
    exact anchor."""
    for t in load_corpus():
        assert t.ticket["title"].strip(), t.name
        assert len(t.ticket["description"]) > 200, f"{t.name}: too thin"
        assert t.ticket["acceptance_criteria"], t.name
        # The anchor: every ticket names at least one file that really exists
        # in its own fixture.
        named = [p for p in (t.repo_fixture.rglob("*"))
                 if p.is_file() and str(p.relative_to(t.repo_fixture))
                 in t.ticket["description"]]
        assert named, f"{t.name}: description names no file in its own repo/"


def test_every_target_file_is_under_a_thousand_lines():
    for path in CORPUS_DIR.rglob("*"):
        if path.is_file() and path.suffix in (".py", ".md", ".json"):
            n = len(path.read_text(encoding="utf-8").splitlines())
            assert n < 1000, f"{path}: {n} lines"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_fixture_suite_is_green_at_base(name, tmp_path):
    task = {t.name: t for t in load_corpus()}[name]
    repo = task.materialize(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests"],
        cwd=repo, capture_output=True, text=True,
        env={"PYTHONPATH": str(repo), "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0, proc.stdout[-2000:]


@pytest.mark.parametrize("name", sorted(n for n in EXPECTED if n != "t1_docs_oneliner"))
def test_each_holdout_is_red_at_base(name, tmp_path):
    """THE non-vacuity control. If this ever goes green the tier stops
    measuring anything and must be fixed, never skipped."""
    task = {t.name: t for t in load_corpus()}[name]
    repo = task.materialize(tmp_path)
    assert task.holdout_cmd, f"{name} must hold a test out"
    proc = subprocess.run(
        task.holdout_cmd, cwd=repo, capture_output=True, text=True,
        env={"PYTHONPATH": str(repo), "PATH": "/usr/bin:/bin"})
    assert proc.returncode != 0, (
        f"{name}: the held-out test ALREADY passes on the untouched repo — "
        f"this tier measures nothing:\n{proc.stdout[-2000:]}")


def test_the_docs_tier_holds_nothing_out_and_says_so():
    """t1 changes prose. There is no behaviour to hold out, so its criteria
    must not demand a green holdout — that would make it unpassable."""
    t1 = {t.name: t for t in load_corpus()}["t1_docs_oneliner"]
    assert t1.holdout_cmd == []
    assert t1.criteria.holdout_green is False


def test_materialize_gives_the_agent_a_local_bare_origin_it_cannot_escape():
    """Same push-proofing as `eval.harness.run_shadow` and
    `eval.northstar._setup_sandbox`: origin is a bare repo inside the run's own
    workdir, so a coder's branch push can never reach anything real."""
    import tempfile
    from pathlib import Path

    dest = Path(tempfile.mkdtemp(prefix="nh-funnel-test-"))
    repo = load_corpus()[0].materialize(dest)
    origin = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo,
                            capture_output=True, text=True).stdout.strip()
    assert Path(origin).resolve().is_relative_to(dest.resolve()), origin
    assert (Path(origin) / "HEAD").exists(), "origin must be a real bare repo"


def test_materialize_is_rerunnable_and_does_not_merge_the_previous_run(tmp_path):
    """A second nightly run against the SAME home must not crash, and must not
    inherit night 1's tree. Before the fix `copytree` raised FileExistsError on
    every tier, so the launchd nightly job succeeded once and crashed forever
    after; merging instead of replacing would be worse — stale residue in the
    fixture contaminates the measurement.
    """
    task = load_corpus()[0]
    first = task.materialize(tmp_path)
    stale = first / "LEFTOVER_FROM_NIGHT_1.txt"
    stale.write_text("residue")
    # ...and the coder branch night 1 pushed to the tier's bare origin. If that
    # survives, night 2's agent can fetch last night's solution.
    subprocess.run(["git", "push", "origin", "HEAD:refs/heads/nh/night-1"],
                   cwd=first, check=True, capture_output=True)

    second = task.materialize(tmp_path)

    assert second == first
    assert not stale.exists(), "re-materialize merged night 1's residue"
    assert (second / ".git").is_dir()
    origin = subprocess.run(["git", "remote", "get-url", "origin"], cwd=second,
                            capture_output=True, text=True).stdout.strip()
    assert Path(origin).resolve().is_relative_to(tmp_path.resolve()), origin
    heads = subprocess.run(["git", "branch", "--format=%(refname:short)"],
                           cwd=origin, capture_output=True,
                           text=True).stdout.split()
    assert heads == ["main"], f"night 1's branches survived in origin: {heads}"


def test_no_fixture_quotes_this_project_or_a_real_employer_subject():
    """The fixtures are invented toys. Nothing here may derive from real
    employer content or from no_human's own source — a corpus that quotes the
    subject under test is a leak channel and an instrument that cheats."""
    for path in CORPUS_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "no_human" not in text, path
        assert "from no_human" not in text, path


def test_no_fixture_TEXT_quotes_this_project_or_a_real_employer_subject():
    """Widened from *.py after the 2026-08-10 review: the tickets, the criteria
    and the fixture README are shipped bytes too, and a scan that only ever
    opened the Python files was evidence about one extension, not about the
    corpus. Clean today — this pins it."""
    for path in CORPUS_DIR.rglob("*"):
        if not path.is_file() or path.suffix not in (".py", ".json", ".md"):
            continue
        text = path.read_text(encoding="utf-8")
        assert "no_human" not in text, path
        assert "eyal" not in text.lower(), path
