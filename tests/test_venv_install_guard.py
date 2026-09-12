"""Structural (resolved-executable) venv-install guard.

Task 16a798c1 ("refuse installs into the shared dev venv from a coder
session") failed three review rounds; all three implementations used lexical
enforcement (a raw-text separator split and/or argv[0]-positional matching)
and were each defeated by shell segmentation. This module's tests are
verdict-derived: each ``test_verdictN_*`` case below is quoted from the
corresponding attempt's review checklist (read from
``attempts.review_checklist`` in ``~/.no_human/no_human.db`` for task
16a798c1) and is paired with the *direct spelling* the defeated lexical
guard of that attempt DID catch — proving the new structural guard is not
trivially deny-everything, only deny-the-things-that-actually-write-outside-
the-worktree.
"""

import ast
import contextlib
import inspect
import logging
import os
import stat
import tempfile

import pytest

from no_human.agent import guard, venv_install_guard

FORBIDDEN = []
PROTECTED = ["main", "master", "release/*"]

#: root/non-POSIX ignores mode bits entirely, so a `chmod` that is supposed
#: to make a path unreadable is a no-op there — the probe under test is a
#: permission probe, and these tests would be vacuously green (or hang) on
#: a runner where `chmod` cannot actually remove access.
_CHMOD_MEANINGFUL = os.name == "posix" and getattr(os, "geteuid", lambda: 1)() != 0
requires_chmod = pytest.mark.skipif(
    not _CHMOD_MEANINGFUL,
    reason="root/non-POSIX ignores mode bits; the probe under test is a permission probe",
)


@contextlib.contextmanager
def _unreadable(path):
    """Strip the execute bit from `path` (a directory) for the duration of
    the `with` block — `os.chmod(0o600)` on a directory removes traversal,
    so anything INSIDE it cannot be stat'd, while `path` itself still stats
    fine from its parent. Always restores the original mode: an unrestored
    mode makes `tmp_path`'s own teardown fail."""
    mode = os.stat(path).st_mode
    os.chmod(path, 0o600)
    try:
        yield
    finally:
        os.chmod(path, mode)


def _mkvenv(root):
    """A real, executable-bit venv layout: <root>/pyproject.toml +
    <root>/.venv/{pyvenv.cfg,bin/{python,pip,uv}}."""
    venv = os.path.join(root, ".venv")
    bindir = os.path.join(venv, "bin")
    os.makedirs(bindir, exist_ok=True)
    with open(os.path.join(venv, "pyvenv.cfg"), "w") as f:
        f.write("home = /usr/bin\n")
    for name in ("python", "python3", "pip", "pip3", "uv"):
        path = os.path.join(bindir, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    with open(os.path.join(root, "pyproject.toml"), "w") as f:
        f.write("[project]\nname = \"x\"\n")
    # realpath'd — macOS resolves tmp_path under /var to /private/var, and
    # every containment assertion below is meaningless unless both roots and
    # every derived path go through the same realpath call (memory: the
    # worktree-pytest trap is the same class of bug — comparing across a
    # symlink boundary silently passes when it should fail).
    return os.path.realpath(root), os.path.realpath(venv)


def _session(tmp_path):
    """Real two-tree layout: `primary/` (the shared dev checkout a coder
    session must never write into) and `wt/` (the session's own worktree)."""
    primary, primary_venv = _mkvenv(tmp_path / "primary")
    wt, wt_venv = _mkvenv(tmp_path / "wt")
    # Exactly what a coder's Bash inherits in production today: PATH/
    # VIRTUAL_ENV pointing at the shared dev venv, regardless of which
    # worktree `cwd` names (PLAN.md context: "Backends inherit os.environ,
    # whose VIRTUAL_ENV/sys.prefix IS the shared dev venv").
    prod_env = {
        "PATH": f"{primary_venv}/bin:/usr/bin:/bin",
        "VIRTUAL_ENV": primary_venv,
    }
    # A session whose own environment correctly points at ITS OWN worktree
    # venv — the shape the control test proves stays allowed.
    wt_env = {
        "PATH": f"{wt_venv}/bin:/usr/bin:/bin",
        "VIRTUAL_ENV": wt_venv,
    }
    return primary, primary_venv, wt, wt_venv, prod_env, wt_env


def _ev(tool, inp, *, cwd, env):
    return guard.evaluate(tool, inp, forbidden_paths=FORBIDDEN,
                           never_push_to=PROTECTED, cwd=cwd, env=env)


# ---------------------------------------------------------------------------
# Verdict 1 — wrapper / nested-shell laundering (attempt 1's checklist).
# Under a lexical guard these ALL passed because the check asked "what is
# argv[0]/argv[1]" (bash/sh/xargs/uv/env/sudo/timeout — never the installer)
# instead of resolving what actually executes and where it writes.
# ---------------------------------------------------------------------------

def test_verdict1_wrapper_and_nested_shell_installs_are_denied(tmp_path):
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cases = [
        f"bash -lc '{primary_venv}/bin/pip install foo'",
        f'sh -c "VIRTUAL_ENV={primary_venv} pip install -e ."',
        f"xargs {primary_venv}/bin/pip install",
        f"uv run pip install --python {primary_venv}/bin/python foo",
        "env -i pip install foo",
        "sudo -H pip install foo",
        f"timeout 300 {primary_venv}/bin/pip install foo",
    ]
    for cmd in cases:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is not None, f"must be denied (verdict 1): {cmd}"
        assert primary_venv in r, f"reason must name {primary_venv}: {r}"
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=prod_env)
        assert not d.allow, f"must be blocked via evaluate(): {cmd}"


def test_verdict1_direct_spelling_twin_is_also_denied(tmp_path):
    """The unwrapped spelling a lexical guard DID catch — proves the
    structural guard is not trivially always-deny; verdict-1 bypasses are, by
    the verdict's own evidence, ALLOW under a lexical scanner and DENY here,
    while this twin is DENY under both."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cmd = f"{primary_venv}/bin/pip install foo"
    r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
    assert r is not None
    assert primary_venv in r


# ---------------------------------------------------------------------------
# Verdict 2 — separator inside the quoted payload (attempt 2's checklist). A
# raw-text `_CMD_SEP`-style split ran BEFORE quote-aware tokenising, so a
# separator hidden inside a shell's quoted script argument was invisible to
# the split, and `--project` was not on the examined flag list.
# ---------------------------------------------------------------------------

def test_verdict2_separator_inside_quoted_payload_is_denied(tmp_path):
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cases = [
        f'sh -c "{primary_venv}/bin/pip install foo && echo ok"',
        f'bash -lc "cd {primary} && uv sync"',
        f'bash -lc "source {primary_venv}/bin/activate && pip install foo"',
        f"uv sync --project {primary}",
    ]
    for cmd in cases:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is not None, f"must be denied (verdict 2): {cmd}"
        assert primary in r, f"reason must name {primary}: {r}"
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=prod_env)
        assert not d.allow, f"must be blocked via evaluate(): {cmd}"


def test_verdict2_direct_spelling_twin_is_also_denied(tmp_path):
    """Also stands in for verdict 3's own "direct spelling twin": both
    verdicts' un-laundered form reduces to the exact same sequential
    command (`cd {primary} && uv sync`), so a second, byte-identical test
    under the verdict-3 heading tested nothing this one does not already
    cover — consolidated here rather than kept as a duplicate (test-count
    invariant; see this task's PR for the accounting)."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cmd = f"cd {primary} && uv sync"
    r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
    assert r is not None
    assert primary in r


# ---------------------------------------------------------------------------
# Verdict 3 — punctuation-run / group tokens (attempt 3's checklist). A
# tokeniser split on punctuation but a positional "argv[0]" check missed the
# real command hiding inside a blank-line, background (`&`), subshell `(...)`
# or brace-group `{...; }` construct.
# ---------------------------------------------------------------------------

def test_verdict3_punctuation_runs_and_groups_are_denied(tmp_path):
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cases = [
        f"echo start\n\n{primary_venv}/bin/pip install -e {primary}",
        f"cd {primary}\n\nuv sync",
        f"cd {primary};\nuv sync",
        f"true &\n{primary_venv}/bin/pip install foo",
        f"(cd {primary} && uv sync)",
        f"{{ cd {primary} && uv sync; }}",
        f"pushd {primary} && uv sync",
    ]
    for cmd in cases:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is not None, f"must be denied (verdict 3): {cmd}"
        assert primary in r, f"reason must name {primary}: {r}"
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=prod_env)
        assert not d.allow, f"must be blocked via evaluate(): {cmd}"


# (verdict 3's own "direct spelling twin" is covered by
# test_verdict2_direct_spelling_twin_is_also_denied above — see its
# docstring; both verdicts' unwrapped form is the same command.)


# ---------------------------------------------------------------------------
# Control — worktree-venv installs stay allowed.
# ---------------------------------------------------------------------------

def test_control_worktree_venv_installs_are_allowed(tmp_path):
    """A session whose own PATH/VIRTUAL_ENV correctly point at its OWN
    worktree venv, and whose install commands never explicitly point outside
    it, must never be blocked by this guard."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cases = [
        f"{wt_venv}/bin/pip install foo",
        f"uv pip install --prefix {wt_venv} foo",
        f"uv pip install --target {wt_venv}/lib/python3.12/site-packages foo",
        f"VIRTUAL_ENV={wt_venv} uv pip install foo",
        "uv sync",
        f'bash -lc "cd {wt} && uv sync"',
        "uv run pytest -q",
    ]
    for cmd in cases:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=wt_env)
        assert r is None, f"must stay allowed: {cmd} — {r}"
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=wt_env)
        assert d.allow, f"must stay allowed via evaluate(): {cmd} — {d.reason}"


def test_control_production_env_uv_commands_stay_allowed(tmp_path):
    """Review finding (venv-install-guard-v2, attempt 2): production callers
    (`claude_backend.py`, `codex_backend.py`) call `guard.evaluate()` with NO
    `env` argument, so `denial_reason` defaults to `env=os.environ` — and
    `os.environ["VIRTUAL_ENV"]` in a real coder session IS the shared dev
    venv (`prod_env` here), regardless of which worktree `cwd` names. A
    guard that treats that *inherited* VIRTUAL_ENV as an install-target
    candidate denies the coder's own routine commands
    (`uv sync`/`uv run pytest -q`) in EVERY session, not just a laundering
    one — this is the exact "wrong env source" bug the review cited. Unlike
    `test_control_worktree_venv_installs_are_allowed` (which uses `wt_env`,
    an env that already correctly points at the worktree), this test uses
    `prod_env` with `cwd=wt` — the actual production shape — so it would
    have failed against the code the reviewer rejected and must pass now.
    """
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cases = ["uv sync", "uv run pytest -q"]
    for cmd in cases:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is None, f"must stay allowed under production env: {cmd} — {r}"
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=prod_env)
        assert d.allow, f"must stay allowed via evaluate(): {cmd} — {d.reason}"


# ---------------------------------------------------------------------------
# `~/.cache/uv` false positive (the defect this task fixes). `--python`/`-p`
# names an interpreter FILE, and a worktree's own `.venv/bin/python3` is
# routinely a SYMLINK to a base interpreter uv manages in a cache directory
# (`~/.cache/uv/...`) — never itself an install destination. On the codex
# backend a guard denial is post-hoc and TERMINATES the attempt, so a false
# "outside the worktree" verdict here was fatal, not merely a blocked call
# (fixed separately in codex_backend.py's severity routing; these two tests
# cover the guard's own resolution logic in isolation). Simulated with a
# real symlink so no assumption about an actual `~/.cache/uv` layout on the
# test machine is required.
# ---------------------------------------------------------------------------

def _mk_cache_interpreter(tmp_path):
    """A real interpreter file OUTSIDE both `primary`/`wt` — stand-in for
    uv's managed-Python cache, which every uv invocation touches but which
    is never itself an install destination."""
    cache_dir = tmp_path / "cache-uv" / "python3.12"
    cache_dir.mkdir(parents=True)
    interpreter = cache_dir / "python3"
    interpreter.write_text("#!/bin/sh\nexit 0\n")
    st = os.stat(interpreter)
    os.chmod(interpreter, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return os.path.realpath(interpreter)


def test_python_flag_naming_worktree_venv_via_cache_symlink_is_allowed(tmp_path):
    """Acceptance criterion (a). The worktree's own `.venv/bin/python3` is a
    symlink into the (simulated) uv cache — an install genuinely targeting
    the worktree's own venv must be ALLOWED despite that cache access. RED
    against the pre-fix code: resolving `_venv_root_of` against the fully
    symlink-followed path finds no `pyvenv.cfg` above the cache file and
    mis-adds the cache path itself as an out-of-tree candidate."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cached = _mk_cache_interpreter(tmp_path)
    linked = os.path.join(wt_venv, "bin", "python3")
    os.remove(linked)
    os.symlink(cached, linked)
    cmd = f"uv pip install --python {linked} foo"
    r = venv_install_guard.denial_reason(cmd, cwd=wt, env=wt_env)
    assert r is None, f"must stay allowed despite the cache symlink: {cmd} — {r}"
    d = _ev("Bash", {"command": cmd}, cwd=wt, env=wt_env)
    assert d.allow, f"must be allowed via evaluate(): {cmd} — {d.reason}"


def test_python_flag_naming_a_genuinely_external_interpreter_stays_denied(tmp_path):
    """Acceptance criterion (b), negative control for (a). A `--python`
    value naming no venv at all — the same simulated cache file, named
    DIRECTLY rather than through the worktree's own symlink — must still be
    denied. Proves the fix does not become allow-everything: only a value
    that structurally NAMES the worktree's own venv is spared, never an
    arbitrary out-of-tree path."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cached = _mk_cache_interpreter(tmp_path)
    cmd = f"uv pip install --python {cached} foo"
    r = venv_install_guard.denial_reason(cmd, cwd=wt, env=wt_env)
    assert r is not None, f"must still be denied: {cmd}"
    assert cached in r, f"reason must name {cached}: {r}"
    d = _ev("Bash", {"command": cmd}, cwd=wt, env=wt_env)
    assert not d.allow, f"must be blocked via evaluate(): {cmd}"


# ---------------------------------------------------------------------------
# Regression — bare read-only pip subcommands must not be treated as an
# install (structural-guard review round, this ticket). Putting the bare
# installer token "pip" in the old `_MUTATING_VERBS` (added to catch `uv pip
# install`) also matched every *read-only* `pip` invocation, because the old
# intent test was just "is any token a mutating verb" and for a bare `pip
# freeze`/`pip list`/`pip show`/`pip --version`/`pip check` command the
# token "pip" IS the resolved installer's own name. Verified live under the
# production env shape (env=os.environ-shaped, PATH/VIRTUAL_ENV naming the
# shared dev venv — the module's own documented reality) before the fix:
# every case below was DENIED. `uv pip install`/`uv pip uninstall` (real
# mutations) must stay denied without relying on a bare "pip" token, since
# "install"/"uninstall" are themselves in `_MUTATING_SUBCOMMANDS` and are the
# structural subcommand of the resolved `pip` token in those commands.
# ---------------------------------------------------------------------------

def test_readonly_pip_subcommands_are_not_treated_as_mutating(tmp_path):
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cases = [
        "pip freeze",
        "pip list",
        "pip show requests",
        "pip --version",
        "pip check",
        "pip config list",
    ]
    for cmd in cases:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is None, f"read-only pip command must not be denied: {cmd} — {r}"
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=prod_env)
        assert d.allow, f"read-only pip command must be allowed via evaluate(): {cmd} — {d.reason}"


def test_uv_pip_install_still_denied_without_bare_pip_verb(tmp_path):
    """`uv pip install`/`uv pip uninstall` (real mutations, verdict-1 style
    laundering via `uv pip ...`) must stay denied even though bare "pip" was
    removed from the mutating set — "install"/"uninstall" alone are enough.
    `uv pip list`/`uv pip show` (also read-only, prefixed with `uv`) must stay
    allowed, same as the bare-pip case above."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    for cmd in (f"uv pip install --target {primary_venv} foo",
                f"uv pip uninstall --target {primary_venv} foo"):
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is not None, f"must still be denied: {cmd}"
        assert primary_venv in r


# ---------------------------------------------------------------------------
# Over-block regression (venv guard v2 FINISH, salvage of 7a2160d3). The
# remaining defect after the `-6` salvage: intent was decided by BARE-TOKEN
# membership (`any(tok in _MUTATING_VERBS for tok in tokens)`), so any
# command that merely MENTIONS a mutating word anywhere — as another
# subcommand's own argument, not as the resolved installer's own adjacent
# subcommand — was denied. These tests are RED against the `-6` base content
# (each contains a token equal to a `_MUTATING_VERBS` member) and GREEN once
# intent is decided structurally: resolved-executable + its own adjacent
# subcommand, never bare-token membership anywhere in the stream.
# ---------------------------------------------------------------------------

def test_mutating_word_as_argument_is_not_install_intent(tmp_path):
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cases = [
        "pip show sync",
        "pip show install",
        "pip list -v",
        "uv run pytest -q -k add",
        "uv run pytest -q",
    ]
    for cmd in cases:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is None, f"a mutating word as an ARGUMENT must not be install intent: {cmd} — {r}"
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=prod_env)
        assert d.allow, f"must be allowed via evaluate(): {cmd} — {d.reason}"


def test_subcommand_is_found_past_flags_and_inner_installer_name(tmp_path):
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    denied = [
        "uv pip install foo",
        "python -m pip install foo",
        f"uv --directory {primary} sync",
        "pip --no-cache-dir install foo",
    ]
    for cmd in denied:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is not None, f"must still be denied: {cmd}"
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=prod_env)
        assert not d.allow, f"must be blocked via evaluate(): {cmd}"

    allowed = [
        "uv pip list",
        "uv pip show foo",
        "python -m pip list",
    ]
    for cmd in allowed:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is None, f"must stay allowed: {cmd} — {r}"
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=prod_env)
        assert d.allow, f"must be allowed via evaluate(): {cmd} — {d.reason}"


def test_intent_requires_a_resolvable_installer(tmp_path):
    """The verb word alone is never intent — it only means anything once it
    is the adjacent subcommand of a RESOLVED installer token."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cases = ["echo install", "git add .", "make sync"]
    for cmd in cases:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is None, f"must stay allowed: {cmd} — {r}"
        d = _ev("Bash", {"command": cmd}, cwd=wt, env=prod_env)
        assert d.allow, f"must be allowed via evaluate(): {cmd} — {d.reason}"


def test_unresolvable_installer_is_allowed_and_logged(tmp_path, caplog):
    """Structure genuinely cannot decide when a token names an installer but
    does not resolve (not on PATH). Criterion: no new lexical pattern closes
    this gap — the fallback is allow-and-log, not a silent, unobserved
    pass."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    # PATH must be a dir where `pip` provably does NOT resolve. "/usr/bin:/bin"
    # only encoded that on macOS — Ubuntu CI runners ship /usr/bin/pip, the
    # token resolved, and the allow-and-log branch never ran (first public CI
    # run, 2026-08-17). An empty tmp dir makes the premise true everywhere.
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    no_pip_env = {"PATH": str(empty_bin), "VIRTUAL_ENV": primary_venv}
    import logging
    with caplog.at_level(logging.WARNING, logger="no_human.agent.venv_install_guard"):
        r = venv_install_guard.denial_reason("pip install foo", cwd=wt, env=no_pip_env)
    assert r is None, f"unresolvable installer must be allowed, not denied: {r}"
    assert any("pip" in rec.message for rec in caplog.records), (
        "the allow must be logged at WARNING and name the token"
    )
    for cmd in ("uv pip list", "uv pip show foo"):
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is None, f"read-only uv pip command must not be denied: {cmd} — {r}"


# ---------------------------------------------------------------------------
# Fail-closed / residual risk (memory: gates must fail CLOSED).
# ---------------------------------------------------------------------------

def test_expansion_and_unknown_installer_fail_closed(tmp_path):
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cases = [
        "pip install $PKG",
        'pip install "$(cat pkgs.txt)"',
        "xargs pip install < list",
    ]
    for cmd in cases:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is not None, f"must fail closed: {cmd}"


def test_no_cwd_is_conservatively_denied(tmp_path):
    # A real, resolvable installer on PATH — the point of this test is the
    # missing `cwd`, not an installer that fails to resolve for an unrelated
    # reason (that path is covered by test_expansion_and_unknown_installer_
    # fail_closed instead).
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    r = venv_install_guard.denial_reason("pip install foo", cwd=None, env=prod_env)
    assert r is not None
    assert "cwd" in r or "worktree" in r


def test_non_install_commands_are_untouched(tmp_path):
    """Over-blocking regression guard: commands that never invoke an
    installer, or invoke one but only pass its text through as an argument
    (never execute it), must stay allowed."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cases = [
        "uv --version",
        "git status",
        'echo "pip install foo"',
        'python -c "print(1)"',
    ]
    for cmd in cases:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is None, f"must stay allowed: {cmd} — {r}"


# ---------------------------------------------------------------------------
# `<interpreter> -m uv …` — agreement check. Once the leading `python` token
# resolves as an installer (it's in `_EXACT_INSTALLERS` / `_VERSIONED_
# PREFIXES` here in v2), `_mutating_subcommand` already walks past `-m`
# (a flag) and past `uv`/`pip` (both installer NAMES it skips) to land on
# the real subcommand (`install`/`add`/`sync`) — the same walk that already
# handles `uv pip install foo`. No new lexical rule needed here; these tests
# pin that the existing walk in fact reaches the right verdict for the `-m`
# spelling, on a resolvable PATH. (None of the cases below use `pypy`: v2's
# `_EXACT_INSTALLERS`/`_VERSIONED_PREFIXES` in `venv_install_guard.py` do
# NOT list it — `pypy`/`pypy3*` are recognised only by v1's `_PY_EXE_RE` in
# `guard.py`, a separate lexical layer covered by `test_guard.py` instead.)
# ---------------------------------------------------------------------------

def test_interpreter_dash_m_uv_install_resolves_and_is_denied(tmp_path):
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cases = [
        "python -m uv pip install evilpkg",
        "python3 -m uv pip install evilpkg",
        "python -m uv add evilpkg",
        "python -m uv sync",
        f"{primary_venv}/bin/python -m uv pip install evilpkg",
    ]
    for cmd in cases:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is not None, f"must be denied: {cmd}"
        assert primary_venv in r, f"reason must name {primary_venv}: {r}"
    d = _ev("Bash", {"command": "python -m uv pip install evilpkg"}, cwd=wt, env=prod_env)
    assert not d.allow, "must be blocked via evaluate() too"


def test_dash_m_uv_non_install_subcommands_are_not_install_intent(tmp_path):
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cases = [
        "python -m uv --version",
        "python -m pip list",
        "uv run pytest -q",
        "python -m pytest -q",
    ]
    for cmd in cases:
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env)
        assert r is None, f"must stay allowed: {cmd} — {r}"


# ---------------------------------------------------------------------------
# Wiring.
# ---------------------------------------------------------------------------

def test_evaluate_denies_through_the_bash_branch(tmp_path):
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    d = _ev("Bash", {"command": f"bash -lc '{primary_venv}/bin/pip install foo'"},
             cwd=wt, env=prod_env)
    assert not d.allow
    assert primary_venv in d.reason


def test_readonly_and_coder_sessions_are_both_covered(tmp_path):
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cmd = f"bash -lc '{primary_venv}/bin/pip install foo'"
    for readonly in (False, True):
        d = guard.evaluate("Bash", {"command": cmd}, forbidden_paths=FORBIDDEN,
                            never_push_to=PROTECTED, readonly=readonly,
                            cwd=wt, env=prod_env)
        assert not d.allow, f"must be blocked, readonly={readonly}"


def test_evaluate_env_defaults_to_os_environ_when_omitted(tmp_path, monkeypatch):
    """`env` is optional on `evaluate()` — omitting it must not crash, and
    must fall back to the real process environment (what both backends do
    today, unchanged)."""
    tmp = tempfile.mkdtemp(prefix="guard-no-venv-session-")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    d = guard.evaluate("Bash", {"command": "git status"}, forbidden_paths=FORBIDDEN,
                        never_push_to=PROTECTED, cwd=tmp)
    assert d.allow


# ---------------------------------------------------------------------------
# Bugfix: chmod on a venv turns the install guard's DENY into ALLOW.
# ---------------------------------------------------------------------------

@requires_chmod
def test_an_unreadable_venv_pyvenv_cfg_still_denies_the_install(tmp_path):
    """The bug: `chmod` on a venv directory (stripping its execute bit)
    makes `pyvenv.cfg` unstat'able. `os.path.isfile`/`Path.is_file()`
    swallow the resulting `PermissionError` and report `False` —
    indistinguishable from "no venv here" — so `_venv_root_of` used to
    conclude "owns no venv" and the install stopped being denied.

    Four spellings, all denied before/during/after the chmod: two that
    resolve their installer via the explicit-path branch of
    `_resolve_installer` (`_venv_root_of` reached directly), and two —
    the bare token `pip install evilpkg` and its `bash -lc` wrapping, the
    spelling a coder actually types — that resolve via the bare-token PATH
    walk instead. The bare-token pair used to survive the earlier fix in
    this ticket: `_resolve_installer`'s bare-token branch resolved through
    `shutil.which`, which calls `os.path.exists` internally and swallows
    the very same `PermissionError`, so it reported "not on PATH" and fell
    through to allow-and-log BEFORE `_venv_root_of` ever ran — that branch
    now walks `PATH` by hand with `_probe_is_file` instead. The "during"
    assertion on every case is the regression this test pins."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cases = [
        (f"{primary_venv}/bin/pip install evilpkg", prod_env),
        (f"uv pip install --python {primary_venv}/bin/python evilpkg", wt_env),
        ("pip install evilpkg", prod_env),
        ("bash -lc 'pip install evilpkg'", prod_env),
    ]
    for cmd, env in cases:
        before = venv_install_guard.denial_reason(cmd, cwd=wt, env=env)
        assert before is not None, f"positive control must deny before chmod: {cmd}"
        assert primary_venv in before
        with _unreadable(primary_venv):
            inside = venv_install_guard.denial_reason(cmd, cwd=wt, env=env)
            assert inside is not None, (
                f"REGRESSION: an unreadable venv must still be denied: {cmd}"
            )
            d = _ev("Bash", {"command": cmd}, cwd=wt, env=env)
            assert not d.allow, f"must still be blocked via evaluate(): {cmd}"
        after = venv_install_guard.denial_reason(cmd, cwd=wt, env=env)
        assert after is not None, f"must stay denied once permissions are restored: {cmd}"


@requires_chmod
def test_probe_distinguishes_absence_from_unreadability(tmp_path):
    """`_probe_is_file`/`_probe_is_dir` are the tri-state replacement for
    `os.path.isfile`/`os.path.isdir`: `True`/`False` agree with the stdlib
    helpers, but a permission error resolves to `None` ("undetermined")
    instead of being silently folded into `False` ("definitively absent")."""
    d = tmp_path / "venv"
    d.mkdir()
    cfg = d / "pyvenv.cfg"
    cfg.write_text("home = /usr/bin\n")
    assert venv_install_guard._probe_is_file(str(cfg)) is True
    assert venv_install_guard._probe_is_dir(str(d)) is True

    missing_cfg = tmp_path / "nope" / "pyvenv.cfg"
    assert venv_install_guard._probe_is_file(str(missing_cfg)) is False
    missing_dir = tmp_path / "also-nope"
    assert venv_install_guard._probe_is_dir(str(missing_dir)) is False

    with _unreadable(d):
        assert venv_install_guard._probe_is_file(str(cfg)) is None

    assert venv_install_guard._probe_is_file(str(cfg)) is True


def test_a_readable_directory_without_a_pyvenv_cfg_is_not_a_venv(tmp_path):
    """A genuinely absent `pyvenv.cfg` (readable dir, no such file) must
    still resolve to "not a venv" — the tri-state fix must not turn every
    plain `bin/` directory into a phantom protected venv. Its twin, one
    directory over, WITH a `pyvenv.cfg`, resolves to that root — both
    directions of `_venv_root_of` asserted separately."""
    plain_bin = tmp_path / "plain" / "bin"
    plain_bin.mkdir(parents=True)
    pip = plain_bin / "pip"
    pip.write_text("#!/bin/sh\nexit 0\n")
    st = os.stat(pip)
    os.chmod(pip, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    assert venv_install_guard._venv_root_of(str(pip)) is None

    venv_root = tmp_path / "venvy"
    venv_bin = venv_root / "bin"
    venv_bin.mkdir(parents=True)
    (venv_root / "pyvenv.cfg").write_text("home = /usr/bin\n")
    pip2 = venv_bin / "pip"
    pip2.write_text("#!/bin/sh\nexit 0\n")
    st2 = os.stat(pip2)
    os.chmod(pip2, st2.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    assert venv_install_guard._venv_root_of(str(pip2)) == os.path.realpath(str(venv_root))


_SWALLOWING_ATTRS = {
    "isfile", "isdir", "exists", "islink", "is_file", "is_dir",
    # `shutil.which` resolves via `os.path.exists` internally and swallows
    # `OSError` exactly like the five names above — the review round that
    # caught this ticket's first draft flagged it by name (it decided
    # through this exact swallow, on the bare-token spelling a coder
    # actually types) as the one this closed set was missing.
    "which",
}


def _oserror_swallowing_call_sites(source):
    """Real call sites only (an `ast.Call` whose method/function name is one
    of the OSError-swallowing stdlib probes) — deliberately NOT a text/regex
    search, which would also flag this module's own docstrings and comments
    that talk ABOUT `os.path.isfile` while documenting why it was removed."""
    tree = ast.parse(source)
    return [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _SWALLOWING_ATTRS
    ]


def test_no_changed_probe_decides_through_an_oserror_swallowing_helper():
    """None of the probe sites this patch touches may reach a decision
    through a stdlib helper that swallows `OSError` (`os.path.isfile`/
    `isdir`/`exists`/`islink`, `Path.is_file`/`is_dir`, `shutil.which`) —
    that swallow is the root cause this patch removes. A positive control
    against guard.py's untouched `_looks_like_pathspec` (which still calls
    `os.path.exists`, unchanged and out of scope for this ticket) proves an
    empty result above is a real absence, not a search that can never
    match anything."""
    module_src = inspect.getsource(venv_install_guard)
    assert _oserror_swallowing_call_sites(module_src) == [], (
        "venv_install_guard.py must not decide through an OSError-swallowing probe call"
    )

    protected_src = inspect.getsource(guard._protected_venvs)
    resolve_src = inspect.getsource(guard._resolve_or_self)
    assert _oserror_swallowing_call_sites(protected_src) == []
    assert _oserror_swallowing_call_sites(resolve_src) == []

    positive_src = inspect.getsource(guard._looks_like_pathspec)
    assert _oserror_swallowing_call_sites(positive_src) == ["exists"], (
        "positive control: a known, untouched os.path.exists() call must still be found"
    )


def test_installs_into_the_sessions_own_worktree_venv_stay_allowed(tmp_path):
    """Containment, not readability, decides: a coder session installing
    into ITS OWN venv must stay allowed whether that venv lives at the
    worktree root OR nested in a monorepo subdirectory (its own separate
    `cwd`/venv pair), with both bare and `uv`-prefixed spellings. This is
    the fix's required negative space — no existing ALLOW may flip to
    DENY."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    sub, sub_venv = _mkvenv(os.path.join(wt, "packages", "app"))
    sub_env = {
        "PATH": f"{sub_venv}/bin:/usr/bin:/bin",
        "VIRTUAL_ENV": sub_venv,
    }

    cases = [
        (f"{wt_venv}/bin/pip install foo", wt, wt_env),
        ("pip install foo", wt, wt_env),
        ("uv pip install foo", wt, wt_env),
        (f"uv pip install --python {wt_venv}/bin/python foo", wt, wt_env),
        (f"{sub_venv}/bin/pip install foo", sub, sub_env),
        ("pip install foo", sub, sub_env),
    ]
    for cmd, cwd, env in cases:
        r = venv_install_guard.denial_reason(cmd, cwd=cwd, env=env)
        assert r is None, f"must stay allowed: {cmd} (cwd={cwd}) — {r}"
        d = _ev("Bash", {"command": cmd}, cwd=cwd, env=env)
        assert d.allow, f"must be allowed via evaluate(): {cmd} (cwd={cwd}) — {d.reason}"


@requires_chmod
def test_an_unreadable_own_worktree_venv_still_stays_allowed(tmp_path):
    """`_is_within` compares realpath'd strings, not filesystem readability
    — so even when a session's OWN venv directory has been made unreadable
    (e.g. by another tool in the pipeline), an explicit-path install into it
    must still be recognised as "inside cwd" and allowed, not denied by the
    fail-closed probe added for the primary-checkout case."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    with _unreadable(wt_venv):
        r = venv_install_guard.denial_reason(
            f"{wt_venv}/bin/pip install foo", cwd=wt, env=wt_env
        )
        assert r is None, f"a coder's own (unreadable) venv must still be allowed: {r}"


def test_a_nonexistent_installer_path_is_still_allowed_and_logged(tmp_path, caplog):
    """A genuinely absent installer path (no such file, not merely
    unreadable) must resolve to allow-and-log, the same as an unresolvable
    bare token — proving `_probe_is_file`'s `False` branch (definitively
    absent) is still reachable and distinct from the `None` branch this
    patch adds."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    cmd = "/no/such/dir/pip install foo"
    with caplog.at_level(logging.WARNING, logger="no_human.agent.venv_install_guard"):
        r = venv_install_guard.denial_reason(cmd, cwd=wt, env=wt_env)
    assert r is None, f"a genuinely absent installer path must be allowed, not denied: {r}"
    assert any("pip" in rec.message for rec in caplog.records)


# --------------------------------------------------------------------------- #
# `--active`: the coder-session half of #128.
#
# `uv run` SYNCS the project into its target environment before running
# anything, so the command need not be install-shaped to rewrite a venv, and
# `--active` aims that sync at the inherited VIRTUAL_ENV. Measured in a
# throwaway repo: `uv run --active python -c "print('ran')"` from inside a
# linked worktree reported "Uninstalled 1 package / Installed 1 package" and
# moved the SHARED venv's .pth from <primary>/src to <worktree>/src. The same
# command without `--active` left the shared venv untouched and built the
# worktree's own .venv instead.
#
# These use their own platform-aware layout rather than `_session` above,
# whose `bin`/`:` shape is POSIX-only.
# --------------------------------------------------------------------------- #

def _os_venv(root):
    """A venv laid out the way THIS platform lays one out."""
    venv = os.path.join(str(root), ".venv")
    bindir = os.path.join(venv, "Scripts" if os.name == "nt" else "bin")
    os.makedirs(bindir, exist_ok=True)
    with open(os.path.join(venv, "pyvenv.cfg"), "w") as f:
        f.write("home = /usr/bin\n")
    for name in ("python", "pip", "uv"):
        path = os.path.join(bindir, name + (".exe" if os.name == "nt" else ""))
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    with open(os.path.join(str(root), "pyproject.toml"), "w") as f:
        f.write("[project]\nname = \"x\"\n")
    return os.path.realpath(str(root)), os.path.realpath(venv), bindir


def _active_session(tmp_path):
    primary, primary_venv, primary_bin = _os_venv(tmp_path / "primary")
    wt, wt_venv, wt_bin = _os_venv(tmp_path / "wt")
    prod_env = {
        "PATH": primary_bin + os.pathsep + os.environ.get("PATH", ""),
        "VIRTUAL_ENV": primary_venv,
    }
    return primary_venv, wt, wt_venv, wt_bin, prod_env


def test_uv_run_active_into_the_shared_venv_is_denied(tmp_path):
    """The incident itself, and note there is no install word in it."""
    _pv, wt, _wv, _wb, prod_env = _active_session(tmp_path)
    for cmd in (
        'uv run --active python -c "print(1)"',
        "uv run --active ruff check .",
        "uv run --active pytest -q",
        "uv sync --active",
        "uv pip install --active -e .",
    ):
        assert venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env), (
            "must be denied, it rewrites the shared venv: %s" % cmd
        )


def test_the_same_commands_without_active_stay_allowed(tmp_path):
    """The control that matters. This module's residual register records that
    trusting the INHERITED VIRTUAL_ENV in general denied `uv sync` and
    `uv run pytest -q` in every session, which is why it was not done. The
    inherited value becomes a signal ONLY when `--active` says to use it, so
    these must be untouched."""
    _pv, wt, _wv, _wb, prod_env = _active_session(tmp_path)
    for cmd in (
        'uv run python -c "print(1)"',
        "uv run pytest -q",
        "uv sync",
        "uv run ruff check .",
    ):
        assert venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env) is None, (
            "must stay allowed, it targets the worktree's own env: %s" % cmd
        )


def test_active_is_allowed_when_the_active_env_is_the_worktrees_own(tmp_path):
    """`--active` is not itself the offence: pointing at your OWN venv is the
    correct use, and the decision stays structural (where it writes) rather
    than lexical (which flag it spells)."""
    _pv, wt, wt_venv, wt_bin, _prod = _active_session(tmp_path)
    own_env = {
        "PATH": wt_bin + os.pathsep + os.environ.get("PATH", ""),
        "VIRTUAL_ENV": wt_venv,
    }
    assert venv_install_guard.denial_reason(
        "uv run --active pytest -q", cwd=wt, env=own_env) is None


def test_active_equals_form_is_read_too(tmp_path):
    """`--active=true` must not be a way around it."""
    _pv, wt, _wv, _wb, prod_env = _active_session(tmp_path)
    assert venv_install_guard.denial_reason(
        "uv run --active=true pytest -q", cwd=wt, env=prod_env)


def test_active_survives_the_shell_laundering_this_module_exists_for(tmp_path):
    """The whole point of the module is that a nested shell does not hide the
    command, so the new signal must survive the same laundering."""
    _pv, wt, _wv, _wb, prod_env = _active_session(tmp_path)
    assert venv_install_guard.denial_reason(
        "bash -lc 'uv run --active pytest -q'", cwd=wt, env=prod_env)


def test_active_in_another_segment_does_not_deny_an_innocent_installer(tmp_path):
    """Review of PR #195. The first version scanned the whole flat token
    stream, so a `--active` ANYWHERE made the inherited venv a candidate for
    an installer elsewhere in the line, and the denial named an install target
    that did not exist:

        echo --active && uv run pytest -q                 -> DENIED
        grep -- --active notes.txt && uv run pytest -q    -> DENIED

    The second is not contrived: a task working on this guard greps for the
    flag and then runs the suite on the same line. `_mutating_subcommand`
    already draws this line, walking from a specific resolved-installer
    position and stopping at a segment break, because a flag reaches a process
    only through that process's own argv."""
    _pv, wt, _wv, _wb, prod_env = _active_session(tmp_path)
    for cmd in (
        "echo --active && uv run pytest -q",
        "grep -- --active notes.txt && uv run pytest -q",
        "echo --active; uv sync",
        "printf --active | uv run pytest -q",
    ):
        assert venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env) is None, (
            "nothing here touches the shared venv: %s" % cmd
        )


def test_active_still_binds_to_its_own_installer_after_a_break(tmp_path):
    """The scoping must not become a way through it: a real `uv run --active`
    later in the same line is still that installer's own flag."""
    _pv, wt, _wv, _wb, prod_env = _active_session(tmp_path)
    for cmd in (
        "echo hello && uv run --active pytest -q",
        "cd . ; uv run --active ruff check .",
    ):
        assert venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env), (
            "the flag is inside uv's own segment: %s" % cmd
        )


def test_no_active_is_not_read_as_active(tmp_path):
    """`--no-active` is a real uv flag and means the opposite."""
    _pv, wt, _wv, _wb, prod_env = _active_session(tmp_path)
    assert venv_install_guard.denial_reason(
        "uv run --no-active pytest -q", cwd=wt, env=prod_env) is None


# --------------------------------------------------------------------------- #
# Review round 3 of PR #195 measured the COST of the first version against the
# fleet's own history: 128 distinct commands carrying `--active`, of which it
# denied 124, and 77 of those denials touched no shared venv at all.
#
#   --no-sync            61   uv does not sync, so nothing is written
#   --no-project         10   same
#   VIRTUAL_ENV= prefix   6   the command clears the variable for its own child
#   plain --active       47   the real thing, correctly denied
#
# The negation was measured against uv 0.12.5 rather than reasoned about: from
# inside a worktree whose VIRTUAL_ENV named the shared checkout, plain
# `--active` moved the shared venv's editable `.pth` to the worktree, while
# `--active --no-sync` and `--active --no-project` left it where it was.
# --------------------------------------------------------------------------- #

def test_no_sync_and_no_project_negate_active(tmp_path):
    """71 of the 77 false denials. The sync is the only reason `--active` is
    intent, so a command that disables the sync writes nothing."""
    _pv, wt, _wv, _wb, prod_env = _active_session(tmp_path)
    for cmd in (
        "uv run --active --no-sync pytest -q",
        "uv run --no-sync --active pytest -q",
        "uv run --active --no-project pytest -q",
    ):
        assert venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env) is None, cmd


def test_a_negation_past_uvs_own_argv_does_not_count(tmp_path):
    """The trap in that narrowing, and the reason it is placement rather than
    membership: after `--` or after the program token, the flag is the
    PROGRAM's, while uv still syncs. Accepting a negation anywhere would
    reopen the incident instead of merely over-denying."""
    _pv, wt, _wv, _wb, prod_env = _active_session(tmp_path)
    for cmd in (
        "uv run --active -- echo --no-sync",
        "uv run --active python -m this --no-sync",
    ):
        assert venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env), (
            "uv still syncs here; the flag belongs to the program: %s" % cmd
        )


def test_an_assignment_prefixing_the_installer_clears_active(tmp_path):
    """6 of the 77. An assignment binds to the command it prefixes, so the
    command is naming the value for its own child."""
    _pv, wt, _wv, _wb, prod_env = _active_session(tmp_path)
    assert venv_install_guard.denial_reason(
        "VIRTUAL_ENV= uv run --active pytest -q", cwd=wt, env=prod_env) is None


def test_an_assignment_in_another_command_does_not_clear_it(tmp_path):
    """The laundering shape the scoping keeps denied: the assignment there
    belongs to `echo`, with a segment break between it and uv."""
    _pv, wt, _wv, _wb, prod_env = _active_session(tmp_path)
    for cmd in (
        "echo VIRTUAL_ENV= && uv run --active pytest -q",
        "echo VIRTUAL_ENV=\nuv run --active pytest -q",
    ):
        assert venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env), cmd


def test_a_flag_after_the_program_belongs_to_the_program(tmp_path):
    """`uv run nh learnings --active` is this repo's OWN CLI flag. Only some
    subcommands invoke a program: after `uv run` the next positional is that
    program, while add/sync/remove/lock/export invoke nothing, so a flag
    anywhere on those lines is uv's."""
    _pv, wt, _wv, _wb, prod_env = _active_session(tmp_path)
    for cmd in ("uv run nh learnings --active", "uv run pytest -q --active"):
        assert venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env) is None, cmd
    # ...and the non-invoking subcommands are unaffected by that rule.
    for cmd in ("uv add pkg --active", "uv sync --extra dev --active"):
        assert venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env), cmd


def test_the_full_bypass_set_stays_denied(tmp_path):
    """Every shape the maintainer ran against the first version. The cost
    narrowings must not move a single one of these."""
    _pv, wt, _wv, _wb, prod_env = _active_session(tmp_path)
    for cmd in (
        "uv run --active pytest -q",
        "uv add pkg --active",
        "uv sync --extra dev --active",
        "uvx --active ruff",
        "uv pip install --active -e .",
        "bash -lc 'uv run --active pytest -q'",
        "uv run --active=true pytest -q",
        "uv --directory . run --active pytest -q",
    ):
        assert venv_install_guard.denial_reason(cmd, cwd=wt, env=prod_env), cmd
