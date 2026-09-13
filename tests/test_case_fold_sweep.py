"""AC4 — a before/after sweep through the REAL `guard.evaluate` entry point.

This is the load-bearing safety net for the whole change: it is not enough
that the new, targeted tests in `test_exec_names.py`/`test_venv_install_guard.py`
pass — this file proves that widening the fold probe did not *also* widen
what gets allowed anywhere else in the guard, by replaying a corpus of real
commands through the same entry point the backends actually call.

Two things are asserted, and they are asserted separately on purpose:

* `test_no_corpus_row_moved_from_denied_to_allowed` — the safety net. Zero
  commands may move from denied (pre-fix) to allowed (now). This is the bar
  a REGRESSION must clear; it says nothing about whether this change did
  anything at all.
* `test_the_sweep_moved_rows_in_the_closing_direction` — the proof this
  change is not a no-op. A named, non-empty set of commands must move from
  allowed (pre-fix) to denied (now) — the capitalised installer spellings
  (`PIP`/`Pip`/`PIP3`/`UV`) the bug let through.

`testdata/case_fold_corpus.json`'s `baseline` was captured by restoring
`exec_names.py`/`venv_install_guard.py` to their pre-fix (parent-commit)
content — via `git show HEAD:<path>`, since this sandbox's destructive-op
guard blocks `git worktree add` — into a scratch copy of `src/no_human` and
re-running the same corpus against it with `sys.path` pointed there instead.
On this dev host that measurement coincides with post-fix for every row in
the JSON corpus (both the old `__file__`-probe and the new cwd/PATH-union
probe happen to measure the same single APFS volume here), so the JSON
corpus alone only exercises the safety net. The closing-direction proof
needs a constructed two-venv session (a foreign "primary" venv on `PATH`,
a separate worktree `cwd`) to structurally trigger the venv-install guard at
all, which is not representable as static `cwd`/`env` string literals — so
that half lives in its own test, below, that builds the session and calls
`guard.evaluate` directly.
"""

import json
from pathlib import Path

import pytest

from no_human.agent import exec_names, guard

CORPUS_PATH = Path(__file__).parent.parent / "testdata" / "case_fold_corpus.json"

#: Rows that must stay allowed throughout — present in the JSON corpus too,
#: named again here so an edit to the corpus file cannot silently drop the
#: over-folding check.
_ALLOW_CONTROLS = (
    "pip list",
    "pip show sync",
    "ls -la",
    "git status",
    "pytest -q",
    "nh learnings",
    "PIPELINE_STATUS=1 echo ok",
    'echo "GH pr merge"',
)


@pytest.fixture(autouse=True)
def _clear_fold_cache():
    """`host_folds_case` is `lru_cache`d per-process; without this a test in
    this file that pins the probe (via monkeypatch) or measures a real
    volume can read a stale answer left behind by a previous test."""
    clear = getattr(exec_names.host_folds_case, "cache_clear", None)
    if clear is not None:
        clear()
    yield
    clear = getattr(exec_names.host_folds_case, "cache_clear", None)
    if clear is not None:
        clear()


def _load_corpus():
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def _now_denied(rows):
    out = {}
    for row in rows:
        d = guard.evaluate(
            "Bash", {"command": row["cmd"]}, forbidden_paths=[],
            never_push_to=["main"], cwd=row.get("cwd", "."),
            env=row.get("env", {"PATH": ""}),
        )
        out[row["cmd"]] = not d.allow
    return out


def test_no_corpus_row_moved_from_denied_to_allowed():
    """AC4's safety net: nothing this change touches may move a command from
    denied to allowed. This is checked against the corpus's own `baseline`,
    not against `now` in the other direction — a row absent from `baseline`
    simply was not measured pre-fix and cannot regress."""
    corpus = _load_corpus()
    now = _now_denied(corpus["rows"])
    baseline = corpus["baseline"]

    was_denied = {cmd for cmd, denied in baseline.items() if denied}
    still_denied = {cmd for cmd, denied in now.items() if denied}
    regressed = was_denied - still_denied

    assert not regressed, (
        "these commands were DENIED pre-fix and are now ALLOWED -- a "
        f"regression this change must not introduce: {sorted(regressed)}")


def test_the_allow_side_controls_still_run():
    """Catches over-folding: ordinary, non-invocation commands (or ones that
    merely *mention* a sensitive word in a string/env-assignment) must stay
    allowed. A fix that denied everything would trivially pass the DENY-only
    test above, so this is the other half of the bar."""
    corpus = _load_corpus()
    now = _now_denied(corpus["rows"])

    for cmd in _ALLOW_CONTROLS:
        assert cmd in now, f"missing allow-control row in the corpus: {cmd!r}"
        assert not now[cmd], (
            f"over-folded: {cmd!r} must stay allowed, but the real "
            "guard.evaluate() now denies it")


# ---------------------------------------------------------------------------
# The closing-direction proof. Needs a real two-venv session (a foreign
# "primary" venv resolvable via PATH, a separate worktree cwd) to structurally
# trigger `venv_install_guard.denial_reason` at all -- see the module
# docstring for why this cannot be a static row in the JSON corpus.
# ---------------------------------------------------------------------------

def _mkvenv(root):
    """A minimal, real venv shape: `<root>/.venv/bin/{pip,pip3,uv}` (each an
    existing, executable file so `_resolve_installer` can actually resolve
    them -- an installer name that CANNOT be resolved via PATH is, by
    design, allowed-and-logged rather than denied, so a fixture missing
    these would test nothing) plus a `pyvenv.cfg` marker."""
    venv = root / ".venv"
    (venv / "bin").mkdir(parents=True)
    for name in ("pip", "pip3", "uv"):
        f = venv / "bin" / name
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
    (venv / "pyvenv.cfg").write_text("home = /usr\n")
    return root, str(venv)


def test_the_sweep_moved_rows_in_the_closing_direction(tmp_path, monkeypatch):
    """Proves the fix actually closes the bypass rather than merely not
    regressing: on a folding host, `PIP`/`Pip`/`PIP3 install .../UV add ...`
    into a foreign (shared) venv must now be denied exactly like the
    lowercase spelling always was -- pre-fix, `_is_installer_name` folded
    case only on `_IS_WINDOWS` (`os.name == "nt"`), so on ANY POSIX test
    runner every capitalised spelling below was unconditionally allowed.
    That pre-fix answer does not depend on measuring this host's real
    filesystem at all -- it is a pure `os.name` check -- which is why it is
    asserted directly here rather than captured by re-running old code.
    """
    monkeypatch.setattr(exec_names, "host_folds_case", lambda *a, **k: True)
    _, primary_venv = _mkvenv(tmp_path / "primary")
    wt, _ = _mkvenv(tmp_path / "wt")
    # Exactly what a coder's Bash inherits in production: PATH/VIRTUAL_ENV
    # pointing at the shared dev venv, regardless of which worktree `cwd`
    # names.
    prod_env = {"PATH": f"{primary_venv}/bin:/usr/bin:/bin",
                "VIRTUAL_ENV": primary_venv}

    cases = [
        "pip install somepkg", "PIP install somepkg", "Pip install somepkg",
        "PIP3 install somepkg", "UV add somepkg",
    ]
    now_denied = {}
    for cmd in cases:
        d = guard.evaluate(
            "Bash", {"command": cmd}, forbidden_paths=[],
            never_push_to=["main"], cwd=str(wt), env=prod_env)
        now_denied[cmd] = not d.allow

    baseline_denied = {
        "pip install somepkg": True,
        "PIP install somepkg": False,
        "Pip install somepkg": False,
        "PIP3 install somepkg": False,
        "UV add somepkg": False,
    }

    for cmd in cases:
        assert now_denied[cmd], (
            f"must be denied now (foreign shared venv, folding host): {cmd}")

    closing = {c for c in cases if not baseline_denied[c] and now_denied[c]}
    assert closing, (
        "no row moved from ALLOW (pre-fix) to DENY (now) -- a no-op fix "
        "would also pass the regression-only test above, so this must be "
        "non-empty")
    assert closing == {
        "PIP install somepkg", "Pip install somepkg",
        "PIP3 install somepkg", "UV add somepkg",
    }, f"unexpected closing set: {sorted(closing)}"

    regressed = {c for c in cases if baseline_denied[c] and not now_denied[c]}
    assert not regressed, f"DENY->ALLOW regression: {sorted(regressed)}"


def test_installing_into_ones_own_worktree_venv_stays_allowed(
    tmp_path, monkeypatch
):
    """Allow-side control for the closing-direction test above: even on a
    folding host, installing via the session's OWN worktree venv (not the
    shared one) must stay allowed -- proving the fix denies on FOREIGN-venv
    resolution, not on capitalisation alone."""
    monkeypatch.setattr(exec_names, "host_folds_case", lambda *a, **k: True)
    wt, wt_venv = _mkvenv(tmp_path / "wt")
    wt_env = {"PATH": f"{wt_venv}/bin:/usr/bin:/bin", "VIRTUAL_ENV": wt_venv}

    d = guard.evaluate(
        "Bash", {"command": f"{wt_venv}/bin/pip install ."},
        forbidden_paths=[], never_push_to=["main"], cwd=str(wt), env=wt_env)
    assert d.allow, f"own-worktree install must stay allowed: {d.reason}"
