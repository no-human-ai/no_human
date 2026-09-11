"""`scripts/verify_artefact.py` and the stamp `packaging/build-installer.sh` writes.

WHY THESE TESTS ARE SHAPED THIS WAY

The verifier exists because a signed, notarized DMG shipped containing a board
44 commits stale. Its first version was hand-tested, and hand-testing is exactly
what let it through: an independent reviewer showed it printing

    verify-artefact: OK — built from c12a5f0f9256 on main, 204 board file(s)

about a board whose index.html read "THIS IS THE WRONG BOARD - 44 commits
stale", because `board_sha256` in the stamp was empty and an empty field was
read as "nothing to check". Two more fields failed open the same way.

So every test here is written as a FAIL-OPEN probe: it constructs an artefact
that is wrong in one specific way, and asserts the verifier says so. A test that
only checks the happy path would have passed against the broken version.

The shell half is tested behaviourally too, by extracting the stamp block out of
build-installer.sh and running it against a fixture with a PATH that has no
`shasum`. That is the host the branch advertises support for, and it is where
the empty digest came from: a failing command substitution inside `echo` does
not propagate under `set -euo pipefail`, so the build wrote a blank and carried
on. Asserting on the script's TEXT would not have caught that; running it does.

NO SOURCE-TEXT GUARDS. The first version of this file asserted the DMG
pipeline's properties by searching make-dmg.sh's text — `dmg.rindex("hdiutil
detach") < dmg.index('if [ "${verify_rc}" != "0" ]')`. A reviewer deleted the
real `hdiutil detach "${v_dev}" -force` line, which leaks a mounted volume on
both exit paths, and the suite still reported 38 passed: `rindex` simply matched
the `vcleanup()` DEFINITION further up, which is also before the decision. A
test its own mutation survives is not a test. Both of those are gone; the
pipeline's verification block is now EXTRACTED and RUN against a stubbed
`hdiutil` that records every call, and the assertions are on what was called, in
what order, and with what exit code.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "verify_artefact.py"
INSTALLER = REPO / "packaging" / "build-installer.sh"
INSTALLER_PS1 = REPO / "packaging" / "build-installer.ps1"
MAKEDMG = REPO / "packaging" / "make-dmg.sh"

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def _load():
    spec = importlib.util.spec_from_file_location("_nh_verify_artefact", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


va = _load()


# --------------------------------------------------------------------------- #
# fixtures: a minimal artefact that looks like a mounted DMG
# --------------------------------------------------------------------------- #

BOARD = {
    "index.html": b"<!doctype html><div id=root>the board</div>",
    "assets/app.js": b"console.log('a feature shipped this week');",
    "assets/app.css": b":root{--bg:#111}",
}

# The tree the shell and Python digests must agree on. Every entry is here
# because it is a way the two halves can silently diverge: a space forces
# correct quoting, a leading dot is a glob hazard, nesting exercises the
# relative-path construction, non-ASCII names exercise the byte encoding and the
# collation, an empty file is a hash of nothing, and two files with IDENTICAL
# contents but different names are the case the old content-only digest could
# not distinguish from one file.
HOSTILE = {
    "index.html": b"<!doctype html>the board",
    "a space/two words.js": b"spaces in the path",
    ".hidden-dotfile": b"dotfile",
    "assets/nested/deep/x.css": b"deep",
    "שלום.txt": "עברית".encode(),
    "emoji-\U0001f389.txt": "\U0001f389".encode(),
    "empty.txt": b"",
    "dupe-a.txt": b"identical bytes",
    "dupe-b.txt": b"identical bytes",
    "zzz-last.txt": b"z",
}


def _digest(files: dict[str, bytes]) -> str:
    """The reference digest: `<sha256>  <relpath>` lines, sorted as BYTES.

    Deliberately written out here rather than calling `va.board_digest`, so a
    change to the implementation has to be matched by a change to the expected
    shape — a test that recomputes its expectation from the code under test
    proves nothing.
    """
    lines = sorted(
        hashlib.sha256(b).hexdigest().encode("ascii") + b"  " + os.fsencode(rel)
        for rel, b in files.items()
    )
    return hashlib.sha256(b"\n".join(lines) + b"\n").hexdigest()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def make_repo(tmp_path: Path, name: str = "src", commits: int = 1) -> tuple[Path, str]:
    """A real git repo with `commits` commits on `main`; returns (path, HEAD)."""
    repo = tmp_path / name
    repo.mkdir(parents=True)
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)],
                   check=True, env=env)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "one"],
                   check=True, env=env)
    for i in range(commits - 1):
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty",
                        "-m", f"c{i}"], check=True, env=env)
    return repo, _git(repo, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def _default_repo(tmp_path_factory):
    """The plain `make_repo(tmp_path)` shape, built ONCE per module.

    Safe to share: `verify_artefact.py` only ever reads `--repo` (rev-parse,
    cat-file -e) — it never writes to it — so every test that just needs *a*
    correct, unmutated repo can use the same one instead of paying `git init`
    + `git commit` again."""
    return make_repo(tmp_path_factory.mktemp("verify-artefact-default-repo"))


def make_bundle(tmp_path: Path, commit: str, *, files: dict[str, bytes] | None = None,
                stamp: dict[str, str] | None = None, name: str = "mnt") -> Path:
    """An artefact directory shaped like a mounted DMG: app/Resources/nh-server."""
    files = BOARD if files is None else files
    bundle = tmp_path / name
    root = bundle / "no_human.app" / "Contents" / "Resources" / "nh-server"
    board = root / "web" / "dist"
    for rel, data in files.items():
        p = board / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    fields = {"commit": commit, "dirty": "no", "board_sha256": _digest(files)}
    if stamp is not None:
        fields.update(stamp)
        fields = {k: v for k, v in fields.items() if v is not None}
    (root / "BUILD_STAMP").write_text(
        "".join(f"{k}={v}\n" for k, v in fields.items()))
    return bundle


def run(args: list[str], capsys) -> tuple[int, str, str]:
    rc = va.main(args)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


# --------------------------------------------------------------------------- #
# the control: a correct artefact passes
# --------------------------------------------------------------------------- #

def test_a_correct_artefact_passes(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 0, err
    assert "verify-artefact: OK" in out
    assert "provenance NOT verified" not in out
    assert "3 board file(s)" in out


def test_the_stale_artefact_it_exists_for_is_caught(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    stale = {**BOARD, "index.html": b"THIS IS THE WRONG BOARD - 44 commits stale"}
    bundle = make_bundle(tmp_path, "c12a5f0f9256" + "0" * 28, files=stale)
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1
    assert "verify-artefact: FAILED" in err
    assert "OK" not in out


# --------------------------------------------------------------------------- #
# DEFECT 1 — an absent or empty board_sha256 must not mean "nothing to check"
# --------------------------------------------------------------------------- #

def test_empty_board_sha256_fails_even_when_the_board_is_wrong(tmp_path, capsys, _default_repo):
    """The reviewer's exact reproduction. rc used to be 0 with an OK line."""
    repo, sha = _default_repo
    wrong = {**BOARD, "index.html": b"THIS IS THE WRONG BOARD - 44 commits stale"}
    bundle = make_bundle(tmp_path, sha, files=wrong, stamp={"board_sha256": ""})
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1, f"a blank digest passed: {out}"
    assert "'board_sha256' is EMPTY" in err
    assert "verify-artefact: OK" not in out


def test_missing_board_sha256_field_fails(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha, stamp={"board_sha256": None})
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1
    assert "no 'board_sha256' field" in err


def test_a_board_modified_after_the_build_still_fails(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    board = next(bundle.rglob("index.html"))
    board.write_bytes(b"tampered after the build")
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1
    assert "does not match its own stamp" in err


def test_swapping_two_board_files_paths_is_caught(tmp_path, capsys, _default_repo):
    """The digest was PATH-BLIND: both halves hashed a multiset of content
    hashes with the names stripped (`awk '{print $1}'`, `blobs.values()`).

    Exchanging index.html with an asset leaves the multiset identical, so the
    board could serve the stale file at its entry point and still print
    `OK — 2 board file(s) matching the stamped digest`, rc=0. Every rename, move
    and swap inside dist/ was invisible.
    """
    repo, sha = _default_repo
    files = {"index.html": b"THE CURRENT BOARD",
             "assets/app.js": b"THIS IS THE WRONG BOARD - 44 commits stale"}
    bundle = make_bundle(tmp_path, sha, files=files)
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 0, err  # control: unswapped, this artefact is correct

    board = next(bundle.rglob("index.html")).parent
    a, b = board / "index.html", board / "assets" / "app.js"
    a_bytes, b_bytes = a.read_bytes(), b.read_bytes()
    a.write_bytes(b_bytes)
    b.write_bytes(a_bytes)

    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1, f"swapping two files' paths left the digest identical: {out}"
    assert "does not match its own stamp" in err


def test_the_digest_distinguishes_a_rename(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    board = next(bundle.rglob("index.html")).parent
    (board / "assets" / "app.js").rename(board / "assets" / "app.legacy.js")
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1, f"a rename inside the board was invisible: {out}"
    assert "does not match its own stamp" in err


def test_a_malformed_digest_is_not_silently_accepted(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha, stamp={"board_sha256": "not-a-digest"})
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1
    assert "not a 64-hex digest" in err


# --------------------------------------------------------------------------- #
# DEFECT 2 — an empty commit, and an empty expectation
# --------------------------------------------------------------------------- #

def test_empty_commit_in_the_stamp_fails(tmp_path, capsys, _default_repo):
    """`"anything".startswith("")` is True, so an empty commit matched anything."""
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha, stamp={"commit": ""})
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1, f"an empty commit passed: {out}"
    assert "'commit' is EMPTY" in err


def test_missing_commit_field_fails(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha, stamp={"commit": None})
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1
    assert "no 'commit' field" in err


def test_empty_expect_commit_is_a_usage_error_not_a_skip(tmp_path, capsys, _default_repo):
    """`--expect-commit "$(git rev-parse HEAD)"` with a failed substitution."""
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, "b" * 40)  # deliberately NOT the repo's sha
    rc, out, err = run([str(bundle), "--repo", str(repo), "--expect-commit", ""],
                       capsys)
    assert rc == 2, f"an empty expectation was treated as no check: {out}"
    assert "usage error" in err
    assert "EMPTY" in err
    assert "verify-artefact: OK" not in out


def test_a_junk_expect_commit_is_a_usage_error(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    rc, out, err = run([str(bundle), "--repo", str(repo),
                        "--expect-commit", "HEAD"], capsys)
    assert rc == 2
    assert "usage error" in err


def test_expect_commit_accepts_an_abbreviation_and_still_compares(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    rc, out, err = run([str(bundle), "--repo", str(repo),
                        "--expect-commit", sha[:12]], capsys)
    assert rc == 0, err
    rc, out, err = run([str(bundle), "--repo", str(repo),
                        "--expect-commit", "0" * 12], capsys)
    assert rc == 1
    assert "stale-artefact failure" in err


def test_a_malformed_commit_in_the_stamp_fails(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha, stamp={"commit": "main"})
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1
    assert "not a 40-hex sha" in err


# --------------------------------------------------------------------------- #
# the stamp as a whole
# --------------------------------------------------------------------------- #

def test_no_stamp_at_all_fails(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    next(bundle.rglob("BUILD_STAMP")).unlink()
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1
    assert "no BUILD_STAMP" in err


def test_dirty_is_refused_unless_allowed(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha, stamp={"dirty": "yes"})
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1
    assert "DIRTY tree" in err


def test_allow_dirty_is_not_an_unqualified_ok(tmp_path, capsys, _default_repo):
    """`make-dmg.sh` passes --allow-dirty for EVERY unsigned build.

    A stale board plus dirty=yes plus a self-consistent digest used to print a
    flat `verify-artefact: OK — … dirty tree …` and return 0, on exactly the
    path whose own comment says it exists because "a friend-shareable DMG can be
    just as stale". `dirty=yes` means the stamped commit does not describe these
    bytes, so it is a DOWNGRADE, not a waiver: rc=3, and the OK line says so.
    """
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha, stamp={"dirty": "yes"})
    rc, out, err = run([str(bundle), "--repo", str(repo), "--allow-dirty"], capsys)
    assert rc == 3, f"a dirty build was indistinguishable from a verified one: {out}"
    assert "provenance NOT verified" in out
    assert "PROVENANCE NOT VERIFIED" in err
    assert "DIRTY tree" in err


def test_a_nonsense_dirty_value_fails(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha, stamp={"dirty": "maybe"})
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1
    assert "neither 'yes' nor 'no'" in err


# --------------------------------------------------------------------------- #
# DEFECT 7 — the stamp is a channel into a third-party artefact
# --------------------------------------------------------------------------- #

def test_the_writer_does_not_put_a_branch_name_in_the_artefact(tmp_path):
    """A branch name is free text a human chose; it ships to strangers.

    Asserted on the stamp the block ACTUALLY WRITES, from a checkout on a branch
    with a name of exactly the shape that leaks, rather than by searching
    build-installer.sh's text for `branch=`.
    """
    root, _sha = make_repo(tmp_path, name="src-branchleak")
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "-b",
                    "fix/a-customer-and-their-ticket-number"], check=True,
                   env={**os.environ, **GIT_ENV})
    bundle = tmp_path / "branchleak"
    _write_board(bundle / "web" / "dist", BOARD)
    script = tmp_path / "block-branchleak.sh"
    script.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + _stamp_block() + "\n")
    proc = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        env={**os.environ, **GIT_ENV, "ROOT": str(root), "BUNDLE": str(bundle)})
    assert proc.returncode == 0, proc.stderr
    stamp = (bundle / "BUILD_STAMP").read_text(encoding="utf-8")
    assert "a-customer-and-their-ticket-number" not in stamp, (
        "the build stamped a branch name into an artefact handed to third parties")
    assert sorted(_fields(stamp)) == ["board_sha256", "commit", "dirty"]


def test_an_unrecognised_stamp_field_fails(tmp_path, capsys, _default_repo):
    """The closed field set is what stops the next convenience field leaking."""
    repo, sha = _default_repo
    bundle = make_bundle(
        tmp_path, sha,
        stamp={"branch": "fix/a-customer-and-their-ticket-number"})
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1, f"an unreviewed field rode along silently: {out}"
    assert "does not know" in err
    assert "branch" in err


def test_the_ok_line_does_not_echo_stamp_free_text(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 0, err
    assert "on main" not in out and "branch" not in out


# --------------------------------------------------------------------------- #
# DEFECT 4 — the published export has its own history
# --------------------------------------------------------------------------- #

def test_a_commit_absent_from_the_repo_is_not_reported_as_staleness(tmp_path, capsys):
    """`build_public_export.py` gives the export a fresh init with ONE commit.

    A public consumer running the default `--repo .` must not be told "this is
    the stale-artefact failure; rebuild it" — that is false, and there is no
    value they could pass to --expect-commit instead.
    """
    export, _ = make_repo(tmp_path)          # stands in for the published repo
    bundle = make_bundle(tmp_path, "a" * 40)  # a commit that repo never had
    rc, out, err = run([str(bundle), "--repo", str(export)], capsys)
    assert rc == 1
    assert "is not an object in" in err
    assert "NOT the stale-artefact failure" in err
    assert "rebuild it" not in err
    assert "--allow-unknown-commit" in err


def test_allow_unknown_commit_passes_but_says_so_loudly(tmp_path, capsys, _default_repo):
    """rc=3, not 0: a caller reading only `$?` must be able to tell it apart.

    Against a board reading "THIS IS THE WRONG BOARD - 44 commits stale" this
    returned 0 with every caveat in stderr prose. `if verify_artefact.py …; then
    ship; fi` shipped it. Prose is not a signal a pipeline can act on.
    """
    export, _ = _default_repo
    stale = {**BOARD, "index.html": b"THIS IS THE WRONG BOARD - 44 commits stale"}
    bundle = make_bundle(tmp_path, "a" * 40, files=stale)
    rc, out, err = run([str(bundle), "--repo", str(export),
                        "--allow-unknown-commit"], capsys)
    assert rc == 3, f"unverified provenance was indistinguishable from OK: {out}"
    assert "PROVENANCE NOT VERIFIED" in err
    assert "was SKIPPED" in err
    assert "provenance NOT verified" in out


def test_allow_unknown_commit_does_not_disable_the_content_check(tmp_path, capsys, _default_repo):
    """The opt-out must weaken exactly one check, not become a bypass."""
    export, _ = _default_repo
    bundle = make_bundle(tmp_path, "a" * 40)
    next(bundle.rglob("index.html")).write_bytes(b"tampered")
    rc, out, err = run([str(bundle), "--repo", str(export),
                        "--allow-unknown-commit"], capsys)
    assert rc == 1
    assert "does not match its own stamp" in err


def test_a_repo_that_is_not_a_repo_fails(tmp_path, capsys):
    bundle = make_bundle(tmp_path, "a" * 40)
    notrepo = tmp_path / "notrepo"
    notrepo.mkdir()
    rc, out, err = run([str(bundle), "--repo", str(notrepo)], capsys)
    assert rc == 1
    assert "cannot resolve the expected commit" in err
    assert "--allow-unknown-commit" in err


def test_a_tarball_consumer_can_use_allow_unknown_commit(tmp_path, capsys):
    """No `.git` at all — a downloaded release ZIP, unpacked next to the DMG.

    `git rev-parse HEAD` fails BEFORE the honouring branch was ever reached, so
    the flag the error message recommends could not be taken: `rc=1  cannot
    resolve the expected commit from --repo .: fatal: not a git repository`,
    with nothing the consumer could do. Absent history is the exact condition
    --allow-unknown-commit exists for.
    """
    bundle = make_bundle(tmp_path, "a" * 40)
    notrepo = tmp_path / "unpacked-tarball"
    notrepo.mkdir()
    rc, out, err = run([str(bundle), "--repo", str(notrepo),
                        "--allow-unknown-commit"], capsys)
    assert rc == 3, f"the documented escape hatch was unreachable: {err}"
    assert "PROVENANCE NOT VERIFIED" in err
    assert "provenance NOT verified" in out


def test_allow_unknown_commit_on_a_tarball_still_checks_the_contents(tmp_path, capsys):
    bundle = make_bundle(tmp_path, "a" * 40)
    next(bundle.rglob("index.html")).write_bytes(b"tampered")
    notrepo = tmp_path / "unpacked-tarball2"
    notrepo.mkdir()
    rc, out, err = run([str(bundle), "--repo", str(notrepo),
                        "--allow-unknown-commit"], capsys)
    assert rc == 1
    assert "does not match its own stamp" in err


# --------------------------------------------------------------------------- #
# DEFECT 5 — fail cleanly, do not traceback
# --------------------------------------------------------------------------- #

needs_nonroot = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root reads unreadable files")


@needs_nonroot
def test_an_unreadable_board_file_fails_cleanly(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    victim = next(bundle.rglob("app.js"))
    victim.chmod(0o000)
    try:
        rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    finally:
        victim.chmod(0o644)
    assert rc == 1
    assert "cannot read bundled board file" in err


@needs_nonroot
def test_an_unreadable_stamp_fails_cleanly(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    stamp = next(bundle.rglob("BUILD_STAMP"))
    stamp.chmod(0o000)
    try:
        rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    finally:
        stamp.chmod(0o644)
    assert rc == 1
    assert "cannot be read" in err


def test_a_stamp_that_is_a_directory_fails_cleanly(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    stamp = next(bundle.rglob("BUILD_STAMP"))
    stamp.unlink()
    stamp.mkdir()
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1
    assert "not a regular file" in err


def test_a_stamp_line_that_is_not_key_value_fails(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    stamp = next(bundle.rglob("BUILD_STAMP"))
    stamp.write_text(stamp.read_text(encoding="utf-8") + "this is not a field\n")
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1
    assert "not key=value" in err


def test_a_missing_bundle_directory_fails_cleanly(tmp_path, capsys):
    rc, out, err = run([str(tmp_path / "nope")], capsys)
    assert rc == 1
    assert "not a directory" in err


# --------------------------------------------------------------------------- #
# DEFECT 8 — first-dist-wins, and the symlink divergence
# --------------------------------------------------------------------------- #

def test_a_decoy_dist_is_ambiguous_not_a_coin_toss(tmp_path, capsys, _default_repo):
    """rglob walks in directory order, so `AAA/dist` used to win outright."""
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    decoy = bundle / "AAA" / "dist"
    decoy.mkdir(parents=True)
    (decoy / "index.html").write_bytes(b"decoy")
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1
    assert "candidate board directories" in err
    assert "AAA/dist" in err


def test_a_symlink_in_the_board_is_reported_not_silently_hashed(tmp_path, capsys, _default_repo):
    """`find -type f` skips symlinks; `is_file()` follows them. They diverged."""
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    board = next(bundle.rglob("index.html")).parent
    (board / "alias.html").symlink_to("index.html")
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1
    assert "symlink" in err
    assert "UNVERIFIED" in err


def test_two_stamps_are_ambiguous(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    original = next(bundle.rglob("BUILD_STAMP"))
    (bundle / "BUILD_STAMP").write_text(original.read_text(encoding="utf-8"))
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 1
    assert "BUILD_STAMP files in the artefact" in err


# --------------------------------------------------------------------------- #
# require / forbid
# --------------------------------------------------------------------------- #

def test_require_and_forbid_read_the_bundled_bytes(tmp_path, capsys, _default_repo):
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    rc, out, err = run([str(bundle), "--repo", str(repo),
                        "--require", "a feature shipped this week",
                        "--forbid", "44 commits stale"], capsys)
    assert rc == 0, err
    rc, out, err = run([str(bundle), "--repo", str(repo),
                        "--require", "a feature shipped NEXT week"], capsys)
    assert rc == 1
    assert "MISSING required string" in err


def test_a_forbidden_string_that_IS_present_fails_the_artefact(tmp_path, capsys, _default_repo):
    """The seventh review's F3: `--forbid` had no positive case at all.

    The test above exercises only the ABSENT half, so the check could not fail
    and nothing observed that. Driven as a mutation against f209d60e —
    `if needle.encode() in blob:` -> `if False and needle.encode() in blob:`,
    verified applied at scripts/verify_artefact.py:536 — the whole suite stayed
    green at 137 passed.

    The final line is asserted too, and that is the point of this test rather
    than a flourish: the mutant still printed `1 forbidden string(s) checked`.
    A check that reports having run while doing nothing is worse than no check,
    because the sentence an operator reads is the same either way.
    """
    repo, sha = _default_repo
    stale = {**BOARD,
             "index.html": b"<!doctype html>THIS IS THE WRONG BOARD - 44 commits stale"}
    bundle = make_bundle(tmp_path, sha, files=stale)

    rc, out, err = run([str(bundle), "--repo", str(repo),
                        "--forbid", "44 commits stale"], capsys)
    assert rc == 1, out + err
    assert "CONTAINS forbidden string: '44 commits stale'" in err, err
    # ...and it must not also be printing the OK line it prints when it passes.
    assert "forbidden string(s) checked" not in out, out


def test_a_forbidden_string_is_found_ACROSS_the_whole_board_not_just_index(
        tmp_path, capsys, _default_repo):
    """The needle is sought in every board file, joined, not only in index.html.

    Without this, narrowing the search to a single file would leave the case
    above green — index.html is where the incident's marker happened to live.
    """
    repo, sha = _default_repo
    files = {**BOARD, "assets/app.css": b":root{--bg:#111}/* leaked-secret */"}
    bundle = make_bundle(tmp_path, sha, files=files)

    rc, out, err = run([str(bundle), "--repo", str(repo),
                        "--forbid", "leaked-secret"], capsys)
    assert rc == 1, out + err
    assert "CONTAINS forbidden string: 'leaked-secret'" in err, err


# --------------------------------------------------------------------------- #
# WHAT board_sha256 DOES NOT COVER — the seventh review's F4
# --------------------------------------------------------------------------- #

def test_the_stamp_digest_covers_ONLY_the_board_not_the_rest_of_the_bundle(
        tmp_path, capsys, _default_repo):
    """`make-dmg.sh`, `verify_artefact.py` and `docs/INSTALLER.md` all said the
    self-referential comparison proves "the DMG was not edited between being
    built and being packaged". It proves that of the BOARD, and this is the case
    that shows the difference: files planted beside the frozen server, in
    `migrations/`, and in the Electron layer all ship at a flat `OK`, rc=0.

    Like the mirror/bundle and ssh residuals, this asserts the artefact DOES
    verify, because that is what the corrected sentences now claim. If it ever
    starts failing, the digest has been widened and those three sentences — and
    the "widening is out of scope" paragraph beside them — are what need
    rewriting, not this test.
    """
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    srv = bundle / "no_human.app" / "Contents" / "Resources" / "nh-server"
    planted = ["_internal/evil.so", "_internal/base_library.zip",
               "migrations/003_backdoor.sql"]
    for rel in planted:
        p = srv / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"PLANTED AFTER THE BUILD")
    asar = bundle / "no_human.app" / "Contents" / "Resources" / "app.asar"
    asar.write_bytes(b"PLANTED ELECTRON CODE")

    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == 0, (
        "the stamp digest now notices something outside web/dist/. That is an "
        "IMPROVEMENT, not a failure — but three files describe the digest's "
        "scope as the board, so rewrite those rather than this test:\n"
        + out + err)
    assert "verify-artefact: OK" in out
    assert "board file(s) matching the stamped digest" in out
    # and nothing in the verdict mentioned any of the planted files
    for rel in planted:
        assert rel not in out and rel not in err, (rel, out, err)


# --------------------------------------------------------------------------- #
# the shell half: packaging/build-installer.sh's stamp block, run for real
# --------------------------------------------------------------------------- #

def _stamp_block() -> str:
    """The BUILD STAMP block, lifted out of build-installer.sh verbatim."""
    lines = INSTALLER.read_text(encoding="utf-8").splitlines()
    # Anchored on the section RULE, not on the words: `"BUILD STAMP" in ln and
    # ln.startswith("#")` also matched an ordinary comment that merely mentioned
    # the block by name, silently pulling `npm run build` and pyinstaller into
    # every extraction and turning 12 tests red for a reason nowhere near them.
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith("# ── BUILD STAMP"))
    end = next(i for i, ln in enumerate(lines) if i > start and ln.startswith("sql_count="))
    block = "\n".join(lines[start:end])
    assert "_stamp=" in block and "board_sha256" in block, "extraction missed the block"
    return block


def _ps1_board_digest_python() -> str:
    """The python source build-installer.ps1 hands to python.exe via `-c`, lifted
    out verbatim from between its `@'` … `'@` here-string delimiters."""
    lines = INSTALLER_PS1.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.rstrip().endswith("-c @'"))
    end = next(i for i, ln in enumerate(lines) if i > start and ln.startswith("'@"))
    src = "\n".join(lines[start + 1:end])
    # An empty extraction must fail here, not pass the guard vacuously.
    assert "hashlib.sha256" in src and "rglob" in src, "extraction missed the python -c block"
    return src


def test_ps1_board_digest_python_carries_no_double_quotes():
    """Windows PowerShell 5.1 — the ONLY PowerShell on a stock Windows box —
    drops embedded double-quotes when it marshals a `-c` argument across to the
    native python.exe, so `rglob("*")` reaches python as `rglob(*)`: a
    SyntaxError that empties the board digest and (correctly) trips the stamp
    gate. It shipped that way until a 2026-08-27 stock-5.1 build tripped it; CI
    never saw it because CI runs `pwsh`, which fixed native-argument passing.

    The durable fix is to keep this python source free of ASCII double-quotes —
    single quotes are byte-identical for these literals, so the .sh/.ps1 digest
    parity is untouched. This guard is a static text check that runs on EVERY
    platform, so a reintroduced double-quote goes red in CI rather than only on
    a stock-Windows release build (the one place the runtime gate can catch it).
    """
    src = _ps1_board_digest_python()
    assert '"' not in src, (
        "build-installer.ps1's `python -c` board-digest source contains an ASCII "
        'double-quote. PowerShell 5.1 strips it at the native-exe boundary, so '
        "python receives mangled source and the digest gate fails on stock "
        "Windows. Use single quotes (or chr(34)) instead — see this test's docstring."
    )


def _write_board(dist: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = dist / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _run_stamp_block(tmp_path: Path, *, path: str | None = None,
                     files: dict[str, bytes] | None = None,
                     name: str = "bundle", env_extra: dict[str, str] | None = None):
    """Run the extracted block against a fixture bundle. Returns (proc, stamp)."""
    root, _sha = make_repo(tmp_path, name=f"src-{name}")
    bundle = tmp_path / name
    _write_board(bundle / "web" / "dist", BOARD if files is None else files)

    script = tmp_path / f"block-{name}.sh"
    script.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + _stamp_block() + "\n")
    env = {**os.environ, **GIT_ENV, "ROOT": str(root), "BUNDLE": str(bundle)}
    env.update(env_extra or {})
    if path is not None:
        env["PATH"] = path
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                          env=env, cwd=str(tmp_path))
    stamp = bundle / "BUILD_STAMP"
    return proc, (stamp.read_text(encoding="utf-8") if stamp.exists() else None)


def _fields(stamp: str) -> dict[str, str]:
    return dict(ln.split("=", 1) for ln in stamp.splitlines() if "=" in ln)


def test_the_stamp_block_writes_a_well_formed_stamp(tmp_path):
    proc, stamp = _run_stamp_block(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert stamp is not None
    fields = _fields(stamp)
    assert sorted(fields) == ["board_sha256", "commit", "dirty"]
    assert re.fullmatch(r"[0-9a-f]{40}", fields["commit"])
    assert re.fullmatch(r"[0-9a-f]{64}", fields["board_sha256"])
    assert fields["dirty"] == "no"


def test_the_shell_digest_equals_the_python_one(tmp_path):
    """If these two ever disagree, every artefact fails its own gate."""
    proc, stamp = _run_stamp_block(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert _fields(stamp)["board_sha256"] == _digest(BOARD)


# Both hashers, because build-installer.sh accepts either and the Linux/Windows
# parity this stamp advertises runs on the sha256sum one.
_SHASUM = shutil.which("shasum")
_SHA256SUM = shutil.which("sha256sum")


def _only(tool: str, tmp_path: Path, tag: str) -> str:
    """A PATH where `tool` is the ONLY sha256 command available."""
    fakebin = tmp_path / f"onlybin-{tag}"
    fakebin.mkdir()
    for name in ("bash", "git", "grep", "find", "awk", "sort", "tr", "printf",
                 "sed", "uname", "cat", "dirname", "expr", "wc", "cut", "env",
                 "sh", "rm", "mkdir", "date", "head", "tail", "xargs"):
        real = shutil.which(name)
        if real:
            (fakebin / name).symlink_to(real)
    (fakebin / tool).symlink_to(shutil.which(tool))
    return str(fakebin)


def _utf8_locale() -> str | None:
    """A UTF-8 locale this host actually has, or None."""
    try:
        have = subprocess.run(["locale", "-a"], capture_output=True, text=True,
                              check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return None
    lowered = {s.lower(): s for s in have}
    for want in ("he_il.utf-8", "he_il.utf8", "en_us.utf-8", "en_us.utf8",
                 "c.utf-8", "c.utf8"):
        if want in lowered:
            return lowered[want]
    return None


@pytest.mark.parametrize("collation", ["C", "utf8"])
@pytest.mark.parametrize("tool", ["shasum", "sha256sum"])
def test_shell_python_digest_parity_on_a_hostile_tree(tmp_path, tool, collation):
    """The parity constraint, on the tree designed to break it.

    Spaces, a dotfile, subdirectories, Hebrew and emoji names, an empty file and
    two files with identical contents — under each hasher on its own, and under
    both an ambient UTF-8 collation and LC_ALL=C. The collation matters now that
    the PATH is inside each hashed line; a line of pure hex was immune to it,
    which is exactly why the path-blind version survived this check before.
    """
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} is not installed on this host")
    if collation == "C":
        loc = "C"
    else:
        loc = _utf8_locale()
        if loc is None:
            pytest.skip("no UTF-8 locale on this host")
    tag = f"{tool}-{collation}"
    proc, stamp = _run_stamp_block(
        tmp_path, files=HOSTILE, name=f"hostile-{tag}",
        path=_only(tool, tmp_path, tag),
        env_extra={"LC_ALL": loc, "LANG": loc})
    assert proc.returncode == 0, proc.stderr
    assert _fields(stamp)["board_sha256"] == _digest(HOSTILE), proc.stderr


def test_the_shell_digest_changes_when_two_paths_are_swapped(tmp_path):
    """The shell half must see the swap too, or it stamps a digest that lies."""
    files = {"index.html": b"THE CURRENT BOARD",
             "assets/app.js": b"THIS IS THE WRONG BOARD - 44 commits stale"}
    swapped = {"index.html": files["assets/app.js"],
               "assets/app.js": files["index.html"]}
    proc_a, stamp_a = _run_stamp_block(tmp_path, files=files, name="swap-a")
    proc_b, stamp_b = _run_stamp_block(tmp_path, files=swapped, name="swap-b")
    assert proc_a.returncode == 0 and proc_b.returncode == 0
    a = _fields(stamp_a)["board_sha256"]
    b = _fields(stamp_b)["board_sha256"]
    assert a != b, "the shell digest is path-blind: a swap left it identical"
    assert a == _digest(files) and b == _digest(swapped)


def test_the_build_aborts_when_one_board_file_cannot_be_hashed(tmp_path):
    """A per-file hash that is not 64 hex must abort, not average out.

    The digest is a hash OF a hash list; a blank or truncated entry still yields
    a perfectly well-formed 64-hex result, so the shape check on the FINAL value
    cannot see it. The check has to be inside the loop, and `set -o pipefail`
    has to carry it out through the pipeline.
    """
    fakebin = tmp_path / "bin-halfbroken"
    fakebin.mkdir()
    (fakebin / "shasum").write_text(
        "#!/bin/sh\n"
        "# hashes normally, but returns nothing for one specific input size\n"
        'data="$(cat)"\n'
        'case "$data" in *app.css*|*"--bg"*) exit 0;; esac\n'
        'printf %s "$data" | /usr/bin/shasum -a 256\n')
    (fakebin / "shasum").chmod(0o755)
    proc, stamp = _run_stamp_block(
        tmp_path, name="halfbroken",
        path=f"{fakebin}:{os.environ.get('PATH', '')}")
    assert proc.returncode != 0, f"a board file hashed to nothing: {stamp!r}"
    assert "could not hash board file" in proc.stderr
    assert stamp is None


def test_the_build_aborts_when_the_digest_cannot_be_computed(tmp_path):
    """The host without `shasum` — where the empty `board_sha256=` came from.

    Under `set -euo pipefail` a command substitution that fails INSIDE an
    `echo` argument does not propagate: `echo` succeeds. So the build wrote a
    blank digest, exited 0, and shipped an artefact that passed its own gate
    regardless of its contents.
    """
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    for tool in ("bash", "git", "grep", "find", "awk", "sort", "tr", "printf",
                 "sed", "uname", "cat", "dirname", "expr", "wc"):
        real = shutil.which(tool)
        if real:
            (fakebin / tool).symlink_to(real)
    assert shutil.which("shasum", path=str(fakebin)) is None
    assert shutil.which("sha256sum", path=str(fakebin)) is None

    proc, stamp = _run_stamp_block(tmp_path, path=str(fakebin))
    assert proc.returncode != 0, (
        f"the build carried on with no hasher; stamp={stamp!r}")
    assert "neither shasum nor sha256sum" in proc.stderr
    assert stamp is None or "board_sha256=\n" not in stamp


def test_the_build_aborts_when_the_hasher_returns_junk(tmp_path):
    """Present-but-broken is the other way an empty digest reaches the stamp.

    `command -v shasum` succeeding is not the same as it WORKING, so the value
    is checked for shape before it is written, not merely for existence.
    """
    fakebin = tmp_path / "bin2"
    fakebin.mkdir()
    (fakebin / "shasum").write_text("#!/bin/sh\nexit 0\n")   # succeeds, prints nothing
    (fakebin / "shasum").chmod(0o755)
    path = f"{fakebin}:{os.environ.get('PATH', '')}"
    proc, stamp = _run_stamp_block(tmp_path, path=path)
    assert proc.returncode != 0, f"a junk digest was written: {stamp!r}"
    assert "is not a" in proc.stderr and "sha256" in proc.stderr
    assert stamp is None


def test_the_build_aborts_outside_a_git_checkout(tmp_path):
    root = tmp_path / "nogit"
    root.mkdir()
    bundle = tmp_path / "bundle2"
    (bundle / "web" / "dist").mkdir(parents=True)
    (bundle / "web" / "dist" / "index.html").write_bytes(b"x")
    script = tmp_path / "block2.sh"
    script.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + _stamp_block() + "\n")
    proc = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        env={**os.environ, "ROOT": str(root), "BUNDLE": str(bundle)})
    assert proc.returncode != 0
    assert "cannot resolve HEAD" in proc.stderr
    assert not (bundle / "BUILD_STAMP").exists()


# --------------------------------------------------------------------------- #
# DEFECT 3 — a gate nobody runs is not a gate
#
# These used to be regex-over-source assertions, and they were WORTHLESS: a
# reviewer deleted the real `hdiutil detach "${v_dev}" -force` — leaking a
# mounted volume on both exit paths — and the suite still reported 38 passed,
# because `dmg.rindex("hdiutil detach")` then matched the `vcleanup()`
# DEFINITION, which is also before the decision index.
#
# The block is now EXTRACTED and RUN with a stub `hdiutil` that announces every
# call on stderr, so the assertions are on observed calls, their ORDER relative
# to the decision, and the exit code. The vcleanup trap redirects `2>&1` to
# /dev/null, so a detach that comes only from the trap is invisible in stderr —
# which is precisely what makes the deletion mutation go red.
# --------------------------------------------------------------------------- #

def _dmg_verify_block() -> str:
    """The 'what is INSIDE the DMG' block, lifted out of make-dmg.sh verbatim."""
    lines = MAKEDMG.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith('echo "==> verifying the CONTENTS'))
    block = "\n".join(lines[start:])
    assert "verify_artefact.py" in block and "verify_rc" in block, (
        "extraction missed the verification block")
    return block


def _dmg_harness(tmp_path: Path, tag: str, *, board: dict[str, bytes],
                 commit: str, sign_mode: str = "unsigned",
                 dirty: str = "no", root: Path | None = None,
                 mutate=None, env_extra: dict[str, str] | None = None):
    """Run make-dmg.sh's verification block for real, against stub `hdiutil`.

    The mount is stubbed; everything downstream of it is the real thing — the
    real verify_artefact.py, reading a real fixture bundle, deciding a real exit
    status. Returns the CompletedProcess.
    """
    work = tmp_path / tag
    work.mkdir(parents=True)

    if root is None:
        root, _ = make_repo(tmp_path, name=f"root-{tag}")
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "verify_artefact.py").write_bytes(SCRIPT.read_bytes())
    venv = root / ".venv" / "bin"
    venv.mkdir(parents=True, exist_ok=True)
    if not (venv / "python").exists():
        (venv / "python").symlink_to(sys.executable)

    # The block greps the attach output for '/Volumes/', so the stub mountpoint
    # has to contain that segment; keep it inside tmp_path so nothing real is
    # touched and nothing is left mounted.
    mnt = work / "Volumes" / "no_human"
    inner = mnt / "no_human.app" / "Contents" / "Resources" / "nh-server"
    _write_board(inner / "web" / "dist", board)
    (inner / "BUILD_STAMP").write_text(
        f"commit={commit}\ndirty={dirty}\nboard_sha256={_digest(board)}\n")

    bindir = work / "bin"
    bindir.mkdir()
    (bindir / "hdiutil").write_text(
        '#!/bin/sh\n'
        'echo "HDIUTIL $*" >&2\n'
        'if [ "$1" = "attach" ]; then\n'
        '  printf \'/dev/disk9\\tApple_HFS\\t%s\\n\' "$NH_TEST_MNT"\n'
        'fi\n'
        'exit 0\n')
    (bindir / "hdiutil").chmod(0o755)

    text = _dmg_verify_block()
    if mutate is not None:
        text = mutate(text)
    script = work / "block.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f'ROOT={_q(root)}\nVOL="nh-test-{tag}"\nDMG={_q(work / "x.dmg")}\n'
        f'SIGN_MODE="{sign_mode}"\n'
        'allow_dirty=""\n'
        '[ "${SIGN_MODE}" = "signed" ] || allow_dirty="--allow-dirty"\n'
        + text + "\n")
    (work / "x.dmg").write_bytes(b"not really a dmg")

    env = {**os.environ, **GIT_ENV,
           "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
           "NH_TEST_MNT": str(mnt)}
    env.update(env_extra or {})
    return subprocess.run(["bash", str(script)], capture_output=True, text=True,
                          env=env, cwd=str(work))


def _q(p) -> str:
    return "'" + str(p).replace("'", "'\\''") + "'"


STALE_BOARD = {**BOARD, "index.html": b"THIS IS THE WRONG BOARD - 44 commits stale"}


def test_the_dmg_pipeline_ships_a_matching_artefact(tmp_path):
    """The control: the verifier runs, passes, and the block exits 0.

    This build is UNSIGNED and clean, and its `OK` used to be flat — which is
    why this test read as coverage of the unsigned path while a 45-commit-stale
    board walked through it. It asserts the QUALIFIED verdict now, so it can
    never again be mistaken for that coverage; the property itself is pinned by
    test_an_unsigned_build_of_a_stale_CLEAN_checkout_is_not_a_flat_ok.
    """
    root, sha = make_repo(tmp_path, name="root-ok")
    proc = _dmg_harness(tmp_path, "ok", board=BOARD, commit=sha, root=root)
    assert proc.returncode == 0, proc.stderr
    assert "verify-artefact: OK (provenance NOT verified)" in proc.stdout
    assert "OK: " in proc.stdout


def test_the_dmg_pipeline_refuses_an_artefact_the_verifier_rejects(tmp_path):
    """The verifier's result must DECIDE the exit status, not merely be printed."""
    root, sha = make_repo(tmp_path, name="root-bad")
    proc = _dmg_harness(tmp_path, "bad", board=BOARD,
                        commit="c12a5f0f9256" + "0" * 28, root=root)
    assert proc.returncode == 1
    assert "Do not distribute this file" in proc.stderr


def test_the_dmg_pipeline_fails_when_the_verifier_is_missing(tmp_path):
    """A gate that is skipped when its own tool is absent is not a gate."""
    root, sha = make_repo(tmp_path, name="root-noverifier")
    proc = _dmg_harness(
        tmp_path, "noverifier", board=BOARD, commit=sha, root=root,
        mutate=lambda t: t.replace("scripts/verify_artefact.py",
                                   "scripts/does_not_exist.py"))
    assert proc.returncode != 0
    assert "Do not distribute this file" in proc.stderr


def test_the_dmg_pipeline_detaches_before_deciding(tmp_path):
    """A failure must not leave /Volumes/no_human mounted for the next build.

    Observed, not read: the stub announces `HDIUTIL detach` on stderr, and the
    explicit detach is the only one whose stderr survives (the EXIT trap sends
    its own to /dev/null). So the marker must be present AND precede the FAIL
    block. Deleting the explicit detach removes the marker entirely.
    """
    root, sha = make_repo(tmp_path, name="root-detach")
    proc = _dmg_harness(tmp_path, "detach", board=BOARD,
                        commit="c12a5f0f9256" + "0" * 28, root=root)
    assert proc.returncode == 1
    assert "HDIUTIL detach /dev/disk9" in proc.stderr, (
        "the verifier's failure path never detached the volume it mounted")
    assert proc.stderr.index("HDIUTIL detach") < proc.stderr.index("FAIL:"), (
        "the volume was still mounted when the pipeline decided to fail")


def test_the_detach_mutation_is_actually_caught(tmp_path):
    """The mutation the source-text version survived, pinned as a test.

    Deleting the real detach leaves the volume mounted until the EXIT trap fires
    after `exit 1`. If this ever passes, the test above has stopped observing
    anything.
    """
    root, sha = make_repo(tmp_path, name="root-mutant")
    proc = _dmg_harness(
        tmp_path, "mutant", board=BOARD, commit="c12a5f0f9256" + "0" * 28,
        root=root,
        mutate=lambda t: t.replace(
            'hdiutil detach "${v_dev}" -force >/dev/null\ntrap - EXIT',
            'trap - EXIT'))
    assert proc.returncode == 1
    assert "HDIUTIL detach /dev/disk9" not in proc.stderr, (
        "the mutation was supposed to remove the only observable detach")


# --------------------------------------------------------------------------- #
# DEFECT 1 (second review) — the gate must not be grounded in the same checkout
# that wrote the stamp, or it can only ever catch tampering, never staleness
# --------------------------------------------------------------------------- #

def _origin_path(tmp_path: Path, tag: str) -> Path:
    """Where `_repo_with_origin` puts the release remote for `tag`.

    One source of truth, so a test that has to reach INTO the remote (to plant a
    tag, or to repoint it) cannot drift from where the helper actually built it.
    """
    return tmp_path / f"origin-{tag}"


def _repo_with_origin(tmp_path: Path, tag: str, behind: int):
    """A checkout `behind` commits behind its origin's main. Returns (root, tip)."""
    origin, _ = make_repo(tmp_path, name=_origin_path(tmp_path, tag).name, commits=1)
    root = tmp_path / f"clone-{tag}"
    subprocess.run(["git", "clone", "-q", str(origin), str(root)], check=True,
                   env={**os.environ, **GIT_ENV})
    head = _git(root, "rev-parse", "HEAD")
    for i in range(behind):
        subprocess.run(["git", "-C", str(origin), "commit", "-q", "--allow-empty",
                        "-m", f"newer {i}"], check=True,
                       env={**os.environ, **GIT_ENV})
    tip = _git(origin, "rev-parse", "HEAD")
    return root, head, tip


def test_a_signed_build_from_a_stale_checkout_is_refused(tmp_path):
    """The incident, end to end: 45 commits behind, clean tree, everything green.

    Grounded in `--repo "${ROOT}"` this was a tautology — the expectation came
    from the same checkout build-installer.sh had read the stamp from — and the
    block printed `verify-artefact: OK — built from 8d8a33130a12, clean tree`,
    rc=0. The expectation now comes from the REMOTE.
    """
    root, head, tip = _repo_with_origin(tmp_path, "stale", behind=45)
    assert head != tip
    proc = _dmg_harness(tmp_path, "stale", board=BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 1, (
        f"a signed DMG built 45 commits behind main passed:\n{proc.stdout}")
    assert "stale-artefact failure" in proc.stderr
    assert "Do not distribute this file" in proc.stderr


def test_a_signed_build_from_the_remote_tip_passes(tmp_path):
    """The control — otherwise the test above could be passing for any reason."""
    root, head, tip = _repo_with_origin(tmp_path, "current", behind=0)
    assert head == tip
    proc = _dmg_harness(tmp_path, "current", board=BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 0, proc.stderr
    assert "verify-artefact: OK" in proc.stdout
    assert "provenance NOT verified" not in proc.stdout


def test_an_unreachable_remote_is_a_failure_not_a_skip(tmp_path):
    """`git fetch … || true` is the fail-open shape this project keeps hitting.

    If the tip cannot be resolved, the commit this release is supposed to BE
    cannot be established. That is the gate's input missing, which is a FAILURE.
    """
    root, head, tip = _repo_with_origin(tmp_path, "unreachable", behind=45)
    subprocess.run(["git", "-C", str(root), "remote", "set-url", "origin",
                    str(tmp_path / "does-not-exist-at-all")], check=True,
                   env={**os.environ, **GIT_ENV})
    proc = _dmg_harness(tmp_path, "unreachable", board=BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 1, (
        f"an unreachable remote was treated as permission to skip:\n{proc.stdout}")
    assert "cannot reach origin" in proc.stderr
    assert "FAILURE, not a skip" in proc.stderr


def test_a_stale_local_main_cannot_stand_in_for_the_remote(tmp_path):
    """The second of the three defeats: the local ref is not the remote's word.

    The old check fell back to a local `main` when `origin/main` was absent, so
    a clone whose refs were 45 commits old satisfied it. Resolving FETCH_HEAD
    means the answer always comes from the fetch that just ran.
    """
    root, head, tip = _repo_with_origin(tmp_path, "localmain", behind=45)
    # the clone's own refs still point at the old tip; only the remote moved
    assert _git(root, "rev-parse", "refs/remotes/origin/main") == head
    proc = _dmg_harness(tmp_path, "localmain", board=BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 1, (
        f"a stale local main was accepted as the release tip:\n{proc.stdout}")
    assert "stale-artefact failure" in proc.stderr


def test_nh_allow_stale_build_cannot_waive_the_signed_check(tmp_path):
    """The third defeat. That flag may make a point-in-time BUILD; not a signed one."""
    root, head, tip = _repo_with_origin(tmp_path, "waive", behind=45)
    proc = _dmg_harness(tmp_path, "waive", board=BOARD, commit=head,
                        sign_mode="signed", root=root,
                        env_extra={"NH_ALLOW_STALE_BUILD": "1"})
    assert proc.returncode == 1, (
        f"NH_ALLOW_STALE_BUILD waived the release gate:\n{proc.stdout}")
    assert "stale-artefact failure" in proc.stderr


def test_a_signed_build_outside_a_git_checkout_is_refused(tmp_path):
    nogit = tmp_path / "nogit-root"
    nogit.mkdir()
    proc = _dmg_harness(tmp_path, "nogit", board=BOARD, commit="a" * 40,
                        sign_mode="signed", root=nogit)
    assert proc.returncode == 1
    assert "not a git checkout" in proc.stderr


def test_an_unsigned_build_reports_provenance_as_unverified(tmp_path):
    """Unsigned builds carry --allow-dirty, so their verdict is rc=3, not a flat OK.

    The block accepts that — the filename and banner already say the artefact is
    not distributable — but it must SAY so rather than print an unqualified OK.
    """
    root, sha = make_repo(tmp_path, name="root-unsigned-dirty")
    proc = _dmg_harness(tmp_path, "unsigneddirty", board=BOARD, commit=sha,
                        dirty="yes", root=root)
    assert proc.returncode == 0, proc.stderr
    assert "provenance NOT verified" in proc.stdout
    assert "provenance for" in proc.stderr and "NOT verified" in proc.stderr


def test_a_signed_build_never_accepts_an_unverified_verdict(tmp_path):
    """rc=3 must not be waved through on the signed path.

    Reached here by handing the signed path a dirty stamp; the release path
    passes no weakening flag, so the verifier fails it outright rather than
    downgrading — either way the DMG does not ship.
    """
    root, head, tip = _repo_with_origin(tmp_path, "signeddirty", behind=0)
    proc = _dmg_harness(tmp_path, "signeddirty", board=BOARD, commit=head,
                        dirty="yes", sign_mode="signed", root=root)
    assert proc.returncode == 1
    assert "Do not distribute this file" in proc.stderr


# --------------------------------------------------------------------------- #
# DEFECT 1 (third review) — the UNSIGNED path printed a flat OK over a board 45
# commits stale, on a CLEAN tree
#
# Every test above that touches the unsigned path passes `dirty="yes"`, which is
# the single input under which `--allow-dirty` downgrades anything. The incident
# checkout was CLEAN. On a clean tree `--allow-dirty` downgrades NOTHING, the
# expectation still came from `--repo "${ROOT}"` — the checkout that built the
# artefact — and the block printed
#
#     verify-artefact: OK — built from 0ca24000e3ac, clean tree
#     OK: …x.dmg [unsigned]
#
# with rc=0 and no caveat anywhere, over a board reading "THIS IS THE WRONG
# BOARD". The tests below pin the property under CLEAN, which is where the
# guarantee is weakest, not under dirty, which is where it was easiest.
#
# The fix is not "check the unsigned build against the remote too" — that would
# make every offline local build fail, and an offline build is legitimate. It is
# that the CALLER declares the expectation is self-referential, and a
# self-referential comparison can never yield "verified".
# --------------------------------------------------------------------------- #

def test_the_checkout_that_built_it_cannot_verify_it(tmp_path, capsys, _default_repo):
    """A repo compared against itself proves the DMG was not edited. Nothing more.

    `--repo-built-this-artefact` is the caller saying "this repo IS the build
    checkout", which makes `stamp.commit == HEAD` a tautology: a clone 45 commits
    behind main satisfies it exactly as well as the tip does.
    """
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    rc, out, err = run([str(bundle), "--repo", str(repo),
                        "--repo-built-this-artefact"], capsys)
    assert rc == va.RC_UNVERIFIED, (
        f"a self-referential comparison reported verified provenance: {out}")
    assert "provenance NOT verified" in out
    assert "PROVENANCE NOT VERIFIED" in err
    assert "which source" in err.lower()


def test_without_that_flag_the_same_comparison_is_a_real_check(tmp_path, capsys, _default_repo):
    """The control. A CONSUMER holding their own clone is doing a real check.

    Without this, the test above would pass if the flag did nothing and the
    default path had simply been broken to always return 3.
    """
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    rc, out, err = run([str(bundle), "--repo", str(repo)], capsys)
    assert rc == va.RC_OK, err
    assert "provenance NOT verified" not in out


def test_declaring_the_repo_built_it_alongside_an_expectation_is_a_usage_error(
        tmp_path, capsys, _default_repo):
    """The two are contradictory: one says self-referential, one names a source.

    Ambiguous input is a FAILURE, never a quiet choice of one of them.
    """
    repo, sha = _default_repo
    bundle = make_bundle(tmp_path, sha)
    rc, out, err = run([str(bundle), "--repo", str(repo), "--expect-commit", sha,
                        "--repo-built-this-artefact"], capsys)
    assert rc == va.RC_USAGE
    assert "contradict" in err.lower()


def test_a_tampered_artefact_still_fails_under_that_flag(tmp_path, capsys):
    """The flag downgrades the VERDICT; it must not disable the comparison.

    A stamp naming a commit this checkout never built is still a failure — that
    is the one thing a self-referential comparison genuinely proves.
    """
    repo, sha = make_repo(tmp_path, commits=3)
    other = _git(repo, "rev-parse", "HEAD~2")
    bundle = make_bundle(tmp_path, other)
    rc, out, err = run([str(bundle), "--repo", str(repo),
                        "--repo-built-this-artefact"], capsys)
    assert rc == va.RC_FAILED
    assert "stale-artefact failure" in err


def test_an_unsigned_build_of_a_stale_CLEAN_checkout_is_not_a_flat_ok(tmp_path):
    """The demonstrated defect, end to end: unsigned, CLEAN, 45 behind, wrong board.

    The build is still allowed — an unsigned point-in-time DMG is a legitimate
    thing to make, and its filename and banner already say it is not
    distributable — but it must not come back as an unqualified OK.
    """
    root, head, tip = _repo_with_origin(tmp_path, "unsignedclean", behind=45)
    assert head != tip
    proc = _dmg_harness(tmp_path, "unsignedclean", board=STALE_BOARD,
                        commit=head, dirty="no", root=root)
    assert proc.returncode == 0, proc.stderr
    assert "provenance NOT verified" in proc.stdout, (
        f"a 45-behind clean unsigned build printed an unqualified OK:\n"
        f"{proc.stdout}")
    ok = [ln for ln in proc.stdout.splitlines() if ln.startswith("OK: ")]
    assert ok, f"the block printed no final verdict line:\n{proc.stdout}"
    assert "provenance NOT verified" in ok[0], (
        "the final line an operator reads is still a flat OK: " + ok[0])
    assert "provenance for" in proc.stderr and "NOT verified" in proc.stderr


# --------------------------------------------------------------------------- #
# DEFECT 2 (third review) — NH_RELEASE_REMOTE / NH_RELEASE_BRANCH WERE the
# NH_ALLOW_STALE_BUILD equivalent the script and the docs said did not exist
#
#     NH_RELEASE_REMOTE=$ROOT NH_RELEASE_BRANCH=HEAD   # signed, 45 behind
#     verify-artefact: OK — built from d7f611faad97
#     OK: …x.dmg [signed]      rc=0
#
# Two env vars pointed the release gate at the artefact's own checkout, which is
# precisely the tautology the signed path was rewritten to escape. They are gone;
# the remote and branch are literals.
# --------------------------------------------------------------------------- #

def test_env_vars_cannot_redirect_the_release_gate(tmp_path):
    """The demonstrated bypass, with the exact values the reviewer used."""
    root, head, tip = _repo_with_origin(tmp_path, "redirect", behind=45)
    proc = _dmg_harness(tmp_path, "redirect", board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root,
                        env_extra={"NH_RELEASE_REMOTE": str(root),
                                   "NH_RELEASE_BRANCH": "HEAD"})
    assert proc.returncode == 1, (
        f"env vars pointed the release gate at the build's own checkout:\n"
        f"{proc.stdout}")
    assert "stale-artefact failure" in proc.stderr
    assert "Do not distribute this file" in proc.stderr


def test_an_origin_that_points_at_the_build_checkout_is_refused(tmp_path):
    """The same tautology reached through git config rather than the environment.

    `origin` repointed at ROOT makes `refs/heads/main` on the "remote" the build
    checkout's own stale branch, so the fetch succeeds, FETCH_HEAD is a commit,
    and the stamped commit matches it. Every fail-closed step passes and the
    answer is still worthless.
    """
    root, head, tip = _repo_with_origin(tmp_path, "selfref", behind=45)
    subprocess.run(["git", "-C", str(root), "remote", "set-url", "origin",
                    str(root)], check=True, env={**os.environ, **GIT_ENV})
    proc = _dmg_harness(tmp_path, "selfref", board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 1, (
        f"the release gate resolved its expectation from the artefact's own "
        f"checkout:\n{proc.stdout}")
    assert "resolves to this very repository" in proc.stderr


@pytest.mark.parametrize("url", [".", ".."])
def test_a_RELATIVE_origin_pointing_at_the_build_checkout_is_refused(tmp_path, url):
    """The same tautology through a RELATIVE url — the weakest input for this check.

    `git -C "${ROOT}" fetch` resolves a relative local url against ${ROOT}. The
    first version of the self-reference check tested `[ -d "${remote_path}" ]`
    in the SHELL's working directory instead, and make-dmg.sh is not run from
    ${ROOT} — `npm run dist:bundled` runs it from ${ROOT}/desktop. So the two
    disagreed, the `&&` chain evaluated to "not self-referential", and the gate
    fetched the artefact's own checkout: signed, 45 behind, wrong board, flat
    `OK: …x.dmg [signed]`, rc=0. An independent reviewer drove it.

    The same config gave two different verdicts depending on where the script
    was invoked from, which is the tell.

    `..` is the second half of the property: it must resolve against ${ROOT}
    (giving ${ROOT}/.., NOT this checkout) rather than against the CWD, so it
    must NOT be flagged — otherwise a passing test here would only prove the
    check had been broken into rejecting everything.
    """
    root, head, tip = _repo_with_origin(tmp_path, f"relself{url.count('.')}",
                                        behind=45)
    if url == "..":
        # ${ROOT}/.. is tmp_path, which is not a git checkout at all: the
        # self-reference check must not fire, and the build must fail for the
        # HONEST reason instead — the fetch cannot resolve a release tip.
        pass
    subprocess.run(["git", "-C", str(root), "remote", "set-url", "origin", url],
                   check=True, env={**os.environ, **GIT_ENV})
    proc = _dmg_harness(tmp_path, f"relself{url.count('.')}", board=STALE_BOARD,
                        commit=head, sign_mode="signed", root=root)
    assert proc.returncode == 1, (
        f"a relative origin url resolved against the shell's CWD instead of "
        f"${{ROOT}}, and a 45-behind signed build shipped:\n{proc.stdout}")
    if url == ".":
        assert "resolves to this very repository" in proc.stderr, proc.stderr
    else:
        assert "resolves to this very repository" not in proc.stderr, (
            "${ROOT}/.. is not this checkout; the check fired on the wrong "
            "thing, so it would reject legitimate relative remotes too")


# --------------------------------------------------------------------------- #
# DEFECT 1 (fourth review) — the self-reference check compared the WRONG git
# identity, so a LINKED WORKTREE of the build checkout walked straight through
#
#     origin/main = 7a3937014813c9c148c6185a4be3452aca520307   <- ROOT's own ref
#     verify-artefact: OK — built from 7a3937014813, clean tree
#     OK: …/x.dmg (4.0K) [signed]                              rc=0
#     (the real origin/main tip was 796c4af4af65)
#
# The check asked `git rev-parse --absolute-git-dir`, which is PER-WORKTREE:
# ${ROOT} answers `${ROOT}/.git` while a linked worktree of ${ROOT} answers
# `${ROOT}/.git/worktrees/<name>`. Two different strings, so the check did not
# fire — and yet the two share ONE object store and ONE `refs/heads/main`, so
# `git fetch origin refs/heads/main` handed back the build checkout's own stale
# local branch. FETCH_HEAD "as the remote said it just now" was the stale local
# main, which make-dmg.sh's own comment lists as a demonstrated defeat.
#
# This is NOT the documented limit. That limit is "a remote that merely holds
# the SAME COMMITS — a bare mirror or a second clone". A linked worktree is
# neither: it IS the same repository, and it shares the ref that answers here.
#
# The `[".", ".."]` parametrization above could not distinguish the two verbs
# the check was choosing between at the time: `.` resolves to ${ROOT} itself,
# where `--absolute-git-dir` and `--git-common-dir` give the SAME answer, and
# `..` is not a checkout at all. Only a linked worktree splits them, which is
# why this survived three reviews.
#
# The check no longer asks either verb — the FIFTH review retired the whole
# path derivation (see the section at the end of this file) — so what makes
# these two tests pass today is that a worktree ADVERTISES the probe ref
# planted in ${ROOT}. The defect and both orientations are unchanged; only the
# mechanism that catches them is.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("orientation", ["origin-is-linked", "root-is-linked"])
def test_an_origin_that_is_a_LINKED_WORKTREE_of_the_build_checkout_is_refused(
        tmp_path, orientation):
    """A linked worktree is the SAME repository, not a second clone.

    Both orientations are driven because the comparison has two sides and a
    half-applied fix passes one of them:

    * `origin-is-linked` — ${ROOT} is the primary checkout, `origin` is a linked
      worktree of it. The probe is planted in the primary and has to be visible
      through the worktree.
    * `root-is-linked` — the build runs from a linked worktree and `origin` is
      the primary checkout. The probe is planted through the worktree and has
      to be visible in the primary.

    Under the derivation this replaced, the two sides needed the fix applied
    separately and a half-applied one passed exactly one of them; the probe has
    only one side, and both orientations are kept because the defect has two.

    In both, the fetch succeeds, FETCH_HEAD is a real 40-hex commit, and it is
    the build checkout's own `refs/heads/main` — 45 commits behind the release
    tip — so every fail-closed step reports success and the answer is the
    tautology this gate exists to end.
    """
    tag = f"wtself{orientation.count('-')}{orientation[0]}"
    root, head, tip = _repo_with_origin(tmp_path, tag, behind=45)
    assert head != tip
    env = {**os.environ, **GIT_ENV}
    linked = tmp_path / f"linked-{tag}"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "--detach",
                    str(linked), "HEAD"], check=True, env=env)

    if orientation == "origin-is-linked":
        build_root, remote_target = root, linked
    else:
        build_root, remote_target = linked, root

    # `remote.origin.url` lives in the COMMON config, shared by both worktrees,
    # so this one call repoints it whichever of the pair is ${ROOT}.
    subprocess.run(["git", "-C", str(build_root), "remote", "set-url", "origin",
                    str(remote_target)], check=True, env=env)

    # The precondition that makes this a real defeat rather than a broken
    # fixture: the "remote" really does serve the build checkout's stale main.
    assert _git(remote_target, "rev-parse", "refs/heads/main") == head != tip

    proc = _dmg_harness(tmp_path, tag, board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=build_root)
    assert proc.returncode == 1, (
        f"a linked worktree of the build checkout was accepted as the release "
        f"remote, so origin/main resolved to ${{ROOT}}'s own stale branch and a "
        f"signed 45-behind DMG shipped:\n{proc.stdout}")
    assert "resolves to this very repository" in proc.stderr, proc.stderr
    # and it must be refused BEFORE the fetch, not by luck downstream
    assert "stale-artefact failure" not in proc.stderr, (
        "refused for the wrong reason — the self-reference check did not fire")


def test_rc3_from_a_SIGNED_build_is_never_collapsed(tmp_path):
    """make-dmg.sh turns rc=3 into 0 — for UNSIGNED builds only. Pin the guard.

    A fourth review flagged the collapse as "prose is not a signal a pipeline
    can act on" one layer out. The decision recorded in make-dmg.sh is that the
    collapse stays, because the FILENAME carries the same predicate: `exit 0`
    plus an untagged name means verified, `exit 0` plus `-UNSIGNED` /
    `-UNNOTARIZED` means it is not.

    That argument only holds if a SIGNED build can never reach `exit 0` with
    unverified provenance. No weakening flag is passed when signed, so rc=3
    cannot arise naturally — which is exactly why it is fault-injected here
    rather than assumed. Widen the collapse to all modes and this goes red.
    """
    root, head, tip = _repo_with_origin(tmp_path, "rc3signed", behind=0)
    proc = _dmg_harness(
        tmp_path, "rc3signed", board=BOARD, commit=head, sign_mode="signed",
        root=root,
        mutate=lambda t: t.replace(
            '"${PY}" "${ROOT}/scripts/verify_artefact.py"', 'sh -c "exit 3" '))
    assert proc.returncode == 1, (
        f"a signed build reported unverified provenance and still exited 0:\n"
        f"{proc.stdout}")
    assert "does not match origin/main" in proc.stderr, proc.stderr
    assert "rc=3" in proc.stderr
    assert "Do not distribute this file" in proc.stderr
    assert "OK: " not in proc.stdout


def test_a_signed_build_still_passes_from_a_genuine_remote(tmp_path):
    """The control for both tests above: a real origin at the tip still ships."""
    root, head, tip = _repo_with_origin(tmp_path, "genuine", behind=0)
    proc = _dmg_harness(tmp_path, "genuine", board=BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 0, proc.stderr
    assert "provenance NOT verified" not in proc.stdout


# --------------------------------------------------------------------------- #
# DEFECT 3 (third review) — an unqualified refspec lets a TAG shadow the branch
#
#     $ git fetch origin main
#      * tag               main       -> FETCH_HEAD
#
# No env var, no config change to the build checkout: a tag named `main` pushed
# to the release remote resolves ahead of the branch of the same name, so
# FETCH_HEAD became whatever the tag pointed at. A signed build 45 commits behind
# printed a flat OK, rc=0, [signed].
# --------------------------------------------------------------------------- #

def test_a_tag_named_main_cannot_shadow_the_release_branch(tmp_path):
    root, head, tip = _repo_with_origin(tmp_path, "tagshadow", behind=45)
    origin = _origin_path(tmp_path, "tagshadow")
    # a tag named exactly like the release branch, pointing at the stale commit
    subprocess.run(["git", "-C", str(origin), "tag", "main", head], check=True,
                   env={**os.environ, **GIT_ENV})
    assert _git(origin, "rev-parse", "refs/tags/main") == head
    assert _git(origin, "rev-parse", "refs/heads/main") == tip

    proc = _dmg_harness(tmp_path, "tagshadow", board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 1, (
        f"a tag named `main` shadowed the release BRANCH and a 45-behind signed "
        f"build shipped:\n{proc.stdout}")
    assert "stale-artefact failure" in proc.stderr


def test_the_release_tip_comes_from_the_branch_when_a_tag_shares_its_name(tmp_path):
    """The control: the same planted tag must not break a legitimate release.

    Without this, the test above would pass if the fetch had simply been broken
    into always failing.
    """
    root, head, tip = _repo_with_origin(tmp_path, "tagok", behind=0)
    origin = _origin_path(tmp_path, "tagok")
    # a decoy commit off to the side, so `main` the BRANCH stays at the release
    # tip while `main` the TAG points somewhere else entirely
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", "-C", str(origin), "checkout", "-q", "-b", "sidebranch"],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(origin), "commit", "-q", "--allow-empty",
                    "-m", "tagged elsewhere"], check=True, env=env)
    decoy = _git(origin, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(origin), "tag", "main", decoy], check=True,
                   env=env)
    assert _git(origin, "rev-parse", "refs/heads/main") == head != decoy
    proc = _dmg_harness(tmp_path, "tagok", board=BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 0, proc.stderr
    assert "provenance NOT verified" not in proc.stdout


# --------------------------------------------------------------------------- #
# DEFECT 1 (fifth review) — `file://<host>/<path>` walked straight through, and
# so did a percent-escape, because the check DERIVED A PATH FROM THE URL
#
#     origin = file://localhost${ROOT}
#     remote_path = localhost/…/${ROOT}      <- the scheme was stripped, not the
#                                               authority: no leading slash
#               -> ${ROOT}/localhost/…/${ROOT}   (joined as if it were relative)
#               -> [ -d … ] false -> the && chain fell through
#               -> "not self-referential", the gate never fired
#
#     $ git ls-remote origin refs/heads/main
#     b48b5ffd7a59…  refs/heads/main         <- ${ROOT}'s OWN stale main
#
#     verify-artefact: OK — built from b48b5ffd7a59, clean tree
#     OK: …x.dmg [signed]                    rc=0   (45 behind, wrong board)
#
# An independent reviewer drove that. While fixing it a FOURTH derivation bug
# turned up in the same two lines: git PERCENT-DECODES a `file://` path and the
# filesystem does not, so `file:///…/%52oot` resolves to the build checkout for
# git and to a non-existent directory for `[ -d ]`. Same fail-open, no host
# needed. Driven here as `percent-escape*`.
#
# That is four refuted derivations in a row (relative, per-worktree verb,
# authority, percent-escape), so the derivation is GONE rather than patched a
# fourth time. make-dmg.sh now asks the remote a question only this repository
# can answer: it plants a unique unreachable ref and checks whether `git
# ls-remote origin` advertises it. git resolves the url — url syntax, insteadOf
# rewrites, symlinks, worktrees and percent-escapes all included — so there is
# no derivation left to get wrong.
# --------------------------------------------------------------------------- #

def _pct_last(p: str) -> str:
    """`/a/b/root` -> `/a/b/roo%74`. git decodes it; `[ -d ]` does not."""
    return p[:-1] + "%%%02X" % ord(p[-1])


def _symlink_to(root: Path) -> str:
    link = root.parent / (root.name + "-symlink")
    link.symlink_to(root)
    return str(link)


def _worktree_gitdir(root: Path) -> str:
    """`${ROOT}/.git/worktrees/<name>` — a real directory git will serve refs from."""
    linked = root.parent / (root.name + "-wtgd")
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "--detach",
                    str(linked), "HEAD"], check=True,
                   env={**os.environ, **GIT_ENV})
    return str(root / ".git" / "worktrees" / linked.name)


# Every spelling of ${ROOT} that git resolves. The last derivation-based version
# of the check was defeated by a subset of these; the probe is asked to answer
# all of them, and each case asserts first that git really does resolve it back
# to the build checkout, so a green test cannot be a broken fixture.
_SELF_URL_FORMS = {
    # plain paths — the shapes a `.git/config` edit reaches for first
    "absolute": lambda r: str(r),
    "trailing-slash": lambda r: f"{r}/",
    "dot-segment": lambda r: f"{r}/.",
    "parent-of-dotgit": lambda r: f"{r}/.git/..",
    "double-slash": lambda r: f"/{r}",
    "the-git-dir": lambda r: f"{r}/.git",
    "a-worktree-git-dir": _worktree_gitdir,
    "a-symlink": _symlink_to,
    # file:// — the fifth review's finding. git ignores the authority entirely,
    # and decodes percent-escapes that the filesystem does not.
    "empty-authority": lambda r: f"file://{r}",
    "localhost": lambda r: f"file://localhost{r}",
    "loopback-ip": lambda r: f"file://127.0.0.1{r}",
    "uppercase-host": lambda r: f"file://LOCALHOST{r}",
    "host-then-dot": lambda r: f"file://localhost/.{r}",
    "arbitrary-host": lambda r: f"file://example.com{r}",
    "userinfo": lambda r: f"file://user@localhost{r}",
    "host-and-port": lambda r: f"file://localhost:1234{r}",
    "percent-escape": lambda r: f"file://localhost{_pct_last(str(r))}",
    "percent-escape-no-host": lambda r: f"file://{_pct_last(str(r))}",
}


def _set_origin(repo: Path, url: str) -> None:
    subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin", url],
                   check=True, env={**os.environ, **GIT_ENV})


def _origin_resolves_to(repo: Path) -> str:
    """What `git ls-remote origin refs/heads/main` actually answers, from `repo`.

    This is the precondition every bypass test below asserts first: git must
    really resolve the configured url back to the build checkout's own stale
    branch. Without it a passing test would prove only that the fixture was
    broken.
    """
    ls = subprocess.run(["git", "-C", str(repo), "ls-remote", "origin",
                         "refs/heads/main"], capture_output=True, text=True,
                        env={**os.environ, **GIT_ENV})
    assert ls.returncode == 0, f"fixture: git could not reach origin: {ls.stderr}"
    return ls.stdout.split()[0] if ls.stdout.strip() else ""


@pytest.mark.parametrize("form", sorted(_SELF_URL_FORMS))
def test_every_url_that_names_the_build_checkout_is_refused(tmp_path, form):
    """Every spelling of ${ROOT} that git resolves must be refused.

    `absolute` and `empty-authority` are the two the last derivation handled;
    they stay in the parametrization as the controls that the rest are not
    passing for some unrelated reason.
    """
    tag = "urlform" + form.replace("-", "")
    root, head, tip = _repo_with_origin(tmp_path, tag, behind=45)
    assert head != tip
    _set_origin(root, _SELF_URL_FORMS[form](root))
    assert _origin_resolves_to(root) == head, (
        "fixture: git does not resolve this url back to the build checkout, so "
        "there is nothing for the gate to catch")

    proc = _dmg_harness(tmp_path, tag, board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 1, (
        f"origin spelled as `{form}` resolved to the build checkout and a "
        f"signed 45-behind DMG shipped:\n{proc.stdout}")
    assert "resolves to this very repository" in proc.stderr, proc.stderr
    assert "stale-artefact failure" not in proc.stderr, (
        "refused for the wrong reason — the self-reference check did not fire")


@pytest.mark.parametrize("orientation", ["origin-is-linked", "root-is-linked"])
def test_a_file_url_naming_a_LINKED_WORKTREE_is_refused(tmp_path, orientation):
    """The fourth review's finding, re-driven through the fifth's url shape.

    Both orientations again, because the two defects compose: a worktree of the
    build checkout, named by a `file://localhost` url. Neither the old verb nor
    the old strip would have caught it.
    """
    tag = f"wtfile{orientation[0]}"
    root, head, tip = _repo_with_origin(tmp_path, tag, behind=45)
    env = {**os.environ, **GIT_ENV}
    linked = tmp_path / f"linkedfile-{tag}"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "--detach",
                    str(linked), "HEAD"], check=True, env=env)
    build_root, remote_target = ((root, linked) if orientation == "origin-is-linked"
                                 else (linked, root))
    _set_origin(build_root, f"file://localhost{remote_target}")
    assert _origin_resolves_to(build_root) == head != tip

    proc = _dmg_harness(tmp_path, tag, board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=build_root)
    assert proc.returncode == 1, (
        f"a linked worktree named by a file://localhost url was accepted as "
        f"the release remote:\n{proc.stdout}")
    assert "resolves to this very repository" in proc.stderr, proc.stderr
    assert "stale-artefact failure" not in proc.stderr


def test_an_insteadOf_rewrite_to_the_build_checkout_is_refused(tmp_path):
    """`url.<base>.insteadOf` — claimed as caught for three reviews, never driven.

    The url in `remote.origin.url` is innocuous; git rewrites it to the build
    checkout at resolution time. The rewrite target is a `file://localhost` url
    because that is the form confirmed to bypass the derivation.
    """
    root, head, tip = _repo_with_origin(tmp_path, "insteadof", behind=45)
    env = {**os.environ, **GIT_ENV}
    _set_origin(root, "nh-release:no_human.git")
    subprocess.run(["git", "-C", str(root), "config",
                    f"url.file://localhost{root}.insteadOf",
                    "nh-release:no_human.git"], check=True, env=env)
    assert _origin_resolves_to(root) == head != tip

    proc = _dmg_harness(tmp_path, "insteadof", board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 1, (
        f"an insteadOf rewrite pointed origin at the build checkout and a "
        f"signed 45-behind DMG shipped:\n{proc.stdout}")
    assert "resolves to this very repository" in proc.stderr, proc.stderr
    assert "stale-artefact failure" not in proc.stderr


def test_a_GENUINE_remote_named_by_a_file_url_still_ships(tmp_path):
    """The control for all of the above: the check must not reject every url.

    A real release remote reached over `file://localhost` is an ordinary local
    remote — a different repository that happens to be on this machine — and it
    must pass. Without this, breaking the check into refusing everything would
    make every test above go green.
    """
    root, head, tip = _repo_with_origin(tmp_path, "genuinefile", behind=0)
    _set_origin(root, f"file://localhost{_origin_path(tmp_path, 'genuinefile')}")
    assert _origin_resolves_to(root) == head == tip

    proc = _dmg_harness(tmp_path, "genuinefile", board=BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 0, proc.stderr
    assert "resolves to this very repository" not in proc.stderr
    assert "provenance NOT verified" not in proc.stdout


@pytest.mark.parametrize("case", ["refused", "shipped", "blind"])
def test_the_self_reference_probe_leaves_no_ref_behind(tmp_path, case):
    """The check writes a ref into the build checkout; it must not litter it.

    Every way out is driven, because they leave by different doors: the refusal
    path exits 1 from inside the check, the success path runs on past it, and
    `blind` — the sixth review's positive control — exits 1 EARLIER than either,
    between `update-ref` and the deletion. A ref left behind would accumulate
    one per build and would make the NEXT build's probe non-unique if the pid
    ever repeated.
    """
    behind = 0 if case == "shipped" else 45
    root, head, tip = _repo_with_origin(tmp_path, f"probe{case}", behind=behind)
    extra = {}
    if case == "refused":
        _set_origin(root, f"file://localhost{root}")
    elif case == "blind":
        extra = _hostile_global(tmp_path, "probeblind",
                                _HIDE_REFS_CONFIGS["transfer"])
    proc = _dmg_harness(tmp_path, f"probe{case}", board=BOARD, commit=head,
                        sign_mode="signed", root=root, env_extra=extra)
    assert proc.returncode == (0 if case == "shipped" else 1), proc.stderr
    if case == "blind":
        assert "probe is blind" in proc.stderr, proc.stderr
    left = _git(root, "for-each-ref", "--format=%(refname)", "refs/nh-self-probe/")
    assert left == "", f"the self-reference probe left {left!r} behind"


def test_an_unreachable_remote_is_refused_by_the_SELF_REFERENCE_check_FIRST(tmp_path):
    """The probe needs the remote too, and it must fail closed on its own.

    `test_an_unreachable_remote_is_a_failure_not_a_skip` cannot see this: the
    fetch below refuses the same build with a message carrying the same two
    phrases, so making the probe fall back to `probe_seen=""` when `ls-remote`
    fails leaves the whole suite green while the self-reference check has
    quietly become skippable whenever the remote is momentarily unreachable —
    the `|| true` shape this branch exists to delete, reintroduced one gate
    earlier. Driven as a mutation: that fallback turns this red and nothing
    else.
    """
    root, head, tip = _repo_with_origin(tmp_path, "unreachprobe", behind=45)
    _set_origin(root, str(tmp_path / "does-not-exist-at-all"))
    proc = _dmg_harness(tmp_path, "unreachprobe", board=BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 1
    assert "to ask whether it is this very" in proc.stderr, (
        "the build was refused, but not by the self-reference check — its own "
        "unreachable-remote path is no longer fail-closed:\n" + proc.stderr)


# --------------------------------------------------------------------------- #
# DEFECT 1 + DEFECT 3 (sixth review) — THE HARNESS WAS CLEANER THAN THE REAL
# BUILD, SO A WHOLE CLASS OF DEFECT WAS STRUCTURALLY INVISIBLE TO IT
#
# Every test above this line runs with GIT_ENV, which pins GIT_CONFIG_GLOBAL and
# GIT_CONFIG_SYSTEM to /dev/null and sets no GIT_* redirect at all. The real
# `npm run dist:bundled` inherits the operator's entire environment and their
# ~/.gitconfig. So anything that goes wrong in what git INHERITS could not be
# seen from here — which is why every earlier round's tests, the 18 driven url
# spellings included, never touched it.
#
# What was found in the gap, driven against fd38ed81:
#
#   * GIT_NAMESPACE=rel makes `git ls-remote origin` answer EMPTY, rc=0, for an
#     origin that IS ${ROOT}. The probe existed; the answer came back silent;
#     silence was read as "not self-referential". (On git 2.49.0 the build then
#     stops at the fetch, which the namespace also hides — so the BLINDING
#     reproduces exactly and the end-to-end ship does not. Both are asserted
#     below, separately, rather than blurred together.)
#   * `transfer.hideRefs = refs/nh-self-probe` in a GLOBAL gitconfig blinds the
#     same probe while leaving `refs/heads/main` advertised — so nothing
#     downstream trips, and a signed, 45-commits-behind, "THIS IS THE WRONG
#     BOARD" DMG walked all the way to `OK: …x.dmg [signed]`, rc=0.
#   * GIT_DIR overrides `git -C "${ROOT}"` outright, moving every question the
#     gate asks — and the commit the STAMP records — into another repository.
#
# GIT_ENV IS DELIBERATELY LEFT HERMETIC. Reading the machine's real ~/.gitconfig
# from the suite would make these tests depend on whoever runs them, which is
# how you get a green suite that means nothing. The fix for the class is the
# opposite: hand the block an explicitly HOSTILE environment and an explicitly
# HOSTILE global config, on purpose, and assert it refuses.
#
# WHAT THESE CASES DO NOT COVER, enumerated rather than left implied. The gate
# path also inherits: PATH (which chooses the `git` binary itself — untested
# here, and unfixable from inside the script), HOME (only via GIT_CONFIG_GLOBAL,
# which is covered), GIT_PROTOCOL and GIT_TRACE* (no answer here depends on them,
# and none was demonstrated), and the SYSTEM gitconfig (same mechanism as the
# global one, covered by the same control, but not separately driven).
#
# GIT_SSH_COMMAND USED TO BE ON THAT LIST WITH A FALSE JUSTIFICATION — "no
# effect on a local origin, driven; on an ssh origin it reaches a DIFFERENT
# repository, which is the declared residual, not a blinding". The first clause
# is true and re-confirmed. The second is refuted: on an ssh origin that command
# is what ANSWERS, so it blinds the probe, and the control cannot see it because
# the control is on the local-path transport. It is now a residual with a driven
# counterexample of its own —
# test_the_declared_residual_an_ssh_redirect_still_walks_through — rather than a
# line in a "not covered" list.
# --------------------------------------------------------------------------- #

def _other_repo(tmp_path: Path, tag: str) -> Path:
    """A second, unrelated repository for the redirect vars to point at."""
    other, _ = make_repo(tmp_path, name=f"other-{tag}", commits=2)
    return other


def _hostile_scenarios(other: Path, root: Path) -> dict[str, dict[str, str]]:
    """Every GIT_* name the gate clears, set to a value that is hostile if honoured.

    Grouped into SCENARIOS rather than one variable per case, because some of
    these are only hostile together — `GIT_CONFIG_COUNT` alone is malformed
    input, and the interesting thing is what it can say when its `KEY_0` comes
    with it.

    Two of these were driven to a demonstrated effect on this gate's own
    commands, and are called out as such:

      * `GIT_NAMESPACE` blinds the probe outright.
      * `GIT_CONFIG_COUNT` REPOINTS `origin` from the environment alone, via an
        `insteadOf` rewrite. The obvious version of that — setting
        `remote.origin.url` — does NOT work, and is left here as a comment
        rather than a test: git treats a remote's url as multi-valued, appends
        the env one and uses the first.

    The rest are here because they redirect where the repo is, which objects
    exist, or what the config says — not because a bypass was demonstrated for
    each.
    """
    return {
        "GIT_NAMESPACE": {"GIT_NAMESPACE": "rel"},
        "GIT_DIR": {"GIT_DIR": str(other / ".git"),
                    "GIT_WORK_TREE": str(other)},
        "GIT_COMMON_DIR": {"GIT_COMMON_DIR": str(other / ".git")},
        "GIT_OBJECT_DIRECTORY": {
            "GIT_OBJECT_DIRECTORY": str(other / ".git" / "objects")},
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other / ".git" / "objects")},
        "GIT_INDEX_FILE": {"GIT_INDEX_FILE": str(other / ".git" / "index")},
        "GIT_GRAFT_FILE": {"GIT_GRAFT_FILE": str(other / "grafts")},
        "GIT_SHALLOW_FILE": {"GIT_SHALLOW_FILE": str(other / "shallow")},
        "GIT_REPLACE_REF_BASE": {"GIT_REPLACE_REF_BASE": "refs/replace-decoy"},
        "GIT_NO_REPLACE_OBJECTS": {"GIT_NO_REPLACE_OBJECTS": "1"},
        "GIT_CEILING_DIRECTORIES": {"GIT_CEILING_DIRECTORIES": str(other)},
        "GIT_CONFIG": {"GIT_CONFIG": str(other / "decoy-config")},
        # An `insteadOf` rewrite injected through the environment: it makes
        # `git ls-remote origin` answer from `other`, so the self-reference
        # check would be asking a repository that is not this one.
        "GIT_CONFIG_PARAMETERS": {
            "GIT_CONFIG_PARAMETERS": f"'url.{other}.insteadOf={root}'"},
        "GIT_CONFIG_COUNT": {"GIT_CONFIG_COUNT": "1",
                             "GIT_CONFIG_KEY_0": f"url.{other}.insteadOf",
                             "GIT_CONFIG_VALUE_0": str(root)},
    }


_HOSTILE_SCENARIOS = sorted(_hostile_scenarios(Path("/other"), Path("/root")))


@pytest.mark.parametrize("name", _HOSTILE_SCENARIOS)
def test_no_inherited_GIT_env_var_lets_a_self_referential_origin_through(
        tmp_path, name):
    """One scenario at a time: a hostile GIT_* must not stop the gate seeing itself.

    `origin` IS the build checkout, 45 commits behind, with a wrong board — the
    exact tautology this gate exists to end. Every one of these must leave that
    refusal intact, and refused for the SELF-REFERENCE reason: being refused
    because the environment broke something unrelated would be luck, not the
    check working, and asserting only the exit code would not tell them apart.
    """
    tag = "henv" + name.lower().replace("_", "")
    root, head, tip = _repo_with_origin(tmp_path, tag, behind=45)
    _set_origin(root, str(root))
    assert _origin_resolves_to(root) == head != tip
    other = _other_repo(tmp_path, tag)

    proc = _dmg_harness(tmp_path, tag, board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root,
                        env_extra=_hostile_scenarios(other, root)[name])
    assert proc.returncode == 1, (
        f"{name} in the environment walked a signed 45-behind DMG through:\n"
        f"{proc.stdout}")
    assert "resolves to this very repository" in proc.stderr, (
        f"{name} did not ship the build, but the SELF-REFERENCE check is not "
        f"what stopped it:\n{proc.stderr}")


def test_the_whole_hostile_environment_at_once_is_survived(tmp_path):
    """All of them together, because they compose in ways one-at-a-time cannot."""
    root, head, tip = _repo_with_origin(tmp_path, "henvall", behind=45)
    _set_origin(root, str(root))
    assert _origin_resolves_to(root) == head != tip
    other = _other_repo(tmp_path, "henvall")
    env: dict[str, str] = {}
    for scenario in _hostile_scenarios(other, root).values():
        env.update(scenario)

    proc = _dmg_harness(tmp_path, "henvall", board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root, env_extra=env)
    assert proc.returncode == 1, proc.stdout
    assert "resolves to this very repository" in proc.stderr, proc.stderr


def test_GIT_NAMESPACE_does_not_blind_the_probe(tmp_path):
    """The sixth review's D1, as its own named case.

    Driven against fd38ed81: `GIT_NAMESPACE=rel` made `git ls-remote origin`
    answer EMPTY with rc=0 while `origin` was ${ROOT} itself, and the block read
    that silence as "not self-referential". GIT_NAMESPACE is not in git's
    `local_repo_env` scrub list, so the `upload-pack` the local transport spawns
    inherits it and advertises only `refs/namespaces/rel/*`.
    """
    root, head, tip = _repo_with_origin(tmp_path, "nsblind", behind=45)
    _set_origin(root, str(root))
    assert _origin_resolves_to(root) == head != tip

    proc = _dmg_harness(tmp_path, "nsblind", board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root,
                        env_extra={"GIT_NAMESPACE": "rel"})
    assert proc.returncode == 1
    assert "resolves to this very repository" in proc.stderr, proc.stderr


def _drop_env_scrub(text: str) -> str:
    """Delete the `unset GIT_*` scrub from the extracted block."""
    i = text.index("unset GIT_NAMESPACE")
    j = text.index("\nRELEASE_REMOTE=", i)
    out = text[:i] + text[j + 1:]
    assert "unset GIT_NAMESPACE" not in out, "mutation did not remove the scrub"
    return out


def _drop_probe_control(text: str) -> str:
    """Delete the probe's positive control from the extracted block."""
    a = text.index("  # THE PROBE'S SILENCE MEANS NOTHING")
    b = text.index("  if ! probe_seen=", a)
    out = text[:a] + text[b:]
    assert "control_seen" not in out, "mutation did not remove the control"
    return out


def test_the_env_scrub_mutation_is_actually_caught(tmp_path):
    """Without the scrub, GIT_NAMESPACE blinds the probe — observed, not assumed.

    This is what makes `test_GIT_NAMESPACE_does_not_blind_the_probe` mean
    something. Remove the `unset` and the SAME build is no longer refused for
    self-reference: the probe comes back empty and only the positive control
    below notices, which is a different message. (It is still refused — the two
    layers are independent on purpose — so asserting the exit code alone would
    have caught nothing.)
    """
    root, head, tip = _repo_with_origin(tmp_path, "nsmut", behind=45)
    _set_origin(root, str(root))
    proc = _dmg_harness(tmp_path, "nsmut", board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root, mutate=_drop_env_scrub,
                        env_extra={"GIT_NAMESPACE": "rel"})
    assert "resolves to this very repository" not in proc.stderr, (
        "the mutation was supposed to blind the self-reference check; if this "
        "still fires, the scrub is not what makes GIT_NAMESPACE harmless")
    assert proc.returncode == 1, (
        "and it must still not SHIP — the positive control is the second layer")
    assert "probe is blind" in proc.stderr, proc.stderr


# --------------------------------------------------------------------------- #
# DEFECT 2 (sixth review) — the trust boundary was written as `.git/config`, and
# a GLOBAL config is outside it
# --------------------------------------------------------------------------- #

_HIDE_REFS_CONFIGS = {
    "transfer": "[transfer]\n\thideRefs = refs/nh-self-probe\n",
    "uploadpack": "[uploadpack]\n\thideRefs = refs/nh-self-probe\n",
}


def _hostile_global(tmp_path: Path, tag: str, body: str) -> dict[str, str]:
    gc = tmp_path / f"hostile-gitconfig-{tag}"
    gc.write_text(body)
    return {"GIT_CONFIG_GLOBAL": str(gc)}


@pytest.mark.parametrize("knob", sorted(_HIDE_REFS_CONFIGS))
def test_a_global_config_that_hides_the_probe_is_a_FAILURE_not_a_pass(
        tmp_path, knob):
    """The driven end-to-end bypass, and the reason the residual list was wrong.

    fd38ed81 filed `uploadpack.hideRefs` under the residual, on the grounds that
    it "sits inside the `.git/config` trust boundary" and that "anyone who can
    set it can repoint `origin`". Both clauses are false: this config is not in
    ${ROOT}'s `.git/config`, and setting it repoints nothing. Driven against
    that commit, it shipped a signed, 45-behind, wrong-board DMG at rc=0.
    """
    tag = "hide" + knob
    root, head, tip = _repo_with_origin(tmp_path, tag, behind=45)
    _set_origin(root, str(root))
    assert _origin_resolves_to(root) == head != tip

    proc = _dmg_harness(tmp_path, tag, board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root,
                        env_extra=_hostile_global(
                            tmp_path, tag, _HIDE_REFS_CONFIGS[knob]))
    assert proc.returncode == 1, (
        f"{knob}.hideRefs in a GLOBAL config walked a signed 45-behind DMG "
        f"through:\n{proc.stdout}")
    assert "probe is blind" in proc.stderr, proc.stderr


def test_a_blinded_probe_fails_a_build_that_would_otherwise_have_shipped(
        tmp_path):
    """Unanswerable is a FAILURE even when nothing is actually wrong.

    `origin` here is a GENUINE remote and the build is current — this is a
    release that should ship, and it does not, because the check that has to
    clear it cannot see. That is the intended trade and it is asserted rather
    than assumed: a gate allowed to pass when its own instrument is broken is
    the shape this whole branch exists to delete.
    """
    root, head, tip = _repo_with_origin(tmp_path, "hideok", behind=0)
    assert _origin_resolves_to(root) == head == tip

    proc = _dmg_harness(tmp_path, "hideok", board=BOARD, commit=head,
                        sign_mode="signed", root=root,
                        env_extra=_hostile_global(
                            tmp_path, "hideok", _HIDE_REFS_CONFIGS["transfer"]))
    assert proc.returncode == 1, proc.stdout
    assert "probe is blind" in proc.stderr, proc.stderr


def test_the_probe_control_mutation_is_actually_caught(tmp_path):
    """Delete the positive control and the driven bypass comes straight back.

    This is the mutation that matters most: it turns a REFUSAL into a shipped,
    signed, 45-commits-behind DMG. If this test ever passes, the control has
    stopped being what stands between the two.
    """
    root, head, tip = _repo_with_origin(tmp_path, "ctlmut", behind=45)
    _set_origin(root, str(root))
    proc = _dmg_harness(tmp_path, "ctlmut", board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root,
                        mutate=_drop_probe_control,
                        env_extra=_hostile_global(
                            tmp_path, "ctlmut", _HIDE_REFS_CONFIGS["transfer"]))
    assert proc.returncode == 0, (
        "the mutation was supposed to restore the bypass; if the build is "
        "still refused, this test is no longer observing the control:\n"
        + proc.stderr)
    assert "[signed]" in proc.stdout


def test_the_probe_control_does_not_refuse_an_ordinary_build(tmp_path):
    """The control's own control: it must be silent when nothing hides the probe.

    Without this, breaking the control into always-failing would leave every
    test above green while making the script unable to release anything.
    """
    root, head, tip = _repo_with_origin(tmp_path, "ctlok", behind=0)
    proc = _dmg_harness(tmp_path, "ctlok", board=BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 0, proc.stderr
    assert "probe is blind" not in proc.stderr
    assert "provenance NOT verified" not in proc.stdout


# --------------------------------------------------------------------------- #
# SEVENTH REVIEW, F1 — THE CONTROL AND THE PROBE WERE ASKING DIFFERENT KINDS OF
# QUESTION, SO A `remote.<name>.*` KEY APPLIED TO ONE AND NOT THE OTHER
#
# The control asked `${ROOT}` — a URL. The probe asked `${RELEASE_REMOTE}` — a
# NAME. Every `remote.<name>.*` config key applies to the name form and to
# nothing else — driven below for `uploadpack`, and for the other sixteen keys
# of the family by test_no_other_remote_family_key_can_move_the_by_url_probe
# further down. (This citation used to read "and in the sweep test"; there was
# no sweep test, and `uploadpack` was the only key any test touched. The claim
# held when driven, but an uncited claim is the exact shape every round of this
# branch's reviews has caught, so the test was written rather than the citation
# deleted.) So a two-line global config
#
#     [remote "origin"]
#         uploadpack = "git -c uploadpack.hideRefs=refs/nh-self-probe upload-pack"
#
# blinded the probe while the control sailed through: driven against f209d60e
# with `origin` = ${ROOT}, 45 commits behind and a "THIS IS THE WRONG BOARD"
# board, it printed `verify-artefact: OK` / `OK: …x.dmg [signed]`, rc=0. Same
# shape as the `transfer.hideRefs` bypass the sixth review fixed, one config key
# later — because the fix was the CONTROL, and the control could not calibrate an
# instrument that was not asking its question.
#
# Both are URLs now. Which URL matters, and two of the three obvious spellings
# are wrong — each is pinned by its own case below:
#
#   * `git remote get-url` returns the url AFTER `insteadOf` rewriting, so
#     re-feeding it to `ls-remote` rewrites a SECOND time. Driven: with
#     `url.A.insteadOf = raw` and `url.B.insteadOf = A`, `ls-remote origin`
#     reaches A and `ls-remote "$(git remote get-url origin)"` reaches B. The
#     probe would then be asking about a repository the fetch never touches.
#   * `git config --get remote.origin.url` returns the LAST value of a
#     multi-valued key; git fetches from the FIRST (driven).
#
# So the probe asks the RAW first-listed url and lets `ls-remote` apply
# `insteadOf` exactly once — the same string, through the same resolver, as
# `git fetch ${RELEASE_REMOTE}`.
# --------------------------------------------------------------------------- #

_HOSTILE_UPLOADPACK = (
    '[remote "origin"]\n'
    '\tuploadpack = "git -c uploadpack.hideRefs=refs/nh-self-probe upload-pack"\n'
)


def test_a_per_remote_uploadpack_cannot_blind_the_self_reference_probe(tmp_path):
    """F1, the merge blocker: driven to a signed 45-behind rc=0 against f209d60e.

    `origin` IS the build checkout. The probe must still see itself, so the
    build must be refused for SELF-REFERENCE — being refused by the control
    ("probe is blind") would mean the probe was still blinded and only the
    second layer noticed, which is a weaker property and a different message.
    """
    root, head, tip = _repo_with_origin(tmp_path, "upblind", behind=45)
    _set_origin(root, str(root))
    assert _origin_resolves_to(root) == head != tip

    proc = _dmg_harness(tmp_path, "upblind", board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root,
                        env_extra=_hostile_global(tmp_path, "upblind",
                                                  _HOSTILE_UPLOADPACK))
    assert proc.returncode == 1, (
        "remote.origin.uploadpack in a GLOBAL config walked a signed "
        f"45-behind DMG through:\n{proc.stdout}")
    assert "resolves to this very repository" in proc.stderr, proc.stderr


def test_a_per_remote_uploadpack_does_not_break_a_genuine_release(tmp_path):
    """The control for the case above: a real remote must still ship.

    The same key is set, and `origin` is a genuine, current release remote. If
    the fix had been "refuse whenever remote.origin.uploadpack exists", the test
    above would pass for the wrong reason and this one would go red.
    """
    root, head, tip = _repo_with_origin(tmp_path, "upgenuine", behind=0)
    assert _origin_resolves_to(root) == head == tip

    proc = _dmg_harness(tmp_path, "upgenuine", board=BOARD, commit=head,
                        sign_mode="signed", root=root,
                        env_extra=_hostile_global(tmp_path, "upgenuine",
                                                  _HOSTILE_UPLOADPACK))
    assert proc.returncode == 0, proc.stderr
    assert "resolves to this very repository" not in proc.stderr
    assert "probe is blind" not in proc.stderr
    assert "provenance NOT verified" not in proc.stdout


def test_a_CHAINED_insteadOf_does_not_send_the_probe_to_a_third_repository(
        tmp_path):
    """The url the probe asks must be the url the FETCH starts from.

    `git remote get-url` reports the url with `insteadOf` ALREADY applied.
    Handing that back to `ls-remote` applies the rewrite table a second time, so
    a two-hop chain sends the probe somewhere `git fetch origin` never goes.
    Here hop 1 is the build checkout itself — the self-reference the gate exists
    to catch — and hop 2 is an unrelated repository with no probe in it.

    This is the mutation guard for using `git remote get-url`'s output as the
    probe url: make that substitution and this is the only test that dies.

    The second hop is keyed on the `file://localhost` SPELLING of the build
    checkout, not on its plain path, on purpose: the control asks the plain path
    literally, so a rewrite keyed on that would redirect the control too and the
    build would be refused by the control instead — fail-closed, but no longer
    able to tell the two spellings of the probe url apart.
    """
    root, head, tip = _repo_with_origin(tmp_path, "chain", behind=45)
    env = {**os.environ, **GIT_ENV}
    third, _ = make_repo(tmp_path, name="third-chain", commits=2)
    hop1 = f"file://localhost{root}"
    _set_origin(root, "nh-release:no_human.git")
    subprocess.run(["git", "-C", str(root), "config",
                    f"url.{hop1}.insteadOf", "nh-release:no_human.git"],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(root), "config",
                    f"url.{third}.insteadOf", hop1], check=True, env=env)
    # The preconditions, asserted rather than assumed: `origin` really does
    # resolve to the build checkout's own stale main, `git remote get-url`
    # really does report the once-rewritten url, and feeding that back really
    # does land somewhere else.
    assert _origin_resolves_to(root) == head != tip
    rewritten = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True, text=True, check=True, env=env).stdout.strip()
    assert rewritten == hop1, rewritten
    hop2 = subprocess.run(
        ["git", "-C", str(root), "ls-remote", rewritten, "refs/heads/main"],
        capture_output=True, text=True, env=env).stdout.split()[0]
    assert hop2 == _git(third, "rev-parse", "HEAD") != head, (
        "fixture: the second insteadOf hop does not reach the third repository, "
        "so there is nothing for this test to catch")

    proc = _dmg_harness(tmp_path, "chain", board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 1, (
        "a chained insteadOf sent the probe to a third repository while the "
        f"fetch stayed on the build checkout:\n{proc.stdout}")
    assert "resolves to this very repository" in proc.stderr, proc.stderr


def test_a_MULTI_VALUED_remote_url_is_probed_at_the_url_git_fetches_from(
        tmp_path):
    """git fetches the FIRST value; `git config --get` reports the LAST (driven).

    `remote.origin.url` is multi-valued here: first the build checkout (which is
    what `git fetch origin` uses, and the self-reference to catch), then a
    genuine remote. Reading the key with `--get` instead of `--get-all | first`
    points the probe at the genuine one, which has no probe in it.

    This is the mutation guard for that spelling: swap `--get-all`/first for
    `--get` and this is the only test that dies.
    """
    root, head, tip = _repo_with_origin(tmp_path, "multiurl", behind=45)
    env = {**os.environ, **GIT_ENV}
    _set_origin(root, str(root))
    subprocess.run(["git", "-C", str(root), "config", "--add",
                    "remote.origin.url",
                    str(_origin_path(tmp_path, "multiurl"))], check=True,
                   env=env)
    assert _origin_resolves_to(root) == head != tip, (
        "fixture: git does not fetch from the FIRST url")
    last = subprocess.run(
        ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
        capture_output=True, text=True, check=True, env=env).stdout.strip()
    assert last == str(_origin_path(tmp_path, "multiurl")), last

    proc = _dmg_harness(tmp_path, "multiurl", board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 1, (
        "a second remote.origin.url value moved the probe off the url git "
        f"actually fetches from:\n{proc.stdout}")
    assert "resolves to this very repository" in proc.stderr, proc.stderr


def test_a_release_remote_with_no_configured_url_is_a_FAILURE(tmp_path):
    """`git remote get-url` answers the remote's NAME when it has no url.

    Driven: with only `remote.origin.pushurl` set, `git remote get-url origin`
    prints `origin` and exits 0 — so the existing "has no remote named origin"
    guard does not fire, and the probe would be handed the string `origin` as if
    it were a url. There is no canonical source in that configuration, and the
    gate says so instead of proceeding.
    """
    root, head, tip = _repo_with_origin(tmp_path, "nourl", behind=0)
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", "-C", str(root), "config", "--unset-all",
                    "remote.origin.url"], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "config", "remote.origin.pushurl",
                    str(_origin_path(tmp_path, "nourl"))], check=True, env=env)
    got = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                         capture_output=True, text=True, env=env)
    assert got.returncode == 0 and got.stdout.strip() == "origin", (
        "fixture: git no longer answers the remote's name here, so this test "
        f"is not exercising what it was written for: {got!r}")

    proc = _dmg_harness(tmp_path, "nourl", board=BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 1, proc.stdout
    assert "no configured url" in proc.stderr, proc.stderr


def test_a_per_remote_uploadpack_cannot_redirect_the_RELEASE_TIP_FETCH(tmp_path):
    """The same key's OTHER half, found while sweeping the family for F1.

    Nothing here is self-referential: `origin` is a genuine, current release
    remote and the probe is right to stay silent. `remote.origin.uploadpack`
    moves the SERVER SIDE of the fetch instead — git appends the repository path
    to that command string and runs it through a shell for a local transport, so
    a trailing `#` comments the real path out and the build checkout is served
    in its place. FETCH_HEAD then comes back as the artefact's own stale commit
    and `--expect-commit` is satisfied by the tautology the signed path exists
    to escape.

    Driven against f209d60e: `verify-artefact: OK` / `OK: …x.dmg [signed]`,
    rc=0, 45 commits behind. Fetching by url instead of by name is what closes
    it; the probe fix alone does not, because the probe was never wrong here.
    """
    root, head, tip = _repo_with_origin(tmp_path, "upfetch", behind=45)
    env = {**os.environ, **GIT_ENV}
    assert _origin_resolves_to(root) == tip != head, (
        "fixture: origin is not a genuine remote ahead of the build checkout")
    gc = tmp_path / "hostile-gitconfig-upfetch"
    gc.write_text('[remote "origin"]\n'
                  f"\tuploadpack = \"git upload-pack '{root}' #\"\n")

    proc = _dmg_harness(tmp_path, "upfetch", board=BOARD, commit=head,
                        sign_mode="signed", root=root,
                        env_extra={"GIT_CONFIG_GLOBAL": str(gc)})
    assert proc.returncode == 1, (
        "remote.origin.uploadpack served the build checkout to the release-tip "
        f"fetch and a signed 45-behind DMG shipped:\n{proc.stdout}")
    assert "stale-artefact failure" in proc.stderr, proc.stderr


# --------------------------------------------------------------------------- #
# EIGHTH REVIEW, F1 — ASKING BY URL PUTS A CONFIG STRING IN git's ARGV, AND A
# CONFIG STRING CAN BE AN OPTION
#
# `remote.<name>.url` is not a url as far as argv is concerned; it is whatever
# the config says. f209d60e asked `${RELEASE_REMOTE}` — a literal — so no config
# value ever reached git as an ARGUMENT, and git resolved the remote's url
# internally, where it is never option-parsed (driven at f209d60e with the
# payload below: rc=1, nothing executed). Asking BY URL hands git that string,
# and a value beginning with `-` is parsed as an OPTION:
#
#     [remote "origin"]
#         url = --upload-pack=touch <sentinel>
#
# in a GLOBAL gitconfig — the class the gate deliberately reads rather than
# clears, and a value there lands FIRST in `config --get-all`, which is exactly
# the one `awk 'NR == 1'` picks. `git remote get-url` reports it too, so the
# earlier "no remote named origin" guard does not fire either.
#
# Driven at d3367638 with the `--` removed from both call sites (mutant M6):
# `git ls-remote --upload-pack=touch <sentinel> refs/nh-self-probe/<pid>` makes
# git run `touch <sentinel> refs/nh-self-probe/<pid>` for the local transport.
# The sentinel is created — ARBITRARY COMMAND EXECUTION inside a signed release
# build — and the gate STILL exits 1 with byte-identical stderr to the unmutated
# run. That identity is why M6 survived all 140 tests: the exit code and every
# message are unchanged, and the side effect is the ONLY observable. So the side
# effect is what these two tests assert.
#
# THE CALL-SITE COMMENT USED TO SAY git "also refuses such a pathname on its
# own; the separator does not depend on that". Measured false, and corrected
# where it was written: WITHOUT the separator the string is never a pathname at
# all — it is consumed as an option long before any pathname check — so
# `fatal: strange pathname … blocked` is reachable ONLY because the separator
# makes git treat the value as the repository. The refusal DEPENDS on the
# separator. (Measured alongside it: git applies that check AFTER `insteadOf`,
# so a rewrite that PRODUCES a `-`-leading url is blocked as well, with or
# without the separator, because the rewritten string never passes through argv.)
#
# WHICH SEPARATOR IS PINNED, because only one of the two can be. The gate exits
# at `ls-remote` whenever the url is refused, so the `fetch`'s own `--` is never
# reached with a `-`-leading value while the `ls-remote` one stands. Driven, all
# three mutants, same fixture:
#
#     ls-remote only   sentinel CREATED   <- killed by the test below
#     both (M6)        sentinel CREATED   <- killed by the test below
#     fetch only       sentinel absent    <- SURVIVES, and must
#
# The fetch's separator is therefore defence in depth against a future edit that
# reorders or removes the `ls-remote` question, and it is deliberately not
# claimed as tested. Recording it here so the survivor is a decision rather than
# an unnoticed hole.
# --------------------------------------------------------------------------- #

def _option_url_fixture(tmp_path: Path, tag: str) -> tuple[Path, dict[str, str]]:
    """A GLOBAL config whose `remote.origin.url` is a git OPTION, not a url.

    Returns the sentinel path the payload would create and the env that installs
    the config. The sentinel must not exist for the assertions to mean anything,
    which is why every caller checks that before running the build.
    """
    sentinel = tmp_path / f"nh-sentinel-{tag}"
    assert " " not in str(sentinel), (
        "fixture: git runs a local transport's upload-pack through a SHELL, so "
        f"a space anywhere in {sentinel} would split the payload and disarm it "
        "silently — the test would then pass while observing nothing")
    assert not sentinel.exists()
    body = '[remote "origin"]\n' f"\turl = --upload-pack=touch {sentinel}\n"
    return sentinel, _hostile_global(tmp_path, tag, body)


def _drop_url_separator(text: str) -> str:
    """Mutant M6: drop `--` from BOTH calls that take the config-derived url."""
    pairs = (('ls-remote -- "${probe_url}"', 'ls-remote "${probe_url}"'),
             ('fetch -q -- "${probe_url}"', 'fetch -q "${probe_url}"'))
    out = text
    for before, after in pairs:
        assert before in out, f"mutation target is gone from the block: {before}"
        out = out.replace(before, after)
        assert before not in out, f"mutation did not apply: {before}"
    return out


def test_a_configured_url_that_is_a_git_OPTION_cannot_execute_a_command(tmp_path):
    """The by-url pivot's own new surface: a `url` beginning with `-`.

    `origin` here is a GENUINE, current remote — nothing about this build is
    stale or self-referential. The only hostile input is the shape of the
    configured url. The build must fail closed, and above all it must not RUN
    the string.
    """
    root, head, tip = _repo_with_origin(tmp_path, "opturl", behind=0)
    sentinel, env_extra = _option_url_fixture(tmp_path, "opturl")

    proc = _dmg_harness(tmp_path, "opturl", board=BOARD, commit=head,
                        sign_mode="signed", root=root, env_extra=env_extra)
    assert not sentinel.exists(), (
        "the configured `remote.origin.url` reached git as an OPTION and its "
        f"command RAN: {sentinel} exists after the build. The `--` separator "
        "before ${probe_url} is what stands between a config value and "
        "arbitrary execution inside a signed release build.")
    assert proc.returncode == 1, (
        "a url git cannot use must fail CLOSED, not ship:\n" + proc.stdout)
    assert "[signed]" not in proc.stdout, proc.stdout
    assert "cannot reach origin" in proc.stderr, proc.stderr


def test_the_url_separator_mutation_is_actually_caught(tmp_path):
    """Remove the separator and the payload runs — observed, not assumed.

    Without this, the test above asserts the absence of something the fixture
    might never have delivered, which is the failure mode that let M6 survive
    140 tests in the first place. The second assertion pins the REASON it
    survived: the exit code and the message are identical either way.
    """
    root, head, tip = _repo_with_origin(tmp_path, "optmut", behind=0)
    sentinel, env_extra = _option_url_fixture(tmp_path, "optmut")

    proc = _dmg_harness(tmp_path, "optmut", board=BOARD, commit=head,
                        sign_mode="signed", root=root,
                        mutate=_drop_url_separator, env_extra=env_extra)
    assert sentinel.exists(), (
        "the mutation was supposed to let the configured url reach git as an "
        "option and execute. If nothing ran, this fixture no longer delivers a "
        "payload and the test above is observing nothing:\n" + proc.stderr)
    assert proc.returncode == 1 and "cannot reach origin" in proc.stderr, (
        "the exit code and stderr must be UNCHANGED by the mutation — that is "
        "why no exit-code or message assertion anywhere in this file could see "
        "it, and why the sentinel is the assertion:\n" + proc.stderr)


# --------------------------------------------------------------------------- #
# EIGHTH REVIEW, F2 — THE KEY-BY-KEY SWEEP, AS A TEST RATHER THAN A PARAGRAPH
#
# make-dmg.sh dismisses the rest of the `remote.<name>.*` family key by key, on
# the argument that asking BY URL never consults a named remote's config at all.
# This is the test the F1 section above used to cite and that did not exist; the
# argument held when driven, and it is pinned here rather than asserted in prose.
#
# `url`, `pushurl` and `uploadpack` are absent on purpose: each already has its
# own case above (multi-valued url, no-url-at-all, and the two halves of
# uploadpack), and folding them in would hide those specific messages behind a
# generic one.
#
# The assertion is the SELF-REFERENCE message, not merely a non-zero exit. That
# distinction is what makes the sweep non-vacuous: a key that broke the
# transport outright would also refuse the build, and would refuse it with the
# unreachable-remote message instead. Requiring "resolves to this very
# repository" asserts that the probe still reached the remote, saw itself, and
# decided — i.e. that the key was inert, which is what the paragraph claims.
# --------------------------------------------------------------------------- #

# Every remaining fetch-side key of the family, with the most redirect-shaped or
# blinding-shaped value each one accepts.
_REMOTE_FAMILY = {
    "receivepack":        "git receive-pack",
    "mirror":             "true",
    "push":               "refs/heads/main:refs/heads/main",
    "skipDefaultUpdate":  "true",
    "skipFetchAll":       "true",
    "followRemoteHEAD":   "never",
    "fetch":              "+refs/heads/nope:refs/remotes/origin/nope",
    "tagOpt":             "--no-tags",
    "prune":              "true",
    "pruneTags":          "true",
    "proxy":              "http://127.0.0.1:1",
    "proxyAuthMethod":    "basic",
    "serverOption":       "nh-bogus",
    "promisor":           "true",
    "partialclonefilter": "blob:none",
    "vcs":                "nh-no-such-helper",
}


def _family_config(tmp_path: Path, tag: str, keys: dict[str, str]) -> dict[str, str]:
    body = '[remote "origin"]\n' + "".join(
        f"\t{k} = {v}\n" for k, v in sorted(keys.items()))
    return _hostile_global(tmp_path, tag, body)


@pytest.mark.parametrize("key", sorted(_REMOTE_FAMILY))
def test_no_other_remote_family_key_can_move_the_by_url_probe(tmp_path, key):
    """One key per case, self-referential `origin`, 45 commits behind.

    If any of these ever starts shipping — or starts refusing with the
    unreachable-remote message instead — the sweep paragraph in `make-dmg.sh`
    has stopped being true and is what needs rewriting.
    """
    tag = "fam" + key.lower()
    root, head, tip = _repo_with_origin(tmp_path, tag, behind=45)
    _set_origin(root, str(root))
    assert _origin_resolves_to(root) == head != tip, (
        "fixture: origin is not the build checkout's own stale main")
    env_extra = _family_config(tmp_path, tag, {key: _REMOTE_FAMILY[key]})
    # The config is really being read — otherwise every case here would pass by
    # doing nothing at all. `--get-all`, not `--get`: `git clone` writes its own
    # `remote.origin.fetch` into the checkout's `.git/config`, and a single-valued
    # read answers with that LOCAL value while the global one is still there.
    seen = subprocess.run(
        ["git", "-C", str(root), "config", "--get-all", f"remote.origin.{key}"],
        capture_output=True, text=True,
        env={**os.environ, **GIT_ENV, **env_extra})
    assert _REMOTE_FAMILY[key] in seen.stdout.splitlines(), (
        f"fixture: git does not see remote.origin.{key} at all: {seen!r}")

    proc = _dmg_harness(tmp_path, tag, board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root, env_extra=env_extra)
    assert proc.returncode == 1, (
        f"remote.origin.{key} in a GLOBAL config walked a signed 45-behind DMG "
        f"through:\n{proc.stdout}")
    assert "resolves to this very repository" in proc.stderr, (
        f"remote.origin.{key} did not ship, but it did not fail for the reason "
        "the sweep paragraph gives either — the probe never reached the remote "
        f"and saw itself:\n{proc.stderr}")


def test_the_whole_remote_family_at_once_does_not_break_a_genuine_release(tmp_path):
    """The sweep's control: all 16 keys set, and a real release must still ship.

    Without this the sweep above would pass just as happily if the fix had been
    "refuse whenever any `remote.origin.*` key exists" — which would break every
    build machine that legitimately configures one. It is one case rather than
    sixteen because they compose, and because the property is about the family
    being INERT for a request made by url, not about any single key.
    """
    root, head, tip = _repo_with_origin(tmp_path, "famok", behind=0)
    assert _origin_resolves_to(root) == head == tip
    proc = _dmg_harness(tmp_path, "famok", board=BOARD, commit=head,
                        sign_mode="signed", root=root,
                        env_extra=_family_config(tmp_path, "famok",
                                                 _REMOTE_FAMILY))
    assert proc.returncode == 0, proc.stderr
    assert "[signed]" in proc.stdout, proc.stdout
    assert "resolves to this very repository" not in proc.stderr
    assert "probe is blind" not in proc.stderr


# --------------------------------------------------------------------------- #
# GIT_DIR moves the gate — and the STAMP — into another repository
# --------------------------------------------------------------------------- #

def test_GIT_DIR_cannot_move_the_stamp_to_another_repository(tmp_path):
    """The stamped commit must name the checkout that built the artefact.

    `GIT_DIR` beats `git -C` (driven: `GIT_DIR=$O/.git git -C R rev-parse HEAD`
    prints O's HEAD), and build-installer.sh asks git two questions that end up
    INSIDE the artefact. A stamp naming a repository that did not build it
    defeats every downstream check by construction, because make-dmg.sh compares
    that stamp against origin/main.
    """
    other, _ = make_repo(tmp_path, name="other-stampdir", commits=2)
    proc, stamp = _run_stamp_block(
        tmp_path, name="stampdir",
        env_extra={"GIT_DIR": str(other / ".git"),
                   "GIT_WORK_TREE": str(other)})
    assert proc.returncode == 0, proc.stderr
    assert stamp is not None
    src = tmp_path / "src-stampdir"
    assert _fields(stamp)["commit"] == _git(src, "rev-parse", "HEAD"), (
        "the stamp records the commit GIT_DIR pointed at, not the one that "
        "built the bundle")
    assert _fields(stamp)["commit"] != _git(other, "rev-parse", "HEAD")


def test_GIT_DIR_cannot_move_the_verifier_to_another_repository(tmp_path):
    """verify_artefact.py is also run standalone, where the env is unfiltered.

    make-dmg.sh clears these before invoking it, so this pins the tool's own
    behaviour: CI and an operator checking a downloaded DMG call it directly.
    The artefact here is honest about the repo that built it, and must not be
    reported stale because the environment pointed `--repo` somewhere else.
    """
    repo, sha = make_repo(tmp_path, name="src-vdir")
    other, _ = make_repo(tmp_path, name="other-vdir", commits=2)
    bundle = make_bundle(tmp_path, sha)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(bundle), "--repo", str(repo)],
        capture_output=True, text=True,
        env={**os.environ, **GIT_ENV, "GIT_DIR": str(other / ".git"),
             "GIT_WORK_TREE": str(other)})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout


# --------------------------------------------------------------------------- #
# DEFECT 4 (sixth review) — two more shapes belong on the RESIDUAL list
#
# These assert that a stale build DOES walk through, which is not a mistake: the
# residual is a written claim in make-dmg.sh, and a written claim with no test
# is what round after round of this branch's reviews kept catching. If either of
# these ever starts failing, the check has grown a capability its own comment
# denies, and that comment must be rewritten — the test says so in its message.
# --------------------------------------------------------------------------- #

def _stale_origin_shape(tmp_path: Path, tag: str, shape: str):
    root, head, tip = _repo_with_origin(tmp_path, tag, behind=45)
    env = {**os.environ, **GIT_ENV}
    if shape == "mirror":
        target = tmp_path / f"{tag}-mirror.git"
        subprocess.run(["git", "clone", "-q", "--mirror", str(root),
                        str(target)], check=True, env=env)
    else:
        target = tmp_path / f"{tag}.bundle"
        subprocess.run(["git", "-C", str(root), "bundle", "create",
                        str(target), "main"], check=True, env=env,
                       capture_output=True)
    _set_origin(root, str(target))
    return root, head, tip


@pytest.mark.parametrize("shape", ["mirror", "bundle"])
def test_the_declared_residual_a_mirror_or_a_bundle_still_walks_through(
        tmp_path, shape):
    """A `--mirror` clone and a BUNDLE FILE serving ${ROOT}'s stale main.

    Both hold the same COMMITS without sharing ${ROOT}'s REFS, so the probe
    cannot see itself in them and the fetch hands back the stale tip the
    artefact was built from. make-dmg.sh listed "a bare mirror, a second clone,
    or a `cp -al` hardlink farm"; a `--mirror` clone and a bundle are the same
    boundary and are now named there too — a bundle particularly, because it is
    a single FILE rather than anything that looks like a repository.
    """
    tag = "resid" + shape
    root, head, tip = _stale_origin_shape(tmp_path, tag, shape)
    assert _origin_resolves_to(root) == head != tip, (
        "fixture: origin does not serve the build checkout's stale main")

    proc = _dmg_harness(tmp_path, tag, board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root)
    assert proc.returncode == 0, (
        "a %s no longer walks a stale signed build through. That is an "
        "IMPROVEMENT, not a failure — but make-dmg.sh's residual paragraph "
        "claims it does, so rewrite that claim rather than this test:\n%s"
        % (shape, proc.stderr))
    assert "resolves to this very repository" not in proc.stderr


@pytest.mark.parametrize("via", ["GIT_SSH_COMMAND", "core.sshCommand"])
def test_the_declared_residual_an_ssh_redirect_still_walks_through(tmp_path, via):
    """The seventh review's F2: `origin` over ssh, answered by the build checkout.

    make-dmg.sh and docs/INSTALLER.md both used to say that redirecting
    `GIT_SSH_COMMAND` "reaches a DIFFERENT repository … not a blinding of this
    check". The second half is FALSE and this is the case that refutes it: the
    ssh command is what answers the probe, so it can hide the probe ref while
    still advertising `refs/heads/main`, and the positive control never sees it
    because the control asks the build checkout over a LOCAL PATH — a transport
    the ssh command is not on.

    Both spellings are parametrized on purpose. `core.sshCommand` in a GLOBAL
    gitconfig does the identical thing with no environment variable at all
    (driven), which is the argument for filing this under the residual rather
    than adding `GIT_SSH_COMMAND` to the scrub: the scrub would close one
    spelling of it and the gate reads that config deliberately.

    Like the mirror and bundle cases above, this asserts the build DOES walk
    through, because that is what the residual claims. If it ever starts
    failing, rewrite the claim rather than this test.
    """
    tag = "sshres" + ("env" if via == "GIT_SSH_COMMAND" else "cfg")
    root, head, tip = _repo_with_origin(tmp_path, tag, behind=45)
    assert head != tip
    wrapper = tmp_path / f"ssh-wrapper-{tag}"
    # git appends its own `git-upload-pack '<path>'` argument; this ignores it
    # and serves the build checkout with the probe ref hidden.
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec git -c uploadpack.hideRefs=refs/nh-self-probe upload-pack '{root}'\n")
    wrapper.chmod(0o755)
    _set_origin(root, f"ssh://localhost{root}")
    if via == "GIT_SSH_COMMAND":
        extra = {"GIT_SSH_COMMAND": str(wrapper)}
    else:
        gc = tmp_path / f"hostile-gitconfig-{tag}"
        gc.write_text(f'[core]\n\tsshCommand = "{wrapper}"\n')
        extra = {"GIT_CONFIG_GLOBAL": str(gc)}

    proc = _dmg_harness(tmp_path, tag, board=STALE_BOARD, commit=head,
                        sign_mode="signed", root=root, env_extra=extra)
    assert proc.returncode == 0, (
        "an ssh redirect no longer walks a stale signed build through. That is "
        "an IMPROVEMENT, not a failure — but make-dmg.sh and docs/INSTALLER.md "
        "record it as a residual with this exact counterexample, so rewrite "
        "those claims rather than this test:\n" + proc.stderr)
    assert "[signed]" in proc.stdout, proc.stdout
    assert "resolves to this very repository" not in proc.stderr
    assert "probe is blind" not in proc.stderr, (
        "the positive control caught it after all — then it is not a residual, "
        "and the paragraphs that call it one are wrong")


# --------------------------------------------------------------------------- #
# The scrub list exists in THREE FILES, as FOUR copies, and must not drift
#
# make-dmg.sh, build-installer.sh (twice) and verify_artefact.py each carry the
# same list of GIT_* names, because each has to clear them before its own first
# git call and none of them can share a runtime with the others — a shell gate
# cannot import a Python tuple, and making the gate depend on one at build time
# would be a worse trade than the duplication.
#
# This is a SOURCE-TEXT assertion, and that is usually a trap: a regex over
# source proves nothing about behaviour, and this suite has been burned by
# exactly that (see the DEFECT 3 note above, where a source-text detach check
# survived deleting the detach). It is the right shape HERE only because the
# property IS source-level — "four copies of one constant agree" is not a proxy
# for anything. The BEHAVIOUR is pinned separately, by the hostile-environment
# cases that run the real block.
# --------------------------------------------------------------------------- #

def _shell_unset_lists(path: Path) -> list[set[str]]:
    """Every `unset GIT_…` statement in a shell script, as sets of names."""
    text = path.read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r"^unset ((?:[^\n\\]|\\\n)*)$", text, re.M):
        out.append(set(m.group(1).replace("\\\n", " ").split()))
    return out


def test_every_copy_of_the_git_env_scrub_list_is_identical():
    """One name added to one copy and not the others is a hole in the other two."""
    from_py = set(_load()._GIT_ENV_REDIRECTS)
    assert len(from_py) == len(_load()._GIT_ENV_REDIRECTS), "duplicate names"

    copies = {"scripts/verify_artefact.py": [from_py]}
    for rel in ("packaging/make-dmg.sh", "packaging/build-installer.sh"):
        found = _shell_unset_lists(REPO / rel)
        assert found, f"{rel} has no `unset GIT_…` statement at all"
        copies[rel] = found

    for rel, lists in copies.items():
        for i, names in enumerate(lists):
            assert names == from_py, (
                f"{rel} copy #{i} of the GIT_* scrub list disagrees with "
                f"verify_artefact.py's:\n  only in {rel}: "
                f"{sorted(names - from_py)}\n  missing from {rel}: "
                f"{sorted(from_py - names)}")


def test_the_scrub_list_covers_every_name_the_hostile_env_cases_set():
    """The tests must not be exercising a variable the scripts never clear.

    Without this, dropping a name from all four copies at once would leave the
    behavioural cases green — they assert the gate holds, and a variable nobody
    clears may still be harmless — while the documented list silently shrank.
    """
    scrubbed = set(_load()._GIT_ENV_REDIRECTS)
    exercised: set[str] = set()
    for scenario in _hostile_scenarios(Path("/other"), Path("/root")).values():
        exercised |= set(scenario)
    # GIT_CONFIG_KEY_0 / GIT_CONFIG_VALUE_0 are inert once GIT_CONFIG_COUNT is
    # gone (driven: git ignores them without the count), so they are set by the
    # cases but deliberately absent from the scrub list.
    exercised -= {"GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"}
    assert exercised <= scrubbed, (
        f"the hostile-environment cases set {sorted(exercised - scrubbed)}, "
        f"which no copy of the scrub list clears")
