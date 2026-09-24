"""End-to-end tests for the `gate` subcommand of
`scripts/history_gate_hit_report.py` — the RANGE-vs-TREE bugfix.

Bug: `verify_public_history.py --ref X --since Y` scans the whole tip TREE at
X regardless of `since`, so its verdict is a property of the repository, not
of the commits being pushed (measured: a single-commit range and a
completely unrelated 7-file range produced byte-identical 19-blob-hit,
195-extra-file verdicts). The scanner itself is private/drop-classified and
absent from this repo, so it cannot be patched directly.

These tests use a REAL scratch git repository (built fresh per test/fixture
under `tmp_path`) and a FAKE scanner module that reproduces the bug
FAITHFULLY: it walks the tip tree at `ref` via real `git` commands and
completely ignores `since`, exactly as measured on the real scanner. Against
that fake, `test_range_verdict_reports_a_hit_planted_in_the_scanned_range` is
the RED test — on the code as it stood before this fix (no `gate` subcommand,
no range attribution at all), it fails; the fix must turn it green without
changing what the fake scanner itself does.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "history_gate_hit_report.py"
HOOK_SRC = REPO / "scripts" / "hooks" / "pre-push"


def _load():
    spec = importlib.util.spec_from_file_location(
        "_nh_history_gate_range_attribution", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations via sys.modules[cls.__module__], so
    # the module must be registered before exec — same pattern
    # history_gate_hit_report.py's own _load_module_by_path uses.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


hgr = _load()


# --------------------------------------------------------------------------- #
# scratch git repo helpers — real `git`, no private repo, no real scanner
# --------------------------------------------------------------------------- #

PLANTED_TERM = "PLANTED_LEAK_TERM_FOR_TEST"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def scratch_repo(tmp_path):
    """A -> B -> C -> D.

    A: clean init.
    B: plants the term in legacy/old.txt (the PRE-EXISTING leak), and adds
       extra/base_extra.bin (the PRE-EXISTING extra file).
    C: plants the term in src/new.txt (the RANGE-introduced leak, over B..C),
       and adds extra/range_extra.bin (the RANGE-introduced extra file).
    D: adds innocuous.txt only — no term, no extra/ file: a range that
       introduces nothing, over C..D.
    """
    repo = tmp_path / "scratch"
    _init_repo(repo)
    sha_a = _commit(repo, {"README.md": "hello\n"}, "A: init")
    sha_b = _commit(repo, {
        "legacy/old.txt": f"pre-existing line with {PLANTED_TERM} in it\n",
        "extra/base_extra.bin": "base extra, present since B\n",
    }, "B: pre-existing leak + pre-existing extra file")
    sha_c = _commit(repo, {
        "src/new.txt": f"freshly introduced line with {PLANTED_TERM}\n",
        "extra/range_extra.bin": "extra file introduced by this range\n",
    }, "C: introduces a new leak + a new extra file")
    sha_d = _commit(repo, {"innocuous.txt": "nothing to see here\n"},
                    "D: innocuous change, introduces nothing")
    return {"repo": repo, "A": sha_a, "B": sha_b, "C": sha_c, "D": sha_d}


# --------------------------------------------------------------------------- #
# the fake scanner: reproduces the tip-tree-only bug FAITHFULLY, via real git
# --------------------------------------------------------------------------- #

_FAKE_SCANNER_SRC = '''
import dataclasses
import subprocess

PLANTED_TERM = "PLANTED_LEAK_TERM_FOR_TEST"


class GateError(RuntimeError):
    pass


class _Builder:
    def _terms_or_die(self, source):
        return {}


def _script_repo_root():
    return None


def _load_builder(repo_root):
    return _Builder()


def load_shapes(source):
    return {}


def load_message_terms(source, builder):
    return {}


@dataclasses.dataclass
class _Report:
    commits: int = 0
    blobs: int = 0
    paths: int = 0
    messages: int = 0
    identities: int = 0
    tags: int = 0
    blob_hits: list = dataclasses.field(default_factory=list)
    path_hits: list = dataclasses.field(default_factory=list)
    message_hits: list = dataclasses.field(default_factory=list)
    identity_hits: list = dataclasses.field(default_factory=list)
    tag_hits: list = dataclasses.field(default_factory=list)
    missing_files: list = dataclasses.field(default_factory=list)
    extra_files: list = dataclasses.field(default_factory=list)


def _git_out(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout


def scan_history(repo, builder, terms, shapes, message_terms=None,
                 workdir=None, since=None, ref=None, progress=None):
    # THE BUG, REPRODUCED FAITHFULLY: walks the TIP TREE ONLY at `ref`,
    # ignoring `since` entirely -- exactly the measured defect this test
    # file exists to catch (a single-commit range and an unrelated 7-file
    # range produced byte-identical verdicts on the real scanner).
    ref_sha = _git_out(repo, "rev-parse", ref).strip()
    names = sorted(
        n for n in
        _git_out(repo, "ls-tree", "-r", "--name-only", ref_sha).splitlines()
        if n
    )
    rep = _Report(commits=1, blobs=len(names))
    for name in names:
        if name.startswith("extra/"):
            rep.extra_files.append(name)
            continue
        blob = _git_out(repo, "show", f"{ref_sha}:{name}")
        if PLANTED_TERM in blob:
            rep.blob_hits.append(
                f"{ref_sha} {name} :: shape_planted_term: {PLANTED_TERM}")
    return rep
'''

_FAKE_SCANNER_GATE_ERROR_SRC = _FAKE_SCANNER_SRC.replace(
    "def scan_history(repo, builder, terms, shapes, message_terms=None,\n"
    "                 workdir=None, since=None, ref=None, progress=None):\n"
    "    # THE BUG",
    "def scan_history(repo, builder, terms, shapes, message_terms=None,\n"
    "                 workdir=None, since=None, ref=None, progress=None):\n"
    "    raise GateError('could not arm the gate')\n"
    "\n"
    "def _unreachable(repo, builder, terms, shapes, message_terms=None,\n"
    "                 workdir=None, since=None, ref=None, progress=None):\n"
    "    # THE BUG",
)

assert _FAKE_SCANNER_GATE_ERROR_SRC != _FAKE_SCANNER_SRC, (
    "the .replace() target drifted out of sync with _FAKE_SCANNER_SRC")


def _write_fake_scanner(tmp_path: Path, *, gate_error: bool = False) -> Path:
    src = _FAKE_SCANNER_GATE_ERROR_SRC if gate_error else _FAKE_SCANNER_SRC
    path = tmp_path / "fake_verify_public_history.py"
    path.write_text(src, encoding="utf-8")
    return path


def _gate_args(tmp_path, *, scanner, repo, ref, since, json_out=None,
               detail_out=None, fail_on="range", source_dir=None):
    source = source_dir or (tmp_path / "source")
    source.mkdir(exist_ok=True)
    return argparse.Namespace(
        scanner=str(scanner), source=str(source), repo=str(repo),
        ref=ref, since=since,
        json=str(json_out) if json_out else None,
        detail=str(detail_out) if detail_out else None,
        progress_log=None, fail_on=fail_on,
    )


# --------------------------------------------------------------------------- #
# AC1: the RED test — plants a term in the scanned range, asserts the RANGE
# verdict catches it. Fails on pre-fix code (no `gate` subcommand at all).
# --------------------------------------------------------------------------- #

def test_range_verdict_reports_a_hit_planted_in_the_scanned_range(
        scratch_repo, tmp_path, capsys):
    scanner = _write_fake_scanner(tmp_path)
    args = _gate_args(tmp_path, scanner=scanner, repo=scratch_repo["repo"],
                      ref=scratch_repo["C"], since=scratch_repo["B"])

    rc = hgr.cmd_gate(args)
    out = capsys.readouterr().out

    assert rc == 1, "a range-introduced hit must fail the gate (exit 1)"
    assert "RANGE VERDICT: FAILED" in out
    assert "src/new.txt" in out, "the range-introduced hit must name its path"
    # the pre-existing leak (legacy/old.txt, planted at B) must be reported
    # too, but as PRE-EXISTING, never silently folded into the range count.
    assert "PRE-EXISTING at base: 1 blob" in out
    # the pre-existing leak (legacy/old.txt, planted at B) is counted in the
    # PRE-EXISTING summary above, but the console verdict's per-hit detail
    # (the "RANGE HITS" block) names only RANGE-introduced paths; its own
    # path never appears there, so it can never be misread as range-caused.
    assert "legacy/old.txt" not in out


# --------------------------------------------------------------------------- #
# AC1 / AC4: a range that introduces nothing must report zero range hits
# while the (non-zero) tip-wide count is preserved, unchanged from a prior
# scan of the same tip state.
# --------------------------------------------------------------------------- #

_TIP_WIDE_BLOB_RE = re.compile(r"TIP-WIDE total at \S+: (\d+) blob")


def test_range_that_introduces_nothing_reports_zero_range_hits_with_tip_count_unchanged(
        scratch_repo, tmp_path, capsys):
    scanner = _write_fake_scanner(tmp_path)

    # The failing scan from the test above, re-run here for a same-tip-state
    # comparison point: ref=C, since=B.
    args_failing = _gate_args(tmp_path, scanner=scanner,
                              repo=scratch_repo["repo"], ref=scratch_repo["C"],
                              since=scratch_repo["B"])
    rc_failing = hgr.cmd_gate(args_failing)
    out_failing = capsys.readouterr().out
    assert rc_failing == 1
    tip_wide_failing = _TIP_WIDE_BLOB_RE.search(out_failing).group(1)

    # D only adds an innocuous file over C -- the range C..D introduces
    # nothing. D's tree still contains everything C's tree did (nothing was
    # removed), so TIP-WIDE at D must report the SAME blob count as TIP-WIDE
    # at C above.
    args_clean = _gate_args(tmp_path, scanner=scanner,
                            repo=scratch_repo["repo"], ref=scratch_repo["D"],
                            since=scratch_repo["C"])
    rc_clean = hgr.cmd_gate(args_clean)
    out_clean = capsys.readouterr().out

    assert rc_clean == 0, "a range that introduces nothing must pass (exit 0)"
    assert "RANGE VERDICT: PASSED" in out_clean
    assert "0 blob, 0 path, 0 message, 0 identity, 0 tag hit(s) introduced " \
          "by this range" in out_clean

    tip_wide_clean = _TIP_WIDE_BLOB_RE.search(out_clean).group(1)
    assert tip_wide_clean == tip_wide_failing, (
        "TIP-WIDE must be unchanged/preserved across an innocuous range, "
        f"not silently zeroed or altered: {tip_wide_clean!r} != "
        f"{tip_wide_failing!r}")
    assert int(tip_wide_clean) > 0, "the preserved tip-wide count must be non-zero"


# --------------------------------------------------------------------------- #
# AC2: the summary line names BOTH numbers, so neither can be read as the
# other.
# --------------------------------------------------------------------------- #

def test_summary_line_names_both_numbers(scratch_repo, tmp_path, capsys):
    scanner = _write_fake_scanner(tmp_path)
    args = _gate_args(tmp_path, scanner=scanner, repo=scratch_repo["repo"],
                      ref=scratch_repo["C"], since=scratch_repo["B"])
    hgr.cmd_gate(args)
    out = capsys.readouterr().out

    verdict_line = [ln for ln in out.splitlines() if "RANGE VERDICT" in ln]
    assert len(verdict_line) == 1
    line = verdict_line[0]
    assert "introduced by this range" in line
    assert "pre-existing at base" in line
    # the two numbers must be distinct in the text, not one figure doing
    # double duty: 1 hit introduced (src/new.txt) vs 1 hit pre-existing
    # (legacy/old.txt) -- same magnitude here by fixture design, but each is
    # spelled out with its own explicit surface breakdown / label so a
    # reader can never mistake one count for the other.
    assert "1 blob, 0 path, 0 message, 0 identity, 0 tag hit(s) introduced " \
          "by this range" in line
    assert "1 hit(s) and 1 extra file(s) pre-existing at base" in line


# --------------------------------------------------------------------------- #
# AC2 / intake Q2: the "extra file(s)" count gets the same range/pre-existing
# split as hits.
# --------------------------------------------------------------------------- #

def test_extra_files_are_split_into_range_and_preexisting(scratch_repo,
                                                           tmp_path):
    scanner_path = _write_fake_scanner(tmp_path)
    mod = hgr._load_module_by_path("_range_attr_fake_scanner", scanner_path)
    source = tmp_path / "source"
    source.mkdir()

    verdict = hgr.run_range_scan(
        mod, source=source, repo=scratch_repo["repo"], ref=scratch_repo["C"],
        since=scratch_repo["B"], progress=lambda msg: None)

    assert verdict.preexisting_extra == ["extra/base_extra.bin"]
    assert verdict.range_extra == ["extra/range_extra.bin"]


# --------------------------------------------------------------------------- #
# per-hit provenance: `introduced_by` is checkable against real `git log`.
# --------------------------------------------------------------------------- #

def test_each_range_hit_carries_the_commit_that_introduced_it(scratch_repo,
                                                               tmp_path):
    scanner_path = _write_fake_scanner(tmp_path)
    mod = hgr._load_module_by_path("_range_attr_fake_scanner2", scanner_path)
    source = tmp_path / "source"
    source.mkdir()

    verdict = hgr.run_range_scan(
        mod, source=source, repo=scratch_repo["repo"], ref=scratch_repo["C"],
        since=scratch_repo["B"], progress=lambda msg: None)

    assert len(verdict.introduced_hits) == 1
    hit = verdict.introduced_hits[0]
    assert hit.path == "src/new.txt"

    # independently checkable: `git log` on that exact path, over that exact
    # range, must agree with what the tool claims introduced it.
    expected = _git(scratch_repo["repo"], "log", "--reverse", "--format=%H",
                    f"{scratch_repo['B']}..{scratch_repo['C']}", "--",
                    "src/new.txt").stdout.strip()
    assert hit.introduced_by == expected == scratch_repo["C"]
    assert hit.provenance_source == "git-log"


# --------------------------------------------------------------------------- #
# AC3: per-hit detail reaches the caller through the pre-push hook path, and
# survives a 600-character tail truncation (the nh-guard failure mode this
# file exists to not repeat).
# --------------------------------------------------------------------------- #

def test_pre_push_hook_path_emits_the_full_hit_list(scratch_repo, tmp_path):
    scanner_path = _write_fake_scanner(tmp_path)
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    repo = scratch_repo["repo"]
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-push"
    if hook_path.exists() or hook_path.is_symlink():
        hook_path.unlink()
    hook_path.symlink_to(HOOK_SRC)

    stdin = f"refs/heads/main {scratch_repo['C']} refs/heads/main {scratch_repo['B']}\n"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "NH_HISTORY_GATE_SCANNER": str(scanner_path),
        "NH_HISTORY_GATE_SOURCE": str(source_dir),
    }
    proc = subprocess.run(["sh", str(hook_path)], input=stdin,
                          capture_output=True, text=True, cwd=repo, env=env)

    assert proc.returncode == 1, (
        f"a range-introduced hit must refuse the push\nstdout:\n{proc.stdout}"
        f"\nstderr:\n{proc.stderr}")
    assert "src/new.txt" in proc.stdout, "full per-hit detail must reach the caller"
    assert "RANGE blob" in proc.stdout

    tail = proc.stdout[-600:]
    assert "RANGE VERDICT" in tail, (
        "the verdict line must survive a 600-character tail truncation")
    assert "RANGE " in tail, (
        "at least one hit line must survive a 600-character tail truncation")


# --------------------------------------------------------------------------- #
# Regression: a brand-new remote ref makes the pre-push hook fall back to the
# well-known empty-tree SHA (4b825dc6...) for `since`. That hash is a valid
# git object (`rev-parse` accepts it) but NOT a commit, so it cannot sit on
# the excluded side of a `base..ref` revision range. `gate` must recognize
# "no common history" and treat the WHOLE ref as the range instead of ever
# putting that tree object into a `rev-list`/`log` range.
# --------------------------------------------------------------------------- #

EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def test_brand_new_ref_treats_whole_ref_as_the_range_without_crashing(
        scratch_repo, tmp_path, capsys):
    scanner = _write_fake_scanner(tmp_path)
    args = _gate_args(tmp_path, scanner=scanner, repo=scratch_repo["repo"],
                      ref=scratch_repo["C"], since=EMPTY_TREE_SHA)

    rc = hgr.cmd_gate(args)
    out = capsys.readouterr().out

    # Must reach a verdict, not raise: a brand-new-ref push (since == the
    # empty tree) used to feed a tree object into `base..ref` rev-list/log
    # ranges.
    assert rc in (0, 1), f"gate must reach a verdict, not crash:\n{out}"
    assert "no common history" in out
    # Nothing pre-exists for a brand-new ref -- the whole tip is the range,
    # so BOTH the B-planted and C-planted hits are RANGE-introduced, and
    # PRE-EXISTING at base must be zero.
    assert "PRE-EXISTING at base: 0 blob" in out
    assert "src/new.txt" in out
    assert "legacy/old.txt" in out
    assert "RANGE VERDICT: FAILED" in out
    assert rc == 1


def test_pre_push_hook_handles_brand_new_ref_push(scratch_repo, tmp_path):
    """The exact scenario the reviewer measured: `remote_sha` is all-zero (a
    brand-new remote ref), so the hook sets `since` to the empty-tree SHA
    before calling `gate`. That must not crash the hook.
    """
    scanner_path = _write_fake_scanner(tmp_path)
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    repo = scratch_repo["repo"]
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-push"
    if hook_path.exists() or hook_path.is_symlink():
        hook_path.unlink()
    hook_path.symlink_to(HOOK_SRC)

    zero = "0" * 40
    stdin = (f"refs/heads/newbranch {scratch_repo['C']} "
             f"refs/heads/newbranch {zero}\n")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "NH_HISTORY_GATE_SCANNER": str(scanner_path),
        "NH_HISTORY_GATE_SOURCE": str(source_dir),
    }
    proc = subprocess.run(["sh", str(hook_path)], input=stdin,
                          capture_output=True, text=True, cwd=repo, env=env)

    assert "Traceback" not in proc.stderr, (
        f"a brand-new-ref push must not crash the gate\nstdout:\n{proc.stdout}"
        f"\nstderr:\n{proc.stderr}")
    assert proc.returncode == 1, (
        f"hits exist in the new ref, so the push must be refused\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    assert "src/new.txt" in proc.stdout
    assert "legacy/old.txt" in proc.stdout


# --------------------------------------------------------------------------- #
# exit-code contract: a gate-arming failure is exit 2, never conflated with a
# leak verdict (exit 1).
# --------------------------------------------------------------------------- #

def test_gate_arming_failure_is_exit_2_not_1(scratch_repo, tmp_path, capsys):
    scanner = _write_fake_scanner(tmp_path, gate_error=True)
    args = _gate_args(tmp_path, scanner=scanner, repo=scratch_repo["repo"],
                      ref=scratch_repo["C"], since=scratch_repo["B"])

    rc = hgr.cmd_gate(args)
    captured = capsys.readouterr()

    assert rc == 2
    assert "could not be armed" in captured.err
    assert "RANGE VERDICT" not in captured.out, "no verdict must be emitted on an arming failure"


# --------------------------------------------------------------------------- #
# `--detail` is uncapped, regardless of how many hits there are.
# --------------------------------------------------------------------------- #

def test_detail_file_is_uncapped(tmp_path):
    repo = tmp_path / "many_hits_repo"
    _init_repo(repo)
    sha_a = _commit(repo, {"README.md": "hello\n"}, "A: init")

    many_files = {
        f"planted/file_{i:03d}.txt": f"line {i} {PLANTED_TERM}\n"
        for i in range(250)
    }
    sha_b = _commit(repo, many_files, "B: plants 250 hits in one range")

    scanner_path = _write_fake_scanner(tmp_path)
    source = tmp_path / "source_many"
    source.mkdir()
    detail_out = tmp_path / "detail.txt"

    args = _gate_args(tmp_path, scanner=scanner_path, repo=repo, ref=sha_b,
                      since=sha_a, detail_out=detail_out,
                      source_dir=source)
    rc = hgr.cmd_gate(args)
    assert rc == 1

    detail_text = detail_out.read_text(encoding="utf-8")
    range_lines = [ln for ln in detail_text.splitlines()
                  if ln.startswith("RANGE blob")]
    assert len(range_lines) == 250, (
        f"the --detail file must contain all 250 planted hits uncapped, "
        f"got {len(range_lines)}")
