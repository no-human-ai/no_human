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

`testdata/case_fold_corpus.json` carries no baseline table — a hand-typed
"what the pre-fix code would have denied" dict cannot be trusted to reflect
what the pre-fix code actually did on whatever host runs the suite, which is
exactly the defect an earlier version of this file shipped: it worked out a
`baseline_nonfolding` table by REASONING about the post-fix matchers rather
than by running the pre-fix probe, so a future regression in the probe's own
measurement could have been compared against an answer authored to match it.

Instead, `test_no_corpus_row_moved_from_denied_to_allowed` computes the
"pre-fix" baseline at run time: `_pre_fix_host_folds_case` is a literal
reproduction of `host_folds_case()` as it read at the commit before this
change (`git show 0b8c2dc4:src/no_human/agent/exec_names.py` —
`_folds_case(os.path.realpath(__file__))` with an `os.name == "nt"`
fallback), executed for real against THIS process's own `exec_names.py` on
THIS host — a genuine measurement, not a transcription. Every corpus row's
denied-ness is driven by call sites (`command_name`'s `fold_case=None`
default, `guard.py`'s import-time `case_flags()`) that pass `host_folds_case`
the exact same no-argument shape both before and after this change — only
what the probe MEASURES changed, not how guard.py consumes the boolean — so
forcing that measured boolean into a FRESH interpreter (`_RM_RF` and
`_GIT_DESTRUCTIVE` bake `case_flags()` into compiled patterns at import time,
so an in-process monkeypatch after `guard` is already imported cannot reach
them; see `exec_names.case_flags`'s own docstring) and importing the
CURRENT, already-fixed `guard.py`/`venv_install_guard.py` fresh reproduces
exactly what the pre-fix code would have decided for these rows, on any
host, without needing the pre-fix source at all. (No corpus row resolves an
installer via `PATH` — every row's `env` is `{"PATH": ""}` — so this
argument does not have to also cover `_is_installer_name`'s `cwd`-threading
change; that half is pinned separately, below and in
`test_venv_install_guard.py`.)

The closing-direction proof needs a constructed two-venv session (a foreign
"primary" venv on `PATH`, a separate worktree `cwd`) to structurally trigger
the venv-install guard at all, which is not representable as static
`cwd`/`env` string literals — so that half lives in its own test, below,
that builds the session and calls `guard.evaluate` directly.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

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


def _pre_fix_host_folds_case() -> bool:
    """A literal reproduction of `host_folds_case()` as it read at
    `0b8c2dc4` (the commit immediately before this change), executed for
    real against THIS process's OWN `exec_names.py` module file on THIS
    host -- a genuine measurement of what the pre-fix probe would have
    answered here, not a transcription of what it answered somewhere else.

    Pre-fix: `_folds_case(os.path.realpath(__file__))` --  swap the case of
    `exec_names.py`'s own basename and ask `samefile`; an unswappable name
    or an unreadable/vanished swapped path falls back to `os.name == "nt"`,
    the permissive answer on POSIX. That fallback is exactly the defect
    this task closes (AC2/AC3) -- reproduced here on purpose, since the
    point of this helper is to measure what the OLD, buggy probe did, not
    to already apply the fix under test.
    """
    directory, name = os.path.split(os.path.realpath(exec_names.__file__))
    swapped_name = name.swapcase()
    if swapped_name == name:
        return os.name == "nt"
    swapped = os.path.join(directory, swapped_name)
    try:
        return os.path.exists(swapped) and os.path.samefile(
            swapped, os.path.realpath(exec_names.__file__))
    except OSError:
        return os.name == "nt"


def _denied_with_forced_fold(commands, fold: bool) -> dict:
    """Guard verdicts for `commands`, from a FRESH interpreter with the fold
    probe pinned to `fold` before `guard` is imported.

    Not an in-process monkeypatch: `_RM_RF`, `_GIT_DESTRUCTIVE` and the
    `_looks_like_git_push` recursion gate bake `exec_names.case_flags()`
    into compiled regex patterns at import time (see `case_flags`'s own
    docstring), so patching `host_folds_case` after `guard` is already
    imported only reaches the name-resolution half -- exactly the shape of
    bug that let a fix for one half look complete while the raw-text half
    stayed open. Mirrors `tests/test_exec_names.py::_verdicts_with_fold`,
    generalised to an arbitrary command list instead of one fixed tuple.

    Reusing the CURRENT, already-fixed `guard.py`/`venv_install_guard.py`
    here (rather than checking out the pre-fix source tree) is sound
    because every corpus row's `env` is `{"PATH": ""}` -- none resolves an
    installer via PATH, so `_is_installer_name`'s cwd-threading fix (the
    other half of this change) never fires for these rows -- and because
    `guard.py`'s call sites pass `host_folds_case`/`case_flags()` the exact
    same no-argument shape both before and after this change; only what the
    probe MEASURES changed, not how the boolean is consumed. Forcing the
    measured pre-fix boolean through the current consumption code therefore
    reproduces exactly what the pre-fix code decided for these rows.
    """
    code = dedent(f"""
        import json
        from no_human.agent import exec_names
        exec_names.host_folds_case = lambda *a, **k: {fold!r}
        from no_human.agent.guard import evaluate
        out = {{}}
        for cmd in {list(commands)!r}:
            decision = evaluate(
                "Bash", {{"command": cmd}}, forbidden_paths=[],
                never_push_to=["main"], cwd=".", env={{"PATH": ""}})
            out[cmd] = not decision.allow
        print(json.dumps(out))
    """)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_no_corpus_row_moved_from_denied_to_allowed():
    """AC4's safety net: nothing this change touches may move a command from
    denied to allowed.

    The baseline is not read from a static table -- see the module
    docstring for why an earlier, hand-typed `baseline_nonfolding` dict was
    exactly the defect this test now avoids. Instead: measure what the
    PRE-FIX probe would answer on THIS real host
    (`_pre_fix_host_folds_case`, real I/O, no monkeypatching), then force
    that measured boolean through the CURRENT guard in a fresh interpreter
    (`_denied_with_forced_fold`) to get the true pre-fix-equivalent denied
    set for every row in the corpus -- on whatever host class runs this
    suite, folding or not."""
    corpus = _load_corpus()
    commands = [row["cmd"] for row in corpus["rows"]]
    now = _now_denied(corpus["rows"])

    pre_fold = _pre_fix_host_folds_case()
    baseline = _denied_with_forced_fold(commands, pre_fold)

    was_denied = {cmd for cmd, denied in baseline.items() if denied}
    still_denied = {cmd for cmd, denied in now.items() if denied}
    regressed = was_denied - still_denied

    assert not regressed, (
        f"pre-fix host_folds_case()={pre_fold} baseline -- these commands "
        "were DENIED pre-fix and are now ALLOWED -- a regression this "
        f"change must not introduce: {sorted(regressed)}")


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

def _mkvenv(root, extra_names=()):
    """A minimal, real venv shape: `<root>/.venv/bin/{pip,pip3,uv}` (each an
    existing, executable file so `_resolve_installer` can actually resolve
    them -- an installer name that CANNOT be resolved via PATH is, by
    design, allowed-and-logged rather than denied, so a fixture missing
    these would test nothing) plus a `pyvenv.cfg` marker.

    `extra_names`: additional literal file spellings to also create (e.g.
    `("PIP", "Pip", "PIP3")`) -- mocking `host_folds_case` only changes the
    CLASSIFICATION decision, it cannot make a genuinely case-sensitive
    test-runner filesystem (Linux/ext4 CI) resolve a literal `"PIP"` PATH
    lookup against a file that is really named `pip`; a test that simulates
    "what a real folding host would see" on a case-sensitive runner needs
    the literal spelling to actually exist on disk."""
    venv = root / ".venv"
    (venv / "bin").mkdir(parents=True)
    for name in ("pip", "pip3", "uv", "uvx", *extra_names):
        f = venv / "bin" / name
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
    (venv / "pyvenv.cfg").write_text("home = /usr\n")
    return root, str(venv)


def test_the_sweep_moved_rows_in_the_closing_direction(tmp_path, monkeypatch):
    """Proves the fix actually closes the bypass rather than merely not
    regressing: on a folding host, `PIP`/`Pip`/`PIP3 install ...` into a
    foreign (shared) venv must now be denied exactly like the lowercase
    spelling always was -- pre-fix, `_is_installer_name` folded case only on
    `_IS_WINDOWS` (`os.name == "nt"`), so on ANY POSIX test runner every
    capitalised spelling below was unconditionally allowed. That pre-fix
    answer does not depend on measuring this host's real filesystem at all
    -- it is a pure `os.name` check -- which is why it is asserted directly
    here rather than captured by re-running old code.

    `UV add somepkg` is intentionally NOT one of these rows: `uv` resolves
    its install target via `cwd`/`pyproject.toml`, never via its own
    resolved binary's location the way `pip`/`python` are (see the long
    comment above the `uv`/`uvx` exclusion in `venv_install_guard.py`), so
    a foreign shared `VIRTUAL_ENV` was never a hole for it -- measured
    unchanged (allowed) for the lowercase spelling at every commit checked,
    including the pre-task baseline. `uv`/`UV`'s own case-consistency (the
    actual BLOCKER-2 regression, `UV sync` denied while `uv sync` allowed)
    is pinned separately by
    `test_venv_install_guard.test_a_capitalised_uv_commands_are_not_denied_like_pip`.
    """
    monkeypatch.setattr(exec_names, "host_folds_case", lambda *a, **k: True)
    _, primary_venv = _mkvenv(
        tmp_path / "primary", extra_names=("PIP", "Pip", "PIP3"))
    wt, _ = _mkvenv(tmp_path / "wt")
    # Exactly what a coder's Bash inherits in production: PATH/VIRTUAL_ENV
    # pointing at the shared dev venv, regardless of which worktree `cwd`
    # names.
    prod_env = {"PATH": f"{primary_venv}/bin:/usr/bin:/bin",
                "VIRTUAL_ENV": primary_venv}

    cases = [
        "pip install somepkg", "PIP install somepkg", "Pip install somepkg",
        "PIP3 install somepkg",
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
        "PIP3 install somepkg",
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


def test_the_sweep_does_not_regress_the_uvx_active_flag_placement(
    tmp_path, monkeypatch
):
    """AC4 visibility for BLOCKER A's bug class: a plain corpus row (empty
    `PATH`, `cwd="."`) cannot exercise this at all -- `uvx` never resolves
    via PATH there, so `_resolve_installer` fails open (allows-and-logs)
    for every case, fix or no fix (see the module docstring on why the
    closing-direction proof needs a real two-venv session instead of static
    corpus rows; this is the same structural reason `_ALLOW_CONTROLS` cannot
    host this row either). This test builds that session so the sweep file
    -- not just `test_venv_install_guard.py`'s targeted unit test -- would
    have caught BLOCKER A: `_uses_active_env`'s `expects_program` check
    compared `_basename(tokens[start]).startswith("uvx")` un-folded, so on a
    folding host `UVX ruff check --active` (trailing `--active`, belongs to
    the invoked program `ruff`) was wrongly DENIED while the identical
    lowercase spelling stayed ALLOWED -- while the reverse shape (a leading
    `--active`, uvx's own flag) must stay denied regardless of case."""
    monkeypatch.setattr(exec_names, "host_folds_case", lambda *a, **k: True)
    # `extra_names` creates the literal `UVX`/`Uvx` files on disk -- mocking
    # `host_folds_case` only changes the CLASSIFICATION decision; on a
    # genuinely case-sensitive test-runner filesystem (Linux/ext4 CI)
    # `_resolve_installer`'s real PATH walk still needs the literal spelling
    # to exist to resolve it the way a real folding host's shell would for
    # free (see `_mkvenv`'s own docstring, and the identical pattern already
    # used above for `PIP`/`Pip`/`PIP3`).
    _, primary_venv = _mkvenv(
        tmp_path / "primary", extra_names=("UVX", "Uvx"))
    wt, _ = _mkvenv(tmp_path / "wt")
    prod_env = {"PATH": f"{primary_venv}/bin:/usr/bin:/bin",
                "VIRTUAL_ENV": primary_venv}

    for cmd in ("uvx ruff check --active", "UVX ruff check --active",
                "Uvx ruff check --active"):
        d = guard.evaluate(
            "Bash", {"command": cmd}, forbidden_paths=[],
            never_push_to=["main"], cwd=str(wt), env=prod_env)
        assert d.allow, (
            f"a trailing --active belongs to the invoked program, not to "
            f"uvx, regardless of uvx's own case: {cmd!r} -- {d.reason}")

    for cmd in ("uvx --active ruff", "UVX --active ruff", "Uvx --active ruff"):
        d = guard.evaluate(
            "Bash", {"command": cmd}, forbidden_paths=[],
            never_push_to=["main"], cwd=str(wt), env=prod_env)
        assert not d.allow, f"a leading --active is uvx's own flag: {cmd!r}"
