"""Tests for the SCRUM-29 reviewer-recall runner (eval/reviewer_recall/runner.py).

The runner lives outside src/no_human (single surface: eval/ CLI python
only — docs/REVIEWER_RECALL_METHOD.md), so it is loaded here by file path,
the same way the CLI wiring (`nh bench report --reviewer-recall`) loads it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "eval" / "reviewer_recall" / "runner.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "test_reviewer_recall_runner_module", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rr = _load_runner()


def _truth(**overrides):
    base = {
        "class": "logic", "file": "app.py", "hunk_lines": [10, 20],
        "description": "off-by-one", "keywords": ["off-by-one", "boundary"],
        "planted_by": "supervising-session", "date": "2026-07-25",
    }
    base.update(overrides)
    return base


def _case(**truth_overrides) -> "rr.CaseSpec":
    return rr.CaseSpec(
        case_id="synthetic-case", dir=Path("/dev/null"), base_ref="deadbeef",
        diff_text="diff --git a/app.py b/app.py\n", truth=_truth(**truth_overrides),
    )


def _control_case() -> "rr.CaseSpec":
    return rr.CaseSpec(
        case_id="synthetic-control", dir=Path("/dev/null"), base_ref="deadbeef",
        diff_text="diff --git a/app.py b/app.py\n",
        truth={"class": "control", "file": None, "hunk_lines": None,
               "description": "clean", "keywords": [],
               "planted_by": "supervising-session", "date": "2026-07-25"},
    )


# --- score_case: the six required stubbed-reviewer scenarios ---------------

def test_caught_case():
    case = _case()
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="app.py", line=15,
                  text="off-by-one at the boundary check", blocking=True),
    ])
    result = rr.score_case(case, outcome)
    assert result.caught is True
    assert result.clean_pass is None


def test_missed_wrong_file():
    case = _case()
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="other.py", line=15,
                  text="off-by-one at the boundary check", blocking=True),
    ])
    result = rr.score_case(case, outcome)
    assert result.caught is False
    assert "planted file" in result.reason


def test_missed_right_file_wrong_lines():
    case = _case()  # hunk_lines [10, 20] -> in-range is [7, 23]
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="app.py", line=99,
                  text="off-by-one at the boundary check", blocking=True),
    ])
    result = rr.score_case(case, outcome)
    assert result.caught is False
    assert "hunk_lines" in result.reason


def test_keyword_miss():
    case = _case()  # keywords: off-by-one, boundary
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="app.py", line=15,
                  text="this code could use a docstring", blocking=True),
    ])
    result = rr.score_case(case, outcome)
    assert result.caught is False
    assert "keyword" in result.reason


def test_control_clean_pass():
    case = _control_case()
    outcome = rr.ReviewOutcome(status="PASS", findings=[])
    result = rr.score_case(case, outcome)
    assert result.clean_pass is True
    assert result.caught is None


def test_control_false_alarm():
    case = _control_case()
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="app.py", line=1, text="looks suspicious", blocking=True),
    ])
    result = rr.score_case(case, outcome)
    assert result.clean_pass is False
    assert "false alarm" in result.reason


# --- additional scoring edges -----------------------------------------------

def test_control_fail_only_non_blocking_is_clean_pass():
    case = _control_case()
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="app.py", line=1, text="nit: naming", blocking=False),
    ])
    result = rr.score_case(case, outcome)
    assert result.clean_pass is True


def test_pass_verdict_is_missed_even_with_stray_finding_text():
    case = _case()
    outcome = rr.ReviewOutcome(status="PASS", findings=[])
    result = rr.score_case(case, outcome)
    assert result.caught is False


# --- render_report: denominators, never a bare percentage -------------------

def test_render_report_format():
    seeded_hit = rr.CaseResult(case_id="a", cls="logic", is_control=False,
                               outcome=rr.ReviewOutcome(status="FAIL"), caught=True)
    seeded_miss = rr.CaseResult(case_id="b", cls="security", is_control=False,
                                outcome=rr.ReviewOutcome(status="PASS"), caught=False)
    control_ok = rr.CaseResult(case_id="c", cls="control", is_control=True,
                               outcome=rr.ReviewOutcome(status="PASS"), clean_pass=True)
    report = rr.RecallReport(
        results=[seeded_hit, seeded_miss, control_ok],
        model="claude-opus-5", run_date="2026-07-25",
    )
    text = rr.render_report(report)
    assert "reviewer recall: 1/2" in text
    assert "logic 1/1" in text and "security 0/1" in text
    assert "specificity:     1/1 clean diffs passed" in text
    assert "model: claude-opus-5" in text
    assert "run date: 2026-07-25" in text
    assert "docs/REVIEWER_RECALL_METHOD.md" in text
    # never a bare percentage: the "(NN%)" always follows an "x/y" denominator
    assert "reviewer recall: 1/2 (50%)" in text


# --- corpus + checkout/apply wiring (real corpus, no LLM calls) -------------

def test_load_cases_real_corpus():
    cases = rr.load_cases()
    assert len(cases) >= 9
    ids = {c.case_id for c in cases}
    assert "logic-stale-renders-fresh" in ids
    assert "control-bench-baseline" in ids
    classes = {c.truth["class"] for c in cases}
    assert classes == {"logic", "security", "spec-miss", "test-tamper",
                       "wiring", "control"}
    # 2026-08-07 expansion: the wiring class (>=3, two recorded-replay cuts +
    # one recorded dogfood cut) and controls 4 -> 10, including >=2
    # benign-unwired controls whose request.txt itself asks for an uncalled
    # artifact — the canary shape that keeps "unreached new code" from ever
    # hardening into "always blocks".
    wiring = [c for c in cases if c.truth["class"] == "wiring"]
    controls = [c for c in cases if c.is_control]
    assert len(wiring) >= 3
    assert len(controls) == 10
    assert sum(1 for c in controls if c.request) >= 2
    for c in wiring:
        assert c.truth.get("caller_file"), (
            f"{c.case_id}: a wiring case scores via an entry_point citing the "
            "production caller, so truth.json must name caller_file")
        assert c.request, (
            f"{c.case_id}: goal reachability is judged against the ticket's "
            "outcome, so a wiring case must ship request.txt")


def test_prepare_case_repo_has_no_descendant_history(tmp_path):
    case = next(c for c in rr.load_cases() if c.case_id == "logic-stale-renders-fresh")
    case_repo = rr.prepare_case_repo(REPO_ROOT, case, tmp_path)
    log = subprocess.run(["git", "rev-list", "--all"], cwd=case_repo,
                         check=True, capture_output=True, text=True)
    commit_shas = log.stdout.split()
    assert len(commit_shas) == 1, "checkout must carry no ancestor/descendant history"
    # the diff actually applied
    diff_status = subprocess.run(["git", "status", "--porcelain"], cwd=case_repo,
                                 check=True, capture_output=True, text=True)
    assert diff_status.stdout.strip() != ""


def test_every_case_prepares_with_no_repo_history_at_all(tmp_path):
    """The corpus must survive the open-source export: a fresh `git init` with
    one commit and none of the objects `base.ref` names.

    This does not simulate that with a flag — it points `repo_root` at a
    directory that is not a git repository at all, so any surviving read of
    repo history (git archive / rev-parse / cat-file) fails loudly.
    """
    not_a_repo = tmp_path / "no-git-here"
    not_a_repo.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    prepared, errors = [], []
    for case in rr.load_cases():
        try:
            case_repo = rr.prepare_case_repo(not_a_repo, case, work)
        except Exception as exc:  # noqa: BLE001 — the point is to report all
            errors.append(f"{case.case_id}: {type(exc).__name__}: {exc}")
            continue
        prepared.append(case.case_id)
        shas = subprocess.run(["git", "rev-list", "--all"], cwd=case_repo,
                              check=True, capture_output=True, text=True)
        assert len(shas.stdout.split()) == 1, case.case_id
        planted = case.truth.get("file")
        if planted:
            assert (case_repo / planted).is_file(), case.case_id
    assert errors == [], errors
    assert len(prepared) == len(rr.load_cases())


def _blob_sha1(data: bytes) -> str:
    """git's blob object id, computed here rather than imported from
    `materialize_base` — a check that calls the extractor's own helper would
    agree with it by construction."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _load_materialize_base():
    """`materialize_base.py`, loaded by path exactly as `rr` is above.

    Its `scrub` IS imported rather than reimplemented, and the difference from
    `_blob_sha1` is the point. A hash has a published definition, so a second
    implementation is an independent witness. The substitution list does not:
    its whole content is "what the operator decided to replace", and a copy here
    would be one more hand-maintained list, free to drift from the materialiser
    and let a fixture pass against rules the materialiser no longer has. The
    claim under test is that the fixtures on disk are what the materialiser
    produces, so the materialiser has to be the reference.
    """
    spec = importlib.util.spec_from_file_location(
        "test_rr_materialize_base",
        REPO_ROOT / "eval" / "reviewer_recall" / "materialize_base.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_materialize_base = _load_materialize_base()
_scrub = _materialize_base.scrub
_SCRUBBED_MARKER = _materialize_base.SCRUBBED_MARKER

# A run of hex long enough for `git rev-parse` to resolve. git's floor for an
# abbreviated object id is 4, so that is the floor here: an independent review
# verified that a FOUR-character prefix of one of the origin blobs this test
# exists to keep out resolves in a real clone. The working prefix is not written
# here — a comment explaining the guard must not itself be a pointer, and the
# earlier draft of this comment shipped one. Use `ffff` if a literal is wanted
# for shape; it resolved to nothing when checked. A 7 here (this repo's
# `core.abbrev`) would have been a threshold set below its own stated rule and
# above the real one.
#
# BOTH CASES. `git cat-file -t` resolves an uppercase or mixed-case object id
# exactly as it resolves a lowercase one (verified 2026-08-02 on a real clone,
# full id and 7-hex abbreviation, both cases), so a `[0-9a-f]`-only class let an
# uppercase origin id sit in column 3 and pass. `[0-9a-fA-F]` closes that.
#
# Anchored on word boundaries, which is load-bearing in both directions: without
# them `[0-9a-f]{4,}` matches `bbed` inside the marker `scrubbed` and the test
# fails on clean manifests. (An earlier version of this comment said `cbbed`;
# `'scrubbed'.find('cbbed')` is -1 — the run that actually matches is `bbed`.
# The threshold was right, the stated reason was not.) With them, a path
# component that happens to be four hex letters (`beef`) would false-positive —
# deliberately tolerated, because a spurious failure is read by a human and a
# missed object id is not.
#
# The anchors also mean word-ADJACENT forms (`_ffff…`, `blob_ffff…`) are not
# matched. Left uncovered on purpose: neither resolves in git as written
# (`git cat-file -t _ffff…` → "Not a valid object name"), so neither is a
# working index, and dropping the anchors to catch them reintroduces the
# `scrubbed` false positive the anchors exist to prevent. The guard covers what
# git will actually dereference; the docstring below says exactly that.
_OBJECT_ID_RUN = re.compile(r"\b[0-9a-fA-F]{4,}\b")


def _read_manifest(case) -> dict[str, tuple[str, str | None]]:
    """`{path: (column-1 blob id, column-3 marker or None)}` for one case."""
    manifest: dict[str, tuple[str, str | None]] = {}
    for line in (case.dir / "base.manifest").read_text(encoding="utf-8").splitlines():
        fields = line.split("  ")
        assert len(fields) in (2, 3), f"{case.case_id}: bad manifest line: {line!r}"
        manifest[fields[1]] = (fields[0], fields[2] if len(fields) == 3 else None)
    return manifest


def _cases_whose_base_ref_is_missing() -> list[str]:
    """INTERNAL case ids whose `base.ref` commit this checkout cannot resolve.

    External-base cases (recorded-replay cuts, `truth.json:
    external_base_ref`) are excluded by definition: their base.ref names a
    replay scratch repo's commit, unresolvable in every checkout of this
    repository — permanently, not as a gc accident.
    """
    missing = []
    for case in rr.load_cases():
        if case.truth.get("external_base_ref"):
            continue
        resolved = subprocess.run(
            ["git", "cat-file", "-e", f"{case.base_ref}^{{commit}}"],
            cwd=REPO_ROOT, capture_output=True,
        ).returncode == 0
        if not resolved:
            missing.append(case.case_id)
    return missing


def test_prepared_case_repo_matches_the_pinned_base_content():
    """Materialising must not have altered what the case measures: every file
    under `base/` has to still be the bytes the manifest pins.

    This runs ALWAYS, including after the export. Column 1 of `base.manifest` is
    the git blob id of the file ON DISK, so the byte-identity pin survives a repo
    where `base.ref` resolves nothing — which is the environment this whole
    change exists to serve, and exactly where a skip would have switched it off.

    Provenance — that the bytes are their pre-scrub original put through
    `scrub()` — needs the history, so it lives in
    `test_scrubbed_fixtures_are_their_origin_blob_put_through_scrub`, which
    SKIPS with a reason where the history is absent instead of quietly
    evaporating inside this one.
    """
    checked = scrubbed = 0
    for case in rr.load_cases():
        base_dir = case.dir / rr.BASE_DIR_NAME
        manifest = _read_manifest(case)
        on_disk = sorted(
            p.relative_to(base_dir).as_posix()
            for p in base_dir.rglob("*") if p.is_file()
        )
        assert on_disk == sorted(manifest), (
            f"{case.case_id}: base/ and base.manifest disagree on which files exist")
        for rel, (sha, marker) in manifest.items():
            data = (base_dir / rel).read_bytes()
            assert _blob_sha1(data) == sha, (
                f"{case.case_id}:{rel} no longer matches its manifest blob id")
            if marker is not None:
                assert marker == _SCRUBBED_MARKER, (
                    f"{case.case_id}:{rel} column 3 is {marker!r}, expected "
                    f"{_SCRUBBED_MARKER!r}")
                scrubbed += 1
            checked += 1
    # 70 -> 83 on 2026-08-07: the wiring/controls expansion — 7 new base
    # files materialised from this repo's history (two controls are
    # create-only and have none), plus 3 per parcelo replay case
    # (hand-pinned; their base.ref is external, see the provenance test).
    assert checked == 83, checked
    # 12 -> 17 on 2026-07-31: five more fixtures now carry a scrub, four of them
    # for the two employer ticket ids that a term list could never have seen.
    # 17 -> 18 later the same day: `control-gate-excerpts/base/tests/test_runner.py`
    # carried `/tmp/pytest-of-<maintainer-first-name>/…`, a pytest temp path
    # captured off the maintainer's laptop and frozen into a fixture. Machine
    # residue, not identity — see the BASE_FIXTURE_SCRUB entry for why the bare
    # first name is scrubbed HERE and deliberately not added to any term list.
    # 18 -> 19 on 2026-08-01: `specmiss-credential-load-outside-gate/base/tests/
    # test_cli_commands.py` carried four seeded strings that between them
    # reconstruct a real debugging episode — a build server behind corporate SSO,
    # a 401 loop, and the credential rule that came out of it. Every noun in it
    # is a PUBLIC product name with no host, number or ticket, so no term list,
    # shape rule or ordinal guard could ever have seen it; a reader found it by
    # asking whether the prose narrates something that really happened.
    # Deliberately a literal and not a count derived from the manifests, which
    # is where `scrubbed` already comes from and would agree by construction.
    # 19 -> 20 on 2026-08-01: the SINGLE-DIGIT form of the tracker prefix.
    # The shape rule needs six digits and the literals covered the 8- and
    # 3-digit forms, so nothing in the tree could see it; it was found by
    # hunting the vocabulary of the other projects on the machine.
    # 20 -> 29 on 2026-08-02, the largest single jump and the first that is not
    # about identifiers at all: BEHAVIOURAL PROSE. Sentences built from an
    # APPROVED pseudonym plus a verb that describes the real system — what a
    # repo's harness prints, that it ships a pre-push, its PR-label policy, its
    # directory layout. No term list can see them because every term in them is
    # allowed. Live source was generalised; these nine fixtures (three
    # `runner.py`, three `test_runner.py`, `test_vcs.py`, `test_profile.py` and
    # three `styles.css` — two more were already scrubbed for other reasons and
    # did not move the count) kept the originals, because a base fixture is cut
    # from a pre-sweep commit. That is worth spelling out: a first pass edited
    # only the live tree, saw every gate go green, and shipped all of it
    # unchanged in the frozen twins. The gates check pins and terms; neither
    # asks "did my edit survive materialisation". Verify on the BUILT EXPORT.
    # 29 -> 31 on 2026-08-03: the private-doc-name sweep scrubbed the two
    # models.py base fixtures (orphan-rescue-v2, d44c4377 follow-up).
    # 31 -> 32 on 2026-08-07, with the wiring/controls corpus expansion
    # (checked 70 -> 83): wiring-demo-verify-sync snapshots the pre-sweep
    # test_narrated_demo.py whose then-current synthetic-namespace test
    # carried the employer name as a plain denylist literal — live source
    # deleted that whole test (8b564af1); the fixture takes a substitution
    # instead, because a scrub rule cannot delete a test.
    # 32 -> 33 on 2026-08-13 (P1 part 2): profile.py:6
    # (logic-default-clobbers-explicit-cap) newly carries a scrub — a bare
    # repo-name mention this pass found and BASE_FIXTURE_SCRUB had no rule
    # for before. 12 other files also changed this pass (core/orchestrator.py
    # x5, api/app.py x6, test_profile.py) but were already marked `scrubbed`
    # for unrelated reasons, so they do not move this count.
    # 33 -> 34 on 2026-08-17 (publication-shape alignment): the pseudonymous
    # db name (BASE_FIXTURE_SCRUB's 9-char 2026-08-17 rule) now scrubs to
    # metrics-core in fixtures, matching the publication map. control-cli-
    # budgets/base/tests/test_task_lifecycle.py newly carries a scrub;
    # control-bench-baseline/base/tests/test_bench_publish.py was already
    # marked for an earlier rule, so only one file moves this count.
    assert scrubbed == 34, scrubbed


def test_manifest_declares_the_scrub_without_indexing_the_original():
    """`base.manifest` ships. It must not point at PRE-scrub content.

    Column 3 used to hold the ORIGIN blob id — the git object id of the bytes
    as they were BEFORE de-identification. All 31 of those ids were reachable
    from `main`, so anyone with the published history could `git cat-file` the
    un-de-identified original straight out of a shipped fixture. The count is
    not fixed: it was 20 when this guard landed and is 31 at `main` 9198adf1,
    because it rises with every substitution rule added.

    A history rewrite does not close that. Blobs are CONTENT-addressed, so
    rewriting the commits changes every commit id and no blob id; only rewriting
    a blob's content retires its id. And a rewrite retracts nothing from clones,
    forks and mirrors already taken. (Being *flagged* is also not being
    *rewritten*: the 102-term scanner flags all 31 of these blobs, but its term
    list and the materialiser's 51 substitution rules are different sets.)

    So the rule is structural, not a spot fix. Stated as what it enforces, not
    as more: **no field after column 1 may contain a standalone run of four or
    more hex characters, in either case** — that is the form `git rev-parse`
    will dereference, and 4 is git's own floor for an abbreviation. It is not
    the broader claim "nothing after column 1 may be an object id": a hex run
    glued to a word character (`blob_ffff…`) is not matched, and deliberately so
    — git does not resolve that form either, so it is not a working index, and
    widening to catch it would flag the marker `scrubbed` on every clean
    manifest. See the comment on `_OBJECT_ID_RUN` for both measurements.

    Column 1 is exempt because it hashes the SHIPPED bytes — its pre-image is
    the file sitting next to it, so it indexes nothing that is not already
    published.

    Removing the column removed a redundant path rather than the content: all 31
    ids it held are also reachable from the `change.diff` shipped in the same
    case directory, so what fell is the number of pointers to those bytes, not
    the bytes' recoverability *in a clone*. It is NOT redundant for a reader who
    has no clone: `change.diff` abbreviates to 7 hex, and GitHub's blob API
    refuses anything short of 40 (422), so the manifest was the only shipped
    source of an id you can look up remotely. What closes the rest is the
    publish target — see the corpus README.
    """
    offenders = []
    for case in rr.load_cases():
        for line in (case.dir / "base.manifest").read_text(encoding="utf-8").splitlines():
            fields = line.split("  ")
            for column, value in enumerate(fields[1:], start=2):
                if _OBJECT_ID_RUN.search(value):
                    offenders.append(f"{case.case_id}: column {column}: {line!r}")
    assert offenders == [], (
        "base.manifest carries what looks like a git object id outside column 1 "
        "— a shipped index into pre-scrub content:\n" + "\n".join(offenders))


@pytest.mark.parametrize("value, why", [
    ("ffff0f1a2b3c4d5e6f708192a3b4c5d6e7f80912", "full-length lowercase"),
    ("FFFF0F1A2B3C4D5E6F708192A3B4C5D6E7F80912",
     "full-length UPPERCASE — `git cat-file -t` dereferences an uppercase "
     "object id exactly as it dereferences the lowercase one (verified "
     "2026-08-02 on a real clone against a real id), so a `[0-9a-f]`-only "
     "class let this shape through"),
    ("FFFF0f1a2b3c4d5e6f708192a3b4c5d6e7f80912", "mixed case, also dereferenced"),
    ("ffff", "4 hex — git's own floor for an abbreviation"),
    ("FFFF", "4 hex, uppercase"),
])
def test_object_id_matcher_sees_every_form_git_will_resolve(value, why):
    """The matcher's positives, pinned against the class it is built from.

    Written after an independent review found the uppercase bypass: the guard
    above asserted "nothing after column 1 may be an object id" while matching
    only `[0-9a-f]`, so an uppercase origin id in column 3 passed a test whose
    docstring said it could not.

    THE LITERALS HERE DEREFERENCE TO NOTHING, deliberately. What is under test
    is the matcher's treatment of a SHAPE, and the shape is all the matcher can
    see — so pinning it needs no live id, and using one would make this file the
    thing it exists to forbid. The first draft of this test did exactly that: it
    pinned a real 40-hex id in five case variants, and that id was one of the
    pre-scrub origin blobs. `ffff…` was checked to resolve in neither the
    published clone nor the full local repo, at 4 hex and at 40, in both cases.
    The claim that git dereferences the uppercase form was verified separately,
    against a real id, and is not re-verified by these fixtures.
    """
    assert _OBJECT_ID_RUN.search(value), why


@pytest.mark.parametrize("value, why", [
    ("scrubbed",
     "the marker itself. The run `bbed` is four hex characters, so this is a "
     "hit WITHOUT the word-boundary anchors — which is the entire reason they "
     "are there. (An earlier comment blamed `cbbed`; that substring does not "
     "occur in `scrubbed` at all.)"),
    ("blob_ffff0f1a2b3c4d5e6f708192a3b4c5d6e7f80912",
     "word-ADJACENT hex, uncovered on purpose. Checked against a REAL id, not "
     "this inert one: `git cat-file -t blob_<real id>` answers `Not a valid "
     "object name`, so the form is not a working index — and matching it means "
     "dropping the anchors, which turns every clean manifest red on `scrubbed`"),
    ("src/no_human/api/app.py", "an ordinary shipped path"),
])
def test_object_id_matcher_suppresses_what_it_must_not_flag(value, why):
    """The matcher's negatives — held, so widening the class cannot silently
    take the anchors with it and turn every clean manifest red.

    Same rule as the positives: the literals dereference to nothing. These pin
    what the matcher does with a shape, not what git does with an object."""
    assert not _OBJECT_ID_RUN.search(value), why


def test_every_base_ref_is_a_full_commit_id():
    """`base.ref` must be 40 hex, not an abbreviation.

    This is the claim the manifest's marker rests on: dropping the origin blob
    id costs nothing *because* `<base.ref>:<path>` already determines that blob,
    and it only determines it if `base.ref` names exactly one commit. An
    abbreviation resolves against whatever object store happens to be present,
    so it is repo-local — the same 7 characters can be unique here, ambiguous in
    a fork, and unresolvable in a clone that lacks the object.

    It was not true when the marker landed: 4 of the 20 cases held 7-hex
    abbreviations, and an independent review found that the one abbreviated case
    carrying a scrubbed file was exactly the case where the claim was
    load-bearing. They were expanded rather than the claim being softened. This
    test is here so the justification cannot quietly stop holding again.
    """
    bad = []
    for case in rr.load_cases():
        ref = (case.dir / "base.ref").read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", ref):
            bad.append(f"{case.case_id}: {ref!r} ({len(ref)} chars)")
    assert bad == [], (
        "base.ref must be a full 40-hex commit id — an abbreviation is "
        "repo-local and does not determine the origin blob:\n" + "\n".join(bad))


def test_scrubbed_fixtures_are_their_origin_blob_put_through_scrub():
    """Provenance, re-derived from git rather than read back off the manifest.

    A file with no column-3 marker must equal its blob at `base.ref` byte for
    byte. A file WITH the marker was de-identified after extraction (see
    `materialize_base.py::scrub`), so it must NOT equal that blob, and the
    difference must be EXACTLY the scrub and nothing else. Both directions are
    asserted: an undeclared edit to a fixture fails, and so does a stale
    `scrubbed` marker on a file that is no longer scrubbed.

    Without the last assertion the checks are self-referential: column 1 is
    recomputed from disk and "they differ" is satisfied by ANY difference. An
    independent review demonstrated it on 2026-07-31 — append a payload line to
    a scrubbed fixture, recompute column 1, and the test passed.

    This is also the check that replaced the manifest's origin-id column
    (`test_manifest_declares_the_scrub_without_indexing_the_original`). Nothing
    was lost with it: `base.ref` is a full commit id, and a commit id fixes its
    whole tree, so `<base.ref>:<path>` already determines the origin blob and
    therefore its hash. The deleted assertion re-checked a value it could
    derive; this one constrains the origin blob's CONTENT, which is strictly
    stronger.

    WHY THIS SKIPS RATHER THAN DEGRADES. It used to live inside the byte-pin
    test behind a bare `if have_history:` keyed on one hardcoded commit — and
    that commit is reachable from no branch, so it exists only in the
    maintainer's local object store. On a fresh clone the flag went False, this
    entire block vanished, and the suite went green with only the byte pin
    having run and nothing on screen to say so. Demonstrated on a real
    `git clone`: append a payload line to a scrubbed fixture, recompute column
    1, and the old test passed there while failing in the maintainer's
    checkout. A guard that quietly downgrades on a clean checkout is not a
    guard.

    The precondition is now read off the corpus itself rather than a hardcoded
    sha, and a partial history SKIPS rather than half-running: measured on a real
    default clone 2026-08-04 (on the then-20-case corpus), 4 of the 20 base.ref
    commits were reachable from a branch and the other 16 were not, so a fresh
    clone can verify a subset — and a subset cannot assert the totals below,
    which is exactly how a run that checks less than it claims stays invisible.

    EXTERNAL-BASE CASES ARE OUT OF THIS TEST'S SUBJECT, not skipped around.
    The two recorded-replay wiring cases (`truth.json: external_base_ref`)
    have a base.ref naming a commit in a replay scratch repo, which NO
    checkout of this repository ever contained — for them "unresolvable" is
    the permanent truth, not a gc symptom, so folding them into `missing`
    would turn this test off forever, everywhere. Their base bytes are pinned
    by the column-1 byte test like everyone's, and their provenance is
    re-derived against the startup scenario definition instead
    (`test_parcelo_wiring_bases_are_the_scenario_definition_verbatim`).
    """
    cases = [c for c in rr.load_cases()
             if not c.truth.get("external_base_ref")]
    missing = _cases_whose_base_ref_is_missing()
    if missing:
        pytest.skip(
            f"{len(missing)} of {len(cases)} internal base.ref commits do not "
            f"resolve in {REPO_ROOT}, so provenance cannot be re-derived for "
            "the corpus as a whole and only the column-1 byte pin ran.\n"
            "EXPECTED in the public export (none of the pre-scrub history) and "
            "on any fresh clone: most internal "
            "base.ref commits are reachable from no branch (16 of 20 measured "
            "2026-08-04), so `git clone` never "
            "fetches them. NOT expected in the maintainer's checkout — seeing "
            "this skip there means those objects have been gc'd and the fixtures "
            "can no longer be verified against their source.\n"
            f"Unresolvable: {', '.join(missing)}")

    checked = scrubbed = 0
    for case in cases:
        base_dir = case.dir / rr.BASE_DIR_NAME
        for rel, (sha, marker) in _read_manifest(case).items():
            data = (base_dir / rel).read_bytes()
            blob = subprocess.run(
                ["git", "cat-file", "blob", f"{case.base_ref}:{rel}"],
                cwd=REPO_ROOT, capture_output=True,
            )
            assert blob.returncode == 0, f"{case.case_id}:{rel} not at base.ref"
            if marker is None:
                assert blob.stdout == data, (
                    f"{case.case_id}:{rel} differs from the blob at "
                    f"{case.base_ref} with no {_SCRUBBED_MARKER!r} column to say so")
            else:
                assert blob.stdout != data, (
                    f"{case.case_id}:{rel} carries the {_SCRUBBED_MARKER!r} "
                    "column but is byte-identical to base.ref — stale declaration")
                assert _scrub(blob.stdout) == data, (
                    f"{case.case_id}:{rel} is not its origin blob put "
                    "through scrub() — the fixture carries an edit that "
                    "the de-identification substitutions do not account "
                    "for. Re-run materialize_base.py.")
                scrubbed += 1
            checked += 1
    # Fixed literals like the byte-pin test, for the same reason: a count
    # derived from the manifests would agree with the manifests by
    # construction. Here they also prove the loop did not silently check
    # nothing. 77 = the byte-pin test's 83 minus the 6 external-base parcelo
    # files whose provenance lives in the scenario-definition test instead.
    # 32 -> 33 on 2026-08-13 (P1 part 2) — see the sibling byte-pin test above
    # for which file newly moved.
    assert checked == 77, checked
    # 33 -> 34 on 2026-08-17 (publication-shape alignment): the pseudonymous
    # db name (BASE_FIXTURE_SCRUB's 9-char 2026-08-17 rule) now scrubs to
    # metrics-core in fixtures, matching the publication map. control-cli-
    # budgets/base/tests/test_task_lifecycle.py newly carries a scrub;
    # control-bench-baseline/base/tests/test_bench_publish.py was already
    # marked for an earlier rule, so only one file moves this count.
    assert scrubbed == 34, scrubbed


def test_materialiser_refuses_to_run_with_no_de_identification_rules(
        monkeypatch, tmp_path):
    """A de-identification path must never silently become a no-op.

    `_load_scrub` returns an empty list when the private supplement is absent.
    That used to mean `scrub()` returned its input unchanged, so a re-materialise
    on a checkout that had lost the supplement would write every base fixture
    UNSCRUBBED and regenerate the manifests to agree with the result — the term
    scanners would then be reading a tree that had never been de-identified. The
    only thing standing in the way was a hardcoded count in another test.

    The legitimate absence is the public export, where the supplement is gone
    AND the script cannot run at all because the pre-scrub history is not there
    either. Those two must not be reported as each other, so `materialise`
    probes the history FIRST and raises `HistoryUnavailable` for it —
    `test_materialiser_names_the_right_reason_when_it_cannot_run` is that half.
    An earlier version of this guard checked the rules first, and an
    independent review caught it firing in the built export with a message
    asserting the checkout could run the materialiser.

    Importing the module must keep working in the export — the byte-pin test
    imports it — which is why the refusal is at the point of USE, not at import.
    """
    mod = _load_materialize_base()
    monkeypatch.setattr(mod, "PRIVATE_TERMS_PATH", tmp_path / "not-a-supplement.py")
    monkeypatch.setattr(mod, "SCRUB", mod._load_scrub())
    assert mod.SCRUB == [], "a missing supplement must load no substitutions"

    with pytest.raises(mod.ScrubRulesUnavailable):
        mod.scrub(b"pre-scrub bytes that must not reach disk")

    # A case whose base.ref DOES resolve, so the history probe passes and the
    # scrub-rules guard is what refuses.
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "base.ref").write_text(
        subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                       check=True, capture_output=True, text=True).stdout.strip())
    with pytest.raises(mod.ScrubRulesUnavailable):
        mod.materialise(case_dir, repo_root=REPO_ROOT)
    assert sorted(p.name for p in case_dir.iterdir()) == ["base.ref"], (
        "materialise refused but had already written to the case directory")


def test_materialiser_names_the_right_reason_when_it_cannot_run(
        monkeypatch, tmp_path):
    """"No history" and "no scrub rules" must not be reported as each other.

    The public export has neither, so a materialiser that checks the rules
    first tells the export its supplement is missing — true, expected, and
    entirely the wrong diagnosis, because that checkout could never have run
    the script at all. The history is therefore probed first.

    Checked with the scrub rules ALSO absent, which is the export's real
    configuration and the only arrangement where the wrong answer is available
    to give.
    """
    mod = _load_materialize_base()
    monkeypatch.setattr(mod, "PRIVATE_TERMS_PATH", tmp_path / "not-a-supplement.py")
    monkeypatch.setattr(mod, "SCRUB", mod._load_scrub())
    assert mod.SCRUB == []

    case_dir = tmp_path / "case"
    case_dir.mkdir()
    # Well-formed and resolvable nowhere — an export's base.ref, in effect.
    (case_dir / "base.ref").write_text("0" * 39 + "1")
    with pytest.raises(mod.HistoryUnavailable):
        mod.materialise(case_dir, repo_root=REPO_ROOT)


@pytest.mark.asyncio
async def test_run_case_invokes_injected_reviewer_with_checked_out_repo(tmp_path):
    case = next(c for c in rr.load_cases() if c.case_id == "logic-stale-renders-fresh")
    seen = {}

    async def stub_reviewer(repo_path, diff_text, case_spec):
        seen["repo_path"] = repo_path
        seen["diff_text"] = diff_text
        seen["case_id"] = case_spec.case_id
        return rr.ReviewOutcome(status="FAIL", findings=[
            rr.Finding(file=case_spec.truth["file"],
                      line=case_spec.truth["hunk_lines"][0] + 1,
                      text="fresh renders stale duplicate divider", blocking=True),
        ])

    result = await rr.run_case(REPO_ROOT, case, stub_reviewer, tmp_path)
    assert seen["case_id"] == "logic-stale-renders-fresh"
    assert seen["diff_text"] == case.diff_text
    assert (Path(seen["repo_path"]) / case.truth["file"]).exists()
    assert result.caught is True


@pytest.mark.asyncio
async def test_run_all_iterates_every_case_with_injected_reviewer(tmp_path):
    call_count = {"n": 0}

    async def stub_reviewer(repo_path, diff_text, case_spec):
        call_count["n"] += 1
        if case_spec.is_control:
            return rr.ReviewOutcome(status="PASS", findings=[])
        return rr.ReviewOutcome(status="PASS", findings=[])  # every seeded case missed

    report = await rr.run_all(REPO_ROOT, reviewer_fn=stub_reviewer,
                              model="stub-model", runs_dir=tmp_path)
    all_cases = rr.load_cases()
    assert call_count["n"] == len(all_cases)
    assert len(report.results) == len(all_cases)
    assert report.model == "stub-model"


def test_no_test_calls_run_all_without_runs_dir():
    """Regression guard for the stub-transcript overwrite hazard: every call
    site in THIS test file of the function under test (module attribute
    "run_all", accessed off the ``rr`` alias) must pass ``runs_dir=`` — a run
    that omits it writes into the real ``eval/reviewer_recall/runs/<today>/``
    and can clobber a same-day real measurement's audit trail.

    Built from parts so this guard's own source never contains the literal
    call-site pattern it searches for (which would make it match itself).
    """
    needle = "rr" + "." + "run_all" + "("
    source = Path(__file__).read_text(encoding="utf-8")
    call_starts = [m.start() for m in re.finditer(re.escape(needle), source)]
    assert call_starts, "expected at least one call site of the function under test"
    for start in call_starts:
        # Grab a generous window after the call open-paren — every call in
        # this file fits well within it — and check runs_dir= appears before
        # the matching close. Cheap and sufficient: no call here nests
        # another such call inside its own arguments.
        window = source[start:start + 400]
        depth = 0
        end = None
        for i, ch in enumerate(window):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        assert end is not None, f"unterminated call at offset {start}"
        call_text = window[:end]
        assert "runs_dir=" in call_text, (
            f"call site at offset {start} is missing runs_dir=: {call_text!r}"
        )


# --- SCRUM-XX: no default model id — an unattributed run must be impossible -

def test_runner_defines_no_default_model():
    assert not hasattr(rr, "DEFAULT_MODEL")
    assert "claude-opus-5" not in RUNNER_PATH.read_text(encoding="utf-8")


def test_run_and_report_requires_an_explicit_model():
    with pytest.raises(TypeError) as excinfo:
        rr.run_and_report(REPO_ROOT)
    assert "model" in str(excinfo.value)


def test_run_all_requires_an_explicit_model(tmp_path):
    async def stub_reviewer(repo_path, diff_text, case_spec):
        raise AssertionError("reviewer must not be invoked: model is missing")

    with pytest.raises(TypeError) as excinfo:
        rr.run_all(REPO_ROOT, reviewer_fn=stub_reviewer, runs_dir=tmp_path)
    assert "model" in str(excinfo.value)


# --- SCRUM-XX: the audit trail fails closed on a non-empty runs/<date>/ ----

@pytest.mark.asyncio
async def test_run_all_refuses_to_overwrite_an_existing_run(tmp_path):
    existing = tmp_path / "2026-07-25"
    existing.mkdir()
    sentinel = existing / "x.json"
    sentinel.write_text('{"case_name": "x"}')
    before = sentinel.read_bytes()

    call_count = {"n": 0}

    async def stub_reviewer(repo_path, diff_text, case_spec):
        call_count["n"] += 1
        return rr.ReviewOutcome(status="PASS", findings=[])

    with pytest.raises(rr.TranscriptOverwriteRefused):
        await rr.run_all(REPO_ROOT, reviewer_fn=stub_reviewer, model="stub-model",
                         run_date="2026-07-25", runs_dir=tmp_path)

    assert call_count["n"] == 0
    assert sentinel.read_bytes() == before


@pytest.mark.asyncio
async def test_run_all_overwrites_only_when_explicitly_asked(tmp_path):
    existing = tmp_path / "2026-07-25"
    existing.mkdir()
    (existing / "x.json").write_text('{"case_name": "x"}')

    async def stub_reviewer(repo_path, diff_text, case_spec):
        return rr.ReviewOutcome(status="PASS", findings=[])

    report = await rr.run_all(REPO_ROOT, reviewer_fn=stub_reviewer, model="stub-model",
                              run_date="2026-07-25", runs_dir=tmp_path, overwrite=True)
    all_cases = rr.load_cases()
    assert len(report.results) == len(all_cases)
    for case in all_cases:
        assert (existing / f"{case.case_id}.json").exists()


def test_write_transcripts_refuses_without_overwrite(tmp_path):
    report = rr.RecallReport(
        results=[rr.CaseResult(case_id="x", cls="logic", is_control=False,
                               outcome=rr.ReviewOutcome(status="PASS", findings=[]),
                               caught=False, status="OK")],
        model="stub-model", run_date="2026-07-25",
    )
    rr.write_transcripts(report, tmp_path)
    with pytest.raises(rr.TranscriptOverwriteRefused):
        rr.write_transcripts(report, tmp_path)
    rr.write_transcripts(report, tmp_path, overwrite=True)


# --- load_cases must never quietly shrink the denominator -------------------

def _copy_corpus(tmp_path):
    dest = tmp_path / "cases"
    shutil.copytree(rr.CASES_DIR, dest)
    return dest


def test_load_cases_ignores_a_directory_that_is_not_a_case(tmp_path):
    cases_dir = _copy_corpus(tmp_path)
    (cases_dir / "__pycache__").mkdir()
    (cases_dir / "__pycache__" / "runner.cpython-312.pyc").write_bytes(b"\x00")
    assert len(rr.load_cases(cases_dir)) == 29


def test_load_cases_raises_on_a_partial_case_instead_of_dropping_it(tmp_path):
    """A case missing one of its three files used to vanish from the run.

    That routes around HeadlineRefusedError: a dropped case never becomes a
    CaseResult, so the refusal guard cannot see it, and the denominator moves
    with nothing to say so (`security 0/3` where it should read 0/4).
    """
    for missing in rr.CASE_FILE_NAMES:
        cases_dir = _copy_corpus(tmp_path / missing)
        (cases_dir / "security-payload-logged" / missing).unlink()
        with pytest.raises(rr.CasePrepError) as exc:
            rr.load_cases(cases_dir)
        assert "security-payload-logged" in str(exc.value)
        assert missing in str(exc.value)


# --- a control's clean pass must be auditable for citation demotions --------

def _demoted_control(demoted):
    """A control where the reviewer raised a blocking finding that the
    citation rule then demoted — so `blocking` is empty and it scores clean."""
    return rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="off-diff.py", line=7, text="looks wrong", blocking=False),
    ], demoted_citations=demoted)


def test_demoted_clean_pass_is_flagged_not_silently_banked():
    case = _control_case()
    result = rr.score_case(case, _demoted_control(["hardcoded path: off-diff.py not found"]))
    assert result.clean_pass is True          # scoring is unchanged...
    assert result.clean_pass_relied_on_demotion is True   # ...but it is marked
    assert "demoted" in result.reason
    assert "off-diff.py" in result.reason


def test_genuine_clean_pass_is_not_flagged():
    result = rr.score_case(_control_case(), rr.ReviewOutcome(status="PASS", findings=[]))
    assert result.clean_pass is True
    assert result.clean_pass_relied_on_demotion is False
    assert result.reason == "clean pass"


def test_render_report_warns_when_specificity_rests_on_demotions():
    suspect = rr.score_case(_control_case(), _demoted_control(["x: off-diff.py not found"]))
    report = rr.RecallReport(results=[suspect], model="claude-opus-5",
                             run_date="2026-07-30")
    text = rr.render_report(report)
    assert "specificity:     1/1 clean diffs passed" in text
    assert "demoted by the citation rule" in text
    assert "off-diff.py" in text


def test_transcript_records_demotions_so_a_score_can_be_audited(tmp_path):
    suspect = rr.score_case(_control_case(), _demoted_control(["x: off-diff.py not found"]))
    report = rr.RecallReport(results=[suspect], model="claude-opus-5",
                             run_date="2026-07-30")
    rr.write_transcripts(report, tmp_path)
    data = json.loads((tmp_path / "2026-07-30" / "synthetic-control.json").read_text(encoding="utf-8"))
    assert data["score"] == 1.0
    assert data["clean_pass_relied_on_demotion"] is True
    assert data["demoted_citations"] == ["x: off-diff.py not found"]


def test_default_reviewer_fn_carries_demoted_citations_off_the_decision():
    """The runner's own adapter used to drop `decision.demoted_citations`, so
    the signal never reached a transcript no matter what the reviewer found."""
    import asyncio
    from no_human.review.selfcheck import ChecklistItem

    class _Decision:
        passed = True
        checklist: list = []
        demoted_citations = ["style: nowhere.py not found in the worktree"]
        failed_items: list = []
        blocking_items: list = []

    class _Reviewer:
        def __init__(self, model): pass
        async def review(self, *a, **k): return _Decision()

    import no_human.review.reviewer as rev
    real, rev.AdversarialReviewer = rev.AdversarialReviewer, _Reviewer
    try:
        fn = rr._default_reviewer_fn("stub")
        outcome = asyncio.run(fn(Path("."), "diff", _control_case()))
    finally:
        rev.AdversarialReviewer = real
    assert outcome.demoted_citations == ["style: nowhere.py not found in the worktree"]
    assert ChecklistItem is not None  # import pinned: the adapter builds from these


# --- SCRUM-47: ERROR path never scores as a miss ----------------------------

@pytest.mark.asyncio
async def test_setup_failure_marks_status_error(tmp_path):
    bad_case = rr.CaseSpec(
        case_id="broken-checkout", dir=Path("/dev/null"),
        base_ref="not-a-real-ref-deadbeef", diff_text="",
        truth=_truth(),
    )

    async def stub_reviewer(repo_path, diff_text, case_spec):
        raise AssertionError("reviewer must never be invoked when setup fails")

    result = await rr.run_case(REPO_ROOT, bad_case, stub_reviewer, tmp_path)
    assert result.status == "ERROR"
    assert result.caught is None
    assert result.clean_pass is None
    assert "case setup failed" in result.reason


# --- SCRUM-47: headline refused when any case errored ------------------------

def test_render_report_refuses_on_error():
    error_result = rr.CaseResult(
        case_id="broken-checkout", cls="logic", is_control=False,
        outcome=rr.ReviewOutcome(status="ERROR"), status="ERROR",
        reason="case setup failed: fatal: bad revision",
    )
    report = rr.RecallReport(results=[error_result], model="claude-opus-5",
                             run_date="2026-07-25")
    with pytest.raises(rr.HeadlineRefusedError):
        rr.render_report(report)


def test_render_report_ok_when_no_errors():
    seeded_hit = rr.CaseResult(case_id="a", cls="logic", is_control=False,
                               outcome=rr.ReviewOutcome(status="FAIL"), caught=True)
    report = rr.RecallReport(results=[seeded_hit], model="claude-opus-5",
                             run_date="2026-07-25")
    text = rr.render_report(report)
    assert "reviewer recall: 1/1" in text


# --- SCRUM-47: transcripts written on every run_all invocation --------------

@pytest.mark.asyncio
async def test_run_all_writes_transcripts(tmp_path):
    async def stub_reviewer(repo_path, diff_text, case_spec):
        return rr.ReviewOutcome(status="PASS", findings=[])

    report = await rr.run_all(REPO_ROOT, reviewer_fn=stub_reviewer,
                              model="stub-model", run_date="2026-07-25",
                              runs_dir=tmp_path)
    all_cases = rr.load_cases()
    out_dir = tmp_path / "2026-07-25"
    assert out_dir.is_dir()
    for case in all_cases:
        case_file = out_dir / f"{case.case_id}.json"
        assert case_file.exists(), f"missing transcript for {case.case_id}"
    sample = json.loads((out_dir / f"{all_cases[0].case_id}.json").read_text(encoding="utf-8"))
    assert {"case_name", "status", "score"} <= sample.keys()
    assert sample["case_name"] == all_cases[0].case_id
    assert sample["status"] == "OK"


# --- SCRUM-47: ERROR cases omit `caught` in transcripts (never a miss) ------

def test_error_transcript_omits_caught(tmp_path):
    error_result = rr.CaseResult(
        case_id="broken-checkout", cls="logic", is_control=False,
        outcome=rr.ReviewOutcome(status="ERROR"), status="ERROR",
        reason="case setup failed: fatal: bad revision",
    )
    report = rr.RecallReport(results=[error_result], model="claude-opus-5",
                             run_date="2026-07-25")
    rr.write_transcripts(report, tmp_path)
    data = json.loads((tmp_path / "2026-07-25" / "broken-checkout.json").read_text(encoding="utf-8"))
    assert data["status"] == "ERROR"
    assert "caught" not in data
    assert data["score"] is None
    assert "case setup failed" in data["error_message_if_error"]


# --- goal-reachability scoring (2026-08-07 wiring class) ---------------------
#
# The second caught-rule: `goal.reachable == false` (not demoted) whose
# entry_point cites the caller_file truth.json names. Fully mechanical, like
# the finding rule — no keywords, no judge.


def _wiring_case() -> "rr.CaseSpec":
    return rr.CaseSpec(
        case_id="synthetic-wiring", dir=Path("/dev/null"), base_ref="deadbeef",
        diff_text="diff --git a/rates.py b/rates.py\n",
        truth=_truth(**{"class": "wiring", "file": "rates.py",
                        "caller_file": "api.py",
                        "keywords": ["handle", "caller"]}),
        request="bill on volumetric weight through the API",
    )


def test_goal_veto_citing_the_caller_scores_a_wiring_catch():
    outcome = rr.ReviewOutcome(status="FAIL", findings=[], goal={
        "reachable": False, "entry_point": "api.py:15",
        "evidence": "handle() never forwards dimensions"})
    result = rr.score_case(_wiring_case(), outcome)
    assert result.caught is True
    assert "goal.reachable=false" in result.reason
    assert "api.py:15" in result.reason


def test_goal_veto_citing_somewhere_else_is_not_a_catch_by_itself():
    """An unrelated veto must not score a wiring catch — the entry_point has
    to cite the production caller the truth names."""
    outcome = rr.ReviewOutcome(status="FAIL", findings=[], goal={
        "reachable": False, "entry_point": "rates.py:3", "evidence": "x"})
    result = rr.score_case(_wiring_case(), outcome)
    assert result.caught is False


def test_demoted_goal_veto_never_scores_a_catch():
    """The citation rule already judged this veto hallucinated; scoring must
    agree with the gate, which it would not have blocked."""
    outcome = rr.ReviewOutcome(status="FAIL", findings=[], goal={
        "reachable": False, "entry_point": "api.py:15", "evidence": "x",
        "demoted": True})
    result = rr.score_case(_wiring_case(), outcome)
    assert result.caught is False


def test_the_finding_rule_still_catches_a_wiring_case_without_a_goal_block():
    """The goal rule is an OR, not a replacement — a blocking finding naming
    the planted file inside the hunk with a keyword still counts."""
    outcome = rr.ReviewOutcome(status="FAIL", findings=[
        rr.Finding(file="rates.py", line=12,
                   text="new params never forwarded by the handle caller",
                   blocking=True)])
    result = rr.score_case(_wiring_case(), outcome)
    assert result.caught is True


def test_goal_veto_on_a_control_is_a_false_alarm_even_with_no_findings():
    """The gate blocks on the veto alone, so specificity must count it: a
    clean diff vetoed as unreachable is exactly the benign-unwired false
    positive the >=2 request-shaped controls exist to measure."""
    outcome = rr.ReviewOutcome(status="FAIL", findings=[], goal={
        "reachable": False, "entry_point": "app.py:1", "evidence": "x"})
    result = rr.score_case(_control_case(), outcome)
    assert result.clean_pass is False
    assert "goal.reachable=false" in result.reason


def test_demoted_goal_veto_on_a_control_stays_a_clean_pass():
    outcome = rr.ReviewOutcome(status="PASS", findings=[], goal={
        "reachable": False, "entry_point": "app.py:1", "evidence": "x",
        "demoted": True})
    result = rr.score_case(_control_case(), outcome)
    assert result.clean_pass is True


def test_transcript_carries_the_goal_block(tmp_path):
    outcome = rr.ReviewOutcome(status="FAIL", findings=[], goal={
        "reachable": False, "entry_point": "api.py:15", "evidence": "e"})
    result = rr.score_case(_wiring_case(), outcome)
    report = rr.RecallReport(results=[result], model="claude-opus-5",
                             run_date="2026-08-07")
    rr.write_transcripts(report, tmp_path)
    data = json.loads(
        (tmp_path / "2026-08-07" / "synthetic-wiring.json").read_text(encoding="utf-8"))
    assert data["goal"]["entry_point"] == "api.py:15"


# --- create-only cases prepare against an empty base -------------------------


def test_a_create_only_case_prepares_without_a_base_directory(tmp_path):
    """The ns-1746bea3 ticket shape ('single new file plus its test') has an
    empty pre-image; git cannot track an empty base/, so its absence is the
    correct materialisation and prepare must carry it."""
    case = next(c for c in rr.load_cases()
                if c.case_id == "control-humanize-count")
    assert not (case.dir / rr.BASE_DIR_NAME).is_dir()
    case_repo = rr.prepare_case_repo(REPO_ROOT, case, tmp_path)
    shas = subprocess.run(["git", "rev-list", "--all"], cwd=case_repo,
                          check=True, capture_output=True, text=True)
    assert len(shas.stdout.split()) == 1
    assert (case_repo / "src/no_human/core/humanize.py").is_file()


def test_a_modifying_case_without_base_content_still_raises(tmp_path):
    """Loosening prepare for create-only diffs must not have loosened it for
    everyone: a diff with a real pre-image and no base/ is still broken."""
    case = rr.CaseSpec(
        case_id="broken-no-base", dir=tmp_path / "nowhere",
        base_ref="deadbeef" * 5,
        diff_text=("diff --git a/app.py b/app.py\n"
                   "--- a/app.py\n+++ b/app.py\n"),
        truth=_truth(),
    )
    with pytest.raises(rr.CasePrepError):
        rr.prepare_case_repo(REPO_ROOT, case, tmp_path)


# --- external-base provenance: pinned to the scenario definition -------------


def test_parcelo_wiring_bases_are_the_scenario_definition_verbatim():
    """The recorded-replay cases' provenance, re-derived from a source in this
    repo. Their base.ref names a replay scratch repo's commit (unresolvable
    here by construction), but that scratch repo was materialised from
    eval/startup_scenario/parcelo.yaml, and startup-01 is the sprint's FIRST
    ticket — so every base file must be byte-identical to the scenario's
    `base:` block. This is the external-base counterpart of
    `test_scrubbed_fixtures_are_their_origin_blob_put_through_scrub`.
    """
    import yaml

    scenario = yaml.safe_load(
        (REPO_ROOT / "eval/startup_scenario/parcelo.yaml").read_text(encoding="utf-8"))
    external = [c for c in rr.load_cases()
                if c.truth.get("external_base_ref")]
    assert len(external) == 2
    checked = 0
    for case in external:
        base_dir = case.dir / rr.BASE_DIR_NAME
        for rel, (sha, marker) in _read_manifest(case).items():
            assert marker is None, (
                f"{case.case_id}:{rel} — scenario content is synthetic and "
                "must never need a scrub marker")
            want = scenario["base"].get(rel)
            assert want is not None, (
                f"{case.case_id}:{rel} is not a file the scenario defines")
            assert want.encode() == (base_dir / rel).read_bytes(), (
                f"{case.case_id}:{rel} differs from the scenario definition")
            checked += 1
    assert checked == 6, checked
