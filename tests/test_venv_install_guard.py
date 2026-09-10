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

import os
import stat
import tempfile

from no_human.agent import guard, venv_install_guard

FORBIDDEN = []
PROTECTED = ["main", "master", "release/*"]


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


def _git_worktree_session(tmp_path):
    """Real worktree shape: `primary/` is an actual checkout (`.git` a
    DIRECTORY) and `wt/` is a linked git worktree of it (`.git` a FILE
    carrying `gitdir: ...`, exactly what `git worktree add` produces on
    disk). `sub` (`<wt>/src/no_human`) is nested two levels inside `wt` and
    carries no marker of its own, so root discovery must ascend past both
    `sub` and `<wt>/src` to find `wt`'s own `.git` file. Every tree, venv
    (with a real `pyvenv.cfg`), and PATH/VIRTUAL_ENV pair is constructed
    here — nothing is inherited from the process actually running the
    test."""
    primary, primary_venv = _mkvenv(tmp_path / "primary")
    os.makedirs(os.path.join(primary, ".git"), exist_ok=True)
    wt, wt_venv = _mkvenv(tmp_path / "wt")
    gitdir = os.path.join(primary, ".git", "worktrees", "wt")
    os.makedirs(gitdir, exist_ok=True)
    with open(os.path.join(wt, ".git"), "w") as f:
        f.write(f"gitdir: {gitdir}\n")
    sub = os.path.join(wt, "src", "no_human")
    os.makedirs(sub, exist_ok=True)
    prod_env = {
        "PATH": f"{primary_venv}/bin:/usr/bin:/bin",
        "VIRTUAL_ENV": primary_venv,
    }
    wt_env = {
        "PATH": f"{wt_venv}/bin:/usr/bin:/bin",
        "VIRTUAL_ENV": wt_venv,
    }
    return primary, primary_venv, wt, wt_venv, sub, prod_env, wt_env


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
# Coder-in-a-subdirectory (this ticket). `denial_reason` compared candidates
# against `cwd` directly, so a coder session that `cd`'d into ANY
# subdirectory of its own worktree had its own PATH-resolved `.venv`
# (`<worktree>/.venv`, sitting outside the subdirectory `cwd`) read as
# "outside this session's worktree" and denied — for both `uv pip install`
# and bare `pip install`, since the earlier `e0541f35` regression (relaxing
# the bare inner `pip` signal) is NOT what causes this: this defect is in
# how the containment boundary itself is computed, not in which tokens are
# treated as installers. Fixed by comparing against the session's worktree
# ROOT (`_session_root`, nearest `.git`-marked ancestor of `cwd`) instead of
# `cwd` itself.
# ---------------------------------------------------------------------------

def test_own_venv_install_is_allowed_from_any_subdirectory(tmp_path):
    primary, primary_venv, wt, wt_venv, sub, prod_env, wt_env = _git_worktree_session(tmp_path)
    wt_src = os.path.join(wt, "src")
    cases = [
        "uv pip install -e .",
        "pip install -e .",
        "uv pip install foo",
        f"{wt_venv}/bin/pip install foo",
        "uv sync",
    ]
    for cwd in (wt, wt_src, sub):
        for cmd in cases:
            r = venv_install_guard.denial_reason(cmd, cwd=cwd, env=wt_env)
            assert r is None, f"cwd={cwd!r} cmd={cmd!r} must be allowed (own venv): {r}"
            d = _ev("Bash", {"command": cmd}, cwd=cwd, env=wt_env)
            assert d.allow, f"cwd={cwd!r} cmd={cmd!r} must be allowed via evaluate(): {d.reason}"


def test_outside_targets_stay_denied_from_a_subdirectory(tmp_path):
    """`env=wt_env` — the ambient venv is the session's OWN venv, innocent —
    so any denial below must come from the target the COMMAND itself names,
    never from the ambient PATH/VIRTUAL_ENV. Covers, each with its own
    positive control: a command-level VIRTUAL_ENV/UV_PROJECT_ENVIRONMENT
    assignment; every explicit target flag; a path-spelled installer; and
    the wrapped forms this module already handles (shell -c, subshell/brace
    groups, `timeout`, `xargs`, `python -m uv`)."""
    primary, primary_venv, wt, wt_venv, sub, prod_env, wt_env = _git_worktree_session(tmp_path)
    cases = [
        (f"VIRTUAL_ENV={primary_venv} pip install foo", primary_venv),
        (f"UV_PROJECT_ENVIRONMENT={primary_venv} uv pip install foo", primary_venv),
        (f"pip install --target {primary_venv} foo", primary_venv),
        (f"pip install --prefix {primary_venv} foo", primary_venv),
        (f"pip install --root {primary} foo", primary),
        (f"uv pip install --python {primary_venv}/bin/python foo", primary_venv),
        (f"uv sync --project {primary}", primary),
        (f"uv sync --directory {primary}", primary),
        (f"{primary_venv}/bin/pip install foo", primary_venv),
        (f"bash -lc '{primary_venv}/bin/pip install foo'", primary_venv),
        (f'sh -c "{primary_venv}/bin/pip install foo && echo ok"', primary_venv),
        (f"(cd {primary} && uv sync)", primary),
        (f"{{ cd {primary} && uv sync; }}", primary),
        (f"timeout 300 {primary_venv}/bin/pip install foo", primary_venv),
        (f"xargs {primary_venv}/bin/pip install", primary_venv),
        (f"{primary_venv}/bin/python -m uv pip install evilpkg", primary_venv),
    ]
    for cwd in (wt, sub):
        for cmd, offending in cases:
            r = venv_install_guard.denial_reason(cmd, cwd=cwd, env=wt_env)
            assert r is not None, f"cwd={cwd!r} must be denied: {cmd}"
            assert offending in r, f"reason must name {offending}: {r}"
            d = _ev("Bash", {"command": cmd}, cwd=cwd, env=wt_env)
            assert not d.allow, f"cwd={cwd!r} must be blocked via evaluate(): {cmd}"


def test_shared_developer_venv_stays_denied_for_every_spelling_from_a_subdirectory(tmp_path):
    """Anti-`e0541f35` pin: an independent review established that
    narrowing the write-target candidates so a BARE inner installer name
    (the `pip` in `uv pip install -e .`) stops being a signal is a security
    regression, since real `uv` writes to the venv named by `VIRTUAL_ENV`/
    `PATH` for exactly that spelling. This must be denied for every
    mutating `uv pip` spelling and for the bare `pip` twin, identically
    from the worktree root and from a subdirectory of it — the boundary
    fix in this ticket (comparing against the worktree ROOT rather than
    `cwd`) must never widen to cover the SHARED dev venv, which sits
    outside the worktree entirely regardless of which root is used."""
    primary, primary_venv, wt, wt_venv, sub, prod_env, wt_env = _git_worktree_session(tmp_path)
    cases = [
        "uv pip install foo",
        "uv pip install -e .",
        "uv pip uninstall foo",
        "uv pip sync",
        "pip install foo",
        f"source {primary_venv}/bin/activate && uv pip install foo",
        "bash -lc 'uv pip install foo'",
    ]
    for cwd in (wt, sub):
        for cmd in cases:
            r = venv_install_guard.denial_reason(cmd, cwd=cwd, env=prod_env)
            assert r is not None, f"cwd={cwd!r} must stay denied (anti-e0541f35): {cmd}"
            assert primary_venv in r, f"reason must name {primary_venv}: {r}"
            d = _ev("Bash", {"command": cmd}, cwd=cwd, env=prod_env)
            assert not d.allow, f"cwd={cwd!r} must be blocked via evaluate(): {cmd}"


def test_root_discovery_falls_back_to_cwd_without_a_git_marker(tmp_path):
    """Register bullet (i): a session tree with no `.git` marker anywhere
    in its lineage gets no boundary widening at all — `_session_root` falls
    back to `cwd` itself, so a subdirectory install there is denied exactly
    as it was before this change (a conservative false positive, never a
    hole)."""
    primary, primary_venv, wt, wt_venv, prod_env, wt_env = _session(tmp_path)
    sub = os.path.join(wt, "src")
    os.makedirs(sub, exist_ok=True)
    cmd = "pip install foo"
    r = venv_install_guard.denial_reason(cmd, cwd=sub, env=wt_env)
    assert r is not None, (
        f"without a .git marker, root falls back to cwd, so the own-venv "
        f"sibling {wt_venv} must still be denied from {sub}: {r}"
    )
    assert wt_venv in r
    d = _ev("Bash", {"command": cmd}, cwd=sub, env=wt_env)
    assert not d.allow


def test_session_root_never_expands_to_home_or_the_filesystem_root(tmp_path, monkeypatch):
    """Bound: even when `$HOME` itself carries a `.git` (and a `.venv` that
    would otherwise look like a perfectly good install target), the walk
    must refuse to treat `$HOME` as a worktree root — accepting it would
    turn "the session's worktree" into "the user's entire home
    directory". Refusing it means `_session_root` falls back to `cwd`, and
    a `PATH` resolving to that `$HOME`-level venv is still denied."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _fake_home_real, home_venv = _mkvenv(fake_home)
    os.makedirs(fake_home / ".git", exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    cwd = fake_home / "a" / "b"
    cwd.mkdir(parents=True)
    cwd_real = os.path.realpath(cwd)

    assert venv_install_guard._session_root(cwd_real) == cwd_real, (
        "a marker sitting at $HOME must not become the accepted root"
    )

    env = {"PATH": f"{home_venv}/bin:/usr/bin:/bin"}
    cmd = "pip install foo"
    r = venv_install_guard.denial_reason(cmd, cwd=str(cwd), env=env)
    assert r is not None, f"must stay denied despite the $HOME-level venv: {r}"
    assert home_venv in r
    d = _ev("Bash", {"command": cmd}, cwd=str(cwd), env=env)
    assert not d.allow

    # Direct unit assertions on `_session_root` itself.
    primary, primary_venv, wt, wt_venv, sub, prod_env, wt_env = _git_worktree_session(tmp_path)
    assert venv_install_guard._session_root(sub) == wt, (
        "a subdirectory two levels below the worktree root must resolve "
        "to that root"
    )

    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / ".git").mkdir()
    inner = outer / "repo"
    inner.mkdir()
    (inner / ".git").mkdir()
    nested_sub = inner / "sub"
    nested_sub.mkdir()
    assert venv_install_guard._session_root(os.path.realpath(nested_sub)) == os.path.realpath(inner), (
        "the NEAREST marker must win over a farther ancestor's"
    )


def test_session_root_fails_closed_when_home_is_unresolvable(tmp_path, monkeypatch):
    """If `Path.home()` itself raises, `_session_root` must not silently
    disable the `$HOME` refusal and keep climbing — that would let a
    `.git`-bearing ancestor at or above an unresolvable `$HOME` become the
    accepted root. It must fail CLOSED: fall straight back to `cwd_real`,
    exactly the pre-widening behaviour, so a `.git` sitting above `cwd`
    is never accepted as the root while home is unknown."""

    def _raise_home():
        raise RuntimeError("could not determine home directory")

    monkeypatch.setattr(venv_install_guard.Path, "home", staticmethod(_raise_home))

    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / ".git").mkdir()
    _outer_real, outer_venv = _mkvenv(outer)
    inner = outer / "inner"
    inner.mkdir()

    inner_real = os.path.realpath(inner)
    assert venv_install_guard._session_root(inner_real) == inner_real, (
        "with home unresolvable, the walk must not climb to the outer "
        ".git at all — it must fall back to cwd_real, not just skip the "
        "home-equality check while still ascending"
    )

    env = {"PATH": f"{outer_venv}/bin:/usr/bin:/bin"}
    cmd = "pip install foo"
    r = venv_install_guard.denial_reason(cmd, cwd=str(inner), env=env)
    assert r is not None, (
        f"an outer .git's venv must stay denied when home cannot be "
        f"resolved, not be accepted as in-root: {r}"
    )
    assert outer_venv in r
    d = _ev("Bash", {"command": cmd}, cwd=str(inner), env=env)
    assert not d.allow
