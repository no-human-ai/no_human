"""`scripts/check_release_manifest.py` — the shipped file-inventory verifier.

RELEASE_MANIFEST.txt pins every released file's sha256 like a lockfile for the
tree, and this script is what a checkout of the release verifies itself with.
It must hold BOTH directions (a tracked file with no row, a row with no file)
and the pin itself (a row whose hash no longer matches), because each is a
different way a published tree stops being the reviewed tree.

Driven end to end through the CLI on throwaway repos, never on this repository:
in the working repo the tree legitimately differs from a release, which is why
the CI job that runs the script is scoped to the released tree. These tests
must pass identically in the private repo and in the export.

That last requirement shapes the second half of the file. `--write` regenerates
the manifest from the tree, and the manifest SHIPS — so in the source repo,
where most tracked files are classified `drop`, regenerating would publish their
paths and forge their approvals wholesale. The guard against that reads
`EXPORT_CLASSIFICATION.txt` through the export builder's own parser, and the
builder is itself a `drop` file: absent from the export. So those tests skip
where the builder is not there, and the fixtures use invented paths — a shipped
test file must not carry a real dropped name any more than the manifest may.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(
    subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent, text=True,
    ).strip()
)
SCRIPT = REPO_ROOT / "scripts" / "check_release_manifest.py"
BUILDER = REPO_ROOT / "scripts" / "build_public_export.py"

needs_builder = pytest.mark.skipif(
    not BUILDER.exists(),
    reason="the export builder is `drop`; the classification guard it powers "
           "cannot arm in a tree that has neither")


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True)


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "README.md").write_text("hello\n")
    (repo / "pkg" / "a.py").write_text("A = 1\n")
    subprocess.check_call(["git", "init", "--quiet"], cwd=str(repo))
    subprocess.check_call(["git", "add", "-A"], cwd=str(repo))
    return repo


# `private/notes.md` stands in for the real dropped set — private guards, term
# inventories, adverse-legal notes. Real dropped names are deliberately NOT used:
# this test file ships, and a shipped file may no more carry a dropped path than
# the manifest may. The counts are what the EXPORT GATE enforces, not this
# script, so nothing here depends on them. The parser comes from the script's
# own repo, so a fixture needs only the classification text.
#
# CLASSIFY_ALL_SHIP ships EXPORT_CLASSIFICATION.txt, which the real repo drops.
# That is deliberate and it is the only artificial thing here: it produces a
# classified tree whose drop set is EMPTY, which is what separates "the guard
# refuses because something must not ship" from the far weaker "the guard
# refuses whenever a classification exists". A guard keyed on the file's mere
# presence would pass every other test in this section.
CLASSIFY_ALL_SHIP = """\
ship  1  README.md
ship  1  pkg/a.py
ship  1  EXPORT_CLASSIFICATION.txt
"""

CLASSIFY_WITH_A_DROP = CLASSIFY_ALL_SHIP + "drop  1  private/notes.md\n"


def add_drop_file(repo: Path) -> None:
    """Introduce a file that must not ship — the state in which regenerating
    the manifest from the tree turns from routine into a leak."""
    (repo / "private").mkdir(exist_ok=True)
    (repo / "private" / "notes.md").write_text("do not publish\n")
    (repo / "EXPORT_CLASSIFICATION.txt").write_text(CLASSIFY_WITH_A_DROP)
    subprocess.check_call(["git", "add", "-A"], cwd=str(repo))


def make_classified_repo(root: Path, classification: str) -> Path:
    """A throwaway repo shaped like the SOURCE tree: a ship/drop classification
    beside the files, and (in the WITH_A_DROP case) a file that must not ship."""
    repo = make_repo(root)
    (repo / "EXPORT_CLASSIFICATION.txt").write_text(classification)
    if "private/notes.md" in classification:
        (repo / "private").mkdir()
        (repo / "private" / "notes.md").write_text("do not publish\n")
    subprocess.check_call(["git", "add", "-A"], cwd=str(repo))
    return repo


def pin(repo: Path, *rels: str) -> None:
    """Write a manifest pinning exactly ``rels``, without going through
    `--write` — the check-side tests must not depend on the very command whose
    behaviour they are pinning down."""
    rows = "".join(
        f"{hashlib.sha256((repo / r).read_bytes()).hexdigest()}  {r}\n"
        for r in sorted(rels))
    (repo / "RELEASE_MANIFEST.txt").write_text("# pins\n" + rows)


def write_manifest(repo: Path) -> None:
    proc = run("--root", str(repo), "--write")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_matching_tree_passes(tmp_path):
    repo = make_repo(tmp_path)
    write_manifest(repo)
    proc = run("--root", str(repo))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK: 2 file(s) match" in proc.stdout


def test_a_changed_file_fails_on_its_hash(tmp_path):
    """The content pin. Same path, same row, different bytes: the row must be
    regenerated (and therefore reviewed in a diff) or the check stays red."""
    repo = make_repo(tmp_path)
    write_manifest(repo)
    (repo / "pkg" / "a.py").write_text("A = 2\n")
    proc = run("--root", str(repo))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "pkg/a.py: content differs from the manifest" in proc.stderr


def test_a_tracked_file_missing_from_the_manifest_warns_and_fails_under_strict(
    tmp_path,
):
    """Split from "fails" (2026-08-02), and the reason is the exit code's signal.

    An unlisted path and a hash mismatch used to share exit 1. In this working
    repository ~93 paths are legitimately unlisted, so the script was ALREADY 1
    before any change: a deliberately bad merge resolution produced a real
    mismatch and the exit code did not move. Only reading the body revealed it,
    which is not a gate.

    So the classes are separated, and BOTH halves are pinned here: the unlisted
    path is still reported by name (it is not being hidden), it no longer sets
    the exit code by itself, and `--strict` — what CI runs on the released tree,
    where completeness is a hard invariant — still fails on it exactly as
    before.
    """
    repo = make_repo(tmp_path)
    write_manifest(repo)
    (repo / "pkg" / "new.py").write_text("N = 1\n")
    subprocess.check_call(["git", "add", "pkg/new.py"], cwd=str(repo))

    proc = run("--root", str(repo))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "pkg/new.py: tracked but not listed" in proc.stderr
    assert "WARN" in proc.stderr

    strict = run("--root", str(repo), "--strict")
    assert strict.returncode == 1, strict.stdout + strict.stderr
    assert "pkg/new.py: tracked but not listed" in strict.stderr


def test_a_hash_mismatch_still_fails_when_unlisted_paths_are_present(tmp_path):
    """The defect this split exists to fix, pinned as a test.

    An unlisted path and a changed file at the same time: before the split both
    produced exit 1 and the mismatch was invisible to any automated caller. The
    unlisted path must now warn, the mismatch must still fail, and the mismatch
    must be the thing that decides the exit code.
    """
    repo = make_repo(tmp_path)
    write_manifest(repo)
    # An unlisted path — on its own, only a warning.
    (repo / "pkg" / "new.py").write_text("N = 1\n")
    subprocess.check_call(["git", "add", "pkg/new.py"], cwd=str(repo))
    assert run("--root", str(repo)).returncode == 0

    # ...and now a real mismatch on top of it.
    (repo / "pkg" / "a.py").write_text("A = 2\n")
    proc = run("--root", str(repo))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "pkg/a.py: content differs from the manifest" in proc.stderr
    assert "pkg/new.py: tracked but not listed" in proc.stderr


def test_a_listed_file_missing_from_the_tree_fails(tmp_path):
    repo = make_repo(tmp_path)
    write_manifest(repo)
    # -f: the fixture stages without committing, and `git rm` refuses a
    # staged-only file otherwise.
    subprocess.check_call(["git", "rm", "--quiet", "-f", "pkg/a.py"], cwd=str(repo))
    proc = run("--root", str(repo))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "pkg/a.py: listed but not in the tree" in proc.stderr


def test_a_manifest_listing_itself_fails(tmp_path):
    """It cannot pin its own content — a self-row is always stale or always
    wrong, and either way it teaches readers to expect a mismatch."""
    repo = make_repo(tmp_path)
    write_manifest(repo)
    manifest = repo / "RELEASE_MANIFEST.txt"
    manifest.write_text(manifest.read_text()
                        + "0" * 64 + "  RELEASE_MANIFEST.txt\n")
    proc = run("--root", str(repo))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "lists itself" in proc.stderr


def test_a_malformed_row_is_an_error_not_a_skip(tmp_path):
    """A row the parser cannot read is a pin that silently stopped pinning."""
    repo = make_repo(tmp_path)
    write_manifest(repo)
    manifest = repo / "RELEASE_MANIFEST.txt"
    manifest.write_text(manifest.read_text() + "not-a-hash  pkg/a.py2\n")
    proc = run("--root", str(repo))
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "cannot parse" in proc.stderr


def test_write_omits_the_manifest_itself_and_sorts_rows(tmp_path):
    repo = make_repo(tmp_path)
    write_manifest(repo)
    subprocess.check_call(["git", "add", "RELEASE_MANIFEST.txt"], cwd=str(repo))
    write_manifest(repo)   # second pass, with the manifest now tracked
    rows = [l for l in (repo / "RELEASE_MANIFEST.txt").read_text().splitlines()
            if l and not l.startswith("#")]
    paths = [r.split("  ", 1)[1] for r in rows]
    assert "RELEASE_MANIFEST.txt" not in paths
    assert paths == sorted(paths)
    proc = run("--root", str(repo))
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --------------------------------------------------------------------------
# the classification guard: --write must never pin what must not ship
# --------------------------------------------------------------------------

def test_write_is_unguarded_where_there_is_no_classification(tmp_path):
    """The RELEASE case, and the reason the guard is conditional rather than
    absolute. A tree with no EXPORT_CLASSIFICATION.txt has no dropped file to
    protect — every tracked file ships — so regenerating is exactly right. It is
    the tree this script is shipped to verify, and `make_repo` is that tree:
    every test above regenerates freely."""
    repo = make_repo(tmp_path)
    assert not (repo / "EXPORT_CLASSIFICATION.txt").exists()
    assert not (repo / "RELEASE_MANIFEST.txt").exists()

    proc = run("--root", str(repo), "--write")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REFUSED" not in proc.stderr
    assert "must not ship" not in proc.stderr
    assert "wrote 2 row(s)" in proc.stdout

    manifest = repo / "RELEASE_MANIFEST.txt"
    assert manifest.exists()
    rows = [l for l in manifest.read_text().splitlines()
            if l and not l.startswith("#")]
    parsed = [r.split("  ", 1) for r in rows]
    assert [rel for _, rel in parsed] == ["README.md", "pkg/a.py"]
    for digest, rel in parsed:
        assert digest == hashlib.sha256((repo / rel).read_bytes()).hexdigest()

    check = run("--root", str(repo))
    assert check.returncode == 0, check.stdout + check.stderr
    assert "OK: 2 file(s) match" in check.stdout

    before = manifest.read_bytes()
    again = run("--root", str(repo), "--write")
    assert again.returncode == 0, again.stdout + again.stderr
    assert manifest.read_bytes() == before


@needs_builder
def test_write_refuses_to_pin_a_drop_classified_path_and_names_it(tmp_path):
    """THE HAZARD. `--write` rebuilds the manifest from the tree, and the
    manifest ships — so a dropped path regenerated into it publishes its own
    name. Refusal must be total (no manifest at all) and must name what it
    refused, because a guard that fails silently teaches nobody."""
    repo = make_classified_repo(tmp_path, CLASSIFY_WITH_A_DROP)
    proc = run("--root", str(repo), "--write")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "REFUSED" in proc.stderr
    assert "private/notes.md — classified `drop`" in proc.stderr
    assert not (repo / "RELEASE_MANIFEST.txt").exists(), \
        "a refused --write must write nothing at all"


@needs_builder
def test_a_refused_write_leaves_an_existing_manifest_byte_identical(tmp_path):
    """Fail closed, not half-open: the refusal happens before the write, so a
    manifest that already holds reviewed pins is not truncated on the way out."""
    repo = make_classified_repo(tmp_path, CLASSIFY_ALL_SHIP)
    assert run("--root", str(repo), "--write").returncode == 0
    before = (repo / "RELEASE_MANIFEST.txt").read_bytes()
    add_drop_file(repo)

    proc = run("--root", str(repo), "--write")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert (repo / "RELEASE_MANIFEST.txt").read_bytes() == before


@needs_builder
def test_write_still_works_when_the_classification_drops_nothing(tmp_path):
    """The guard must not have replaced a dangerous tool with a useless one: a
    classified tree whose files all ship is regenerable, because there is
    nothing there that regenerating could wrongly pin."""
    repo = make_classified_repo(tmp_path, CLASSIFY_ALL_SHIP)
    proc = run("--root", str(repo), "--write")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "wrote " in proc.stdout


def test_a_classification_with_no_parser_refuses_rather_than_guesses(tmp_path):
    """Present-but-unparseable is the one state where guessing would be worst:
    we know the tree has a ship/drop split and cannot read which side a file is
    on. Called directly, because the parser is located from the script's own
    repo — the CLI cannot be pointed at a repo that lacks it."""
    spec = importlib.util.spec_from_file_location("_nh_check_manifest", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    repo = make_classified_repo(tmp_path, CLASSIFY_WITH_A_DROP)
    empty = tmp_path / "no-builder-here"
    empty.mkdir()

    # no classification at all -> None, and that is a real answer: the release
    # tree has nothing to protect.
    assert mod.load_unpinnable(repo.parent, builder_root=empty) is None
    with pytest.raises(mod.Refused) as exc:
        mod.load_unpinnable(repo, builder_root=empty)
    assert "build_public_export.py" in str(exc.value)


# --------------------------------------------------------------------------
# the signpost: an unpinned dropped file is HEALTHY, and must not read as a bug
# --------------------------------------------------------------------------

@needs_builder
def test_an_unpinned_drop_classified_path_is_not_a_problem(tmp_path):
    """What made the old message dangerous: "tracked but not listed" is the
    NORMAL, REQUIRED state for a file that does not ship, so the check reported
    ~93 healthy rows as failures and recommended the destructive cure on each.
    A check that cannot tell health from breakage gets obeyed blindly."""
    repo = make_classified_repo(tmp_path, CLASSIFY_WITH_A_DROP)
    pin(repo, "README.md", "pkg/a.py", "EXPORT_CLASSIFICATION.txt")

    proc = run("--root", str(repo))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "correctly unpinned" in proc.stdout
    assert "private/notes.md" not in proc.stderr


@needs_builder
def test_a_correctly_unpinned_path_is_not_fatal_even_under_strict(tmp_path):
    """Where the two fixes meet, and the one place they could have cancelled out.

    `--strict` exists to make an unlisted file fatal, because on the RELEASE
    tree completeness is a hard invariant. A dropped file is unlisted for the
    opposite reason: the classification requires it. If --strict reached that
    bucket it would fail the source repo on ~93 healthy paths and be unusable
    exactly where the classification lives. So the classification is asked
    first, and its answer is terminal — --strict never sees these at all.
    """
    repo = make_classified_repo(tmp_path, CLASSIFY_WITH_A_DROP)
    pin(repo, "README.md", "pkg/a.py", "EXPORT_CLASSIFICATION.txt")

    proc = run("--root", str(repo), "--strict")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "correctly unpinned" in proc.stdout
    assert "private/notes.md" not in proc.stderr

    # ...and --strict is not simply inert here: a genuinely unlisted SHIP file
    # in the SAME tree still fails, so the exemption is scoped to the drop set
    # rather than switched off by the mere presence of a classification.
    (repo / "pkg" / "b.py").write_text("B = 1\n")
    (repo / "EXPORT_CLASSIFICATION.txt").write_text(
        CLASSIFY_WITH_A_DROP + "ship  1  pkg/b.py\n")
    subprocess.check_call(["git", "add", "-A"], cwd=str(repo))
    # the classification file's own bytes changed, so re-pin the tree's ship set
    pin(repo, "README.md", "pkg/a.py", "EXPORT_CLASSIFICATION.txt")
    strict = run("--root", str(repo), "--strict")
    assert strict.returncode == 1, strict.stdout + strict.stderr
    assert "pkg/b.py: tracked but not listed" in strict.stderr
    assert "private/notes.md" not in strict.stderr


@needs_builder
def test_a_pinned_drop_classified_path_is_a_problem(tmp_path):
    """The detector for the hazard having ALREADY fired — a manifest someone
    regenerated and committed. The row itself is the leak: the manifest ships,
    so it publishes the path even though the file's bytes stay behind."""
    repo = make_classified_repo(tmp_path, CLASSIFY_WITH_A_DROP)
    pin(repo, "README.md", "pkg/a.py", "EXPORT_CLASSIFICATION.txt",
        "private/notes.md")

    proc = run("--root", str(repo))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "private/notes.md: pinned in RELEASE_MANIFEST.txt" in proc.stderr
    assert "publishes a path that must not appear in it" in proc.stderr


@needs_builder
def test_the_remedy_never_recommends_write_in_a_classified_tree(tmp_path):
    """The signpost fix itself. The old text recommended `--write` on every
    failure, in the one repo where `--write` is the damaging action; the safe
    remedy is the deliberate, scanned, one-path-at-a-time approval path.

    The trigger is a genuinely unlisted SHIP file, which is a WARNING here and
    fatal only under --strict — so the remedy must print on the warning too.
    Advice that only appears once the exit code has already gone red is advice
    nobody reads at the moment they could still act on it.
    """
    repo = make_classified_repo(tmp_path, CLASSIFY_WITH_A_DROP)
    # a SHIP file with no pin: really incomplete, unlike private/notes.md
    pin(repo, "README.md", "EXPORT_CLASSIFICATION.txt")
    proc = run("--root", str(repo))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "pkg/a.py: tracked but not listed" in proc.stderr
    assert "export_guard.py approve" in proc.stderr
    assert "--write`" not in proc.stderr
    assert "Do NOT run --write in this tree" in proc.stderr

    strict = run("--root", str(repo), "--strict")
    assert strict.returncode == 1, strict.stdout + strict.stderr
    assert "export_guard.py approve" in strict.stderr
    assert "--write`" not in strict.stderr


def test_the_remedy_does_recommend_write_where_it_is_safe(tmp_path):
    """And the converse, so the guard is not just always-off: with no
    classification there is nothing to protect and regeneration is the answer.

    Same trigger as the classified case above — an unlisted file, warning by
    default — so the two tests differ in exactly one variable, the presence of a
    classification, and therefore isolate what the remedy text keys on."""
    repo = make_repo(tmp_path)
    write_manifest(repo)
    (repo / "pkg" / "new.py").write_text("N = 1\n")
    subprocess.check_call(["git", "add", "pkg/new.py"], cwd=str(repo))
    proc = run("--root", str(repo))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--write`" in proc.stderr
    assert "no EXPORT_CLASSIFICATION.txt" in proc.stderr
    assert "export_guard.py approve` instead" in proc.stderr


def test_write_produces_lf_bytes_on_every_platform(tmp_path):
    """`--write` goes through Python's text layer, which on Windows would turn
    every row into CRLF. The manifest is compared as bytes by git, so a CRLF
    write makes all rows look changed while the tool itself stays quiet."""
    repo = make_repo(tmp_path)
    write_manifest(repo)

    written = (repo / "RELEASE_MANIFEST.txt").read_bytes()
    assert b"\r" not in written
    assert written.endswith(b"\n")


def test_rewriting_the_manifest_is_idempotent_byte_for_byte(tmp_path):
    """A second `--write` over an unchanged tree must not alter a single byte,
    which is what makes a large diff a real signal rather than noise."""
    repo = make_repo(tmp_path)
    write_manifest(repo)
    first = (repo / "RELEASE_MANIFEST.txt").read_bytes()
    write_manifest(repo)

    assert (repo / "RELEASE_MANIFEST.txt").read_bytes() == first


def test_write_notes_when_it_rewrites_most_of_the_manifest(tmp_path):
    """The line-ending failure looks exactly like a huge honest diff, so say
    something. A warning, never a refusal: wide sweeps are legitimate."""
    repo = make_repo(tmp_path)
    write_manifest(repo)

    # Change every tracked file, the shape a line-ending mismatch produces.
    (repo / "README.md").write_text("hello, again\n")
    (repo / "pkg" / "a.py").write_text("A = 2\n")
    subprocess.check_call(["git", "add", "-A"], cwd=str(repo))

    proc = run("--root", str(repo), "--write")
    assert proc.returncode == 0
    assert "existing row(s) changed" in proc.stderr
    assert "core.autocrlf" in proc.stderr


def test_write_is_quiet_when_only_a_little_changed(tmp_path):
    """One row in a wider manifest is the ordinary case and must stay silent."""
    repo = make_repo(tmp_path)
    for i in range(8):
        (repo / f"f{i}.txt").write_text(f"{i}\n")
    subprocess.check_call(["git", "add", "-A"], cwd=str(repo))
    write_manifest(repo)

    (repo / "f0.txt").write_text("changed\n")
    subprocess.check_call(["git", "add", "-A"], cwd=str(repo))

    proc = run("--root", str(repo), "--write")
    assert proc.returncode == 0
    assert "existing row(s) changed" not in proc.stderr


def test_write_recovers_a_manifest_left_mid_merge(tmp_path):
    """`--write` is how a conflicted manifest gets cleared, and the manifest
    conflicts on nearly every merge because it is generated. So the write side
    has to survive READING a file that does not parse; `parse_manifest` raising
    on the first bad row is right for the check side and fatal for this one."""
    repo = make_repo(tmp_path)
    (repo / "RELEASE_MANIFEST.txt").write_text(
        "# pins\n"
        "<<<<<<< HEAD\n"
        + "a" * 64 + "  README.md\n"
        "=======\n"
        + "b" * 64 + "  README.md\n"
        ">>>>>>> origin/main\n"
    )

    proc = run("--root", str(repo), "--write")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    written = (repo / "RELEASE_MANIFEST.txt").read_text()
    assert "<<<<<<<" not in written
    assert ">>>>>>>" not in written
    assert "=======" not in written

    # Recovery means the checker accepts the result, not merely that the write
    # did not crash.
    assert run("--root", str(repo)).returncode == 0


def test_an_unreadable_previous_manifest_costs_only_the_note(tmp_path):
    """The comparison is a convenience. Losing it must never cost the write."""
    repo = make_repo(tmp_path)
    (repo / "RELEASE_MANIFEST.txt").write_text("# pins\nnot a manifest row at all\n")

    proc = run("--root", str(repo), "--write")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "existing row(s) changed" not in proc.stderr
